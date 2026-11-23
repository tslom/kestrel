"""Venues — live lookup of real businesses and places, for "where should I go"
(Google Places API (New) + Routes).

Not `app/places.py`: that one is the user's *own* dwelt places, clustered from
location fixes. This one is the outside world — restaurants, cafés, parks, a
venue an event is at — and it exists because web search was the only way the
console could answer those, and an audit of its sessions (2026-08..10) found
web search failing at exactly the parts a map knows:

  * addresses — a street address never resolved at all;
  * distance — "closest KBBQ to Maple Hall" was the top search hit, and every
    "~12 min Uber" in a comparison table was invented, not computed;
  * open/closed — three of a dinner shortlist turned out permanently or
    temporarily closed only after the user asked for a re-check, and at 9pm a
    study-spot list offered two libraries that shut at 8.

So a search here returns, per place, the formatted address, `businessStatus`,
whether it is open *now* and when that changes, rating and review count, price
level, and the reservable / good-for-groups flags; then one Routes matrix call
adds real walk and drive minutes from the origin. What it cannot know — an AYCE
tier's price, a campground's alcohol rule — still needs the venue's own page.

Like reddit's `search()`, this is a live, uncached query with no range axis, so
it is not in REGISTRY (nothing for the poller to refresh, no days to read):

    python -m app.providers.venues "ayce kbbq" --near "Maple Hall, USC"
    python -m app.providers.venues "breakfast" --near "1513 S Grand Ave, LA" --open-now
    python -m app.providers.venues --details <place_id>     # + reviews, hours

Billing note: asking for `reservable`/`goodForGroups`/reviews puts a request in
Google's Enterprise + Atmosphere SKU, which has the smallest free monthly cap.
That is the point of the call, so they are asked for; reviews are left to
details() so a ten-result search doesn't pay for fifty of them.
"""

import json
import math
import sys
import urllib.error
import urllib.parse
from datetime import datetime, timedelta, timezone

from ..config import GOOGLE_MAPS_API_KEY
from .http import json_request

NAME = "venues"

SEARCH_URL = "https://places.googleapis.com/v1/places:searchText"
DETAILS_URL = "https://places.googleapis.com/v1/places/"
MATRIX_URL = ("https://routes.googleapis.com/distanceMatrix/v2:"
              "computeRouteMatrix")

DEFAULT_LIMIT = 8
MAX_LIMIT = 20                 # the API's own pageSize ceiling
DEFAULT_RADIUS_M = 5000        # a bias, not a fence: farther hits still come
MAX_RADIUS_M = 50000           # back, labelled with their real distance
# A location fix older than this isn't "where the user is" any more; fall back
# to home. Looser than leave.py's LEAVE_FIX_MAX_MIN (30): a leave-by alarm acts on
# the number, a "near me" list only ranks by it.
FIX_MAX_MIN = 60
WALK_MAX_MIN = 90

_PLACE_FIELDS = (
    "id", "displayName", "formattedAddress", "location", "businessStatus",
    "rating", "userRatingCount", "priceLevel", "priceRange",
    "currentOpeningHours.openNow", "currentOpeningHours.nextOpenTime",
    "currentOpeningHours.nextCloseTime", "utcOffsetMinutes",
    "primaryTypeDisplayName", "reservable", "goodForGroups", "googleMapsUri",
)
_DETAIL_FIELDS = _PLACE_FIELDS + (
    "websiteUri", "nationalPhoneNumber", "editorialSummary",
    "regularOpeningHours.weekdayDescriptions", "reviews",
)

_PRICE = {"PRICE_LEVEL_FREE": "free", "PRICE_LEVEL_INEXPENSIVE": "$",
          "PRICE_LEVEL_MODERATE": "$$", "PRICE_LEVEL_EXPENSIVE": "$$$",
          "PRICE_LEVEL_VERY_EXPENSIVE": "$$$$"}
_CLOSED = {"CLOSED_TEMPORARILY": "TEMPORARILY CLOSED",
           "CLOSED_PERMANENTLY": "PERMANENTLY CLOSED"}


