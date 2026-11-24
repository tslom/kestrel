"""Name candidates for an unnamed place cluster: calendar recurrence, Google
reverse geocode and nearby, Overpass.

It proposes, never decides: nothing here writes the vault or changes a metric.
Accepting a name writes a `type: place` note elsewhere. Source gates, ranking
and learned weights: DESIGN.md → `app/place_suggest.py`: name candidates for a cluster.
"""

import json
import logging
import math
import re
import time
import urllib.parse
import urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

from . import db, places
from .config import GOOGLE_MAPS_API_KEY
from .providers import location
from .tiles import USER_AGENT

log = logging.getLogger(__name__)

GEOCODE_URL = "https://maps.googleapis.com/maps/api/geocode/json"
# Overpass mirrors, raced in `_first_answer`; all failing is a soft loss of the source.
OVERPASS_URLS = (
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
)
# Per-request timeout; a healthy Overpass answer takes 1-8s.
TIMEOUT_S = 10
# A source that just failed at a cell is not re-asked for this long (never stored).
FAIL_BACKOFF_S = 300

# Google Places (New) Nearby Search; field mask is the Pro SKU. Measured: 20 places in 0.5s.
NEARBY_URL = "https://places.googleapis.com/v1/places:searchNearby"
NEARBY_FIELDS = ("places.displayName,places.location,places.primaryType,"
                 "places.primaryTypeDisplayName,places.types")
# Cap: a strip of offices returns one per storefront and pushes other sources off the list.
NEARBY_MAX = 6

# Calendar gate: share and overlap count. Measured: true answers 0.71/0.75, false <= 0.29.
CAL_MIN_SHARE = 0.5
CAL_MIN_OVERLAPS = 3
# Geocoded event location must be this close to the centroid.
CAL_MAX_DIST_M = 200

# Overpass radius: the cluster span, clamped.
OSM_MIN_RADIUS_M = 60
OSM_MAX_RADIUS_M = 150

# Types that are not places you stand in (localities, bus stops).
GOOGLE_SKIP_TYPES = frozenset({
    "plus_code", "locality", "political", "postal_code", "route",
    "neighborhood", "sublocality", "transit_station", "bus_station",
    "administrative_area_level_1", "administrative_area_level_2",
    "administrative_area_level_3", "country",
})
# Bare street addresses kept; extras only push real candidates off the list.
GOOGLE_MAX_ADDRESSES = 2

# Roads are never the answer.
OSM_SKIP_KEYS = ("highway", "railway", "waterway", "barrier", "boundary")
# Nor are settlements (`place=square` stays).
OSM_SKIP_PLACES = frozenset({"city", "town", "village", "hamlet", "suburb",
                             "quarter", "neighbourhood", "county", "state"})
# Plausible-destination tags, best first; a prior only.
OSM_RANK_KEYS = ("amenity", "leisure", "shop", "office", "tourism",
                 "healthcare", "building", "landuse")

# The weighted unit is the kind of evidence, not the API.
def _bucket(cand: dict) -> str:
    if cand["source"] != "google":
        return cand["source"]
    return "google_poi" if cand.get("poi") else "google_address"


# Priors; with no feedback they are the whole answer (see `learned_weights`).
PRIOR_WEIGHT = {"calendar": 1.0, "google_poi": 0.55, "google_address": 0.25,
                "google_nearby": 0.5, "overpass": 0.45, "typed": 0.0}

# Observations needed to move a weight halfway off its prior.
PRIOR_STRENGTH = 8

# No per-cluster "rejected here before" penalty, on purpose: see DESIGN.md.


# ------------------------------------------------------------------ plumbing

def _http_json(url: str, *, data: bytes | None = None) -> dict | None:
    """A JSON GET/POST that fails soft: an unreachable source costs its candidates, not the request."""
    req = urllib.request.Request(
        url, data=data, headers={"User-Agent": USER_AGENT},
        method="POST" if data else "GET")
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
            return json.loads(resp.read())
    except Exception as e:
        log.warning("place_suggest: %s failed: %s", urllib.parse.urlparse(url).netloc, e)
        return None


