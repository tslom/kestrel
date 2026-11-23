"""providers/features — the cross-provider day matrix. It's pure SQL/
python over the other providers' cache tables, so the tests seed those tables
directly and assert the derivation. The contract that matters: passthrough
tables are renamed straight through, raw-event tables aggregate the same way
their own provider does, an absent source is a GAP (no row) not a 0, tasks fold
on day_key (4am rollover), and the metric filter picks one feature's series."""

import math
import statistics
from datetime import date, datetime, timedelta

from app import db
from app.providers import features, location, ranges

DAY = "2026-07-20"
NOW = "2026-07-22T10:00:00"


def _rng(start=DAY, end=DAY):
    return ranges.parse_range(f"{start}..{end}")


def _seed(con):
    with con:
        # weather_days: passthrough with a rename (tmax -> weather_tmax).
        con.execute("INSERT INTO weather_days (day, metric, value, refreshed_at)"
                    " VALUES (?, 'tmax', 90.0, ?)", (DAY, NOW))
        # health_days: passthrough, same name.
        con.execute("INSERT INTO health_days (day, metric, value, refreshed_at)"
                    " VALUES (?, 'sleep_hours', 7.5, ?)", (DAY, NOW))
        con.execute("INSERT INTO health_days (day, metric, value, refreshed_at)"
                    " VALUES (?, 'hrv', 50.0, ?)", (DAY, NOW))
        # messages: 2 sent, 1 received, 2 distinct real handles (the '' handle
        # must not count, matching messages.get's `if r["handle"]`).
        for guid, frm, handle in [("g1", 1, "a@x.com"), ("g2", 1, "b@x.com"),
                                  ("g3", 0, "a@x.com"), ("g4", 0, "")]:
            con.execute(
                "INSERT INTO messages (guid, day, ts, handle, is_from_me, refreshed_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (guid, DAY, f"{DAY}T12:00:00", handle, frm, NOW))
        # tasks: one done at noon on DAY, one done at 2am the NEXT civil date —
        # which day_key's back onto DAY (4am rollover). Both count -> 2.
        con.execute("INSERT INTO tasks (title, status, created_at, completed_at)"
                    " VALUES ('a', 'done', ?, ?)", (NOW, f"{DAY}T12:00:00"))
        con.execute("INSERT INTO tasks (title, status, created_at, completed_at)"
                    " VALUES ('b', 'done', ?, '2026-07-21T02:00:00')", (NOW,))
        # an open task must not count.
        con.execute("INSERT INTO tasks (title, status, created_at) VALUES ('c', 'open', ?)",
                    (NOW,))


def _matrix(con):
    con.close()
    return {it["feature"]: it["value"]
            for it in features.get(_rng())["data"]}


def test_passthrough_rename_and_aggregation(dbfile):
    con = db.connect()
    _seed(con)
    feats = _matrix(con)
    # rename: the weather metric surfaces under its feature name, not 'tmax'.
    assert feats["weather_tmax"] == 90.0
    assert "tmax" not in feats
    # passthrough, same name.
    assert feats["sleep_hours"] == 7.5
    assert feats["hrv"] == 50.0
    # message aggregation matches messages.get.
    assert feats["msgs_sent"] == 2.0
    assert feats["msgs_received"] == 2.0
    assert feats["contacts"] == 2.0  # '' handle excluded


def test_tasks_fold_on_day_key(dbfile):
    con = db.connect()
    _seed(con)
    feats = _matrix(con)
    assert feats["tasks_done"] == 2.0  # incl. the 2am-next-day completion


def test_absent_source_is_a_gap_not_zero(dbfile):
    con = db.connect()
    _seed(con)  # no location_days / plays / fin_transactions seeded
    feats = _matrix(con)
    for absent in ("time_at_home_min", "places", "listen_min", "spend"):
        assert absent not in feats  # gap, never 0-filled


def test_metric_filter_vs_full_matrix(dbfile):
    con = db.connect()
    _seed(con)
    con.close()
    one = features.get(_rng(), "sleep_hours")["data"]
    assert one == [{"day": DAY, "value": 7.5}]  # single-series shape, no 'feature'
    full = features.get(_rng())["data"]
    assert all({"day", "feature", "value"} == set(it) for it in full)


def test_refresh_recomputes_today_and_yesterday(dbfile):
    con = db.connect()
    con.close()
    out = features.refresh()
    today = db.day_key()
    assert today in out["days"] and len(out["days"]) == 2


# ---------- intraday HR -> hr_var_day (§12) ----------

def _seed_hours(con, day, values):
    """Seed health_hours: one hourly mean bpm per entry in `values`."""
    with con:
        for h, v in enumerate(values):
            con.execute(
                "INSERT INTO health_hours (day, hour, metric, value, refreshed_at)"
                " VALUES (?, ?, 'hr_hourly', ?, ?)", (day, h, float(v), NOW))


def test_hr_var_day_is_stdev_of_hourly_means(dbfile):
    con = db.connect()
    vals = [60, 62, 90, 120, 58, 61, 63, 100, 72, 66, 59, 88,
            70, 74, 95, 110, 57, 60, 64, 105, 68, 62, 58, 80]  # full 24h day
    _seed_hours(con, DAY, vals)
    feats = _matrix(con)
    # population stdev of the day's hourly means, matching _hr_var_rows.
    assert feats["hr_var_day"] == round(statistics.pstdev(vals), 2)


def test_hr_var_day_partial_wear_is_gap(dbfile):
    con = db.connect()
    _seed_hours(con, DAY, [70] * 11)  # below HR_MIN_HOURS (12) -> unreliable
    feats = _matrix(con)
    assert "hr_var_day" not in feats  # gap, never a misleading 0


def test_hr_var_day_tagged_health_domain():
    # so cross_domain correlations drop the mechanical hr_var_day ~ resting_hr /
    # ~ hrv pairs while keeping genuine cross-signal ones.
    assert features.FEATURE_DOMAIN["hr_var_day"] == "health"


# ---------- genre_variety ----------

def _seed_plays(con, day, artists):
    """One play per entry in `artists` (a name repeated = repeated plays)."""
    with con:
        for i, a in enumerate(artists):
            con.execute(
                "INSERT INTO plays (played_at, day, track_id, track, artist,"
                " artist_id, album, ms, refreshed_at)"
                " VALUES (?,?,?,?,?,?,NULL,180000,?)",
                (f"{day}T{i // 60:02d}:{i % 60:02d}:00Z", day, f"t{i}", f"t{i}",
                 a, "id", NOW))


def _seed_tags(con, mapping, now=NOW):
    import json
    with con:
        for key, tags in mapping.items():
            con.execute(
                "INSERT INTO artist_tags (artist_key, name, tags, raw, mbid,"
                " refreshed_at, missing) VALUES (?,?,?,NULL,NULL,?,0)",
                (key, key, json.dumps(tags), now))


def test_genre_variety_is_zero_when_the_whole_day_is_one_sound(dbfile):
    con = db.connect()
    _seed_plays(con, DAY, ["A", "B", "C", "D", "E", "F"])
    _seed_tags(con, {k.casefold(): ["hip hop"] for k in "ABCDEF"})
    feats = _matrix(con)
    assert feats["genre_variety"] == 0.0     # a real zero, not a gap


def test_genre_variety_is_one_when_evenly_spread(dbfile):
    con = db.connect()
    _seed_plays(con, DAY, ["A", "B", "C", "D", "E", "F"])
    _seed_tags(con, {"a": ["rock"], "b": ["rock"], "c": ["jazz"],
                     "d": ["jazz"], "e": ["house"], "f": ["house"]})
    feats = _matrix(con)
    assert feats["genre_variety"] == 1.0     # three genres, 2 plays each


def test_genre_variety_gaps_below_the_coverage_floor(dbfile):
    """Half the day untagged: the spread would describe which artists Last.fm
    happens to know, not what was played."""
    con = db.connect()
    _seed_plays(con, DAY, ["A", "B", "C", "D", "E", "F"])
    _seed_tags(con, {"a": ["rock"], "b": ["jazz"]})   # 2 of 6 -> 33%
    feats = _matrix(con)
    assert "genre_variety" not in feats


def test_genre_variety_gaps_on_a_thin_day(dbfile):
    con = db.connect()
    _seed_plays(con, DAY, ["A", "B"])                 # below GENRE_MIN_PLAYS
    _seed_tags(con, {"a": ["rock"], "b": ["jazz"]})
    feats = _matrix(con)
    assert "genre_variety" not in feats


def test_genre_variety_is_tagged_spotify_not_lastfm():
    """It's computed off the same `plays` rows as listen_min/novelty, so it has
    to share their domain or correlations() will pair it with them and report
    the play count correlating with itself."""
    assert features.FEATURE_DOMAIN["genre_variety"] == "spotify"


# ---------- correlations ----------

def test_corr_pvalue_matches_t_table():
    # two-tailed t-test anchors for Pearson r (df = n-2).
    assert abs(features._corr_p(0.5, 30) - 0.0049) < 5e-4
    assert abs(features._corr_p(0.361, 30) - 0.05) < 5e-3   # the df=28 crit value
    assert features._corr_p(0.0, 50) == 1.0                 # no correlation
    assert features._corr_p(1.0, 25) == 0.0                 # perfect -> p 0


def test_pearson_perfect_and_constant():
    assert features._pearson([(1, 2), (2, 4), (3, 6)]) == 1.0     # y = 2x
    assert features._pearson([(1, 6), (2, 4), (3, 2)]) == -1.0    # y = -2x + 8
    assert features._pearson([(1, 5), (2, 5), (3, 5)]) is None    # constant series


def _seed_series(con, first="2026-01-01", days=24):
    """`days` consecutive days where listen_min is an exact negative linear
    function of sleep_hours (so they correlate at r=-1), each in its own
    source table — a genuinely cross-domain pair."""
    last = None
    with con:
        for i in range(days):
            d = (date.fromisoformat(first) + timedelta(days=i)).isoformat()
            last = d
            sleep = 6.0 + (i % 5) * 0.5
            listen_min = 300.0 - sleep * 20      # perfectly anti-correlated
            con.execute("INSERT INTO health_days (day, metric, value, refreshed_at)"
                        " VALUES (?, 'sleep_hours', ?, ?)", (d, sleep, NOW))
            con.execute(
                "INSERT INTO plays (played_at, day, track_id, track, artist,"
                " artist_id, album, ms, refreshed_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (f"{d}T12:00:00Z", d, "t", "trk", "art", "a", None,
                 int(listen_min * 60000), NOW))
    return last