def _key() -> str:
    if not GOOGLE_MAPS_API_KEY:
        raise RuntimeError("venues: GOOGLE_MAPS_API_KEY is not set")
    return GOOGLE_MAPS_API_KEY


def api_request(url: str, *, body: dict | None, field_mask: str,
             timeout: float = 30) -> dict | list:
    """One Places/Routes call. Tests monkeypatch this name."""
    try:
        return json_request(
            url, label="google places", method="POST" if body is not None
            else "GET", body=body, idempotent=True, timeout=timeout,
            headers={"X-Goog-Api-Key": _key(), "X-Goog-FieldMask": field_mask})
    except RuntimeError as e:
        # Google's 403 for an API that isn't switched on is a wall of JSON
        # with the reason ~600 chars in (so the body is read whole, and only
        # trimmed here); the one thing worth saying is which switch.
        if "SERVICE_DISABLED" in str(e):
            api = "Routes API" if "routes" in url else "Places API (New)"
            raise RuntimeError(
                f"{api} is not enabled for this GOOGLE_MAPS_API_KEY's project"
                " — enable it in Google Cloud Console") from None
        raise RuntimeError(str(e)[:400]) from None


# --- origin -----------------------------------------------------------------

def _default_origin() -> dict:
    """Where "near me" means: a fresh location fix, else home."""
    from .. import places
    from . import location
    fix = location.latest_fix()
    if fix and fix.get("age_min", 1e9) < FIX_MAX_MIN:
        return {"label": f"your location ({fix['age_min']:.0f} min ago)",
                "lat": fix["lat"], "lon": fix["lon"], "kind": "fix"}
    home = places.home_now()
    return {"label": "home", "lat": home.lat, "lon": home.lon, "kind": "home"}


def resolve(near: str, bias: dict | None = None) -> dict | None:
    """A place name or address -> {label, lat, lon}, via Places itself (it
    knows "Maple Hall" the way the Geocoding API knows street numbers), biased
    toward `bias` so a common name resolves to the local one. Falls back to
    leave.py's geocoder. The label is the resolved address, so a wrong resolution
    is visible to whoever reads the result instead of silently skewing every
    distance."""
    body = {"textQuery": near, "pageSize": 1}
    if bias:
        body["locationBias"] = {"circle": {
            "center": {"latitude": bias["lat"], "longitude": bias["lon"]},
            "radius": MAX_RADIUS_M}}
    try:
        blob = api_request(SEARCH_URL, body=body, field_mask=(
            "places.displayName,places.formattedAddress,places.location"))
        hits = blob.get("places") or []
    except (RuntimeError, urllib.error.URLError, TimeoutError):
        hits = []
    if hits:
        p = hits[0]
        name = (p.get("displayName") or {}).get("text", "")
        addr = p.get("formattedAddress", "")
        label = addr if not name or addr.startswith(name) else f"{name}, {addr}"
        return {"label": label, "lat": p["location"]["latitude"],
                "lon": p["location"]["longitude"], "kind": "near"}
    from .. import leave
    coords = leave.geocode(near)
    if coords is None:
        return None
    return {"label": f"{near} (geocoded)", "lat": coords[0], "lon": coords[1],
            "kind": "near"}


# --- shaping ----------------------------------------------------------------

def _local_hm(rfc3339: str | None, offset_min: int | None) -> str | None:
    """'2026-10-13T05:00:00Z' -> '10pm' in the *place's* zone (its own UTC
    offset, not this machine's — a search from NC about LA reads LA hours)."""
    if not rfc3339:
        return None
    try:
        t = datetime.fromisoformat(rfc3339.replace("Z", "+00:00"))
    except ValueError:
        return None
    if offset_min is not None:
        t = t.astimezone(timezone(timedelta(minutes=offset_min)))
    now = datetime.now(t.tzinfo)
    hm = t.strftime("%-I%p" if t.minute == 0 else "%-I:%M%p").lower()
    days = (t.date() - now.date()).days
    if (t.hour, t.minute) == (0, 0) and days == 1:
        # 00:00 tomorrow is tonight's close; "12am tomorrow" read as a day off.
        return "midnight"
    if days == 1:
        return f"{hm} tomorrow"
    if days > 1:
        return f"{t.strftime('%a')} {hm}"
    return hm


