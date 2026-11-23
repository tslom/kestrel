"""Naming a cluster the map drew — the candidate ranking, and the gate.

Every threshold asserted here came off this vault's own fixes over
2026-08-01..09-17, and the two tests worth reading first are the ones about
when the suggester must stay *quiet*:

* `test_hub_cluster_gets_no_calendar_name` — the busiest unnamed cluster
  (1,158 min over 24 days) overlaps KAP 140, DMC 155 and ACC 201 at 17% each.
  A three-way tie is not an answer, and offering the first one would name the
  place you study at after a lecture hall across campus.

* `test_event_you_did_not_attend_is_rejected` — the reason the distance gate
  exists at all. Share alone cannot tell "I was in that class" from "I stayed
  home while that class happened".

...and the one about when it must *not* go quiet: `test_campus_centroid_...`,
where the geocoder's inability to place a room code was silently vetoing the
two best true answers this vault has.
"""

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta

import pytest

from app import db, indexer, place_suggest, places
from app.providers import location, ranges

USC = (34.02235, -112.28512)      # what a bare room code geocodes to
THH = (34.02296, -112.28437)      # a real building, 95m off the centroid
HOME = (34.0262, -112.2900)        # made up

RANGE = "2026-08-01..2026-08-31"


def _group(lat, lon, day_hours):
    """A clustered place with one stay per (day, hour) given.

    Stay times go through `location.parse_ts` rather than being built naive,
    because that is how `suggest` builds them from `location_points` and both
    sides of the overlap comparison have to be tz-aware — the listener stores
    UTC with a Z and Google stores an offset, and neither is naive."""
    stays = []
    for day, hour in day_hours:
        first = location.parse_ts(f"{day}T{hour:02d}:05:00")
        stays.append({"lat": lat, "lon": lon, "seconds": 3000, "n": 4,
                      "first": first, "last": first + timedelta(minutes=50),
                      "day": day})
    return {"lat": lat, "lon": lon, "seconds": 3000 * len(stays),
            "n": 4 * len(stays), "visits": len(stays),
            "days": {d for d, _ in day_hours}, "name": None,
            "members": [(lat, lon)] * len(stays), "stays": stays,
            "first": stays[0]["first"], "last": stays[-1]["last"],
            "ambiguous_with": []}


def _events(con, rows):
    for i, (day, hour, summary, loc) in enumerate(rows):
        start = f"{day}T{hour:02d}:00:00"
        end = f"{day}T{hour:02d}:50:00"
        con.execute(
            "INSERT INTO events (day, event_id, calendar_id, summary,"
            " start_at, end_at, all_day, location) VALUES (?,?,?,?,?,?,0,?)",
            (day, f"e{i}", "school", summary, start, end, loc))
    con.commit()


@pytest.fixture
def geocoder(monkeypatch):
    """A stand-in for §14's geocoder. Defaults to the campus-centroid answer
    Google really gives for a bare room code."""
    table = {}

    def fake(loc):
        return table.get(loc, USC)

    import app.leave
    monkeypatch.setattr(app.leave, "geocode", fake)
    monkeypatch.setattr("app.config.LEAVE_GEOCODE_CONTEXT", "USC", raising=False)
    monkeypatch.setattr(place_suggest, "_is_campus_centroid",
                        lambda c: abs(c[0] - USC[0]) < 1e-4 and abs(c[1] - USC[1]) < 1e-4)
    return table


# ------------------------------------------------------------------ calendar

def test_recurring_event_names_the_place(dbfile, geocoder):
    """The measured true positive: SAL 101 on 6 of 8 visits."""
    con = db.connect()
    days = [(f"2026-08-{d:02d}", 9) for d in (3, 5, 7, 10, 12, 14, 17, 19)]
    _events(con, [(d, h, "CSCI 103", "SAL 101, 941 Bloom Walk") for d, h in days[:6]]
            + [(d, h, "Advising", "GFS 212, Los Angeles") for d, h in days[6:]])
    con.close()

    out = place_suggest._calendar_candidates(_group(*THH, days))
    assert len(out) == 1
    assert out[0]["name"] == "SAL 101"
    assert out[0]["confidence"] == pytest.approx(0.75 * 0.85, rel=1e-3)
    assert "6 of 8" in out[0]["why"]