def test_correlations_finds_cross_source_pair(dbfile):
    con = db.connect()
    end = _seed_series(con)
    con.close()
    pairs = {(c["a"], c["b"]): c
             for c in features.correlations(end, window_days=60, min_n=20)}
    c = pairs[("listen_min", "sleep_hours")]
    assert c["r"] < -0.9 and c["n"] == 24 and c["p"] < 0.01


def test_correlations_min_n_floor_and_focus(dbfile):
    con = db.connect()
    end = _seed_series(con, days=15)  # below the default floor
    con.close()
    assert features.correlations(end, window_days=60, min_n=20) == []
    # focus keeps only pairs touching the named feature
    hit = features.correlations(end, window_days=60, min_n=10, focus="listen_min")
    assert hit and all("listen_min" in (c["a"], c["b"]) for c in hit)


def test_correlations_cross_domain_drops_same_source(dbfile):
    con = db.connect()
    with con:
        for i in range(24):
            d = (date.fromisoformat("2026-01-01") + timedelta(days=i)).isoformat()
            # two health metrics -> same domain, must be dropped when cross_domain
            con.execute("INSERT INTO health_days (day, metric, value, refreshed_at)"
                        " VALUES (?, 'sleep_hours', ?, ?)", (d, 6.0 + i * 0.1, NOW))
            con.execute("INSERT INTO health_days (day, metric, value, refreshed_at)"
                        " VALUES (?, 'hrv', ?, ?)", (d, 40.0 + i * 0.5, NOW))
    con.close()
    end = "2026-01-24"
    within = features.correlations(end, window_days=60, min_n=20)
    assert ("hrv", "sleep_hours") in {(c["a"], c["b"]) for c in within}
    cross = features.correlations(end, window_days=60, min_n=20, cross_domain=True)
    assert cross == []  # the only pair is within-domain
    # both are health, so both are keyed on the civil date: the key stays
    # absent, and the brief pays no tokens for the common case.
    assert all("cross_basis" not in c for c in within)


