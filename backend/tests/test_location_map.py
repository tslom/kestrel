"""location.trace() and the basemap tile cache — the two halves of the map.

The rule under most of these is that the map must not invent a second answer:
it reports the same places the `places` metric counts, breaks a path wherever
the day metrics would stop counting dwell, and thins only what it draws.
"""

from datetime import datetime, timedelta

from app import db, tiles
from app.config import HOME_LAT, HOME_LON, HOME_RADIUS_M
from app.providers import location
from app.providers.ranges import parse_range


def _seed(con, day, rows):
    con.executemany(
        "INSERT INTO location_points (ts, day, lat, lon) VALUES (?,?,?,?)",
        [(ts, day, lat, lon) for ts, lat, lon in rows])
    con.commit()


def _stay(base, lat, lon, minutes, every=1):
    """A stay: a fix every `every` minutes for `minutes` minutes."""
    return [((base + timedelta(minutes=m)).isoformat() + "Z", lat, lon)
            for m in range(0, minutes + 1, every)]


def test_trace_groups_points_by_day_and_keeps_the_places(dbfile):
    con = db.connect()
    base = datetime(2026, 7, 23, 12, 0, 0)
    _seed(con, "2026-07-23", _stay(base, 35.9000, -106.0500, 30))
    _seed(con, "2026-07-24", _stay(base + timedelta(days=1), 35.9200, -106.0700, 20))
    con.close()

    got = location.trace(parse_range("2026-07-23..2026-07-24"))

    assert [d["day"] for d in got["days"]] == ["2026-07-23", "2026-07-24"]
    assert got["fixes"] == got["shown"] == 31 + 21
    assert {(round(p["lat"], 2), round(p["lon"], 2)) for p in got["places"]} == {
        (35.9, -106.05), (35.92, -106.07)}
    # ordered by dwell, longest first — the list beside the map reads top-down
    assert got["places"][0]["minutes"] > got["places"][1]["minutes"]
    assert got["bounds"] == {"min_lat": 35.9, "max_lat": 35.92,
                             "min_lon": -106.07, "max_lon": -106.05}


def test_somewhere_only_passed_through_is_not_a_place(dbfile):
    """The map's places are the provider's: same stay detection, same
    three-minute floor. A map that drew its own would disagree with the card."""
    con = db.connect()
    base = datetime(2026, 7, 23, 9, 0, 0)
    rows = _stay(base, 35.9000, -106.0500, 30)                  # a real stop
    rows += [((base + timedelta(minutes=31 + i)).isoformat() + "Z",
              35.9010 + i * 0.001, -106.0510 - i * 0.001) for i in range(6)]
    con.execute("DELETE FROM location_points")
    _seed(con, "2026-07-23", rows)
    con.close()

    places = location.trace(parse_range("2026-07-23"))["places"]

    assert len(places) == 1
    assert (round(places[0]["lat"], 2), round(places[0]["lon"], 2)) == (35.9, -106.05)


def test_two_mornings_in_one_room_are_two_visits_and_one_place(dbfile):
    con = db.connect()
    base = datetime(2026, 7, 23, 9, 0, 0)
    rows = _stay(base, 35.9000, -106.0500, 20)
    rows += _stay(base + timedelta(hours=3), 35.9400, -106.0900, 20)   # elsewhere
    rows += _stay(base + timedelta(hours=6), 35.9000, -106.0500, 20)   # back again
    _seed(con, "2026-07-23", rows)
    con.close()

    places = {(round(p["lat"], 2), round(p["lon"], 2)): p
              for p in location.trace(parse_range("2026-07-23"))["places"]}

    assert places[(35.9, -106.05)]["visits"] == 2
    assert places[(35.9, -106.05)]["days"] == 1


def test_a_quiet_stretch_does_not_hand_a_place_the_afternoon(dbfile):
    """Same gap ceiling the day metrics use: a fix followed by four hours of
    silence books MAX_GAP_MIN, not four hours."""
    con = db.connect()
    base = datetime(2026, 7, 23, 9, 0, 0)
    _seed(con, "2026-07-23", [
        (base.isoformat() + "Z", 35.9000, -106.0500),
        ((base + timedelta(hours=4)).isoformat() + "Z", 35.9000, -106.0500),
        ((base + timedelta(hours=4, minutes=10)).isoformat() + "Z", 35.9000, -106.0500),
    ])
    con.close()

    place = location.trace(parse_range("2026-07-23"))["places"][0]

    assert place["minutes"] == location.MAX_GAP_MIN + 10


def test_the_path_is_thinned_but_the_places_are_not(dbfile, monkeypatch):
    """Thinning is a drawing decision. Computing dwell over a thinned set would
    quietly shorten every stay."""
    monkeypatch.setattr(location, "MAP_MAX_POINTS", 50)
    con = db.connect()
    base = datetime(2026, 7, 23, 0, 0, 0)
    _seed(con, "2026-07-23", _stay(base, 35.9000, -106.0500, 300))
    con.close()

    got = location.trace(parse_range("2026-07-23"))

    assert got["fixes"] == 301
    assert got["shown"] <= 50
    assert got["places"][0]["minutes"] == 300      # full resolution, not thinned