def test_hub_cluster_gets_no_calendar_name(dbfile, geocoder):
    """A three-way tie names nothing. The real one was 17/17/17."""
    con = db.connect()
    days = [(f"2026-08-{d:02d}", 13) for d in (3, 5, 7, 10, 12, 14)]
    _events(con, [(d, h, "class", loc) for (d, h), loc in zip(
        days, ["KAP 140", "DMC 155", "ACC 201", "KAP 140", "DMC 155", "ACC 201"])])
    con.close()

    assert place_suggest._calendar_candidates(_group(*THH, days)) == []


def test_too_few_overlaps_names_nothing(dbfile, geocoder):
    """Two overlaps out of two is 100% of almost nothing."""
    con = db.connect()
    days = [(f"2026-08-{d:02d}", 9) for d in (3, 5)]
    _events(con, [(d, h, "class", "SAL 101") for d, h in days])
    con.close()

    assert place_suggest._calendar_candidates(_group(*THH, days)) == []


def test_event_you_did_not_attend_is_rejected(dbfile, geocoder):
    """Every visit to *home* overlaps a lecture across campus. Share says 100%;
    the geocode says half a kilometre. The geocode wins — this is the one lie
    concentration cannot catch."""
    geocoder["THH 102"] = THH          # a specific geocode, not the centroid
    con = db.connect()
    days = [(f"2026-08-{d:02d}", 10) for d in (3, 5, 7, 10)]
    _events(con, [(d, h, "CSCI 170", "THH 102") for d, h in days])
    con.close()

    assert place_suggest._calendar_candidates(_group(*HOME, days)) == []


def test_campus_centroid_geocode_does_not_veto(dbfile, geocoder):
    """The regression this module was rewritten for.

    "SAL 101", "KAP 140" and "KAP 267" all geocode to the identical campus
    centroid, 510-570m from their own buildings. Treating that as a distance
    measurement rejected the two strongest true answers in the vault, so a
    geocode that is plainly just the campus abstains — and says so, rather
    than claiming a position it does not have."""
    con = db.connect()
    days = [(f"2026-08-{d:02d}", 11) for d in (3, 5, 7, 10)]
    _events(con, [(d, h, "MATH 229", "KAP 140") for d, h in days])
    con.close()

    # The cluster is 570m from where "KAP 140" geocodes — past CAL_MAX_DIST_M.
    out = place_suggest._calendar_candidates(_group(34.02251, -112.29130, days))
    assert len(out) == 1
    assert out[0]["name"] == "KAP 140"
    assert out[0]["distance_m"] is None, "must not claim a distance it doesn't have"
    assert "unverified" in out[0]["why"]


def test_virtual_locations_never_become_places(dbfile, geocoder):
    """A Zoom link is on the calendar, is not on the map, and geocodes — §14
    learned this the expensive way (a usc.zoom.us URL resolves to campus)."""
    con = db.connect()
    days = [(f"2026-08-{d:02d}", 15) for d in (3, 5, 7, 10)]
    _events(con, [(d, h, "standup", "https://usc.zoom.us/j/9876543210")
                  for d, h in days])
    con.close()

    assert place_suggest._calendar_candidates(_group(*THH, days)) == []


# ------------------------------------------------------------------- sources

def test_google_drops_cities_and_bus_stops(dbfile, monkeypatch):
    """All three of these arrived typed `establishment`+`point_of_interest`
    from the real API, so the type allowlist alone would have let them in."""
    monkeypatch.setattr(place_suggest, "GOOGLE_MAPS_API_KEY", "k")
    monkeypatch.setattr(place_suggest, "_http_json", lambda *a, **k: {
        "status": "OK",
        "results": [
            {"types": ["establishment", "point_of_interest", "transit_station"],
             "formatted_address": "Jefferson / Royal, Los Angeles, CA"},
            {"types": ["locality", "political"],
             "formatted_address": "Los Angeles, CA, USA"},
            {"types": ["establishment", "point_of_interest", "park"],
             "formatted_address": "McCarthy Quad, 3551 Trousdale Pkwy"},
        ]})

    names = [c["name"] for c in place_suggest._google_candidates(*THH)]
    assert names == ["McCarthy Quad"]