# ---------- day basis ----------


def test_every_domain_has_a_day_basis():
    """A new domain must say which notion of a day its rows are keyed on, or
    correlations() silently stops flagging every pair that touches it."""
    missing = {dom for dom in features.FEATURE_DOMAIN.values()
               if dom not in features.DOMAIN_DAY_BASIS}
    assert not missing
    # and the derived per-feature map then covers every feature
    assert set(features.FEATURE_DAY_BASIS) == set(features.FEATURE_DOMAIN)


def test_correlations_marks_cross_basis_pairs(dbfile):
    """sleep_hours is keyed on the civil date, listen_min on the day_key."""
    con = db.connect()
    end = _seed_series(con)
    con.close()
    pairs = {(c["a"], c["b"]): c
             for c in features.correlations(end, window_days=60, min_n=20)}
    assert pairs[("listen_min", "sleep_hours")]["cross_basis"] is True


# ---------- coverage gate: a thin location day is a gap ----------


def _seed_fixes(con, day, n, *, minutes_apart=60, lat=None, lon=None):
    """n fixes on `day`, at home unless a coordinate is given.

    Spacing is a parameter because the gate reads it: n fixes spaced at or
    beyond MAX_GAP_MIN cover (n-1) * MAX_GAP_MIN minutes, while the same n
    bunched together cover almost nothing. _covering_fixes below is the count
    that clears the floor at this spacing.
    """
    from app.config import HOME_LAT, HOME_LON
    lat, lon = (HOME_LAT if lat is None else lat), (HOME_LON if lon is None else lon)
    with con:
        for i in range(n):
            start = datetime.fromisoformat(f"{day}T08:00:00")
            ts = start + timedelta(minutes=i * minutes_apart)
            con.execute(
                "INSERT INTO location_points (ts, day, lat, lon) VALUES (?,?,?,?)",
                (ts.isoformat(), day, lat, lon))