def _price_range(pr: dict | None) -> str | None:
    if not pr:
        return None
    lo = (pr.get("startPrice") or {}).get("units")
    hi = (pr.get("endPrice") or {}).get("units")
    if lo and hi:
        return f"${lo}–{hi}"
    return f"${lo}+" if lo else None


def _haversine_m(a_lat, a_lon, b_lat, b_lon) -> float:
    r = 6371000.0
    p1, p2 = math.radians(a_lat), math.radians(b_lat)
    dp, dl = p2 - p1, math.radians(b_lon - a_lon)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


def _shape(p: dict, origin: dict) -> dict:
    hours = p.get("currentOpeningHours") or {}
    off = p.get("utcOffsetMinutes")
    loc = p.get("location") or {}
    lat, lon = loc.get("latitude"), loc.get("longitude")
    return {
        "id": p.get("id"),
        "name": (p.get("displayName") or {}).get("text", ""),
        "address": p.get("formattedAddress", ""),
        "type": (p.get("primaryTypeDisplayName") or {}).get("text"),
        "status": p.get("businessStatus", "OPERATIONAL"),
        "rating": p.get("rating"),
        "reviews": p.get("userRatingCount"),
        "price": _PRICE.get(p.get("priceLevel")),
        "price_range": _price_range(p.get("priceRange")),
        # None = Google has no hours for it, which is not the same as closed.
        "open_now": hours.get("openNow"),
        "closes": _local_hm(hours.get("nextCloseTime"), off),
        "opens": _local_hm(hours.get("nextOpenTime"), off),
        "reservable": p.get("reservable"),
        "good_for_groups": p.get("goodForGroups"),
        "lat": lat, "lon": lon,
        "straight_km": (round(_haversine_m(origin["lat"], origin["lon"],
                                           lat, lon) / 1000, 2)
                        if lat is not None else None),
        "walk_min": None, "drive_min": None, "drive_km": None,
        "maps_url": p.get("googleMapsUri"),
    }


def _add_travel(items: list[dict], origin: dict) -> str | None:
    """Fill walk_min / drive_min / drive_km from one matrix call per mode.
    Returns an error string instead of raising: a list without travel times
    is still worth more than no list."""
    dests = [it for it in items if it["lat"] is not None]
    if not dests:
        return None
    body = {
        "origins": [{"waypoint": {"location": {"latLng": {
            "latitude": origin["lat"], "longitude": origin["lon"]}}}}],
        "destinations": [{"waypoint": {"location": {"latLng": {
            "latitude": it["lat"], "longitude": it["lon"]}}}} for it in dests],
    }
    for mode in ("WALK", "DRIVE"):
        req = {**body, "travelMode": mode}
        if mode == "DRIVE":
            req["routingPreference"] = "TRAFFIC_AWARE"
        try:
            rows = api_request(MATRIX_URL, body=req, field_mask=(
                "destinationIndex,duration,distanceMeters,condition"))
        except (RuntimeError, urllib.error.URLError, TimeoutError) as e:
            return f"travel times unavailable: {e}"
        for row in rows if isinstance(rows, list) else []:
            if row.get("condition") != "ROUTE_EXISTS":
                continue
            it = dests[row["destinationIndex"]]
            dur = row.get("duration", "")
            if not (dur.endswith("s") and dur[:-1].isdigit()):
                continue
            minutes = round(int(dur[:-1]) / 60)
            if mode == "WALK":
                it["walk_min"] = minutes
            else:
                it["drive_min"] = minutes
                if row.get("distanceMeters") is not None:
                    it["drive_km"] = round(row["distanceMeters"] / 1000, 1)
    return None


# --- the two reads ----------------------------------------------------------