def test_overpass_drops_roads_and_far_polygon_centres(dbfile, monkeypatch):
    """`around` matches a polygon's edge, so a campus-sized way qualifies on a
    60m query while its centre sits 500m out; and a road is never a place."""
    monkeypatch.setattr(place_suggest, "_http_json", lambda *a, **k: {
        "elements": [
            {"lat": THH[0], "lon": THH[1],
             "tags": {"name": "Hoover Boulevard", "highway": "residential"}},
            {"lat": 34.0270, "lon": -112.2880,
             "tags": {"name": "University Park Campus", "amenity": "university"}},
            {"lat": THH[0] + 0.0002, "lon": THH[1],
             "tags": {"name": "Leavey Library", "amenity": "library"}},
        ]})

    names = [c["name"] for c in place_suggest._overpass_candidates(*THH, 60)]
    assert names == ["Leavey Library"]


def test_lookup_cache_asks_once(dbfile, monkeypatch):
    """Buildings don't move; the second ask must not reach the network."""
    calls = []

    def fetch():
        calls.append(1)
        return [{"name": "Leavey Library"}]

    assert place_suggest._cached("overpass", *THH, fetch) == [{"name": "Leavey Library"}]
    assert place_suggest._cached("overpass", *THH, fetch) == [{"name": "Leavey Library"}]
    assert len(calls) == 1


@pytest.fixture(autouse=True)
def _forget_failures():
    """The failure backoff is module state; one test's dead mirror must not
    silence the next test's source."""
    place_suggest._failed_at.clear()
    yield
    place_suggest._failed_at.clear()


def test_failed_lookup_is_not_cached_as_empty(dbfile, monkeypatch):
    """A geocoder that was down must be asked again, not remembered as 'nothing
    is here' forever — there is no TTL to rescue a negative. Only not *at
    once*: within the backoff a reopen skips it rather than sitting out the
    same timeout again."""
    clock = [1000.0]
    monkeypatch.setattr(place_suggest.time, "monotonic", lambda: clock[0])
    calls = []

    def ok():
        calls.append(1)
        return [{"name": "X"}]

    assert place_suggest._cached("google", *THH, lambda: None) == []
    assert place_suggest._cached("google", *THH, ok) == [], "inside the backoff"
    assert calls == []
    clock[0] += place_suggest.FAIL_BACKOFF_S + 1
    assert place_suggest._cached("google", *THH, ok) == [{"name": "X"}]
    assert not place_suggest._recently_failed("google", *THH)


def test_overpass_races_mirrors_and_skips_the_town(dbfile, monkeypatch):
    """The mirror that answers wins even when the preferred one is down, and
    the town every query sits inside is not a place you were at."""
    def fake(url, *, data=None):
        if url == place_suggest.OVERPASS_URLS[0]:
            return None                                   # the 504
        return {"elements": [
            {"lat": THH[0], "lon": THH[1],
             "tags": {"name": "Manhattan Beach", "place": "city"}},
            {"lat": THH[0] + 0.0002, "lon": THH[1],
             "tags": {"name": "Metlox", "building": "yes"}}]}
    monkeypatch.setattr(place_suggest, "_http_json", fake)

    got = place_suggest._overpass_candidates(*THH, 60)
    assert [c["name"] for c in got] == ["Metlox"]
    assert got[0]["why"].startswith("building "), "building=yes reads as a kind"


