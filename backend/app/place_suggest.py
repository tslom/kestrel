"""What is this place called — candidates for a cluster you haven't named.

§24 draws 93 places over seven weeks and two of them have names. The other 91
are centroids, which answer "where" and not "where *was I*". This module turns
a centroid into a ranked shortlist you can accept, edit, or ignore; accepting
one writes a `type: place` note, and from then on `location._group_stays`
claims every stay inside it and never guesses again.

**It proposes; it never decides.** Nothing here writes to the vault and nothing
here changes a metric. That matters because the measurements below say the
guess is right about a third of the time, wrong-but-quiet a third, and
confidently wrong the rest — and a suggestion that is wrong costs a click,
while an auto-applied name would silently corrupt `time_at_home_min`.

Three sources, measured on this vault's own fixes (2026-08-01..09-17):

* **Calendar recurrence** — which located event recurs across a place's
  visits. The strongest source when it fires and the only one that knows
  anything about *you*: one lecture-hall cluster overlaps THH 102 on 5 of 7
  event-overlapping visits, and another overlaps SAL 101 on 6 of 8. Both are
  right.

  It needs a gate, though, because the same query run per-stay is noise: only
  11% of stays overlap a located event at all, one long SPARK! event matched
  five stays several hundred metres apart, and the home dorm — already declared,
  already home — "recurs" with DMC 155 on 4 of 14 overlaps because you are at
  home while a class you did not attend is on the calendar. What separates the
  two cases is *concentration*: the right answers took 71% and 75% of their
  overlaps, every wrong one sat at or below 29%. Hence MIN_SHARE/MIN_OVERLAPS
  below, plus a distance gate, because an event's location is geocodable and a
  candidate that resolves half a mile away is not this place whatever the
  clock says.

* **Google reverse geocoding** — already enabled for §14's `leave`, so no new
  API and no new key. Returns one good POI when the point is on one
  ("McCarthy Quad", typed `point_of_interest`), and a street address when it
  is not. Never ranked first: it is a guess about the world, not about you.

* **Overpass** — every *named* feature within the cluster's own span. Keyless,
  and we are already a polite OSM client in `tiles.py`. This is the honest
  breadth: 80m around that same point holds Leavey Library, McCarthy Quad,
  Everybody's Kitchen, Birnkrant, and the Social Sciences Building. Which is
  exactly the point `places.py` opens with — six named things inside one GPS
  error — so Overpass alone can only ever offer a menu, never an answer.
  Nominatim's reverse endpoint was tried first and is the wrong tool: it
  returns the single nearest feature, which at that coordinate is an unnamed
  fountain.

* **Google Places nearby** (added 2026-10-13) — the same breadth as Overpass
  from a source that stays up, each place carrying a category ("Parking Lot",
  "Library"), so a parking structure and the building beside it read apart.
  Overpass stays for what Google doesn't list (named quads, OSM-only
  buildings) and as the fallback when the API is off.

All four are asked at once; see NEARBY_URL and TIMEOUT_S for the measurements.

Ranking weights are fixed constants here. They are what a later pass replaces
with weights learned from which suggestions you accept.
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
# Overpass in order of preference. The main instance is free, public, and
# regularly saturated — it answered one of the verification runs here with a
# 504 — and a mirror is the difference between a shortlist and four street
# addresses. Tried in order, first answer wins; all failing is a soft loss of
# this one source, like an unreachable tile server is a soft loss of the
# basemap.
OVERPASS_URLS = (
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
)
# Mirrors are raced, not tried in turn, and each gets this long. It was 25s
# tried in order, and on 2026-10-13 the main instance 504'd and the mirror
# read-timed-out, so naming one place sat for ~50s and then showed Google's
# addresses alone. A healthy answer takes 1-8s; past that the instance is
# saturated and waiting longer buys a shortlist nobody is still looking at.
TIMEOUT_S = 10
# A source that just failed at a cell is not asked again for this long, so
# reopening the panel doesn't sit out the same dead mirror twice. Short on
# purpose: a failure is still never *stored* (see `_cached`).
FAIL_BACKOFF_S = 300

# Google Places (New) Nearby Search — the typed answer to "what is here".
# Reverse geocoding returns addresses and at most one POI; this returns every
# listed place within the radius with a category ("Parking Lot", "Library",
# "Local Government Office"), which is the parking-deck-vs-building question.
# Measured at a Manhattan Beach cluster where reverse geocoding gave three
# street addresses: 20 places, the two civic-centre parking lots among them,
# in 0.5s. Field mask is names, positions and types only (the Pro SKU), and
# results are cached per cell forever like every source here.
NEARBY_URL = "https://places.googleapis.com/v1/places:searchNearby"
NEARBY_FIELDS = ("places.displayName,places.location,places.primaryType,"
                 "places.primaryTypeDisplayName,places.types")
# A strip of offices returns a lawyer, a jeweller and a skincare clinic per
# storefront; the nearest few are the menu, the rest push Overpass and the
# calendar off the end of the shortlist.
NEARBY_MAX = 6

# A calendar location is only offered when it takes at least MIN_SHARE of at
# least MIN_OVERLAPS event overlaps. Measured: the true answers scored 0.71 and
# 0.75, the false ones 0.29 and below, and the flat 17/17/17 three-way tie at
# the busiest unnamed cluster clears neither bar — which is correct, because
# that cluster is a hub and no single event names it.
CAL_MIN_SHARE = 0.5
CAL_MIN_OVERLAPS = 3
# ...and its geocoded location must be this close to the centroid. A room code
# often geocodes only to the campus centroid (see leave.geocode), so this is
# deliberately loose enough to accept a building and tight enough to reject
# the other side of campus.
CAL_MAX_DIST_M = 200

# How far out to ask Overpass. The cluster's own span when it has one, since a
# place that sprawls 150m should be asked about over 150m, clamped so a tight
# cluster still catches the building it sits in and a sprawling one does not
# drag in the next block.
OSM_MIN_RADIUS_M = 60
OSM_MAX_RADIUS_M = 150

# Result types that name something other than a place you could stand in.
# Measured against this vault's clusters, all of these arrived typed as
# `establishment`/`point_of_interest` and so passed the first cut: "Los
# Angeles" (a locality), "Jefferson / Royal" and "Vermont / 36th" (bus stops).
# A city is too big to be a place and a bus stop is somewhere you waited.
GOOGLE_SKIP_TYPES = frozenset({
    "plus_code", "locality", "political", "postal_code", "route",
    "neighborhood", "sublocality", "transit_station", "bus_station",
    "administrative_area_level_1", "administrative_area_level_2",
    "administrative_area_level_3", "country",
})
# How many bare street addresses to keep. A POI is a place; an address is a
# way of saying you don't know which one.
GOOGLE_MAX_ADDRESSES = 2

# Roads are named features and are never the answer to "what is this place".
# A stay *on* Hoover Boulevard is a stay at whatever building fronts it.
OSM_SKIP_KEYS = ("highway", "railway", "waterway", "barrier", "boundary")
# Same for a settlement: "Manhattan Beach" comes back on every query in
# Manhattan Beach. A `place=square` is somewhere you stand, so it stays.
OSM_SKIP_PLACES = frozenset({"city", "town", "village", "hamlet", "suburb",
                             "quarter", "neighbourhood", "county", "state"})
# Tags that make a named feature a plausible destination, best first. Ordering
# is a prior and nothing more: a library beats a car park at the same distance.
OSM_RANK_KEYS = ("amenity", "leisure", "shop", "office", "tourism",
                 "healthcare", "building", "landuse")

# The unit that gets weighted, and later learned. Not the API but the *kind of
# evidence*, because that is what can actually be right or wrong more often:
# Google's POIs and Google's street addresses are one endpoint and two
# different qualities of claim, and lumping them together would average a good
# signal with a bad one and learn nothing about either.
def _bucket(cand: dict) -> str:
    if cand["source"] != "google":
        return cand["source"]
    return "google_poi" if cand.get("poi") else "google_address"


# Starting weights. These are priors now rather than constants: with no
# feedback recorded they are the whole answer, and they lose influence as
# evidence arrives (see `learned_weights`).
PRIOR_WEIGHT = {"calendar": 1.0, "google_poi": 0.55, "google_address": 0.25,
                "google_nearby": 0.5, "overpass": 0.45, "typed": 0.0}

# How many observations it takes to move a weight halfway off its prior. Eight
# is chosen for the scale this actually runs at: a solo vault names a handful
# of places a month, so a prior that dissolved after two clicks would swing on
# noise, and one that needed a hundred would never move at all.
PRIOR_STRENGTH = 8

# There is deliberately no per-cluster "you rejected this here before" penalty,
# though it was built first and removed once measured. It cannot work, for a
# reason worth writing down so it doesn't get proposed again:
#
# The two ways a shortlist ends are accepting a name and walking away. Accept
# one and the cluster is declared, so it is never suggested again and no
# negative about it can ever be used. Walk away and *every* candidate on the
# list is rejected equally — a uniform penalty across the whole list, which
# re-ranks nothing. So the per-candidate negative is either unusable or a
# no-op, and the honest place for a dismissal is the bucket weight below:
# a kind of evidence you keep closing the panel on is a kind of evidence that
# is not helping.


# ------------------------------------------------------------------ plumbing

def _http_json(url: str, *, data: bytes | None = None) -> dict | None:
    """A JSON GET/POST that fails soft, like tiles.py's fetch: an unreachable
    geocoder costs this one source's candidates, never the request."""
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
    """POST `data` to every mirror at once; the first to answer wins and the
    rest are abandoned (their threads finish in the background, bounded by
    TIMEOUT_S). None only when every mirror failed."""
    pool = ThreadPoolExecutor(max_workers=len(urls))
    try:
        futures = [pool.submit(_http_json, u, data=data) for u in urls]
        for f in as_completed(futures):
            if f.result() is not None:
                return f.result()
        return None
    finally:
        pool.shutdown(wait=False, cancel_futures=True)