def _covering_fixes():
    """The fewest hourly fixes that clear location's coverage floor."""
    return math.ceil(location.COVERAGE_FLOOR_MIN / location.MAX_GAP_MIN) + 1


def _seed_location_days(con, day, now=NOW):
    with con:
        for metric, value in (("time_at_home_min", 0.0), ("places", 1.0),
                              ("entropy", 0.0)):
            con.execute(
                "INSERT INTO location_days (day, metric, value, refreshed_at)"
                " VALUES (?,?,?,?)", (day, metric, value, now))


def test_thin_location_day_is_a_gap_not_a_day_indoors(dbfile):
    """One fix computes 0 places / 0 min at home, which is byte-for-byte a day
    spent indoors. location.summary() already refuses such a day; the matrix
    has to agree or the correlations read a coverage gap as staying home."""
    con = db.connect()
    _seed_location_days(con, DAY)
    _seed_fixes(con, DAY, 1)  # below location.SUMMARY_MIN_FIXES
    feats = _matrix(con)
    for gated in ("time_at_home_min", "places", "entropy"):
        assert gated not in feats


def test_covered_location_day_passes_through(dbfile):
    """The gate must not swallow a day that really was recorded — including a
    genuine 0, which above the floor is an observation."""
    con = db.connect()
    _seed_location_days(con, DAY)
    _seed_fixes(con, DAY, _covering_fixes())
    feats = _matrix(con)
    assert feats["places"] == 1.0
    assert feats["time_at_home_min"] == 0.0  # real zero, kept