def test_nearby_places_carry_their_category(dbfile, monkeypatch):
    """The point of the source: a parking lot says it is one. Bus stops and
    the city are dropped like in the reverse geocode, and the radius is a
    fence, nearest first."""
    from app.providers import venues
    monkeypatch.setattr(place_suggest, "GOOGLE_MAPS_API_KEY", "k")

    def place(name, ptype, label, dlat, types=()):
        return {"displayName": {"text": name}, "primaryType": ptype,
                "primaryTypeDisplayName": {"text": label},
                "types": [ptype, *types],
                "location": {"latitude": THH[0] + dlat, "longitude": THH[1]}}
    monkeypatch.setattr(venues, "api_request", lambda *a, **k: {"places": [
        place("Library", "library", "Library", 0.0004),
        place("Civic Center Lower", "parking_lot", "Parking Lot", 0.0001),
        place("Vermont / 36th", "transit_station", "Transit Station", 0.0),
        place("Stop", "establishment", "Place", 0.0, ("bus_stop",)),
        place("Far Away", "cafe", "Cafe", 0.01),
    ]})

    got = place_suggest._nearby_candidates(*THH, 60)
    assert [c["name"] for c in got] == ["Civic Center Lower", "Library"]
    assert got[0]["why"] == "Parking Lot 11m away"
    assert got[0]["source"] == place_suggest._bucket(got[0]) == "google_nearby"


def test_nearby_failure_is_soft_and_reported(dbfile, monkeypatch):
    """Places off or down costs its rows, never the shortlist — and suggest
    says which source was missing, so a short list isn't read as 'nothing
    here'."""
    from app.providers import venues
    monkeypatch.setattr(place_suggest, "GOOGLE_MAPS_API_KEY", "k")

    def boom(*a, **k):
        raise RuntimeError("Places API (New) is not enabled")
    monkeypatch.setattr(venues, "api_request", boom)
    monkeypatch.setattr(place_suggest, "_calendar_candidates", lambda g: [])
    monkeypatch.setattr(place_suggest, "_google_candidates", lambda *a: [])
    monkeypatch.setattr(place_suggest, "_overpass_candidates", lambda *a: [
        {"name": "Leavey Library", "source": "overpass", "confidence": 0.9,
         "distance_m": 10, "why": "library 10m away"}])
    _points_at(THH)

    out = place_suggest.suggest(*THH, ranges.parse_range("2026-08-03"))
    assert [c["name"] for c in out["candidates"]] == ["Leavey Library"]
    assert out["unavailable"] == ["google_nearby"]


def _points_at(pt):
    con = db.connect()
    for i in range(6):
        con.execute("INSERT INTO location_points (ts, day, lat, lon, accuracy)"
                    " VALUES (?,?,?,?,?)",
                    (f"2026-08-03T09:{i * 5:02d}:00", "2026-08-03",
                     pt[0], pt[1], 20))
    con.commit()
    con.close()


# ------------------------------------------------------------------- ranking

def test_agreement_promotes_but_does_not_multiply(dbfile, monkeypatch):
    """Two sources naming one building is one building. The merged row keeps
    the better source's score plus a flat nudge, so a weak candidate named
    twice can't outrank a strong one named once."""
    monkeypatch.setattr(place_suggest, "_calendar_candidates", lambda g: [])
    monkeypatch.setattr(place_suggest, "_google_candidates", lambda *a: [
        {"name": "Leavey Library", "source": "google", "poi": True,
         "confidence": 0.8, "distance_m": None, "why": "poi"}])
    monkeypatch.setattr(place_suggest, "_overpass_candidates", lambda *a: [
        {"name": "leavey library", "source": "overpass", "confidence": 0.9,
         "distance_m": 10, "why": "library"},
        {"name": "Taco Bell", "source": "overpass", "confidence": 0.5,
         "distance_m": 55, "why": "fast_food"}])
    monkeypatch.setattr(place_suggest, "_nearby_candidates", lambda *a: [])
    _points_at(THH)

    out = place_suggest.suggest(*THH, ranges.parse_range("2026-08-03"))
    names = [c["name"].casefold() for c in out["candidates"]]
    assert names == ["leavey library", "taco bell"], "one building, one row"

    top = out["candidates"][0]
    # google_poi's 0.8*0.55 = 0.44 beats overpass's 0.9*0.45 = 0.405, so
    # Google wins the row and Overpass corroborates it.
    assert top["also"] == ["overpass"]
    assert top["score"] == pytest.approx(0.49, abs=1e-3), "0.44 + one flat nudge"
    # The nudge must stay small enough that agreement cannot overturn a real
    # gap — a calendar hit at 0.5 would still outrank this.
    assert top["score"] < place_suggest.PRIOR_WEIGHT["calendar"] * 0.5 + 0.05