def _first_answer(urls, data: bytes) -> dict | None:
    """POST `data` to every mirror at once; first answer wins. None only when all failed."""
    pool = ThreadPoolExecutor(max_workers=len(urls))
    try:
        futures = [pool.submit(_http_json, u, data=data) for u in urls]
        for f in as_completed(futures):
            if f.result() is not None:
                return f.result()
        return None
    finally:
        pool.shutdown(wait=False, cancel_futures=True)


# (source, lat4, lon4) -> monotonic time of the last failed fetch; in memory on purpose.
_failed_at: dict[tuple, float] = {}


def _cell(source: str, lat: float, lon: float) -> tuple:
    return (source, round(lat, 4), round(lon, 4))


def _recently_failed(source: str, lat: float, lon: float) -> bool:
    at = _failed_at.get(_cell(source, lat, lon))
    return at is not None and time.monotonic() - at < FAIL_BACKOFF_S


def _cached(source: str, lat: float, lon: float, fetch):
    """Ask `fetch` once per (source, ~11m cell), then never again (no TTL)."""
    _, key_lat, key_lon = cell = _cell(source, lat, lon)
    with db.session() as con:
        row = con.execute(
            "SELECT payload FROM geo_lookup_cache WHERE source=? AND lat=? AND lon=?",
            (source, key_lat, key_lon)).fetchone()
        if row:
            return json.loads(row["payload"])
        if _recently_failed(source, lat, lon):
            return []
        out = fetch()
        if out is None:                 # a failed lookup is not a cached empty
            _failed_at[cell] = time.monotonic()
            return []
        _failed_at.pop(cell, None)
        con.execute(
            "INSERT OR REPLACE INTO geo_lookup_cache"
            " (source, lat, lon, payload, fetched_at) VALUES (?,?,?,?,?)",
            (source, key_lat, key_lon, json.dumps(out),
             datetime.now().isoformat(timespec="seconds")))
        con.commit()
        return out


# ---------------------------------------------------------------- the sources

def _calendar_candidates(group: dict) -> list[dict]:
    """The located event that recurs across this place's visits, if one does (aggregated, not per stay)."""
    stays = [s for s in group.get("stays", []) if s.get("day")]
    if not stays:
        return []

    days = sorted({s["day"] for s in stays})
    with db.session() as con:
        rows = con.execute(
            "SELECT day, summary, location, start_at, end_at FROM events"
            " WHERE day BETWEEN ? AND ? AND all_day = 0"
            " AND location IS NOT NULL AND location != ''",
            (days[0], days[-1])).fetchall()

    by_day: dict[str, list] = {}
    for r in rows:
        by_day.setdefault(r["day"], []).append(r)

    hits: Counter = Counter()
    summaries: dict[str, str] = {}
    for s in stays:
        for e in by_day.get(s["day"], ()):
            try:
                start = location.parse_ts(e["start_at"])
                end = location.parse_ts(e["end_at"])
            except (ValueError, TypeError):
                continue
            if s["first"] < end and start < s["last"]:
                label = event_place(e["location"])
                if label:
                    hits[label] += 1
                    summaries.setdefault(label, e["summary"] or "")

    total = sum(hits.values())
    if total < CAL_MIN_OVERLAPS:
        return []
    label, n = hits.most_common(1)[0]
    share = n / total
    if share < CAL_MIN_SHARE:
        return []

    # Distance gate: catches an event you didn't attend overlapping every visit to home.
    from .leave import geocode
    coords = geocode(label)
    if coords is None:
        return []
    dist = places.haversine_m(group["lat"], group["lon"], coords[0], coords[1])

    vague = _is_campus_centroid(coords)
    if not vague and dist > CAL_MAX_DIST_M:
        return []

    why = f"{n} of {total} visits here overlap {summaries.get(label) or label}"
    if vague:
        # A campus-level geocode carries no position information: abstain, don't veto.
        why += " (campus-level geocode — position unverified)"

    return [{
        "name": label,
        "source": "calendar",
        "confidence": share * (0.85 if vague else 1.0),
        "distance_m": None if vague else round(dist),
        "why": why,
    }]