def test_a_burst_of_fixes_never_reaches_the_matrix(dbfile):
    """The matrix has to refuse a burst for the same reason the card does, and
    it matters more here: location clears CORR_MIN_N, so these days land in
    pairs that Patterns shows rather than in an empty row."""
    con = db.connect()
    _seed_location_days(con, DAY)
    # Four times the fixes a covering day needs, one minute apart.
    _seed_fixes(con, DAY, _covering_fixes() * 4, minutes_apart=1)
    feats = _matrix(con)
    for gated in ("time_at_home_min", "places", "entropy"):
        assert gated not in feats


def test_location_gate_does_not_leak_to_other_passthroughs(dbfile):
    """The health/weather pollers run server-side whether or not anything
    happens, so their zeros are observations and must never be gated on
    location's fixes."""
    con = db.connect()
    _seed(con)  # health_days seeded, no location_points at all
    feats = _matrix(con)
    assert feats["sleep_hours"] == 7.5



# ---------- staleness: a source that lands after the day was computed ----------


def test_late_source_reaches_an_already_computed_day(dbfile):
    """get()'s backfill test is per day, so a day that had *any* feature row
    was never revisited and a later-arriving source stayed invisible — as a
    gap, i.e. as the source having no data, which is the reading the data was
    supposed to correct. The location backfill hit exactly this."""
    con = db.connect()
    _seed(con)          # health/messages/tasks, but no location
    feats = _matrix(con)
    assert "places" not in feats

    # Age the computed day, as the nightly refresh would have left it, so the
    # source below genuinely lands *after* the features were computed.
    con = db.connect()
    with con:
        con.execute("UPDATE features SET refreshed_at = '2026-07-21T03:55:00'"
                    " WHERE day = ?", (DAY,))
    _seed_location_days(con, DAY, now="2026-07-23T09:00:00")
    _seed_fixes(con, DAY, _covering_fixes())
    feats = _matrix(con)
    assert feats["places"] == 1.0        # the day was recomputed
    assert feats["sleep_hours"] == 7.5   # and the rest of it survived


def test_stale_sweep_converges(dbfile):
    """One recompute must settle it: _compute_days stamps features later than
    the source that triggered the sweep, or every read recomputes forever."""
    con = db.connect()
    _seed(con)
    con.close()
    features.get(_rng())
    con = db.connect()
    assert features._stale_days(con, [DAY]) == set()
    con.close()


def test_stale_sweep_survives_a_missing_table(dbfile):
    """An absent source table is skipped, not an OperationalError that takes
    every other source's detection down with it."""
    con = db.connect()
    _seed(con)
    with con:
        con.execute("DROP TABLE plays")
    assert features._stale_days(con, [DAY]) == set()  # no crash
    con.close()


def test_stale_sweep_survives_half_a_joined_source(dbfile):
    """The lastfm arm needs `plays` *and* `artist_tags` — a one-table presence
    check would have built a join against a table that isn't there."""
    con = db.connect()
    _seed(con)
    with con:
        con.execute("DROP TABLE artist_tags")
    assert features._stale_days(con, [DAY]) == set()  # no crash
    con.close()


# ---------- the three sources whose timestamp isn't next to their day ----------


