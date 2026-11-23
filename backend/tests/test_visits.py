"""Which place each visit was, and what the signature learner may claim.

The premise is a correction to §24's: a cluster is not a place. The hub
cluster in this vault spans 168m and holds Leavey Library, McCarthy Quad,
Everybody's Kitchen, Birnkrant and SSCI, and coordinates cannot separate them
(p50 17m / p90 47m of error against ~15m of separation), nor can time alone,
nor can the calendar (8 of 71 overlaps spatially usable). So the label is
yours, and what is learned is the shape of your visits.

The tests that matter most are the ones bounding what the learner is allowed
to say: it stays silent under three examples, it never learns from a
single-fix sighting, and it never writes a label you did not click.
"""

from datetime import timedelta

import pytest

from app import db, visits
from app.providers import location, ranges

HUB = (34.02146, -112.28293)


def _stay(day, hour, minutes, fixes=3):
    first = location.parse_ts(f"{day}T{hour:02d}:00:00")
    return {"lat": HUB[0], "lon": HUB[1], "seconds": minutes * 60, "n": fixes,
            "first": first, "last": first + timedelta(minutes=minutes),
            "day": day}


def _seed_fixes(day="2026-09-05", n=6):
    con = db.connect()
    for i in range(n):
        con.execute("INSERT INTO location_points (ts, day, lat, lon, accuracy)"
                    " VALUES (?,?,?,?,?)",
                    (f"{day}T19:{i * 5:02d}:00", day, HUB[0], HUB[1], 20))
    con.commit()
    con.close()


# -------------------------------------------------------------------- storing

def test_label_round_trips_and_clears(dbfile):
    visits.label("2026-09-05T19:00:00", *HUB, "Leavey Library")
    assert [r["name"] for r in visits.labels_at(*HUB)] == ["Leavey Library"]

    visits.label("2026-09-05T19:00:00", *HUB, "Everybody's Kitchen")
    assert [r["name"] for r in visits.labels_at(*HUB)] == ["Everybody's Kitchen"], \
        "relabelling replaces, it does not append a second truth"

    visits.label("2026-09-05T19:00:00", *HUB, "")
    assert visits.labels_at(*HUB) == []


def test_labels_are_local_to_their_cluster(dbfile):
    visits.label("2026-09-05T19:00:00", *HUB, "Leavey Library")
    assert visits.labels_at(34.1153, -112.2882) == []


# ------------------------------------------------------------------ predicting

def _examples(rows):
    return [(visits._signature(_stay(d, h, m)), name) for d, h, m, name in rows]


def test_silent_below_the_evidence_floor(dbfile):
    """Two labels can only say 'one of the two things you have named'. A
    confident-looking guess off two examples is worse than an empty field."""
    ex = _examples([("2026-09-01", 19, 90, "Leavey Library"),
                    ("2026-09-02", 12, 20, "Everybody's Kitchen")])
    assert visits.predict(_stay("2026-09-03", 19, 80), ex) is None


def test_evening_and_long_separates_from_midday_and_short(dbfile):
    """The whole bet: the two things you do at one cluster have different
    shapes even though they have the same coordinates."""
    ex = _examples([
        ("2026-09-01", 19, 90, "Leavey Library"),
        ("2026-09-02", 20, 120, "Leavey Library"),
        ("2026-09-03", 21, 75, "Leavey Library"),
        ("2026-09-04", 12, 25, "Everybody's Kitchen"),
        ("2026-09-05", 12, 20, "Everybody's Kitchen"),
    ])

    evening = visits.predict(_stay("2026-09-08", 20, 100), ex)
    assert evening["name"] == "Leavey Library"
    assert evening["share"] > 0.8
    assert evening["from_labels"] == 3

    midday = visits.predict(_stay("2026-09-09", 12, 22), ex)
    assert midday["name"] == "Everybody's Kitchen"
    assert midday["share"] > 0.8


def test_a_visit_unlike_everything_gets_no_answer(dbfile):
    """03:00 for four minutes resembles nothing labelled here, and the vote
    share cannot see that: it is relative, so it would hand this to
    "Everybody's Kitchen" at ~90% for no better reason than that lunch is also
    short. `support` is the absolute number that catches it — 0.0001 here
    against 0.91 for a genuine match."""
    ex = _examples([
        ("2026-09-01", 19, 90, "Leavey Library"),
        ("2026-09-02", 20, 120, "Leavey Library"),
        ("2026-09-04", 12, 25, "Everybody's Kitchen"),
        ("2026-09-05", 12, 20, "Everybody's Kitchen"),
    ])
    assert visits.predict(_stay("2026-09-09", 3, 4), ex) is None

    close = visits.predict(_stay("2026-09-09", 20, 100), ex)
    assert close["name"] == "Leavey Library" and close["support"] > 0.8