def _is_campus_centroid(coords: tuple[float, float]) -> bool:
    """Whether a geocode is really LEAVE_GEOCODE_CONTEXT's own centroid (within 30m)."""
    from .config import LEAVE_GEOCODE_CONTEXT
    from .leave import geocode
    if not LEAVE_GEOCODE_CONTEXT:
        return False
    centre = geocode(LEAVE_GEOCODE_CONTEXT)
    if centre is None:
        return False
    return places.haversine_m(coords[0], coords[1], centre[0], centre[1]) <= 30


_ROOM_RE = re.compile(r"^[A-Z]{2,4}\s?\d{1,4}[A-Z]?$")


def event_place(raw: str) -> str | None:
    """'KAP 267, 3620 S Vermont Ave, ...' -> 'KAP 267'; None for a URL or bare address."""
    from .leave import is_virtual
    if not raw or is_virtual(raw):
        return None
    head = raw.split(",")[0].strip()
    if not head or head.replace(" ", "").isdigit():
        return None
    return head[:60]


def _google_candidates(lat: float, lon: float) -> list[dict]:
    """Google's reverse geocode, POIs kept and addresses kept as a fallback."""
    if not GOOGLE_MAPS_API_KEY:
        return []

    def fetch():
        q = urllib.parse.urlencode({"latlng": f"{lat},{lon}",
                                    "key": GOOGLE_MAPS_API_KEY})
        blob = _http_json(f"{GEOCODE_URL}?{q}")
        if not blob or blob.get("status") != "OK":
            return None
        out = []
        for r in blob.get("results", [])[:8]:
            types = r.get("types", [])
            if any(t in GOOGLE_SKIP_TYPES for t in types):
                continue
            head = (r.get("formatted_address") or "").split(",")[0].strip()
            if not head:
                continue
            poi = "point_of_interest" in types or "establishment" in types
            out.append({"name": head, "poi": poi, "types": types[:3]})
        return out

    seen, cands, addresses = set(), [], 0
    for r in _cached("google", lat, lon, fetch):
        if r["name"] in seen:
            continue
        seen.add(r["name"])
        if not r.get("poi"):
            # five interchangeable doors on one forecourt: keep two
            addresses += 1
            if addresses > GOOGLE_MAX_ADDRESSES:
                continue
        cands.append({
            "name": r["name"],
            "source": "google",
            "poi": bool(r.get("poi")),      # which bucket this lands in
            "confidence": 0.8 if r.get("poi") else 0.35,
            "distance_m": None,
            "why": "point of interest here" if r.get("poi") else "address here",
        })
    return cands


def _overpass_candidates(lat: float, lon: float, radius_m: float) -> list[dict]:
    """Every named non-road feature within `radius_m`, nearest first."""
    radius = int(max(OSM_MIN_RADIUS_M, min(OSM_MAX_RADIUS_M, radius_m or 0)))

    def fetch():
        query = (f'[out:json][timeout:{TIMEOUT_S}];'
                 f'(nwr(around:{radius},{lat},{lon})["name"];);'
                 f'out center tags 40;')
        body = urllib.parse.urlencode({"data": query}).encode()
        blob = _first_answer(OVERPASS_URLS, body)
        if blob is None:
            return None
        out = []
        for el in blob.get("elements", []):
            tags = el.get("tags") or {}
            name = tags.get("name")
            if not name or any(k in tags for k in OSM_SKIP_KEYS):
                continue
            if tags.get("place") in OSM_SKIP_PLACES:
                continue
            centre = el.get("center") or el
            if centre.get("lat") is None or centre.get("lon") is None:
                continue
            # `building=yes` is most of OSM's buildings; the key is the kind.
            kind = next((k if tags[k] == "yes" else tags[k]
                         for k in OSM_RANK_KEYS if tags.get(k)), None)
            out.append({"name": name, "kind": kind,
                        "rank": next((i for i, k in enumerate(OSM_RANK_KEYS)
                                      if tags.get(k)), len(OSM_RANK_KEYS)),
                        "lat": centre["lat"], "lon": centre["lon"]})
        return out

    cands = []
    for r in _cached("overpass", lat, lon, fetch):
        dist = places.haversine_m(lat, lon, r["lat"], r["lon"])
        # `around` matches any part of a way; a big polygon's centre can be far outside (509m from a 60m ask).
        if dist > radius:
            continue
        near = 1.0 - min(dist, OSM_MAX_RADIUS_M) / (OSM_MAX_RADIUS_M * 2)
        cands.append({
            "name": r["name"],
            "source": "overpass",
            "confidence": round(near * (1.0 - 0.04 * r["rank"]), 3),
            "distance_m": round(dist),
            "why": f"{_kind_text(r['kind']) or 'named'} {round(dist)}m away",
        })
    return sorted(cands, key=lambda c: -c["confidence"])[:8]


