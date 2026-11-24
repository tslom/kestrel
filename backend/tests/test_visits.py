"""Which place each visit was, and what the signature learner may claim.

A cluster is not a place: the label is the user's, the learner only knows visit shape.
Most important: it is silent under three examples, never learns from a one-fix sighting, never writes a label.
Design: DESIGN.md → `app/visits.py`: which place, on which visit.
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


def _examples(rows):
    return [(visits._signature(_stay(d, h, m)), name) for d, h, m, name in rows]


def test_silent_below_the_evidence_floor(dbfile):
    """Two labelled examples are too few to guess from."""
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
    """An unlike visit gets None: vote share is relative, `support` is absolute (0.0001 here vs 0.91 for a match)."""
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
    """Hour distance is circular (23:40 is an hour from 00:40)."""
    assert visits._hour_gap(23.5, 0.5) == pytest.approx(1.0)
    assert visits._hour_gap(1.0, 23.0) == pytest.approx(2.0)


def test_duration_compares_as_a_ratio(dbfile):
    """20m vs 40m is the same gap as 40m vs 80m."""
    a = visits._signature(_stay("2026-09-01", 19, 20))
    b = visits._signature(_stay("2026-09-01", 19, 40))
    c = visits._signature(_stay("2026-09-01", 19, 80))
    assert visits._similarity(a, b) == pytest.approx(visits._similarity(b, c), rel=1e-6)


def test_sightings_are_shown_marked_and_never_learned_from(dbfile, monkeypatch):
    """One-fix sightings are listed, marked, and never learned from."""
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
    """A prediction is never written."""
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
    """A label matches by containment, so a range starting mid-stay doesn't orphan it."""
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
    """Names already used at this cluster are offered back."""
    _seed_fixes()
    monkeypatch.setattr(location, "cluster_at", lambda *a: {
        "lat": HUB[0], "lon": HUB[1], "name": None, "span_m": 168.0,
        "stays": [_stay("2026-09-01", 19, 90)], "members": [], "visits": 1})
    visits.label("2026-09-01T19:00:00", *HUB, "Leavey Library")
    visits.label("2026-08-20T12:00:00", *HUB, "Everybody's Kitchen")

    out = visits.over(*HUB, ranges.parse_range("2026-09-01..2026-09-30"))
    assert out["used_before"] == ["Everybody's Kitchen", "Leavey Library"]


def test_same_recurring_event_pulls_two_visits_together(dbfile):
    """The calendar is used for identity only: visits sharing a recurring event are alike."""
    # Unlike on the clock (19:00/90m vs 09:00/25m) on purpose.
    a = visits._signature(_stay("2026-09-01", 19, 90), "CSCI 103")
    b = visits._signature(_stay("2026-09-08", 9, 25), "CSCI 103")
    plain_a = visits._signature(_stay("2026-09-01", 19, 90))
    plain_b = visits._signature(_stay("2026-09-08", 9, 25))

    bare = visits._similarity(plain_a, plain_b)
    assert bare < 0.05, "on clock and duration alone these look unrelated"
    assert visits._similarity(a, b) > bare
    assert visits._similarity(a, b) == pytest.approx(
        bare + (1 - bare) * visits.EVENT_AGREEMENT)
    assert visits._similarity(a, b) <= 1.0

    # Identical visits stay at 1.0.
    same = visits._signature(_stay("2026-09-01", 19, 90), "CSCI 103")
    assert visits._similarity(same, same) == pytest.approx(1.0)


def test_different_events_say_nothing(dbfile):
    """Different events leave similarity unchanged: a building holds many lectures."""
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
    """A one-off event spanning visits is not identity: it must recur EVENT_MIN_RECURRENCE times."""
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
