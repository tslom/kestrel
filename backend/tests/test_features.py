"""providers/features: tests seed the source cache tables and assert the derived day matrix.

Design: DESIGN.md → §11 Feature matrix."""

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
        con.execute("INSERT INTO weather_days (day, metric, value, refreshed_at)"
                    " VALUES (?, 'tmax', 90.0, ?)", (DAY, NOW))
        con.execute("INSERT INTO health_days (day, metric, value, refreshed_at)"
                    " VALUES (?, 'sleep_hours', 7.5, ?)", (DAY, NOW))
        con.execute("INSERT INTO health_days (day, metric, value, refreshed_at)"
                    " VALUES (?, 'hrv', 50.0, ?)", (DAY, NOW))
        # 2 sent, 1 received; the '' handle must not count as a contact.
        for guid, frm, handle in [("g1", 1, "a@x.com"), ("g2", 1, "b@x.com"),
                                  ("g3", 0, "a@x.com"), ("g4", 0, "")]:
            con.execute(
                "INSERT INTO messages (guid, day, ts, handle, is_from_me, refreshed_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (guid, DAY, f"{DAY}T12:00:00", handle, frm, NOW))
        # One done at noon on DAY, one at 2am the next civil date (day_key folds it onto DAY).
        con.execute("INSERT INTO tasks (title, status, created_at, completed_at)"
                    " VALUES ('a', 'done', ?, ?)", (NOW, f"{DAY}T12:00:00"))
        con.execute("INSERT INTO tasks (title, status, created_at, completed_at)"
                    " VALUES ('b', 'done', ?, '2026-07-21T02:00:00')", (NOW,))
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
    assert feats["weather_tmax"] == 90.0
    assert "tmax" not in feats
    assert feats["sleep_hours"] == 7.5
    assert feats["hrv"] == 50.0
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
    assert feats["hr_var_day"] == round(statistics.pstdev(vals), 2)


def test_hr_var_day_partial_wear_is_gap(dbfile):
    con = db.connect()
    _seed_hours(con, DAY, [70] * 11)  # below HR_MIN_HOURS (12) -> unreliable
    feats = _matrix(con)
    assert "hr_var_day" not in feats  # gap, never a misleading 0


def test_hr_var_day_tagged_health_domain():
    # cross_domain must drop the mechanical hr_var_day ~ resting_hr / hrv pairs.
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
    """Half the day untagged: the spread would describe Last.fm's coverage, not the plays."""
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
    """Same `plays` rows as listen_min/novelty, so it must share their domain (no self-correlation)."""
    assert features.FEATURE_DOMAIN["genre_variety"] == "spotify"


# ---------- correlations ----------

def test_corr_pvalue_matches_t_table():
    assert abs(features._corr_p(0.5, 30) - 0.0049) < 5e-4
    assert abs(features._corr_p(0.361, 30) - 0.05) < 5e-3   # the df=28 crit value
    assert features._corr_p(0.0, 50) == 1.0                 # no correlation
    assert features._corr_p(1.0, 25) == 0.0                 # perfect -> p 0


def test_pearson_perfect_and_constant():
    assert features._pearson([(1, 2), (2, 4), (3, 6)]) == 1.0     # y = 2x
    assert features._pearson([(1, 6), (2, 4), (3, 2)]) == -1.0    # y = -2x + 8
    assert features._pearson([(1, 5), (2, 5), (3, 5)]) is None    # constant series


def _seed_series(con, first="2026-01-01", days=24):
    """`days` days where listen_min is an exact negative linear function of sleep_hours (r=-1)."""
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
    hit = features.correlations(end, window_days=60, min_n=10, focus="listen_min")
    assert hit and all("listen_min" in (c["a"], c["b"]) for c in hit)


def test_correlations_cross_domain_drops_same_source(dbfile):
    con = db.connect()
    with con:
        for i in range(24):
            d = (date.fromisoformat("2026-01-01") + timedelta(days=i)).isoformat()
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
    # same basis -> the key stays absent (the brief pays no tokens for the common case)
    assert all("cross_basis" not in c for c in within)


# ---------- day basis ----------


def test_every_domain_has_a_day_basis():
    """A new domain must declare its day basis, or correlations() stops flagging its pairs."""
    missing = {dom for dom in features.FEATURE_DOMAIN.values()
               if dom not in features.DOMAIN_DAY_BASIS}
    assert not missing
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
    """n fixes on `day`, at home unless a coordinate is given. Spacing matters: the gate reads coverage minutes."""
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
    """One fix computes 0 places / 0 min at home, identical to a day indoors; the matrix must refuse it like location.summary()."""
    con = db.connect()
    _seed_location_days(con, DAY)
    _seed_fixes(con, DAY, 1)  # below location.SUMMARY_MIN_FIXES
    feats = _matrix(con)
    for gated in ("time_at_home_min", "places", "entropy"):
        assert gated not in feats


def test_covered_location_day_passes_through(dbfile):
    """A genuinely recorded day passes the gate, including a real 0."""
    con = db.connect()
    _seed_location_days(con, DAY)
    _seed_fixes(con, DAY, _covering_fixes())
    feats = _matrix(con)
    assert feats["places"] == 1.0
    assert feats["time_at_home_min"] == 0.0  # real zero, kept


def test_a_burst_of_fixes_never_reaches_the_matrix(dbfile):
    """A burst of fixes (one minute apart) is refused: coverage, not count."""
    con = db.connect()
    _seed_location_days(con, DAY)
    _seed_fixes(con, DAY, _covering_fixes() * 4, minutes_apart=1)
    feats = _matrix(con)
    for gated in ("time_at_home_min", "places", "entropy"):
        assert gated not in feats


def test_location_gate_does_not_leak_to_other_passthroughs(dbfile):
    """Health/weather zeros are observations; never gated on location's fixes."""
    con = db.connect()
    _seed(con)
    feats = _matrix(con)
    assert feats["sleep_hours"] == 7.5



# ---------- staleness: a source that lands after the day was computed ----------


def test_late_source_reaches_an_already_computed_day(dbfile):
    """A source landing after a day was computed must reach that day (get() backfills only never-computed days)."""
    con = db.connect()
    _seed(con)
    feats = _matrix(con)
    assert "places" not in feats

    # Age the day so the source below lands after the features.
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
    """One recompute settles the day, or every read recomputes forever."""
    con = db.connect()
    _seed(con)
    con.close()
    features.get(_rng())
    con = db.connect()
    assert features._stale_days(con, [DAY]) == set()
    con.close()


def test_stale_sweep_survives_a_missing_table(dbfile):
    """An absent source table is skipped, not an OperationalError for every other source."""
    con = db.connect()
    _seed(con)
    with con:
        con.execute("DROP TABLE plays")
    assert features._stale_days(con, [DAY]) == set()  # no crash
    con.close()


def test_stale_sweep_survives_half_a_joined_source(dbfile):
    """The lastfm arm needs both `plays` and `artist_tags`."""
    con = db.connect()
    _seed(con)
    with con:
        con.execute("DROP TABLE artist_tags")
    assert features._stale_days(con, [DAY]) == set()  # no crash
    con.close()


# ---------- the three sources whose timestamp isn't next to their day ----------


def test_late_tags_reach_an_already_computed_day(dbfile):
    """Late artist_tags (lazily fetched; 284 of 365 days predated them) reach a computed day via the plays that reference them."""
    con = db.connect()
    _seed_plays(con, DAY, ["A", "B", "C", "D", "E", "F"])
    feats = _matrix(con)
    assert "genre_variety" not in feats      # nothing tagged yet: 0% coverage

    con = db.connect()
    with con:
        # After the plays, so only the tags below can flag the day.
        con.execute("UPDATE features SET refreshed_at = '2026-07-22T11:00:00'"
                    " WHERE day = ?", (DAY,))
    _seed_tags(con, {"a": ["rock"], "b": ["rock"], "c": ["jazz"], "d": ["jazz"],
                     "e": ["house"], "f": ["house"]}, now="2026-07-23T09:00:00")
    assert _matrix(con)["genre_variety"] == 1.0


def test_late_calendar_sync_reaches_an_already_computed_day(dbfile):
    """`events` has no timestamp; calendar_days' per-day sync stamp flags the day."""
    con = db.connect()
    _seed(con)
    feats = _matrix(con)
    assert "calendar_events" not in feats

    con = db.connect()
    with con:
        # After every seeded source, so only calendar_days can flag the day.
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
    """calendar.SPAN_SQL: a multi-day event clamped to the day must not read as 24h busy."""
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
    """A 2am completion marks the previous day stale (the SQL fold must agree with db.day_key)."""
    con = db.connect()
    _seed(con)
    nextday = (date.fromisoformat(DAY) + timedelta(days=1)).isoformat()
    with con:   # nextday gets its own feature row so it could be flagged wrongly
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
    """The tasks_touch trigger's space-separated stamp must compare correctly with ISO-T feature stamps."""
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
    """Pins the premise of the arm's replace(): the trigger writes a space-separated stamp."""
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
    """A day scrolled out of calendar's window keeps its features on rebuild (34 of 45 days were lost before the guard)."""
    con = db.connect()
    _seed(con)
    _seed_calendar(con, DAY)
    feats = _matrix(con)
    assert feats["calendar_events"] == 1.0

    # The day scrolls out of the window: events and the calendar_days marker pruned.
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
    """A synced day whose events were all cancelled loses the feature (the guard doesn't freeze it)."""
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
    """Carried-forward rows are re-stamped, or the day stays stale and recomputes on every read."""
    con = db.connect()
    _seed(con)
    _seed_calendar(con, DAY)
    _matrix(con)
    con = db.connect()
    with con:
        con.execute("DELETE FROM events WHERE day = ?", (DAY,))
        con.execute("DELETE FROM calendar_days WHERE day = ?", (DAY,))
        # Age the day below the sources so the read takes the carry-forward path.
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
    """_DOMAIN_FEATURES covers every FEATURE_DOMAIN feature."""
    flat = {f for feats in features._DOMAIN_FEATURES.values() for f in feats}
    assert flat == set(features.FEATURE_DOMAIN)
    assert set(features._RETENTION_GATES) <= set(features._DOMAIN_FEATURES)


def test_new_arms_converge_in_one_sweep(dbfile):
    """test_stale_sweep_converges for the three joined arms; calendar matters most (_sync re-stamps its window every poll)."""
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