def test_a_range_with_no_fixes_answers_empty_rather_than_failing(dbfile):
    got = location.trace(parse_range("2026-07-23"))
    assert got == {**got, "days": [], "places": [], "bounds": None, "fixes": 0}


def test_home_travels_with_the_payload(dbfile):
    """The map draws the home radius as a real distance, so it needs the real
    numbers rather than a copy of them in the frontend."""
    got = location.trace(parse_range("2026-07-23"))
    assert got["home"]["lat"] == HOME_LAT and got["home"]["lon"] == HOME_LON
    assert got["home"]["radius_m"] == HOME_RADIUS_M


def test_a_tile_is_fetched_once_and_then_served_from_the_cache(dbfile, monkeypatch):
    calls = []
    monkeypatch.setattr(tiles, "_fetch", lambda z, x, y: calls.append((z, x, y)) or b"PNG")

    assert tiles.tile(15, 5636, 13081) == b"PNG"
    assert tiles.tile(15, 5636, 13081) == b"PNG"

    assert calls == [(15, 5636, 13081)]
    with db.session() as con:
        assert con.execute("SELECT COUNT(*) AS n FROM tile_cache").fetchone()["n"] == 1


def test_an_unreachable_tile_server_is_a_missing_tile_not_an_error(dbfile, monkeypatch):
    """The map degrades to the vector-only rendering — which was the other
    option in the first place — rather than the sheet failing."""
    monkeypatch.setattr(tiles, "_fetch", lambda z, x, y: None)

    assert tiles.tile(15, 5636, 13081) is None
    with db.session() as con:
        assert con.execute("SELECT COUNT(*) AS n FROM tile_cache").fetchone()["n"] == 0


def test_tiles_off_the_map_are_refused_without_asking_upstream(dbfile, monkeypatch):
    asked = []
    monkeypatch.setattr(tiles, "_fetch", lambda z, x, y: asked.append(1) or b"PNG")

    assert tiles.tile(25, 1, 1) is None            # past MAX_Z
    assert tiles.tile(1, 0, 0) is None             # below MIN_Z
    assert tiles.tile(4, 99, 0) is None            # x outside the grid at z=4
    assert tiles.tile(4, 0, -1) is None
    assert asked == []


def test_one_building_straddling_a_grid_line_is_one_place(dbfile):
    """The regression the stay rewrite exists for.

    These fixes are a single stationary hour scattered by ordinary GPS error
    across the 35.900/35.901 and -106.050/-106.051 boundaries. Grid clustering
    filed them as up to four places; measured on the real vault it turned one
    campus building into three and one flat into twelve. A stay is cut on
    distance from its own running centroid, so where the grid lines fall stops
    mattering."""
    con = db.connect()
    base = datetime(2026, 7, 23, 9, 0, 0)
    # ~40m of jitter around a boundary corner — well inside STAY_RADIUS_M.
    corners = [(35.90005, -106.05005), (35.89995, -106.04995),
               (35.90005, -106.04995), (35.89995, -106.05005)]
    rows = [((base + timedelta(minutes=m)).isoformat() + "Z",
             *corners[m % len(corners)]) for m in range(0, 61)]
    _seed(con, "2026-07-23", rows)
    con.close()

    places = location.trace(parse_range("2026-07-23"))["places"]

    assert len(places) == 1, f"one stationary hour split into {len(places)} places"
    assert places[0]["visits"] == 1
    assert places[0]["minutes"] == 60


def test_a_declared_place_holds_together_what_scatter_would_split(dbfile, monkeypatch):
    """Declared places outrank clustering: a flat whose own fixes spread wider
    than STAY_RADIUS_M is still one place, and it is named."""
    import json, sqlite3
    from contextlib import contextmanager
    from app import indexer

    idx = sqlite3.connect(":memory:")
    idx.row_factory = sqlite3.Row
    idx.execute("CREATE TABLE notes (path TEXT, title TEXT, frontmatter TEXT)")
    idx.execute("INSERT INTO notes VALUES (?,?,?)", (
        "02-Areas/places/maple.md", "Maple Hall",
        json.dumps({"type": "place", "name": "Maple Hall",
                    "lat": 35.9000, "lon": -106.0500, "radius_m": 250})))

    @contextmanager
    def session():
        yield idx

    monkeypatch.setattr(indexer, "session", session)

    con = db.connect()
    base = datetime(2026, 7, 23, 9, 0, 0)
    # ~180m apart: two separate stays by distance, one building by declaration.
    rows = _stay(base, 35.9000, -106.0500, 25)
    rows += _stay(base + timedelta(minutes=30), 35.9016, -106.0500, 25)
    _seed(con, "2026-07-23", rows)
    con.close()

    places = location.trace(parse_range("2026-07-23"))["places"]

    assert len(places) == 1, "a declared place was split by clustering"
    assert places[0]["name"] == "Maple Hall"
    assert places[0]["visits"] == 2
