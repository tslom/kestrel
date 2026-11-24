"""Named places — where you are, by name rather than by coordinate.

One note per place with `type: place` frontmatter (read from index.db's frontmatter
JSON; no table). Home is a place with `kind: home`; a day no entry covers has no
home, which is an absence, not a zero. With no place notes the config constants
apply. Design: DESIGN.md → `app/places.py`: declared places.

    ---
    type: place
    name: Maple Hall
    lat: 39.8283
    lon: -98.5795
    radius_m: 250
    kind: home
    from: 2026-08-18
    ---

"""

import json
import math
import re
from dataclasses import dataclass, field
from datetime import datetime

import frontmatter

from . import indexer
from .config import HOME_LAT, HOME_LON, HOME_RADIUS_M, VAULT_PATH

EARTH_RADIUS_M = 6_371_000

# Default radius: one building plus the scatter of its own fixes.
DEFAULT_RADIUS_M = HOME_RADIUS_M


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlambda / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(a))


@dataclass
class Place:
    name: str
    lat: float
    lon: float
    radius_m: float = DEFAULT_RADIUS_M
    kind: str = "place"            # "home" is the only kind with behaviour
    since: str | None = None       # day_key, inclusive; None = always has been
    until: str | None = None       # day_key, inclusive; None = still is
    path: str | None = None        # the note it came from
    ambiguous_with: list[str] = field(default_factory=list)

    @property
    def is_home(self) -> bool:
        return self.kind == "home"

    def covers_day(self, day: str) -> bool:
        """Was this entry true on `day`? Bounds are inclusive."""
        if self.since and day < self.since:
            return False
        if self.until and day > self.until:
            return False
        return True

    def contains(self, lat: float, lon: float) -> bool:
        return haversine_m(lat, lon, self.lat, self.lon) <= self.radius_m

    def as_dict(self) -> dict:
        return {"name": self.name, "lat": self.lat, "lon": self.lon,
                "radius_m": self.radius_m, "kind": self.kind,
                "since": self.since, "until": self.until,
                "ambiguous_with": list(self.ambiguous_with)}


def _coerce_day(raw) -> str | None:
    """A frontmatter date arrives as a `date` (unquoted YAML), a string, or nothing."""
    if raw is None or raw == "":
        return None
    text = raw.isoformat() if hasattr(raw, "isoformat") else str(raw).strip()
    return text[:10] or None


def _coerce_float(raw) -> float | None:
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def load() -> list[Place]:
    """Every `type: place` note; one missing lat/lon is skipped, not raised on (hand-edited vault)."""
    rows = []
    with indexer.session() as con:
        for row in con.execute(
                "SELECT path, title, frontmatter FROM notes ORDER BY path"):
            meta = json.loads(row["frontmatter"] or "{}")
            if meta.get("type") != "place":
                continue
            lat = _coerce_float(meta.get("lat"))
            lon = _coerce_float(meta.get("lon"))
            if lat is None or lon is None:
                continue
            radius = _coerce_float(meta.get("radius_m"))
            rows.append(Place(
                name=str(meta.get("name") or row["title"] or row["path"]),
                lat=lat, lon=lon,
                radius_m=radius if radius and radius > 0 else DEFAULT_RADIUS_M,
                kind=str(meta.get("kind") or "place"),
                since=_coerce_day(meta.get("from") or meta.get("since")),
                until=_coerce_day(meta.get("until") or meta.get("to")),
                path=row["path"]))
    _mark_ambiguous(rows)
    return rows


def _mark_ambiguous(places: list[Place]) -> None:
    """Flag pairs whose radii overlap while both are in effect (not an error, not deduplicated)."""
    for i, a in enumerate(places):
        for b in places[i + 1:]:
            if not _overlap_in_time(a, b):
                continue
            if haversine_m(a.lat, a.lon, b.lat, b.lon) <= a.radius_m + b.radius_m:
                a.ambiguous_with.append(b.name)
                b.ambiguous_with.append(a.name)