# (source, lat4, lon4) -> monotonic time of the last failed fetch. In memory on
# purpose: a reload forgetting it costs one more slow ask, and a failure that
# outlived the process would be the "cached negative" `_cached` refuses.
_failed_at: dict[tuple, float] = {}


def _cell(source: str, lat: float, lon: float) -> tuple:
    return (source, round(lat, 4), round(lon, 4))


def _recently_failed(source: str, lat: float, lon: float) -> bool:
    at = _failed_at.get(_cell(source, lat, lon))
    return at is not None and time.monotonic() - at < FAIL_BACKOFF_S


def _cached(source: str, lat: float, lon: float, fetch):
    """Ask `fetch` once per (source, ~11m cell), then never again.

    Buildings do not move, so a hit is as good as a fetch; see the v51
    migration for why there is no TTL."""
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
    """The located event that recurs across this place's visits, if one does.

    Aggregated over the group and not per stay: a single overlap is a
    coincidence of the clock (11% of stays have one), while the same room
    turning up across most of a place's visits is the place being that room."""
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

    # The clock agrees. Ask the map whether it could possibly be here — the
    # geocoder is §14's, cached and context-retrying, rather than a second one.
    #
    # The gate is here to catch one specific lie: an event you did not attend,
    # overlapping every visit to the place you stayed home in instead. Share
    # alone cannot see that; distance can.
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
        # Measured: "SAL 101", "KAP 140" and "KAP 267" all geocode to the exact
        # same point, the campus centroid 510-570m from any of their real
        # buildings — the failure leave.py's docstring predicts for bare room
        # codes. Vetoing on that distance rejected both of this vault's
        # strongest true answers (75% and 60% share). So when the geocoder has
        # plainly given us the campus rather than the building, the distance
        # gate abstains instead of voting: it has no information, and no
        # information is not evidence against.
        why += " (campus-level geocode — position unverified)"

    return [{
        "name": label,
        "source": "calendar",
        "confidence": share * (0.85 if vague else 1.0),
        "distance_m": None if vague else round(dist),
        "why": why,
    }]