def test_midnight_is_close_to_one_am(dbfile):
    """Hour distance is circular. A 23:40 visit is an hour from 00:40, not
    twenty-three — the hub's visits really do run past midnight."""
    assert visits._hour_gap(23.5, 0.5) == pytest.approx(1.0)
    assert visits._hour_gap(1.0, 23.0) == pytest.approx(2.0)


def test_duration_compares_as_a_ratio(dbfile):
    """20m against 40m is the same gap as 40m against 80m — a lecture is not a
    coffee, but two lectures of different lengths are both lectures."""
    a = visits._signature(_stay("2026-09-01", 19, 20))
    b = visits._signature(_stay("2026-09-01", 19, 40))
    c = visits._signature(_stay("2026-09-01", 19, 80))
    assert visits._similarity(a, b) == pytest.approx(visits._similarity(b, c), rel=1e-6)


# ----------------------------------------------------------------- the listing

def test_sightings_are_shown_marked_and_never_learned_from(dbfile, monkeypatch):
    """25 of the hub's 60 real visits are one fix and then MAX_GAP_MIN of
    booked silence. Hiding them would be its own lie; learning from them would
    teach the signature that a 20-minute phantom is what a visit looks like."""
    _seed_fixes()
    solo = _stay("2026-09-06", 19, 20, fixes=1)
    real = [_stay("2026-09-01", 19, 90), _stay("2026-09-02", 20, 120),
            _stay("2026-09-03", 21, 75)]
    monkeypatch.setattr(location, "cluster_at", lambda *a: {
        "lat": HUB[0], "lon": HUB[1], "name": None, "span_m": 168.0,
        "stays": real + [solo], "members": [], "visits": 4})

    for s in real:
        visits.label(s["first"].isoformat(), *HUB, "Leavey Library")
    visits.label(solo["first"].isoformat(), *HUB, "Leavey Library")

    out = visits.over(*HUB, ranges.parse_range("2026-09-01..2026-09-30"))
    assert out["place"]["sightings"] == 1
    marked = [v for v in out["visits"] if v["sighting"]]
    assert len(marked) == 1 and marked[0]["name"] == "Leavey Library", \
        "a sighting can still be labelled — it just isn't evidence"
    assert out["labelled"] == 3, "the sighting must not count toward the floor"


def test_a_prediction_is_never_stored(dbfile, monkeypatch):
    """Nothing is true here because the learner thought so."""
    _seed_fixes()
    real = [_stay("2026-09-01", 19, 90), _stay("2026-09-02", 20, 120),
            _stay("2026-09-03", 21, 75)]
    unlabelled = _stay("2026-09-07", 20, 95)
    monkeypatch.setattr(location, "cluster_at", lambda *a: {
        "lat": HUB[0], "lon": HUB[1], "name": None, "span_m": 168.0,
        "stays": real + [unlabelled], "members": [], "visits": 4})
    for s in real:
        visits.label(s["first"].isoformat(), *HUB, "Leavey Library")

    out = visits.over(*HUB, ranges.parse_range("2026-09-01..2026-09-30"))
    guessed = [v for v in out["visits"] if v["started_at"].startswith("2026-09-07")][0]
    assert guessed["name"] is None, "a guess is not a label"
    assert guessed["predicted"]["name"] == "Leavey Library"
    assert len(visits.labels_at(*HUB)) == 3, "and it wrote nothing"


def test_label_survives_a_stay_split_by_the_range_edge(dbfile, monkeypatch):
    """Stay boundaries come from the fixes in the range asked for, so a range
    that starts mid-stay yields a stay beginning later than the labelled
    instant. The label matches on containment so it isn't orphaned."""
    _seed_fixes()
    whole = _stay("2026-09-05", 19, 60)
    visits.label(whole["first"].isoformat(), *HUB, "Leavey Library")

    clipped = dict(whole, first=whole["first"] + timedelta(minutes=20))
    monkeypatch.setattr(location, "cluster_at", lambda *a: {
        "lat": HUB[0], "lon": HUB[1], "name": None, "span_m": 168.0,
        "stays": [clipped], "members": [], "visits": 1})

    out = visits.over(*HUB, ranges.parse_range("2026-09-05"))
    assert out["visits"][0]["name"] == "Leavey Library"


def test_names_used_here_are_offered_back(dbfile, monkeypatch):
    """The picker's chips are the names you have already used at this cluster
    — the second visit to the library should be one click, not typing."""
    _seed_fixes()
    monkeypatch.setattr(location, "cluster_at", lambda *a: {
        "lat": HUB[0], "lon": HUB[1], "name": None, "span_m": 168.0,
        "stays": [_stay("2026-09-01", 19, 90)], "members": [], "visits": 1})
    visits.label("2026-09-01T19:00:00", *HUB, "Leavey Library")
    visits.label("2026-08-20T12:00:00", *HUB, "Everybody's Kitchen")

    out = visits.over(*HUB, ranges.parse_range("2026-09-01..2026-09-30"))
    assert out["used_before"] == ["Everybody's Kitchen", "Leavey Library"]


