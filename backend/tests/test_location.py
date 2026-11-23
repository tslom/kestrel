"""location.py feature computation — the derived metrics over raw
`location_points`. Focus on the dwell-gated `places` count (the driving-day
transit-inflation fix, walk-tested) and that entropy stays over all
clusters."""

import math
from datetime import datetime, timedelta


from app import db
from app.config import HOME_LAT, HOME_LON
from app.providers import location, location_relay

# What a day has to look like to clear location.COVERAGE_FLOOR_MIN. The gate
# reads how much of the day the fixes span, not how many arrived, so a burst
# of eight 5 minutes apart is a thin day however many rows it holds. Derived
# from the constants, and one gap past the floor, so moving the floor moves
# these tests and none of them turn into a test of the boundary.
COVERING_GAP_MIN = location.MAX_GAP_MIN
COVERING_N = math.ceil(location.COVERAGE_FLOOR_MIN / COVERING_GAP_MIN) + 2


def _seed_points(con, day, rows):
    """rows: list of (ts_iso, lat, lon). Insert raw fixes for a day_key."""
    con.executemany(
        "INSERT INTO location_points (ts, day, lat, lon) VALUES (?,?,?,?)",
        [(ts, day, lat, lon) for ts, lat, lon in rows])
    con.commit()


def _run(con, day):
    location._compute_days(con, [day], datetime.now().isoformat(timespec="seconds"))
    rows = con.execute(
        "SELECT metric, value FROM location_days WHERE day=?", (day,)).fetchall()
    return {r["metric"]: r["value"] for r in rows}


def test_places_excludes_transit_cells(dbfile):
    """A cell the car only passes through (a single ~1min gap) is not a place;
    a cell dwelt in past PLACE_MIN_DWELL_SEC is."""
    con = db.connect()
    day = "2026-07-23"
    base = datetime(2026, 7, 23, 12, 0, 0)
    # Two genuine stops (many fixes minutes apart, well past the threshold),
    # then a run of drive-through cells each visited once ~1min apart.
    rows = []
    t = base
    # stop A: 5 fixes over ~10 min at one cell (away from home)
    for _ in range(5):
        rows.append((t.isoformat() + "Z", 34.100, -118.100))
        t += timedelta(minutes=2, seconds=30)
    # transit: 8 distinct ~110m cells, one fix each, ~1 min apart
    for i in range(8):
        rows.append((t.isoformat() + "Z", 34.200 + i * 0.002, -118.200 + i * 0.002))
        t += timedelta(minutes=1)
    # stop B: 5 fixes over ~10 min at another cell
    for _ in range(5):
        rows.append((t.isoformat() + "Z", 34.300, -118.300))
        t += timedelta(minutes=2, seconds=30)
    _seed_points(con, day, rows)

    m = _run(con, day)
    # Only the two dwelt cells count; the 8 transit cells drop out even though
    # they're distinct grid clusters.
    assert m["places"] == 2.0
    con.close()


def test_entropy_counts_all_clusters(dbfile):
    """entropy is unfiltered — transit cells still contribute (tiny) weight, so
    a spread-out day reads as higher entropy than a single-stop day even when
    `places` is gated."""
    con = db.connect()
    day = "2026-07-22"
    base = datetime(2026, 7, 22, 12, 0, 0)
    rows, t = [], base
    for i in range(10):
        rows.append((t.isoformat() + "Z", 34.400 + i * 0.002, -118.400 + i * 0.002))
        t += timedelta(minutes=1)
    _seed_points(con, day, rows)
    m = _run(con, day)
    # 10 distinct transit cells, all sub-threshold -> 0 places, but entropy > 0.
    assert m["places"] == 0.0
    assert m["entropy"] > 0.0
    con.close()


def _seed_day(con, day, *, n, minutes_apart=COVERING_GAP_MIN, start_hour=12,
              at_home_first=True, at_home_last=True):
    """A day of `n` fixes `minutes_apart` apart, with control over whether the
    first and last are at home — which is what decides leave_observed/
    return_observed.

    Spacing defaults to the covering gap because most callers want a day the
    summary will actually center on; pass a smaller one to build the burst
    shape the coverage gate exists to refuse."""
    t = datetime(*[int(x) for x in day.split("-")], start_hour, 0, 0)
    away = (HOME_LAT + 0.05, HOME_LON + 0.05)
    rows = []
    for i in range(n):
        first, last = i == 0, i == n - 1
        home = (at_home_first if first else at_home_last if last else False)
        rows.append((t.isoformat() + "Z", *(( HOME_LAT, HOME_LON) if home else away)))
        t += timedelta(minutes=minutes_apart)
    _seed_points(con, day, rows)


