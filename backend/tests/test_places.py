"""Named places and the dated home: a day whose home is unknown must produce no home metric at all (a zero is indistinguishable from a day out).

Design: DESIGN.md → `tests/test_places.py`.
"""

import json
import sqlite3
from contextlib import contextmanager

import pytest

from app import indexer, places
from app.providers import location

# Two real coordinates, far enough apart that no radius confuses them.
OLD = (40.0150, -105.2705)    # made up: town centroids, not anyone's home
NEW = (47.6062, -122.3321)
MOVE_DAY = "2026-08-17"


def _seed(monkeypatch, notes):
    """Build an index.db of place notes through the real `_jsonable` (so unquoted YAML dates travel the real path)."""
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.execute("CREATE TABLE notes (path TEXT, title TEXT, frontmatter TEXT)")
    for i, meta in enumerate(notes):
        con.execute("INSERT INTO notes VALUES (?,?,?)",
                    (f"02-Areas/places/p{i}.md", meta.get("name", f"p{i}"),
                     json.dumps(indexer._jsonable(meta))))

    @contextmanager
    def session():
        yield con

    monkeypatch.setattr(indexer, "session", session)
    return con


def _home_notes():
    return [
        {"type": "place", "name": "Old Town", "kind": "home",
         "lat": OLD[0], "lon": OLD[1], "radius_m": 250, "until": "2026-08-16"},
        {"type": "place", "name": "Maple Hall", "kind": "home",
         "lat": NEW[0], "lon": NEW[1], "radius_m": 250, "from": "2026-08-18"},
    ]


def test_home_follows_the_move(monkeypatch):
    _seed(monkeypatch, _home_notes())
    assert places.home_on("2026-07-25").name == "Old Town"
    assert places.home_on("2026-09-01").name == "Maple Hall"


def test_the_day_of_the_move_has_no_home(monkeypatch):
    """Neither entry covers 08-17 on purpose: mid-transit fixes belong to neither address, and absence is honest."""
    _seed(monkeypatch, _home_notes())
    assert places.home_on(MOVE_DAY) is None


def test_an_unknown_home_yields_no_metric_rather_than_a_zero(dbfile, monkeypatch):
    """A day with no known home is absent from time_at_home_min, never 0.0."""
    _seed(monkeypatch, _home_notes())
    from app import db
    con = db.connect()
    with con:
        for i in range(6):
            con.execute(
                "INSERT INTO location_points (ts, day, lat, lon) VALUES (?,?,?,?)",
                (f"2026-08-17T1{i}:00:00Z", MOVE_DAY, OLD[0], OLD[1]))
    location._compute_days(con, [MOVE_DAY], "2026-09-14T00:00:00")
    got = {r["metric"] for r in con.execute(
        "SELECT metric FROM location_days WHERE day=?", (MOVE_DAY,))}
    con.close()
    assert "time_at_home_min" not in got, "a homeless day was given a home number"
    assert "first_leave" not in got and "last_return" not in got
    # The metrics that don't depend on home are still computed.
    assert "km_traveled" in got and "places" in got


def test_a_pre_move_day_is_scored_against_the_pre_move_home(dbfile, monkeypatch):
    """Fixes at the North Carolina address on a North Carolina day are home (the old single constant scored 0.0)."""
    _seed(monkeypatch, _home_notes())
    from app import db
    con = db.connect()
    with con:
        for i in range(6):
            con.execute(
                "INSERT INTO location_points (ts, day, lat, lon) VALUES (?,?,?,?)",
                (f"2026-07-25T1{i}:00:00Z", "2026-07-25", OLD[0], OLD[1]))
    location._compute_days(con, ["2026-07-25"], "2026-09-14T00:00:00")
    row = con.execute(
        "SELECT value FROM location_days WHERE day=? AND metric='time_at_home_min'",
        ("2026-07-25",)).fetchone()
    con.close()
    assert row is not None, "a day at the old home produced no home metric"
    assert row["value"] > 0, "a day spent at the old home scored as time away"


def test_no_place_notes_falls_back_to_the_config_constant(monkeypatch):
    """An install with no place notes behaves exactly as before."""
    _seed(monkeypatch, [])
    from app.config import HOME_LAT, HOME_LON
    home = places.home_on("2026-07-25")
    assert home is not None
    assert (home.lat, home.lon) == (HOME_LAT, HOME_LON)


def test_a_narrower_entry_wins_over_an_open_ended_one(monkeypatch):
    """Note order must not decide which home applies."""
    _seed(monkeypatch, [
        {"type": "place", "name": "Open", "kind": "home",
         "lat": NEW[0], "lon": NEW[1]},
        {"type": "place", "name": "Dated", "kind": "home",
         "lat": OLD[0], "lon": OLD[1], "from": "2026-07-01", "until": "2026-08-16"},
    ])
    assert places.home_on("2026-07-25").name == "Dated"
    assert places.home_on("2026-09-01").name == "Open"


def test_a_note_missing_coordinates_costs_only_itself(monkeypatch):
    _seed(monkeypatch, [
        {"type": "place", "name": "Broken"},
        {"type": "place", "name": "Fine", "lat": NEW[0], "lon": NEW[1]},
    ])
    names = [p.name for p in places.load()]
    assert names == ["Fine"]


def test_places_too_close_to_separate_say_so(monkeypatch):
    """Overlapping declarations (gym 15 s from the dorm, inside GPS error) are reported, not silently resolved."""
    _seed(monkeypatch, [
        {"type": "place", "name": "Maple Hall", "lat": NEW[0], "lon": NEW[1],
         "radius_m": 120},
        {"type": "place", "name": "Lyon Center", "lat": NEW[0] + 0.0002,
         "lon": NEW[1], "radius_m": 60},
    ])
    by_name = {p.name: p for p in places.load()}
    assert by_name["Maple Hall"].ambiguous_with == ["Lyon Center"]
    assert by_name["Lyon Center"].ambiguous_with == ["Maple Hall"]


def test_places_that_never_coexist_are_not_ambiguous(monkeypatch):
    """Two homes at the same coordinates in different years overlap in space only: not ambiguous."""
    _seed(monkeypatch, [
        {"type": "place", "name": "Old", "lat": NEW[0], "lon": NEW[1],
         "until": "2026-06-30"},
        {"type": "place", "name": "New", "lat": NEW[0], "lon": NEW[1],
         "from": "2026-07-01"},
    ])
    assert all(not p.ambiguous_with for p in places.load())


def test_resolve_names_a_fix_and_declines_when_it_is_nowhere(monkeypatch):
    _seed(monkeypatch, _home_notes())
    assert places.resolve(NEW[0], NEW[1], "2026-09-01").name == "Maple Hall"
    # Same coordinates before the move: not home yet, and no other note covers it.
    assert places.resolve(OLD[0], OLD[1], "2026-09-01") is None


@pytest.mark.parametrize("raw,want", [
    ("2026-08-18", "2026-08-18"),
    ("2026-08-18T00:00:00", "2026-08-18"),
    (None, None),
    ("", None),
])
def test_dates_survive_both_yaml_shapes(raw, want):
    assert places._coerce_day(raw) == want