def test_late_tags_reach_an_already_computed_day(dbfile):
    """artist_tags is keyed by artist, so its arrival has to be noticed through
    the plays that reference it. Tags are fetched lazily one artist at a time,
    so on a long history nearly every day's genre_variety was computed before
    the tags it rests on existed — 284 of 365 days on the real DB. Both halves
    of _genre_rows move underneath: the entropy, and the coverage gate that had
    been keeping the day out as a gap entirely."""
    con = db.connect()
    _seed_plays(con, DAY, ["A", "B", "C", "D", "E", "F"])
    feats = _matrix(con)
    assert "genre_variety" not in feats      # nothing tagged yet: 0% coverage

    con = db.connect()
    with con:
        # *After* the plays (NOW), so the plays arm is settled and the tags
        # below are the only thing that can bring the day back.
        con.execute("UPDATE features SET refreshed_at = '2026-07-22T11:00:00'"
                    " WHERE day = ?", (DAY,))
    _seed_tags(con, {"a": ["rock"], "b": ["rock"], "c": ["jazz"], "d": ["jazz"],
                     "e": ["house"], "f": ["house"]}, now="2026-07-23T09:00:00")
    assert _matrix(con)["genre_variety"] == 1.0


def test_late_calendar_sync_reaches_an_already_computed_day(dbfile):
    """`events` rows carry no timestamp of their own; calendar_days holds the
    per-day sync stamp, on the same civil date the features key on."""
    con = db.connect()
    _seed(con)
    feats = _matrix(con)
    assert "calendar_events" not in feats

    con = db.connect()
    with con:
        # *After* every source _seed wrote (NOW), so calendar_days below is the
        # only thing that can flag the day.
        con.execute("UPDATE features SET refreshed_at = '2026-07-22T11:00:00'"
                    " WHERE day = ?", (DAY,))
        con.execute(
            "INSERT INTO events (day, event_id, calendar_id, calendar_name,"
            " summary, start_at, end_at, all_day, location)"
            " VALUES (?,'e1','c1','Personal','Lunch',?,?,0,NULL)",
            (DAY, f"{DAY}T12:00:00", f"{DAY}T13:00:00"))
        con.execute("INSERT INTO calendar_days (day, refreshed_at)"
                    " VALUES (?, '2026-07-23T09:00:00')", (DAY,))
    feats = _matrix(con)
    assert feats["calendar_events"] == 1.0
    assert feats["busy_min"] == 60.0


def test_a_timed_event_over_a_day_is_not_load(dbfile):
    """calendar.SPAN_SQL: a retreat or a mis-dated hold clamped to the day
    still read as 24h busy on each day it touched."""
    con = db.connect()
    with con:
        for eid, summary, s, e in (
                ("e1", "Lunch", f"{DAY}T12:00:00-07:00", f"{DAY}T13:00:00-07:00"),
                ("e2", "Retreat", "2026-07-18T14:00:00-07:00", "2026-07-22T10:00:00-07:00")):
            con.execute(
                "INSERT INTO events (day, event_id, calendar_id, calendar_name,"
                " summary, start_at, end_at, all_day, location)"
                " VALUES (?,?,'c1','Personal',?,?,?,0,NULL)", (DAY, eid, summary, s, e))
        con.execute("INSERT INTO calendar_days (day, refreshed_at) VALUES (?, ?)", (DAY, NOW))
    feats = _matrix(con)
    assert feats["calendar_events"] == 1.0
    assert feats["busy_min"] == 60.0


def test_late_task_completion_folds_on_day_key_not_civil_date(dbfile):
    """The tasks arm re-does _tasks_rows' 4am fold in SQL, so a 2am completion
    has to mark the *previous* day stale — the day whose tasks_done would
    change — and not the civil date it happened on. Both days are computed
    here, so a civil-date fold would flag the wrong one rather than none.
    DAY_ROLLOVER_HOUR is interpolated into that SQL; this pins the result
    against db.day_key so the two can't drift."""
    con = db.connect()
    _seed(con)
    nextday = (date.fromisoformat(DAY) + timedelta(days=1)).isoformat()
    with con:   # give nextday a feature row of its own, so it *could* be flagged
        con.execute("INSERT INTO weather_days (day, metric, value, refreshed_at)"
                    " VALUES (?, 'tmax', 88.0, ?)", (nextday, NOW))
    con.close()
    features.get(_rng(DAY, nextday))

    con = db.connect()
    with con:
        con.execute("UPDATE features SET refreshed_at = '2026-07-23T03:55:00'")
        con.execute(
            "INSERT INTO tasks (title, status, source, created_at, completed_at,"
            " updated_at) VALUES ('late','done','ui',?,?,?)",
            (f"{DAY}T09:00:00", f"{nextday}T02:00:00", "2026-07-24T09:00:00"))
    assert db.day_key(datetime.fromisoformat(f"{nextday}T02:00:00")) == DAY
    assert features._stale_days(con, [DAY, nextday]) == {DAY}
    con.close()