def test_summary_marks_an_unseen_departure(dbfile, monkeypatch):
    """first_leave is the first fix that was *away*, which is a real departure
    only if a fix at home came before it. A day whose coverage starts after
    the person already left must not read as "left at 12:00"."""
    day = "2026-07-23"
    monkeypatch.setattr(db, "day_key", lambda dt=None: day)
    con = db.connect()
    _seed_day(con, day, n=COVERING_N, at_home_first=False, at_home_last=True)
    con.close()

    s = location.summary()
    assert s["day"] == day
    assert s["first_leave_at"] is not None      # the metric still computes
    assert s["leave_observed"] is False         # but the card must qualify it
    assert s["return_observed"] is True


def test_summary_skips_a_day_too_thin_to_trust(dbfile, monkeypatch):
    """A day with one fix computes 0km / 0 places, which renders identically
    to never leaving the house. It is a gap, not the day the card centers on."""
    thin, full = "2026-07-24", "2026-07-23"
    monkeypatch.setattr(db, "day_key", lambda dt=None: thin)
    con = db.connect()
    _seed_day(con, full, n=COVERING_N)
    _seed_day(con, thin, n=1)
    con.close()

    s = location.summary()
    assert s["day"] == full                     # not the newer, emptier day
    assert s["is_today"] is False
    by_day = {p["day"]: p["value"] for p in s["trend"]}
    assert by_day[thin] is None                 # a gap, never a zero
    assert by_day[full] is not None


def test_a_burst_of_fixes_is_not_a_covered_day(dbfile, monkeypatch):
    """A gate that counts fixes comes apart on a dense burst: the day with
    the highest fix count in the whole series (121) holds 2.4h of coverage —
    one drive, waved through as a full day whose 0 places and 0 min at home
    then read as facts. Many fixes over a
    short window is the *least* trustworthy shape, not the most."""
    burst, full = "2026-07-24", "2026-07-23"
    monkeypatch.setattr(db, "day_key", lambda dt=None: burst)
    con = db.connect()
    _seed_day(con, full, n=COVERING_N)
    # Four times the fixes of the covering day, inside a fraction of its span.
    _seed_day(con, burst, n=COVERING_N * 4, minutes_apart=1)
    con.close()

    s = location.summary()
    assert s["day"] == full                     # not the newer, denser day
    by_day = {p["day"]: p["value"] for p in s["trend"]}
    assert by_day[burst] is None                # a gap, however many rows


def test_the_card_and_the_gate_read_one_coverage(dbfile, monkeypatch):
    """_day_shape shows coverage on the card and coverage_minutes gates on it.
    A card displaying one number while the gate reads another is two answers
    to one question, which is the disagreement this whole change closed."""
    day = "2026-07-23"
    monkeypatch.setattr(db, "day_key", lambda dt=None: day)
    con = db.connect()
    _seed_day(con, day, n=COVERING_N)
    shown = location.summary()["coverage_min"]
    with db.session() as con:
        assert location.coverage_minutes(con, [day])[day] == shown
    assert not location.is_thin(shown)


def test_summary_backfills_days_the_poller_never_saw(dbfile, monkeypatch):
    """refresh() only ever computes today and yesterday, so a day the backend
    was down for is never computed by the poller. Every reader backfills its
    own window — seven such days were sitting uncomputed when this shipped."""
    old, today = "2026-07-23", "2026-07-28"
    monkeypatch.setattr(db, "day_key", lambda dt=None: today)
    con = db.connect()
    _seed_day(con, old, n=COVERING_N)
    assert con.execute("SELECT COUNT(*) c FROM location_days").fetchone()["c"] == 0
    con.close()

    s = location.summary()
    assert s["day"] == old
    with db.session() as con:
        assert con.execute("SELECT COUNT(*) c FROM location_days"
                           " WHERE day=?", (old,)).fetchone()["c"] > 0


