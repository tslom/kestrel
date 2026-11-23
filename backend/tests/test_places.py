"""Named places, and the dated home that motivated them.

The bug these tests pin is not hypothetical. `HOME_LAT`/`HOME_LON` were a
single constant, this vault's owner moved cross-country, and every day before the
move was scored against the address they moved *to* — 3,561 km away. So
`time_at_home_min` read a flat 0.0 for a month of days spent at home, twelve of
which reached the correlation matrix, where they manufactured a
`steps ~ time_at_home_min` correlation of +0.70 that collapses to +0.15 once
the mis-scored days are dropped.

The assertions are therefore mostly about what must NOT happen: a day whose
home is unknown must produce no home metric at all, because a zero there is
indistinguishable from a real day spent out.
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
    """Stand up an index.db holding the given place notes, through the same
    `_jsonable` the real indexer writes with — so an unquoted YAML date is
    tested along the path it actually travels."""
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
    """Neither entry covers 08-17, and that is deliberate: the fixes that day
    are mid-transit and belong to neither address. An absence is the honest
    answer; picking one would invent a homecoming or a departure."""
    _seed(monkeypatch, _home_notes())
    assert places.home_on(MOVE_DAY) is None


def test_an_unknown_home_yields_no_metric_rather_than_a_zero(dbfile, monkeypatch):
    """The whole point. A day with no known home must be absent from
    time_at_home_min, not present as 0.0 — a zero is a measurement, and this
    would be a measurement of the config rather than of the day."""
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
    """The regression proper: these fixes are at the North Carolina address on
    a North Carolina day, so they are *home*. Against the old single constant
    they scored 0.0."""
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
    """An untouched install must behave exactly as before — the notes are an
    upgrade path, never a prerequisite."""
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
    """The gym is a 15-second walk from the dorm — closer than the GPS error on
    the fixes that would have to tell them apart. Overlapping declarations are
    reported rather than silently resolved, because the honest answer is that a
    fix there could be either."""
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
    """Two homes at the same coordinates in different years overlap in space
    and not in time, which is not an ambiguity."""
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
    # The same coordinates before the move: that address wasn't home yet, and
    # no other note covers it.
    assert places.resolve(OLD[0], OLD[1], "2026-09-01") is None


@pytest.mark.parametrize("raw,want", [
    ("2026-08-18", "2026-08-18"),
    ("2026-08-18T00:00:00", "2026-08-18"),
    (None, None),
    ("", None),
])
def test_dates_survive_both_yaml_shapes(raw, want):
    assert places._coerce_day(raw) == want