def test_trigger_stamped_task_is_comparable_with_features(dbfile):
    """A task's stamp comes from the tasks_touch trigger as
    datetime('now','localtime') — space-separated, so it sorts *before* every
    ISO-T stamp of the same date and a task updated at 14:22 read as older than
    features computed at 00:00. Only weather is seeded, older than the features,
    so the tasks arm is the only thing that can flag the day."""
    assert f"{DAY} 14:22:46" < f"{DAY}T00:00:00"    # the trap, as a string fact
    con = db.connect()
    with con:
        con.execute("INSERT INTO weather_days (day, metric, value, refreshed_at)"
                    " VALUES (?, 'tmax', 90.0, '2026-07-19T00:00:00')", (DAY,))
    con.close()
    features.get(_rng())

    con = db.connect()
    with con:
        con.execute("UPDATE features SET refreshed_at = ?", (f"{DAY}T00:00:00",))
        con.execute(
            "INSERT INTO tasks (title, status, source, created_at, completed_at,"
            " updated_at) VALUES ('t','done','ui',?,?,?)",
            (f"{DAY}T09:00:00", f"{DAY}T14:22:46", f"{DAY} 14:22:46"))
    assert features._stale_days(con, [DAY]) == {DAY}
    con.close()


def test_touch_trigger_really_writes_a_space_separated_stamp(dbfile):
    """The premise the arm's replace() rests on — pinned separately so a change
    to the trigger shows up here rather than as a silently dead normalization."""
    con = db.connect()
    with con:
        con.execute("INSERT INTO tasks (title, status, source, created_at)"
                    " VALUES ('t','open','ui','2026-07-20T09:00:00')")
    with con:
        con.execute("UPDATE tasks SET status = 'done' WHERE title = 't'")
    touched = con.execute(
        "SELECT updated_at FROM tasks WHERE title = 't'").fetchone()["updated_at"]
    assert " " in touched and "T" not in touched
    con.close()


# ---------- a source that can no longer answer for an old day ----------


def _seed_calendar(con, day, *, synced=NOW, event=True):
    with con:
        if event:
            con.execute(
                "INSERT INTO events (day, event_id, calendar_id, calendar_name,"
                " summary, start_at, end_at, all_day, location)"
                " VALUES (?,'e1','c1','Personal','Lunch',?,?,0,NULL)",
                (day, f"{day}T12:00:00", f"{day}T13:00:00"))
        if synced:
            con.execute("INSERT INTO calendar_days (day, refreshed_at) VALUES (?,?)"
                        " ON CONFLICT(day) DO UPDATE SET refreshed_at=excluded.refreshed_at",
                        (day, synced))


def test_rebuild_keeps_features_the_calendar_can_no_longer_answer_for(dbfile):
    """calendar._sync keeps a rolling window, so an old day's `events` rows are
    gone while `features` is the only record left of that day's load. Rebuilding
    the day — for any reason, a late tag is enough — would delete them and
    republish the day as having had no events. On the real DB this cost 34 of 45
    calendar days before the guard existed."""
    con = db.connect()
    _seed(con)
    _seed_calendar(con, DAY)
    feats = _matrix(con)
    assert feats["calendar_events"] == 1.0

    # The day scrolls out of the window: events pruned, and with them the
    # calendar_days marker that said the day was ever fetched.
    con = db.connect()
    with con:
        con.execute("DELETE FROM events WHERE day = ?", (DAY,))
        con.execute("DELETE FROM calendar_days WHERE day = ?", (DAY,))
    features._compute_days(con, [DAY], "2026-07-25T09:00:00")
    feats = {it["feature"]: it["value"] for it in features.get(_rng())["data"]}
    assert feats["calendar_events"] == 1.0    # carried forward, not republished as a gap
    assert feats["busy_min"] == 60.0
    assert feats["sleep_hours"] == 7.5        # and the rebuilt half still rebuilt
    con.close()