def test_suggest_refuses_a_point_with_no_cluster(dbfile):
    con = db.connect()
    con.execute("INSERT INTO location_points (ts, day, lat, lon, accuracy)"
                " VALUES (?,?,?,?,?)",
                ("2026-08-03T09:00:00", "2026-08-03", THH[0], THH[1], 20))
    con.commit()
    con.close()

    with pytest.raises(LookupError):
        place_suggest.suggest(35.9, -79.0, ranges.parse_range("2026-08-03"))


# -------------------------------------------------------------------- radius

@pytest.mark.parametrize("span, want", [
    (0, 60),        # a point: still never tighter than the p90 GPS error
    (20, 60),
    (79, 100),
    (132, 170),
    (400, 250),     # clamped: past this you have named a neighbourhood
])
def test_radius_covers_the_spread_seen(span, want):
    assert place_suggest._radius_for(span) == want


# --------------------------------------------------------------------- write

def test_created_place_is_read_back_by_the_clusterer(tmp_path, monkeypatch):
    """The whole point of the correction: once written, the declared place
    claims its stays and the guess never runs again."""
    vault = tmp_path / "vault"
    (vault / "02-Areas" / "places").mkdir(parents=True)
    monkeypatch.setattr(places, "VAULT_PATH", vault)
    monkeypatch.setattr(indexer, "VAULT_PATH", vault)

    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.executescript(indexer.SCHEMA)

    @contextmanager
    def session():
        yield con

    monkeypatch.setattr(indexer, "session", session)
    monkeypatch.setattr(indexer, "connect", lambda: con)

    out = places.create("Leavey Library", THH[0], THH[1], radius_m=100)
    assert out["path"] == "02-Areas/places/leavey-library.md"

    meta = json.loads(con.execute("SELECT frontmatter FROM notes").fetchone()[0])
    assert meta["type"] == "place" and meta["name"] == "Leavey Library"
    assert meta["radius_m"] == 100
    # Undated by default, and that is the correct default: an undated place is
    # true on every day, so it resolves fixes from before you got round to
    # naming it. A `from` taken from the range you happened to be viewing
    # would silently orphan its own history.
    assert "from" not in meta

    text = (vault / out["path"]).read_text()
    assert text.splitlines()[1] == "type: place", "reads like a hand-written note"

    loaded = places.load()
    assert [p.name for p in loaded] == ["Leavey Library"]
    assert loaded[0].contains(THH[0], THH[1])

    # ...and the clusterer now defers to it.
    stays = [{"lat": THH[0], "lon": THH[1], "seconds": 3000, "n": 4,
              "first": datetime(2026, 8, 5, 9), "last": datetime(2026, 8, 5, 10),
              "day": "2026-08-05"}]
    assert location._group_stays(stays, loaded)[0]["name"] == "Leavey Library"


def test_create_refuses_to_overwrite(tmp_path, monkeypatch):
    """Renaming a place is an edit to a note you can see, never a map click."""
    vault = tmp_path / "vault"
    (vault / "02-Areas" / "places").mkdir(parents=True)
    monkeypatch.setattr(places, "VAULT_PATH", vault)
    monkeypatch.setattr(indexer, "VAULT_PATH", vault)

    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.executescript(indexer.SCHEMA)

    @contextmanager
    def session():
        yield con

    monkeypatch.setattr(indexer, "session", session)

    places.create("Leavey Library", THH[0], THH[1])
    with pytest.raises(FileExistsError):
        places.create("leavey  library", THH[0], THH[1])


# ------------------------------------------------------------------ learning