def _is_campus_centroid(coords: tuple[float, float]) -> bool:
    """Whether a geocode is really just LEAVE_GEOCODE_CONTEXT's own centroid.

    Detected by asking for the context alone and comparing: a room code that
    resolves to the same spot as "University of Southern California" resolved
    to the university, not the room."""
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
    """The place part of a calendar location: 'KAP 267, 3620 S Vermont Ave,
    Los Angeles' -> 'KAP 267'. A URL or a bare address names no building."""
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
            # Reverse-geocoding a campus returns five doors on the same
            # forecourt, and they are interchangeable: the first two say
            # "roughly here, unnamed" as well as five do, and the other three
            # only push real candidates off the end of the shortlist.
            addresses += 1
            if addresses > GOOGLE_MAX_ADDRESSES:
                continue
        cands.append({
            "name": r["name"],
            "source": "google",
            "poi": bool(r.get("poi")),      # which bucket this lands in
            # A named establishment is a place; a street address is where a
            # place is. Both are offered, never at the same confidence.
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
        # `around` matches any part of a way, so a large polygon qualifies on
        # an edge while its centre sits far outside the query — "University
        # Park Campus" came back 509m from a 60m ask. The centre is what we
        # measure and rank by, so it has to be inside the radius too.
        if dist > radius:
            continue
        # Nearer and more destination-like is better, but only ever as a
        # ranking: six of these can sit inside one GPS error and the module
        # docstring says why none of them is therefore the answer.
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
    """OSM's `fast_food` -> `fast food`. A kind cached before `building=yes`
    was read as `building` still says `yes`; this is what it meant."""
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
            # Same exclusions as the reverse geocode: a bus stop is somewhere
            # you waited, a locality is too big to be a place.
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
    """Ranked name candidates for the cluster centred near (lat, lon).

    The cluster is recomputed by `location.cluster_at`, the same
    `_stays`/`_group_stays` the map and the `places` metric use, rather than
    taken from the caller — so a suggestion is always about a place the rest of
    the app agrees exists, and the visit labeller is looking at the same one."""
    group = location.cluster_at(lat, lon, rng)
    span = group["span_m"]

    # Asked side by side: each is a network round trip at worst, and in turn
    # they summed — the slowest source used to be added to everything else.
    c_lat, c_lon = group["lat"], group["lon"]
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(_calendar_candidates, group),
                   pool.submit(_google_candidates, c_lat, c_lon),
                   pool.submit(_nearby_candidates, c_lat, c_lon, span),
                   pool.submit(_overpass_candidates, c_lat, c_lon, span)]
        cands = [c for f in futures for c in f.result()]
    # Said out loud, so a shortlist of bare addresses reads as "the map was
    # down", not "there is nothing here".
    unavailable = [s for s in ("google", "google_nearby", "overpass")
                   if _recently_failed(s, c_lat, c_lon)]

    weights = learned_weights()

    # One row per name: the same building can come back from all three, and
    # agreement is evidence, so the best source wins the row and the others
    # are recorded as corroboration rather than dropped or double-counted.
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
        # Corroboration is a tie-break, not a multiplier: three sources naming
        # the same building is one building, not three times the evidence.
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
            # What to write if one is accepted: wide enough to hold the fixes
            # actually seen here rather than the 250m default, which is sized
            # for a home and swallows a neighbour at campus density.
            "suggested_radius_m": _radius_for(span),
            "first_day": min(group["days"]) if group["days"] else None,
        },
        "candidates": ranked[:10],
        "unavailable": unavailable,
        "range": {"start": rng.start_key, "end": rng.end_key},
        # What the ranking is currently believing, and on how much evidence.
        # Exposed rather than hidden because a weight learned from six clicks
        # is a number you should be able to see before you trust the order it
        # produced.
        "weights": weights,
    }