def search(query: str, near: str | None = None, *,
           radius_m: int = DEFAULT_RADIUS_M, open_now: bool = False,
           limit: int = DEFAULT_LIMIT, by_distance: bool = False,
           travel: bool = True) -> dict:
    """Live Places text search around `near` (a name or address) or, without
    it, the user's fresh location / home. Raises RuntimeError on an API
    refusal and ValueError on bad input."""
    query = (query or "").strip()
    if not query:
        raise ValueError("venues search needs a query")
    limit = max(1, min(int(limit), MAX_LIMIT))
    radius_m = max(100, min(int(radius_m), MAX_RADIUS_M))
    _key()

    here = _default_origin()
    if near:
        origin = resolve(near, bias=here)
        if origin is None:
            raise ValueError(f"couldn't find {near!r} on a map — give a"
                             " street address or a venue with its city")
    else:
        origin = here

    body = {"textQuery": query, "pageSize": limit,
            "locationBias": {"circle": {
                "center": {"latitude": origin["lat"],
                           "longitude": origin["lon"]},
                "radius": float(radius_m)}}}
    if open_now:
        body["openNow"] = True
    if by_distance:
        body["rankPreference"] = "DISTANCE"
    blob = api_request(SEARCH_URL, body=body, field_mask=",".join(
        f"places.{f}" for f in _PLACE_FIELDS))
    items = [_shape(p, origin) for p in blob.get("places") or []]

    travel_error = _add_travel(items, origin) if travel and items else None
    if by_distance:
        items.sort(key=lambda it: (
            it["walk_min"] if it["walk_min"] is not None else 1e9,
            it["straight_km"] if it["straight_km"] is not None else 1e9))

    out = {"source": NAME, "search": query,
           "origin": {k: origin[k] for k in ("label", "kind", "lat", "lon")},
           "radius_m": radius_m, "open_now_only": open_now,
           "count": len(items), "data": items}
    if travel_error:
        out["travel_error"] = travel_error
    return out


def details(place_id: str) -> dict:
    """One place in full: everything search() gives plus the week's hours,
    website, phone, Google's editorial line and up to five reviews."""
    place_id = (place_id or "").strip().removeprefix("places/")
    if not place_id:
        raise ValueError("venues details needs a place id")
    p = api_request(DETAILS_URL + urllib.parse.quote(place_id), body=None,
                 field_mask=",".join(_DETAIL_FIELDS))
    loc = p.get("location") or {}
    out = _shape(p, {"lat": loc.get("latitude", 0),
                     "lon": loc.get("longitude", 0)})
    for k in ("straight_km", "walk_min", "drive_min", "drive_km"):
        out.pop(k)
    out.update({
        "website": p.get("websiteUri"),
        "phone": p.get("nationalPhoneNumber"),
        "summary": (p.get("editorialSummary") or {}).get("text"),
        "hours": (p.get("regularOpeningHours") or {}).get(
            "weekdayDescriptions") or [],
        "review_list": [{
            "rating": r.get("rating"),
            "when": r.get("relativePublishTimeDescription"),
            "text": " ".join(((r.get("text") or {}).get("text") or "").split()),
        } for r in p.get("reviews") or []],
    })
    return out


# --- rendering (CLI brief + console) ---------------------------------------

def _flag(v, yes: str, no: str) -> str | None:
    """True/False/None -> its word, or nothing when Google doesn't say."""
    return yes if v else (no if v is False else None)