def test_a_day_with_no_fixes_is_computed_once(dbfile, monkeypatch):
    """A fix-less day writes no location_days rows (a gap, not zeros), and
    judged by those it read as never-computed — so every GET re-ran the
    DELETE for it. location_sync marks it computed; only a fix landing on it
    later (a late-flushed OwnTracks queue) sends it back through."""
    gap, today = "2026-07-25", "2026-07-28"
    monkeypatch.setattr(db, "day_key", lambda: today)
    with db.session() as con:
        assert location._ensure_days(con, [gap]) == [gap]
        assert con.execute("SELECT COUNT(*) c FROM location_days"
                           " WHERE day=?", (gap,)).fetchone()["c"] == 0
        assert location._ensure_days(con, [gap]) == []     # not recomputed

    # summary() walks a window with this gap in it; a second call recomputes
    # nothing in it (today included — its count hasn't moved either).
    location.summary()
    calls = []
    real = location._compute_days
    monkeypatch.setattr(location, "_compute_days",
                        lambda con, days, now: (calls.append(days), real(con, days, now)))
    location.summary()
    assert calls == []

    # A fix that arrives late for the gap day makes it computable again.
    with db.session() as con:
        _seed_day(con, gap, n=COVERING_N)
        assert location._ensure_days(con, [gap]) == [gap]
        assert con.execute("SELECT COUNT(*) c FROM location_days"
                           " WHERE day=?", (gap,)).fetchone()["c"] > 0
        assert location._ensure_days(con, [gap]) == []


def _seed_ago(con, hours_ago, *, at_home, n=3):
    """n fixes ending `hours_ago` hours before now, at home or away. Written
    in UTC because that is how the listener stores them."""
    from datetime import timezone
    end = datetime.now(timezone.utc) - timedelta(hours=hours_ago)
    lat, lon = (HOME_LAT, HOME_LON) if at_home else (HOME_LAT + 0.05, HOME_LON + 0.05)
    rows = []
    for i in range(n):
        ts = end - timedelta(minutes=(n - 1 - i))
        rows.append((ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
                     ts.astimezone().date().isoformat(), lat, lon))
    con.executemany(
        "INSERT INTO location_points (ts, day, lat, lon) VALUES (?,?,?,?)", rows)
    con.commit()


def _relay_on(monkeypatch):
    """health() reads 'off' with no ingest path configured."""
    monkeypatch.setattr(location_relay, "LOCATION_RELAY_URL", "https://relay.test")
    monkeypatch.setattr(location_relay, "LOCATION_RELAY_TOKEN", "t")


def test_silence_at_home_is_not_a_broken_source(dbfile, monkeypatch):
    """Overland reports movement, so a phone sitting at home produces no fixes
    at all — 40 hours of that is a weekend in, not a dead app. The dot must
    stay ok, or a homebody week teaches you to ignore the panel."""
    _relay_on(monkeypatch)
    con = db.connect()
    _seed_ago(con, 40, at_home=True)
    con.close()

    h = location.health()
    assert h["state"] == "ok"
    assert "at home" in h["detail"]

    s = location.summary()
    assert s["fix_stale"] is False          # nothing to flag
    assert s["home_since"] is not None      # the honest reading instead
    assert s["home_for_min"] > 39 * 60


def test_silence_away_from_home_still_escalates(dbfile, monkeypatch):
    """The other half: silence that ends away from home means the thread was
    genuinely lost, and that is what the dot exists for."""
    _relay_on(monkeypatch)
    con = db.connect()
    _seed_ago(con, 40, at_home=False)
    con.close()

    assert location.health()["state"] == "stale"
    s = location.summary()
    assert s["fix_stale"] is True
    assert s["home_since"] is None


def test_home_stretch_starts_at_the_arrival(dbfile, monkeypatch):
    """The stay began when they got home, not at the last fix of it — the
    trailing run of at-home fixes is walked back to its first."""
    _relay_on(monkeypatch)
    con = db.connect()
    _seed_ago(con, 50, at_home=False, n=2)   # out earlier
    _seed_ago(con, 40, at_home=True, n=4)    # home since ~40h ago, four fixes
    con.close()

    stretch = location.home_stretch()
    # Four fixes a minute apart ending 40h ago -> the stay began ~40h3m ago.
    assert 40 * 60 + 2 < stretch["minutes"] < 40 * 60 + 5


def test_never_left_home(dbfile):
    """Every fix inside HOME_RADIUS_M: one place, no first_leave/last_return."""
    con = db.connect()
    day = "2026-07-21"
    base = datetime(2026, 7, 21, 12, 0, 0)
    rows, t = [], base
    for _ in range(6):
        rows.append((t.isoformat() + "Z", HOME_LAT, HOME_LON))
        t += timedelta(minutes=5)
    _seed_points(con, day, rows)
    m = _run(con, day)
    assert m["places"] == 1.0
    assert m["time_at_home_min"] > 0
    assert "first_leave" not in m
    assert "last_return" not in m
    con.close()
