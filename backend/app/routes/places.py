"""Place writes: naming a cluster the map drew. Creates a vault note and never
overwrites (`app/places.py` owns the note format; edits happen in Obsidian).
Design: DESIGN.md → `routes/places.py`.
"""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from .. import place_suggest, places

router = APIRouter(prefix="/api")


class NewPlace(BaseModel):
    name: str
    lat: float
    lon: float
    radius_m: float = Field(default=places.DEFAULT_RADIUS_M, gt=0)
    kind: str = "place"
    # Bounds usually absent: only a home has a history worth dating (places.home_on).
    since: str | None = None
    until: str | None = None
    note: str = ""


@router.get("/places")
def list_places():
    """Every declared place, with whatever each one overlaps. Not a provider
    range/metric slice — it is the vault's own list."""
    return {"places": [p.as_dict() | {"path": p.path} for p in places.load()]}


@router.post("/places")
def create_place(body: NewPlace):
    try:
        return places.create(
            name=body.name, lat=body.lat, lon=body.lon,
            radius_m=body.radius_m, kind=body.kind,
            since=body.since, until=body.until, note=body.note)
    except FileExistsError as e:
        raise HTTPException(409, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))


class Shown(BaseModel):
    name: str
    bucket: str = "unknown"


class Feedback(BaseModel):
    lat: float
    lon: float
    shown: list[Shown] = []
    # The name kept, or null for a shortlist closed without taking anything.
    chosen: str | None = None


@router.post("/places/feedback")
def place_feedback(body: Feedback):
    """What became of one shortlist — the only record that a candidate was
    ever turned down.

    Separate from the create above rather than folded into it because the
    interesting half is the case where nothing was created: a shortlist you
    closed is evidence about the ranking, and a create-only endpoint would
    only ever hear about the times it worked."""
    return place_suggest.record_feedback(
        body.lat, body.lon,
        [s.model_dump() for s in body.shown], body.chosen)