def brief_line(it: dict) -> str:
    """One place per line, what a recommendation needs to stand on:
    Name (type) — address | ★4.5 (1,203) $$ $20–30 | open, closes 10pm |
    walk 12 min · drive 6 min (3.1 km) | reservable, groups | [id]"""
    head = f"{it['name']}"
    if it.get("type"):
        head += f" ({it['type']})"
    head += f" — {it['address']}"
    if it["status"] in _CLOSED:
        return f"{head} | {_CLOSED[it['status']]} | [{it['id']}]"
    parts = [head]
    rate = []
    if it.get("rating") is not None:
        rate.append(f"★{it['rating']} ({(it.get('reviews') or 0):,})")
    rate += [x for x in (it.get("price"), it.get("price_range")) if x]
    if rate:
        parts.append(" ".join(rate))
    if it.get("open_now") is True:
        parts.append("open" + (f", closes {it['closes']}" if it.get("closes")
                               else ""))
    elif it.get("open_now") is False:
        parts.append("CLOSED now" + (f", opens {it['opens']}"
                                     if it.get("opens") else ""))
    else:
        parts.append("hours unknown")
    trip = []
    # "min", not "m": "walk 892m" read as metres. Past WALK_MAX_MIN a walk
    # time is noise next to the drive, so it's left out of the line.
    if it.get("walk_min") is not None and it["walk_min"] <= WALK_MAX_MIN:
        trip.append(f"walk {it['walk_min']} min")
    if it.get("drive_min") is not None:
        trip.append(f"drive {it['drive_min']} min"
                    + (f" ({it['drive_km']} km)" if it.get("drive_km") else ""))
    if not trip and it.get("straight_km") is not None:
        trip.append(f"{it['straight_km']}km straight-line")
    if trip:
        parts.append(" · ".join(trip))
    flags = [f for f in (
        _flag(it.get("reservable"), "reservable", "not reservable"),
        _flag(it.get("good_for_groups"), "groups", "not for groups")) if f]
    if flags:
        parts.append(", ".join(flags))
    parts.append(f"[{it['id']}]")
    return " | ".join(parts)


def search_brief(hits: dict) -> str:
    o = hits["origin"]
    head = (f"venues {hits['search']!r} near {o['label']}"
            + (" (open now only)" if hits.get("open_now_only") else "")
            + f" ({hits['count']} places)")
    lines = [head]
    if hits.get("travel_error"):
        lines.append(f"({hits['travel_error']})")
    lines += [brief_line(it) for it in hits["data"]] or ["(nothing)"]
    return "\n".join(lines)


def details_brief(d: dict, review_chars: int = 400) -> str:
    lines = [brief_line({**d, "walk_min": None, "drive_min": None,
                         "straight_km": None})]
    for k in ("summary", "website", "phone", "maps_url"):
        if d.get(k):
            lines.append(f"{k}: {d[k]}")
    if d.get("hours"):
        lines.append("hours: " + "; ".join(d["hours"]))
    for r in d.get("review_list") or []:
        text = r["text"][:review_chars] + ("…" if len(r["text"]) > review_chars
                                            else "")
        lines.append(f"  ★{r['rating']} {r['when']}: {text}")
    return "\n".join(lines)


def _main(argv: list[str] | None = None) -> None:
    import argparse
    p = argparse.ArgumentParser(
        prog="python -m app.providers.venues",
        description="Live Google Places search with real travel times.")
    p.add_argument("query", nargs="?")
    p.add_argument("--near", help="place name or address to search around"
                   " (default: fresh location fix, else home)")
    p.add_argument("--radius-km", type=float, default=DEFAULT_RADIUS_M / 1000)
    p.add_argument("--open-now", action="store_true")
    p.add_argument("--by-distance", action="store_true",
                   help="rank nearest first instead of by relevance")
    p.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    p.add_argument("--details", metavar="PLACE_ID",
                   help="one place in full, with reviews")
    p.add_argument("--format", default="brief", choices=["json", "brief"],
                   dest="format_")
    args = p.parse_args(argv)
    try:
        if args.details:
            d = details(args.details)
            print(details_brief(d) if args.format_ == "brief"
                  else json.dumps(d, indent=2))
            return
        if not args.query:
            p.error("a query is required (or --details PLACE_ID)")
        hits = search(args.query, args.near,
                      radius_m=int(args.radius_km * 1000),
                      open_now=args.open_now, limit=args.limit,
                      by_distance=args.by_distance)
        print(search_brief(hits) if args.format_ == "brief"
              else json.dumps(hits, indent=2))
    except (ValueError, RuntimeError) as e:
        sys.exit(f"error: {e}")


if __name__ == "__main__":
    _main()
