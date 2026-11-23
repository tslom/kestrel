"""§25 food tracker routes — /api/food/*. Writes go through app/food.py; the
menus read pulls the usc_dining provider's cache and kicks the estimator for
anything it hasn't seen."""

import json
from datetime import date

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from .. import db, food, food_estimate
from ..providers import offline, usc_dining

router = APIRouter(prefix="/api/food")


def _do(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except food.FoodError as e:
        raise HTTPException(400, str(e)) from None


@router.get("/day")
def get_day(day: str | None = None):
    return food.day_summary(day)


@router.get("/week")
def get_week(end: str | None = None, days: int = 7):
    return food.week(end, max(1, min(days, 31)))


@router.get("/search")
def get_search(q: str, venue: str | None = None):
    return {"query": q, "results": food.search(q, venue=venue)}


@router.get("/barcode/{code}")
def get_barcode(code: str):
    try:
        return _do(food.from_barcode, code)
    except HTTPException:
        raise
    except Exception as e:  # network failure, phrased
        raise HTTPException(502, f"barcode lookup failed: {e}") from None


@router.get("/suggest")
def get_suggest(day: str | None = None, meal: str | None = None):
    return _do(food.suggest, day, meal)


class LogIn(BaseModel):
    food: str
    servings: float | None = None
    grams: float | None = None
    portion: str | None = None
    meal: str | None = None
    venue: str | None = None
    price: float | None = None
    paid_with: str | None = None
    day: str | None = None
    note: str | None = None


@router.post("/log")
def post_log(body: LogIn):
    return _do(food.log, **body.model_dump(), source="ui")


class EntryPatch(BaseModel):
    servings: float | None = None
    meal: str | None = None
    price: float | None = None
    paid_with: str | None = None
    note: str | None = None


@router.patch("/log/{log_id}")
def patch_log(log_id: int, body: EntryPatch):
    return _do(food.update_entry, log_id, **body.model_dump())


@router.delete("/log/{log_id}")
def delete_log(log_id: int):
    return _do(food.delete_entry, log_id)


class PortionIn(BaseModel):
    x: float | None = None     # × the logged amount; None dismisses


@router.post("/log/{log_id}/portion")
def post_portion(log_id: int, body: PortionIn):
    return _do(food.answer_portion, log_id, body.x)


@router.get("/recents")
def get_recents(meal: str | None = None, day: str | None = None):
    return {"foods": food.recents(8, meal), "meals": food.repeatable_meals(day)}


@router.post("/log/restore")
def restore_log(entry: dict):
    return _do(food.restore_entry, entry)


class RepeatIn(BaseModel):
    from_day: str
    to_day: str | None = None
    meal: str | None = None


@router.post("/repeat")
def post_repeat(body: RepeatIn):
    return {"entries": _do(food.repeat, body.from_day, body.to_day, body.meal)}


class FoodIn(BaseModel):
    name: str
    nutrients: dict[str, float]
    venue: str | None = None
    serving_desc: str = "1 serving"
    serving_g: float | None = None


@router.post("/foods")
def post_food(body: FoodIn):
    return _do(food.create_custom, body.name, body.nutrients, venue=body.venue,
               serving_desc=body.serving_desc, serving_g=body.serving_g,
               confidence=1.0)


class FoodPatch(BaseModel):
    nutrients: dict[str, float] | None = None
    serving_desc: str | None = None
    serving_g: float | None = None
    name: str | None = None


@router.patch("/foods/{food_id}")
def patch_food(food_id: int, body: FoodPatch):
    return _do(food.fix_food, food_id, **body.model_dump())


@router.get("/foods/{food_id}")
def get_food(food_id: int):
    with db.session() as con:
        f = food.get_food(con, food_id)
    if f is None:
        raise HTTPException(404, f"no food f{food_id}")
    return f


class CloseIn(BaseModel):
    day: str | None = None
    reopen: bool = False


@router.post("/close")
def post_close(body: CloseIn):
    return _do(food.close_day, body.day, reopen=body.reopen, source="ui")


@router.get("/profile")
def get_profile():
    with db.session() as con:
        return {"profile": food.get_profile(con),
                "targets": {r["nutrient"]: r["target"] for r in
                            con.execute("SELECT * FROM food_targets")}}


@router.put("/profile")
def put_profile(body: dict):
    return {"profile": _do(food.set_profile, **body)}


class TargetIn(BaseModel):
    nutrient: str
    target: float | None = None


@router.put("/target")
def put_target(body: TargetIn):
    _do(food.set_target, body.nutrient, body.target)
    return {"ok": True}


class WeighIn(BaseModel):
    kg: float | None = None
    lb: float | None = None
    day: str | None = None


@router.post("/weigh")
def post_weigh(body: WeighIn):
    return _do(food.weigh, body.kg, body.lb, body.day)


@router.get("/menus")
def get_menus(day: str | None = None):
    """The three halls for one day, meal → station → items, each item with
    its estimate when there is one. Fetches the day if it isn't cached and
    starts estimating anything new in the background."""
    day = day or db.day_key()
    try:
        d = date.fromisoformat(day)
    except ValueError:
        raise HTTPException(400, "day is YYYY-MM-DD") from None
    with offline.collect() as degraded:
        with offline.best_effort("usc menus"):
            usc_dining.refresh_menus([d])
        with offline.best_effort("usc venues"):
            usc_dining.refresh_venues()
    with db.session() as con:
        rows = con.execute(
            "SELECT m.*, f.id food_id, f.nutrients, f.serving_desc, f.confidence"
            " FROM usc_menu_items m LEFT JOIN foods f ON f.source='usc_estimate'"
            " AND f.source_id=m.item_key WHERE m.date=?"
            " ORDER BY m.venue, m.meal, m.station, m.item", (day,)).fetchall()
        venues = {r["slug"]: dict(r) for r in con.execute(
            "SELECT * FROM food_venues WHERE kind='dining_hall'")}
        pending = len(food_estimate.pending(con, 1))
    if pending:
        food_estimate.run_pending_async()
    order = {"Breakfast": 0, "Brunch": 1, "Lunch": 2, "Dinner": 3}
    halls: dict[str, dict] = {}
    for r in rows:
        n = json.loads(r["nutrients"] or "{}")
        h = halls.setdefault(r["venue"], {
            "slug": r["venue"],
            "name": venues.get(r["venue"], {}).get("name", r["venue"]),
            "meals": {}})
        st = h["meals"].setdefault(r["meal"], {}).setdefault(r["station"], [])
        st.append({"item": r["item"], "food_id": r["food_id"],
                   "serving_desc": r["serving_desc"],
                   "confidence": r["confidence"],
                   "kcal": round(n["kcal"]) if "kcal" in n else None,
                   "protein": round(n["protein"]) if "protein" in n else None,
                   "allergens": json.loads(r["allergens"] or "[]"),
                   "prefs": json.loads(r["prefs"] or "[]")})
    out = []
    for h in halls.values():
        meals = [{"meal": m, "stations": [{"station": s, "items": items}
                                          for s, items in stations.items()]}
                 for m, stations in sorted(h["meals"].items(),
                                           key=lambda kv: order.get(kv[0], 9))]
        out.append({**h, "meals": meals})
    return {"day": day, "halls": out, "estimating": bool(pending),
            **({"stale": True, "offline": degraded} if degraded else {})}


@router.get("/venues")
def get_venues():
    with offline.best_effort("usc venues"):
        usc_dining.refresh_venues()
    with db.session() as con:
        rows = [dict(r) for r in con.execute(
            "SELECT v.*, (SELECT COUNT(*) FROM foods f WHERE f.venue=v.slug) foods"
            " FROM food_venues v ORDER BY v.kind, v.name")]
    for r in rows:
        r["shifts"] = json.loads(r["shifts"] or "[]")
    return {"venues": rows}
