"""Time-to-leave: today's remaining located events joined with a traffic-aware route.

leave_by = event start - drive_with_traffic - LEAVE_BUFFER_MIN; origin is a fresh fix
(< LEAVE_FIX_MAX_MIN) else home. Every external call degrades to None; lookups sit
behind a 5-minute in-memory TTL cache.
Design: DESIGN.md → `app/leave.py`: time-to-leave.
"""

import http.client
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta

from fastapi import APIRouter

from . import db, places
from .providers import calendar, location, weather
from .config import (GOOGLE_MAPS_API_KEY, LEAVE_BUFFER_MIN, LEAVE_FIX_MAX_MIN,
                     LEAVE_GEOCODE_CONTEXT)
from .providers.ranges import parse_range

router = APIRouter(prefix="/api")

ROUTES_URL = "https://routes.googleapis.com/directions/v2:computeRoutes"
GEOCODE_URL = "https://maps.googleapis.com/maps/api/geocode/json"
_TTL_SECONDS = 300

# key -> (expiry_epoch, value); shared by geocode + route lookups.
_cache: dict[str, tuple[float, object]] = {}


def _ttl_get(key: str):
    hit = _cache.get(key)
    if hit and hit[0] > time.time():
        return hit[1]
    return None


def _ttl_set(key: str, value) -> None:
    _cache[key] = (time.time() + _TTL_SECONDS, value)


def _http_json(url: str, *, data: bytes | None = None, headers: dict) -> dict | None:
    req = urllib.request.Request(url, data=data, headers=headers,
                                 method="POST" if data else "GET")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read())
    # OSError, not URLError: a mid-response drop is an unwrapped ConnectionResetError; HTTPException covers IncompleteRead.
    except (OSError, http.client.HTTPException, ValueError):
        return None


# Locations that name no place; Google geocodes them confidently anyway. URL anywhere, word markers only as a prefix.
_URL_RE = re.compile(r"https?://", re.I)
_VIRTUAL_RE = re.compile(
    r"^\s*(tbd|tba|n/?a|none|online|virtual|remote|zoom|webex|skype|"
    r"hangouts?|google\s*meet|meet\.google|ms\s*teams|teams|"
    r"phone|conference\s*call|call)\b", re.I)


def is_virtual(loc: str) -> bool:
    """True for a location that isn't a place to drive to."""
    return bool(_URL_RE.search(loc) or _VIRTUAL_RE.match(loc))


def _google_geocode(address: str) -> tuple[float, float] | None:
    if not GOOGLE_MAPS_API_KEY:
        return None
    q = urllib.parse.urlencode({"address": address, "key": GOOGLE_MAPS_API_KEY})
    blob = _http_json(f"{GEOCODE_URL}?{q}", headers={})
    if blob and blob.get("status") == "OK" and blob.get("results"):
        loc0 = blob["results"][0]["geometry"]["location"]
        return (loc0["lat"], loc0["lng"])
    return None


def geocode(loc: str) -> tuple[float, float] | None:
    """Location string -> (lat, lon): Google, then Open-Meteo, then one retry with
    LEAVE_GEOCODE_CONTEXT appended (bare room codes). TTL-cached; None if unresolved or virtual.

    A bare room code may resolve only to the campus centroid."""
    key = f"geo:{loc}"
    cached = _ttl_get(key)
    if cached is not None:
        return cached or None  # cached negative stored as ()
    if is_virtual(loc):
        return None
    coords = _google_geocode(loc)
    if coords is None:
        coords = weather.geocode(loc)  # also caches in geo_cache
    if coords is None and LEAVE_GEOCODE_CONTEXT and \
            LEAVE_GEOCODE_CONTEXT.lower() not in loc.lower():
        coords = _google_geocode(f"{loc}, {LEAVE_GEOCODE_CONTEXT}")
    _ttl_set(key, coords or ())
    return coords


def _drive_minutes(origin: tuple[float, float],
                   dest: tuple[float, float]) -> float | None:
    """Traffic-aware DRIVE minutes origin->dest, or None on failure / missing key. TTL-cached."""
    if not GOOGLE_MAPS_API_KEY:
        return None
    key = ("route:%.4f,%.4f->%.4f,%.4f" %
           (origin[0], origin[1], dest[0], dest[1]))
    cached = _ttl_get(key)
    if cached is not None:
        return cached or None
    body = json.dumps({
        "origin": {"location": {"latLng": {"latitude": origin[0],
                                            "longitude": origin[1]}}},
        "destination": {"location": {"latLng": {"latitude": dest[0],
                                                 "longitude": dest[1]}}},
        "travelMode": "DRIVE",
        "routingPreference": "TRAFFIC_AWARE",
    }).encode()
    blob = _http_json(ROUTES_URL, data=body, headers={
        "Content-Type": "application/json",
        "X-Goog-Api-Key": GOOGLE_MAPS_API_KEY,
        "X-Goog-FieldMask": "routes.duration",
    })
    minutes = None
    if blob and blob.get("routes"):
        dur = blob["routes"][0].get("duration", "")  # e.g. "2395s"
        if dur.endswith("s") and dur[:-1].isdigit():
            minutes = int(dur[:-1]) / 60
    # failures cached as () too
    _ttl_set(key, minutes if minutes is not None else ())
    return minutes


def _origin() -> tuple[tuple[float, float], str]:
    """(coords, source). Location fix younger than LEAVE_FIX_MAX_MIN, else HOME."""
    fix = location.latest_fix()
    if fix and fix.get("age_min", 1e9) < LEAVE_FIX_MAX_MIN:
        return (fix["lat"], fix["lon"]), "fix"
    home = places.home_now()
    return (home.lat, home.lon), "home"


def compute_leaves(day: str | None = None, *, strict: bool = False) -> list[dict]:
    """Remaining timed located events on `day`, each with leave_by (null if it won't geocode or route).

    `strict` re-raises a failed calendar read: the alarm reconciler withdraws alarms on an
    empty list, so an outage must not look like a clear afternoon."""
    day = day or db.day_key()
    try:
        # range anchored on `day`; a past day comes back empty
        events = calendar.get(parse_range("today", date.fromisoformat(day)))["data"]
    except Exception:  # no calendar auth etc. -> nothing to leave for
        if strict:
            raise
        return []
    now = datetime.now().astimezone()
    origin, origin_src = _origin()
    out: list[dict] = []
    for ev in events:
        if ev.get("all_day") or not ev.get("location"):
            continue
        try:
            start_dt = datetime.fromisoformat(ev["start"]).astimezone()
        except (ValueError, TypeError):
            continue
        if start_dt <= now:
            continue  # already started — nothing to leave for
        row = {
            "event": ev.get("summary"),
            # alarms key on the id, which survives a rename or move
            "event_id": ev.get("id"),
            "location": ev["location"],
            "start": ev["start"],
            "leave_by": None,
            "drive_min": None,
            "origin": origin_src,
        }
        dest = geocode(ev["location"])
        if dest is not None:
            drive = _drive_minutes(origin, dest)
            if drive is not None:
                leave_dt = start_dt - timedelta(minutes=drive + LEAVE_BUFFER_MIN)
                row["drive_min"] = round(drive)
                row["leave_by"] = leave_dt.isoformat()
        out.append(row)
    out.sort(key=lambda r: r["start"])
    return out


@router.get("/leave")
def get_leave():
    """Located timed events remaining today, each with a traffic-aware
    leave_by (null if it couldn't be computed)."""
    data = compute_leaves()
    return {"data": data, "buffer_min": LEAVE_BUFFER_MIN}
