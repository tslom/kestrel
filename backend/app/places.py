"""Named places — where you are, by name rather than by coordinate.

The vault stays the source of truth, the same arrangement course notes use: one
note per place with `type: place` frontmatter, no table and no migration,
because index.db already stores frontmatter as JSON. A place you add or edit in
Obsidian shows up on the next reindex.

    ---
    type: place
    name: Maple Hall
    lat: 39.8283
    lon: -98.5795
    radius_m: 250
    kind: home
    from: 2026-08-18
    ---

Why this exists at all, rather than a tighter clustering constant: at campus
density the distance between two genuinely different places (a dorm and the gym
at its foot) is smaller than the GPS error on the fixes that would have to tell
them apart — measured on this vault's own fixes, accuracy near home runs p50
17m / p90 47m against a ~15m separation. No radius separates what the sensor
never resolved. So the places you care about are *declared*, and clustering is
left to handle only the ones you haven't named.

**Home is just a place with `kind: home`, and that is the point.** It used to be
one fixed coordinate in config, which silently assumes you never move. This
vault's owner moved cross-country, and every day before the move was scored against the
new coordinate 3,561km away — so `time_at_home_min` read a flat 0.0 for a month
of days on which they were, in fact, home. A dated place fixes that class of
error rather than that instance of it: `from`/`until` bound when an entry is
true, and a day no entry covers has **no** home, which is an absence and not a
zero (DESIGN.md, "a sensor's silence is only a zero if the sensor was
watching").

Nothing here is required. With no `type: place` notes in the vault the whole
module falls back to the config constants and behaves exactly as before, so the
notes are an upgrade path and never a prerequisite.
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

# A declared place with no radius of its own. Deliberately the same default the
# home radius has always used: it is sized to hold one building including the
# scatter of its own fixes, which is the unit a person names.
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
        """Was this entry true on `day`? Both bounds are inclusive, so a move
        is written as the old place's `until` and the new place's `from` on
        adjacent days and neither day is left uncovered."""
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
    """A frontmatter date may arrive as a `date`, a string, or nothing. YAML
    parses an unquoted 2026-08-18 into a date object, so both shapes are
    ordinary rather than one being a mistake."""
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
    """Every `type: place` note in the vault, worst-case invalid ones skipped.

    A note missing lat/lon is dropped rather than raised on: the vault is hand
    edited, and a half-typed note should cost that one place, not the card."""
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
    """Flag pairs whose radii overlap while both are in effect.

    Not an error and not deduplicated: two places a short walk apart are a real
    fact about the world, and the honest response is to say the fixes there
    can't be attributed rather than to silently pick the nearer centroid. The
    map and the CLI both surface this."""
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
    """The home in effect on `day`, or None if the vault doesn't say.

    None is a real answer and callers must treat it as one: it means "no home
    was known for this day", which is why the day's `time_at_home_min` is
    omitted rather than written as 0. When the vault declares no home at all we
    fall back to the config constants, so an untouched install keeps its old
    behaviour — but once *any* `kind: home` note exists the notes are the whole
    truth, because a half-declared history is the bug this replaces."""
    homes = [p for p in (places if places is not None else load()) if p.is_home]
    if not homes:
        return _config_home()
    hits = [p for p in homes if p.covers_day(day)]
    if not hits:
        return None
    # Narrowest entry wins, so a dated stay overrides an open-ended one rather
    # than depending on note order.
    return sorted(hits, key=lambda p: (p.since is None, p.until is None))[0]


def _config_home() -> Place:
    return Place(name="Home", lat=HOME_LAT, lon=HOME_LON,
                 radius_m=HOME_RADIUS_M, kind="home")


def home_now(places: list[Place] | None = None) -> Place:
    """Home as of today, for the callers that mean "now" rather than a past day
    (weather's origin, leave.py's leave-by origin). Always answers: those callers
    need somewhere to start from, and the config constant is a better guess
    than nothing."""
    from . import db
    return home_on(db.day_key(), places) or _config_home()


def named_on(day: str, places: list[Place] | None = None) -> list[Place]:
    """Places declared as true on `day`, home included."""
    return [p for p in (places if places is not None else load()) if p.covers_day(day)]


def resolve(lat: float, lon: float, day: str,
            places: list[Place] | None = None) -> Place | None:
    """The declared place a fix falls in, nearest centroid first, or None.

    Nearest-centroid is a tie-break and not a claim: when the winner carries
    `ambiguous_with`, the fix could honestly belong to either."""
    hits = [(haversine_m(lat, lon, p.lat, p.lon), p)
            for p in named_on(day, places)
            if p.contains(lat, lon)]
    if not hits:
        return None
    return min(hits, key=lambda h: h[0])[1]


# ------------------------------------------------------------------- writing
#
# Naming a place from the map writes the same note you would have written in
# Obsidian, in the same folder, and then the vault is the source of truth again
# exactly as before — there is no second store and no "app-created" variant of
# a place. This is the only writer; everything above only reads.
#
# Not 06-Generated/: that folder is for notes an agent wrote and you have not
# vetted, and its provenance hook stamps them as such. A place note is the
# opposite — it is your assertion, made with a click, about what somewhere is
# called, and the dashboard's own clustering defers to it forever after.

PLACES_DIR = "02-Areas/places"


def _slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", name.casefold()).strip("-")
    return s or "place"


def create(name: str, lat: float, lon: float, radius_m: float = DEFAULT_RADIUS_M,
           kind: str = "place", since: str | None = None,
           until: str | None = None, note: str = "") -> dict:
    """Write one `type: place` note and index it, so the next read sees it.

    Indexing here rather than waiting for the watcher is what makes the map
    redraw with the name on the very next request: the whole point of the
    button is that the correction lands while you are looking at the place it
    is about."""
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
        # Renaming or moving a place is an edit to a note you can see, not a
        # silent overwrite from a map click.
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

    # sort_keys=False so the note reads the way the ones you wrote by hand do
    # — `type` and `name` first, the numbers after. Alphabetical order puts
    # `from` above `kind` above `lat`, which is correct and unreadable, and
    # this file's whole premise is that you open it in Obsidian.
    post = frontmatter.Post(body, **meta)
    path.write_text(frontmatter.dumps(post, sort_keys=False), encoding="utf-8")

    with indexer.session() as con:
        indexer.index_note(con, path)
        con.commit()

    return {"path": f"{PLACES_DIR}/{path.name}", **meta}