def test_a_synced_day_with_no_events_is_still_a_real_gap(dbfile):
    """The guard must not freeze a day the calendar *can* answer for: a day
    inside the window whose events were all cancelled has to lose the feature,
    or a deleted event would live in the matrix forever."""
    con = db.connect()
    _seed(con)
    _seed_calendar(con, DAY)
    assert _matrix(con)["calendar_events"] == 1.0

    con = db.connect()
    with con:      # event gone, but the day is still synced
        con.execute("DELETE FROM events WHERE day = ?", (DAY,))
    features._compute_days(con, [DAY], "2026-07-25T09:00:00")
    feats = {it["feature"]: it["value"] for it in features.get(_rng())["data"]}
    assert "calendar_events" not in feats
    con.close()


def test_carried_forward_day_does_not_recompute_forever(dbfile):
    """The rows are preserved, so their refreshed_at would have stayed older
    than every source and left the day permanently stale — recomputing on every
    read. They're re-stamped instead."""
    con = db.connect()
    _seed(con)
    _seed_calendar(con, DAY)
    _matrix(con)
    con = db.connect()
    with con:
        con.execute("DELETE FROM events WHERE day = ?", (DAY,))
        con.execute("DELETE FROM calendar_days WHERE day = ?", (DAY,))
        # Age the day below the seeded sources, so the read below really does
        # rebuild it and really does take the carry-forward path.
        con.execute("UPDATE features SET refreshed_at = '2026-07-21T03:55:00'")
    assert features._stale_days(con, [DAY]) == {DAY}
    con.close()

    features.get(_rng())
    con = db.connect()
    assert con.execute(
        "SELECT COUNT(*) c FROM features WHERE day = ? AND feature = 'calendar_events'",
        (DAY,)).fetchone()["c"] == 1        # still carried forward
    assert features._stale_days(con, [DAY]) == set()
    con.close()


def test_domain_features_covers_every_declared_feature():
    """_DOMAIN_FEATURES is derived from FEATURE_DOMAIN so a new feature can't be
    left out of the carry-forward set for its source."""
    flat = {f for feats in features._DOMAIN_FEATURES.values() for f in feats}
    assert flat == set(features.FEATURE_DOMAIN)
    assert set(features._RETENTION_GATES) <= set(features._DOMAIN_FEATURES)


def test_new_arms_converge_in_one_sweep(dbfile):
    """Same contract as test_stale_sweep_converges, for the three joined arms:
    one recompute must settle them, or every read recomputes forever. The
    calendar arm matters most — _sync re-stamps its whole window each poll
    whether or not an event changed."""
    con = db.connect()
    _seed(con)
    _seed_plays(con, DAY, ["A", "B", "C", "D", "E", "F"])
    _seed_tags(con, {k.casefold(): ["hip hop"] for k in "ABCDEF"})
    with con:
        con.execute("INSERT INTO events (day, event_id, calendar_id,"
                    " calendar_name, summary, start_at, end_at, all_day, location)"
                    " VALUES (?,'e1','c1','Personal','Lunch',?,?,0,NULL)",
                    (DAY, f"{DAY}T12:00:00", f"{DAY}T13:00:00"))
        con.execute("INSERT INTO calendar_days (day, refreshed_at) VALUES (?,?)",
                    (DAY, NOW))
    con.close()

    features.get(_rng())
    con = db.connect()
    assert features._stale_days(con, [DAY]) == set()
    con.close()