def _kind_text(kind: str | None) -> str | None:
    """`fast_food` -> `fast food`; a cached legacy `yes` means `building`."""
    if not kind:
        return None
    return "building" if kind == "yes" else kind.replace("_", " ")


def _nearby_candidates(lat: float, lon: float, radius_m: float) -> list[dict]:
    """Google's listed places within `radius_m`, each with its category."""
    if not GOOGLE_MAPS_API_KEY:
        return []
    radius = int(max(OSM_MIN_RADIUS_M, min(OSM_MAX_RADIUS_M, radius_m or 0)))

    def fetch():
        from .providers import venues
        body = {"locationRestriction": {"circle": {
                    "center": {"latitude": lat, "longitude": lon},
                    "radius": radius}},
                "maxResultCount": 20, "rankPreference": "DISTANCE"}
        try:
            blob = venues.api_request(NEARBY_URL, body=body,
                                   field_mask=NEARBY_FIELDS, timeout=TIMEOUT_S)
        except Exception as e:
            log.warning("place_suggest: places nearby failed: %s", e)
            return None
        out = []
        for p in blob.get("places") or []:
            name = (p.get("displayName") or {}).get("text")
            loc = p.get("location") or {}
            types = p.get("types") or []
            if not name or loc.get("latitude") is None:
                continue
            # same exclusions as the reverse geocode
            if p.get("primaryType") in GOOGLE_SKIP_TYPES or "bus_stop" in types:
                continue
            out.append({"name": name,
                        "kind": (p.get("primaryTypeDisplayName") or {}).get("text"),
                        "lat": loc["latitude"], "lon": loc["longitude"]})
        return out

    cands = []
    for r in _cached("google_nearby", lat, lon, fetch):
        dist = places.haversine_m(lat, lon, r["lat"], r["lon"])
        if dist > radius:
            continue
        near = 1.0 - min(dist, OSM_MAX_RADIUS_M) / (OSM_MAX_RADIUS_M * 2)
        cands.append({
            "name": r["name"],
            "source": "google_nearby",
            "confidence": round(near, 3),
            "distance_m": round(dist),
            "why": f"{r['kind'] or 'place'} {round(dist)}m away",
        })
    return sorted(cands, key=lambda c: -c["confidence"])[:NEARBY_MAX]


# ------------------------------------------------------------------- ranking