# ------------------------------------------------------------------ learning
#
# What this learns and what it does not, stated plainly because the gap matters:
#
# It learns **which kind of evidence you accept** — that is one number per
# bucket, estimated from a few dozen clicks, and it is honest at that size.
# It does not learn what places are called, cluster them better, or predict
# where you will be. Naming a place already achieves the first two outright:
# a declared place claims its stays forever, which is a stronger and more
# permanent correction than any weight.
#
# The estimator is a Laplace-smoothed accept rate, which is the simplest thing
# that degrades correctly. At zero feedback it *is* the prior; at ten
# observations it is most of the way to the observed rate; it never divides by
# zero and never swings to 0 or 1 on a single click.

def learned_weights() -> dict[str, dict]:
    """Per-bucket accept rate, smoothed toward the prior.

    Reads as: of the candidates from this kind of evidence that you have been
    shown, how often was one of them the name you kept.

    That is a per-*candidate* rate, which is the quantity the ranking needs —
    it multiplies one candidate's own confidence — and it has a consequence
    worth stating rather than discovering. A bucket that offers eight guesses
    can never score above about an eighth, while the calendar offers at most
    one and can approach one. Observed here after three shortlists: Overpass
    0/19 -> 0.13, Google's POIs 1/3 -> 0.49. That is not a bug being tolerated,
    it is the honest reading: eight names is weaker evidence than one name, and
    a ranking that treated breadth as confidence would put a car park above a
    lecture hall the calendar actually places you in."""
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
    """Log one shortlist and what became of it.

    `chosen` is the name you kept, or None if you closed the panel. A name you
    typed that matches nothing on the list is still an accept — of your own
    typing, in the `typed` bucket, which is the bucket that tells us the
    shortlist was no help at all."""
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
    """A radius that covers the observed spread with a margin, rounded to
    something a human would have typed. Never below the GPS floor measured in
    `places.py` (p90 47m) — a radius tighter than the error makes `at_home`
    flicker, which is the bug the home-dorm note records."""
    want = max(60.0, span_m * 1.25)
    return int(min(250, math.ceil(want / 10) * 10))