# -------------------------------------------------------------------- calendar

def test_same_recurring_event_pulls_two_visits_together(dbfile):
    """The calendar's one safe use here: identity, never position. Two visits
    that coincided with the same lecture are alike, whichever place that is."""
    # Deliberately unlike each other on the clock — 19:00 for 90m against
    # 09:00 for 25m — which is exactly the case the event is there to rescue.
    a = visits._signature(_stay("2026-09-01", 19, 90), "CSCI 103")
    b = visits._signature(_stay("2026-09-08", 9, 25), "CSCI 103")
    plain_a = visits._signature(_stay("2026-09-01", 19, 90))
    plain_b = visits._signature(_stay("2026-09-08", 9, 25))

    bare = visits._similarity(plain_a, plain_b)
    assert bare < 0.05, "on clock and duration alone these look unrelated"
    assert visits._similarity(a, b) > bare
    # Pulled halfway to certainty, never past it.
    assert visits._similarity(a, b) == pytest.approx(
        bare + (1 - bare) * visits.EVENT_AGREEMENT)
    assert visits._similarity(a, b) <= 1.0

    # And it cannot manufacture more than certainty where there was already
    # certainty: two identical visits stay at 1.0.
    same = visits._signature(_stay("2026-09-01", 19, 90), "CSCI 103")
    assert visits._similarity(same, same) == pytest.approx(1.0)


def test_different_events_say_nothing(dbfile):
    """One building holds many lectures, so a disagreement is not evidence
    against — it must leave the similarity exactly where it was."""
    a = visits._signature(_stay("2026-09-01", 19, 90), "CSCI 103")
    b = visits._signature(_stay("2026-09-08", 19, 90), "MATH 229")
    plain = visits._similarity(visits._signature(_stay("2026-09-01", 19, 90)),
                               visits._signature(_stay("2026-09-08", 19, 90)))
    assert visits._similarity(a, b) == pytest.approx(plain)


def test_series_identity_ignores_instance_noise(dbfile):
    assert visits._series("CSCI 103 (Lecture)") == visits._series("CSCI 103 (Discussion)")
    assert visits._series("CSCI 103: Object Oriented Programming (Lab)") \
        == "CSCI 103: Object Oriented Programming"
    assert visits._series("  ") is None


def test_a_one_off_that_ran_long_is_not_identity(dbfile, monkeypatch):
    """"Bid Day" spanned three hub visits. Gluing them together on that would
    be reading a coincidence of the clock as a fact about a place, so a series
    has to recur EVENT_MIN_RECURRENCE times at the cluster to count."""
    _seed_fixes()
    stays = [_stay("2026-09-01", 19, 90), _stay("2026-09-02", 12, 20)]
    monkeypatch.setattr(location, "cluster_at", lambda *a: {
        "lat": HUB[0], "lon": HUB[1], "name": None, "span_m": 168.0,
        "stays": stays, "members": [], "visits": 2})

    con = db.connect()
    for i, s in enumerate(stays):
        con.execute(
            "INSERT INTO events (day, event_id, calendar_id, summary,"
            " start_at, end_at, all_day, location) VALUES (?,?,?,?,?,?,0,?)",
            (s["day"], f"e{i}", "school", "Bid Day",
             s["first"].isoformat(), s["last"].isoformat(), "McCarthy Quad, LA"))
    con.commit()
    con.close()

    out = visits.over(*HUB, ranges.parse_range("2026-09-01..2026-09-30"))
    assert all(v["event"]["summary"] == "Bid Day" for v in out["visits"]), \
        "still shown — it is what was on the calendar"
    assert not any(v["event"]["recurring"] for v in out["visits"]), \
        "...but twice is not a series"


def test_virtual_events_are_not_offered_as_places(dbfile, monkeypatch):
    """"standup" is not somewhere you stood."""
    _seed_fixes()
    stay = _stay("2026-09-05", 19, 60)
    monkeypatch.setattr(location, "cluster_at", lambda *a: {
        "lat": HUB[0], "lon": HUB[1], "name": None, "span_m": 168.0,
        "stays": [stay], "members": [], "visits": 1})
    con = db.connect()
    con.execute(
        "INSERT INTO events (day, event_id, calendar_id, summary,"
        " start_at, end_at, all_day, location) VALUES (?,?,?,?,?,?,0,?)",
        (stay["day"], "e0", "school", "standup",
         stay["first"].isoformat(), stay["last"].isoformat(),
         "https://usc.zoom.us/j/9876543210"))
    con.commit()
    con.close()

    out = visits.over(*HUB, ranges.parse_range("2026-09-05"))
    assert out["visits"][0]["event"] is None