def suggest(lat: float, lon: float, rng) -> dict:
    """Ranked name candidates for the cluster near (lat, lon), recomputed by `location.cluster_at` so it matches the map."""
    group = location.cluster_at(lat, lon, rng)
    span = group["span_m"]

    # side by side: sequentially the slowest source is added to the rest
    c_lat, c_lon = group["lat"], group["lon"]
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(_calendar_candidates, group),
                   pool.submit(_google_candidates, c_lat, c_lon),
                   pool.submit(_nearby_candidates, c_lat, c_lon, span),
                   pool.submit(_overpass_candidates, c_lat, c_lon, span)]
        cands = [c for f in futures for c in f.result()]
    # so bare addresses read as "the map was down"
    unavailable = [s for s in ("google", "google_nearby", "overpass")
                   if _recently_failed(s, c_lat, c_lon)]

    weights = learned_weights()

    # One row per name: best source wins, the others go in `also`.
    merged: dict[str, dict] = {}
    for c in cands:
        key = c["name"].casefold()
        bucket = _bucket(c)
        c = dict(c, bucket=bucket,
                 score=round(weights[bucket]["weight"] * c["confidence"], 4))
        prior = merged.get(key)
        if prior is None:
            merged[key] = dict(c, also=[])
            continue
        if c["score"] > prior["score"]:
            c["also"] = prior["also"] + [prior["source"]]
            merged[key] = c
        else:
            prior["also"].append(c["source"])

    ranked = sorted(merged.values(), key=lambda c: -c["score"])
    for c in ranked:
        # corroboration is a tie-break, not a multiplier
        if c["also"]:
            c["score"] = round(c["score"] + 0.05 * len(set(c["also"])), 4)
    ranked.sort(key=lambda c: -c["score"])

    return {
        "place": {
            "lat": round(group["lat"], 6), "lon": round(group["lon"], 6),
            "minutes": round(group["seconds"] / 60, 1),
            "visits": group["visits"], "days": len(group["days"]),
            "span_m": round(span),
            "name": group["name"],
            # radius to write on accept; the 250m default swallows a neighbour
            "suggested_radius_m": _radius_for(span),
            "first_day": min(group["days"]) if group["days"] else None,
        },
        "candidates": ranked[:10],
        "unavailable": unavailable,
        "range": {"start": rng.start_key, "end": rng.end_key},
        # exposed so a weight learned from few clicks is visible
        "weights": weights,
    }


# ------------------------------------------------------------------ learning
# Learns which kind of evidence is accepted, not names or clusters: DESIGN.md.

def learned_weights() -> dict[str, dict]:
    """Per-bucket accept rate per candidate shown, Laplace-smoothed toward the prior.

    A bucket offering eight guesses scores at most ~1/8 (Overpass 0/19 -> 0.13); intended."""
    with db.session() as con:
        rows = con.execute(
            "SELECT bucket,"
            "       SUM(outcome = 'accepted') AS hits,"
            "       COUNT(*) AS shown"
            " FROM place_feedback GROUP BY bucket").fetchall()

    seen = {r["bucket"]: (r["hits"] or 0, r["shown"] or 0) for r in rows}
    out = {}
    for bucket, prior in PRIOR_WEIGHT.items():
        hits, shown = seen.get(bucket, (0, 0))
        weight = (hits + PRIOR_STRENGTH * prior) / (shown + PRIOR_STRENGTH)
        out[bucket] = {"weight": round(weight, 4), "prior": prior,
                       "accepted": hits, "shown": shown}
    return out


def record_feedback(lat: float, lon: float, shown: list[dict],
                    chosen: str | None) -> dict:
    """Log one shortlist; `chosen` is the kept name (None if dismissed). A typed non-match is an accept in `typed`."""
    now = datetime.now().isoformat(timespec="seconds")
    key_lat, key_lon = round(lat, 4), round(lon, 4)
    picked = (chosen or "").strip().casefold()
    matched = False

    rows = []
    for i, c in enumerate(shown):
        name = (c.get("name") or "").strip()
        if not name:
            continue
        if picked and name.casefold() == picked:
            outcome, matched = "accepted", True
        elif chosen:
            outcome = "passed"
        else:
            outcome = "dismissed"
        rows.append((now, key_lat, key_lon, name,
                     c.get("bucket") or c.get("source") or "unknown", i, outcome))

    if picked and not matched:
        rows.append((now, key_lat, key_lon, chosen.strip(), "typed",
                     len(shown), "accepted"))

    with db.session() as con:
        con.executemany(
            "INSERT INTO place_feedback (at, lat, lon, name, bucket, rank, outcome)"
            " VALUES (?,?,?,?,?,?,?)", rows)
        con.commit()
    return {"recorded": len(rows), "weights": learned_weights()}


def _radius_for(span_m: float) -> int:
    """Span x 1.25, min 60, max 250, rounded to 10s; never under the GPS floor (p90 47m)."""
    want = max(60.0, span_m * 1.25)
    return int(min(250, math.ceil(want / 10) * 10))