def _overlap_in_time(a: Place, b: Place) -> bool:
    if a.until and b.since and a.until < b.since:
        return False
    if b.until and a.since and b.until < a.since:
        return False
    return True


def home_on(day: str, places: list[Place] | None = None) -> Place | None:
    """The home in effect on `day`, or None: unknown, so callers omit the metric rather than write 0.

    No `kind: home` note anywhere falls back to config; once any exists the notes are the whole truth."""
    homes = [p for p in (places if places is not None else load()) if p.is_home]
    if not homes:
        return _config_home()
    hits = [p for p in homes if p.covers_day(day)]
    if not hits:
        return None
    # narrowest entry wins, independent of note order
    return sorted(hits, key=lambda p: (p.since is None, p.until is None))[0]


def _config_home() -> Place:
    return Place(name="Home", lat=HOME_LAT, lon=HOME_LON,
                 radius_m=HOME_RADIUS_M, kind="home")


def home_now(places: list[Place] | None = None) -> Place:
    """Home as of today (weather and leave origins). Always answers, falling back to config."""
    from . import db
    return home_on(db.day_key(), places) or _config_home()


def named_on(day: str, places: list[Place] | None = None) -> list[Place]:
    """Places declared as true on `day`, home included."""
    return [p for p in (places if places is not None else load()) if p.covers_day(day)]


def resolve(lat: float, lon: float, day: str,
            places: list[Place] | None = None) -> Place | None:
    """The declared place a fix falls in (nearest centroid wins, a tie-break not a claim), or None."""
    hits = [(haversine_m(lat, lon, p.lat, p.lon), p)
            for p in named_on(day, places)
            if p.contains(lat, lon)]
    if not hits:
        return None
    return min(hits, key=lambda h: h[0])[1]


# ------------------------------------------------------------------- writing
# The only writer. Not 06-Generated/: a place note is the user's own assertion.

PLACES_DIR = "02-Areas/places"


def _slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", name.casefold()).strip("-")
    return s or "place"


def create(name: str, lat: float, lon: float, radius_m: float = DEFAULT_RADIUS_M,
           kind: str = "place", since: str | None = None,
           until: str | None = None, note: str = "") -> dict:
    """Write one `type: place` note and index it now, so the next request sees it."""
    name = (name or "").strip()
    if not name:
        raise ValueError("a place needs a name")
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise ValueError("coordinates out of range")
    if radius_m <= 0:
        raise ValueError("radius must be positive")

    target_dir = (VAULT_PATH / PLACES_DIR).resolve()
    if VAULT_PATH not in target_dir.parents and target_dir != VAULT_PATH:
        raise ValueError("places dir escapes vault")
    target_dir.mkdir(parents=True, exist_ok=True)

    path = target_dir / f"{_slug(name)}.md"
    if path.exists():
        raise FileExistsError(f"{PLACES_DIR}/{path.name} already exists")

    meta = {"type": "place", "name": name, "kind": kind,
            "lat": round(float(lat), 6), "lon": round(float(lon), 6),
            "radius_m": int(radius_m)}
    if since:
        meta["from"] = since
    if until:
        meta["until"] = until

    body = note.strip() or (
        f"# {name}\n\n"
        f"Named from the map on {datetime.now().date().isoformat()}. The "
        f"{int(radius_m)}m radius covers the fixes actually seen here; widen it "
        f"if `at_home`-style flicker shows up, and check `ambiguous_with` on "
        f"the map if something else sits inside it.")

    # sort_keys=False: `type` and `name` first, like hand-written notes
    post = frontmatter.Post(body, **meta)
    path.write_text(frontmatter.dumps(post, sort_keys=False), encoding="utf-8")

    with indexer.session() as con:
        indexer.index_note(con, path)
        con.commit()

    return {"path": f"{PLACES_DIR}/{path.name}", **meta}