def test_weights_start_at_their_priors(dbfile):
    """No feedback means no knowledge, and the estimator has to say so rather
    than collapsing to zero or to one."""
    w = place_suggest.learned_weights()
    for bucket, prior in place_suggest.PRIOR_WEIGHT.items():
        assert w[bucket]["weight"] == pytest.approx(prior)
        assert w[bucket]["shown"] == 0


def test_accepting_a_candidate_lifts_its_bucket(dbfile):
    shown = [{"name": "SAL 101", "bucket": "calendar"},
             {"name": "3710 McClintock Ave", "bucket": "google_address"}]
    place_suggest.record_feedback(*THH, shown, "SAL 101")

    w = place_suggest.learned_weights()
    assert w["calendar"]["accepted"] == 1 and w["calendar"]["shown"] == 1
    assert w["google_address"]["accepted"] == 0
    assert w["google_address"]["shown"] == 1
    # The passed-over bucket is dragged below its prior, the accepted one is
    # not dragged above 1.0 — smoothing is doing its job in both directions.
    assert w["google_address"]["weight"] < place_suggest.PRIOR_WEIGHT["google_address"]
    assert w["calendar"]["weight"] <= 1.0


def test_one_click_cannot_swing_a_weight(dbfile):
    """The whole point of smoothing at this scale: a single observation is
    evidence, not a conclusion."""
    before = place_suggest.learned_weights()["overpass"]["weight"]
    place_suggest.record_feedback(*THH, [{"name": "Taco Bell", "bucket": "overpass"}],
                                  "Somewhere Else")
    after = place_suggest.learned_weights()["overpass"]["weight"]
    assert after < before
    assert before - after < 0.1, "one pass-over must not gut a bucket"


def test_typing_your_own_name_is_recorded_as_such(dbfile):
    """The signal that the shortlist was no help at all — every candidate
    passed over, and the accept credited to a bucket the ranking cannot
    offer."""
    shown = [{"name": "Taco Bell", "bucket": "overpass"},
             {"name": "McCarthy Quad", "bucket": "google_poi"}]
    place_suggest.record_feedback(*THH, shown, "The good study room")

    w = place_suggest.learned_weights()
    assert w["typed"]["accepted"] == 1
    assert w["overpass"]["accepted"] == 0 and w["overpass"]["shown"] == 1
    assert w["google_poi"]["accepted"] == 0 and w["google_poi"]["shown"] == 1


def test_case_and_space_still_count_as_the_same_name(dbfile):
    """The field is editable, so the accepted name arrives however it was
    typed. "leavey library " is not a different building."""
    place_suggest.record_feedback(
        *THH, [{"name": "Leavey Library", "bucket": "overpass"}],
        "  leavey library ")
    w = place_suggest.learned_weights()
    assert w["overpass"]["accepted"] == 1
    assert w["typed"]["shown"] == 0, "not a typed name — it matched the list"


def test_walking_away_rejects_the_whole_list_not_one_name(dbfile):
    """Why there is no per-cluster negative, pinned so it isn't rebuilt.

    A dismissal marks every candidate rejected, because that is what it means.
    A penalty derived from it would therefore scale the entire shortlist by the
    same factor and re-rank nothing — and the other way a shortlist ends, an
    accept, declares the place and stops it ever being suggested again. So the
    only home for this evidence is the bucket weight."""
    shown = [{"name": "Taco Bell", "bucket": "overpass"},
             {"name": "McCarthy Quad", "bucket": "google_poi"}]
    place_suggest.record_feedback(*THH, shown, None)

    con = db.connect()
    outcomes = {r["name"]: r["outcome"] for r in
                con.execute("SELECT name, outcome FROM place_feedback")}
    con.close()
    assert set(outcomes.values()) == {"dismissed"}, "the list is rejected, not an item"

    # ...and it lands where it can actually do something: both buckets drop.
    w = place_suggest.learned_weights()
    assert w["overpass"]["weight"] < place_suggest.PRIOR_WEIGHT["overpass"]
    assert w["google_poi"]["weight"] < place_suggest.PRIOR_WEIGHT["google_poi"]
    assert not hasattr(place_suggest, "_dismissed_at"), \
        "per-cluster negatives were measured useless; see the module comment"
