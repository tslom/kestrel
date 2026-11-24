"""Nutrition for USC dining-hall items (the menu API has names only), once per distinct name.

decompose (model, tag-constrained) -> match in SR28 -> pick; stored as a usc_estimate
foods row, never overwritten (a food.fix_food correction is confidence 1). Failures live
in food_estimates: MAX_ATTEMPTS, no sooner than RETRY_AFTER; non-dishes are parked.
Design: DESIGN.md → Food (§25).
"""

import asyncio
import json
import re
import threading
from datetime import datetime, timedelta

from . import agent, db, food, model_catalog
from .config import FOOD_ESTIMATE_MODEL

BATCH = 25
PER_RUN = 150          # items per run; a cold start spreads over a few cycles
MAX_ATTEMPTS = 3
RETRY_AFTER = timedelta(hours=6)
CANDIDATES = 6
NO_THINKING = {"type": "disabled"}

_lock = threading.Lock()

DECOMPOSE_SYSTEM = (
    "You estimate the composition of university dining-hall and campus café "
    "menu items (`where` is the station or café). For each item, give the "
    "portion one serving there would be — a plated dining-hall serving, or "
    "the café's single order or package — and break it into 1-5 components a "
    "USDA food database "
    "would have, each with grams in that portion. Use plain database-style "
    "food names: 'chicken thigh roasted', 'white rice cooked', 'broccoli "
    "cooked', 'cheddar cheese', 'olive oil'. Count cooking oil and sauces when "
    "they matter. Respect the tags: a vegan item has no animal products, a "
    "vegetarian one no meat or fish. If the name is not a single food (a "
    "condiment bar, 'assorted cereals', a made-to-order station), still give "
    "the most typical single serving someone would take. Use components=[] "
    "only for things that are not food at all. For every component also give "
    "your best estimate of its kcal, protein, carbs and fat *for those grams* "
    "— used only if the database has no match (branded meat substitutes, "
    "specialty breads).\n"
    "Reply with only a JSON array: [{\"key\": ..., \"portion\": \"1 plate "
    "(about 350 g)\", \"grams\": 350, \"kcal_guess\": 520, \"components\": "
    "[{\"food\": \"...\", \"g\": 120, \"kcal\": 250, \"protein\": 30, "
    "\"carbs\": 0, \"fat\": 14}]}]")

PICK_SYSTEM = (
    "You match food components to USDA SR28 database entries. For each "
    "component, pick the candidate id that best matches the food as it would "
    "be served (cooked over raw when the dish is cooked; plain over branded). "
    "If no candidate is the same food, answer null.\n"
    "Reply with only a JSON array: [{\"key\": ..., \"picks\": [\"sr:...\" or "
    "null, ...]}] with one pick per component, in order.")


def _json_array(text: str) -> list:
    m = re.search(r"\[.*\]", text, flags=re.S)
    if not m:
        raise ValueError(f"no JSON array in model reply: {text[:200]!r}")
    return json.loads(m.group(0))


def _ask(prompt: str, system: str) -> list:
    return _json_array(asyncio.run(agent.ask(
        prompt, model=FOOD_ESTIMATE_MODEL, system=system, thinking=NO_THINKING)))


def pending(con, limit: int = PER_RUN) -> list[dict]:
    """Menu names with no foods row and no exhausted attempt record, most
    recent menus first (today's lunch matters more than last week's)."""
    cutoff = (datetime.now() - RETRY_AFTER).isoformat(timespec="seconds")
    rows = con.execute(
        "SELECT m.item_key, MIN(m.item) item, MIN(m.station) station,"
        " MAX(m.allergens) allergens, MAX(m.prefs) prefs, MAX(m.date) d"
        " FROM usc_menu_items m"
        " LEFT JOIN foods f ON f.source='usc_estimate' AND f.source_id=m.item_key"
        " LEFT JOIN food_estimates e ON e.item_key=m.item_key"
        " WHERE f.id IS NULL AND (e.item_key IS NULL OR"
        "   (e.attempts < ? AND e.attempted_at < ?))"
        " GROUP BY m.item_key ORDER BY d DESC LIMIT ?",
        (MAX_ATTEMPTS, cutoff, limit)).fetchall()
    return [dict(r) for r in rows]


def record_failure(con, key: str, error: str, final: bool = False) -> None:
    now = datetime.now().isoformat(timespec="seconds")
    con.execute(
        "INSERT INTO food_estimates (item_key, attempted_at, attempts, error)"
        " VALUES (?,?,?,?) ON CONFLICT(item_key) DO UPDATE SET"
        " attempted_at=excluded.attempted_at, error=excluded.error,"
        " attempts=CASE WHEN ? THEN ? ELSE food_estimates.attempts+1 END",
        (key, now, MAX_ATTEMPTS if final else 1, error[:300], final, MAX_ATTEMPTS))


MACROS = ("kcal", "protein", "carbs", "fat")


def compose(components: list[dict], picks: list) -> tuple[dict, list[dict], int]:
    """Σ picked SR28 nutrients × grams/100 → (nutrients, kept components,
    how many components fell back to the model's own macros).

    An unmatched component contributes the model's macros (never silently zero);
    its micros stay unknown, so totals under-report them rather than invent."""
    by_id = food.sr28()["by_id"]
    parts, kept, fallback = [], [], 0
    for c, pick in zip(components, picks):
        it = by_id.get(pick) if pick else None
        g = float(c.get("g") or 0)
        row = {"food": c.get("food"), "g": g,
               "match": it["name"] if it else None, "id": pick if it else None}
        if it and g > 0:
            parts.append(food.scale(it["n"], g / 100))
        elif g > 0:
            est = {k: float(c[k]) for k in MACROS
                   if isinstance(c.get(k), (int, float)) and c[k] >= 0}
            if est.get("kcal"):
                parts.append(est)
                row["model_macros"] = est
                fallback += 1
        kept.append(row)
    return food.add_nutrients(parts), kept, fallback


def estimate_dishes(items: list[dict]) -> dict[str, dict]:
    """One decompose + one pick call for up to BATCH dishes, no writes.

    `items` are {key, item, where, tags}. Returns {key: {"ok": True, nutrients,
    serving_desc, serving_g, confidence, detail}} or {key: {"ok": False, error, final}}.
    Shared by estimate_batch and app/food_seed.py."""
    agent.raise_if_blocked()
    ask = [{"key": it["key"], "item": it["item"], "where": it.get("where"),
            "tags": it.get("tags") or []} for it in items]
    decomposed = {d.get("key"): d for d in _ask(
        "Items:\n" + json.dumps(ask, indent=1), DECOMPOSE_SYSTEM) if isinstance(d, dict)}

    pick_req, cands = [], {}
    for it in items:
        d = decomposed.get(it["key"])
        if not d or not d.get("components"):
            continue
        comps = []
        for c in d["components"][:5]:
            hits = food.search_sr28(str(c.get("food") or ""), CANDIDATES)
            comps.append({"food": c.get("food"), "candidates":
                          [{"id": h["id"], "name": h["name"]} for h in hits]})
        cands[it["key"]] = comps
        pick_req.append({"key": it["key"], "item": it["item"], "components": comps})
    picks = {}
    if pick_req:
        picks = {p.get("key"): p.get("picks") or [] for p in _ask(
            "Components:\n" + json.dumps(pick_req, indent=1), PICK_SYSTEM)
            if isinstance(p, dict)}

    out: dict[str, dict] = {}
    for it in items:
        key = it["key"]
        d = decomposed.get(key)
        if d is None:
            out[key] = {"ok": False, "error": "model skipped the item", "final": False}
            continue
        if not d.get("components"):
            out[key] = {"ok": False, "error": "not a dish", "final": True}
            continue
        allowed = [{c["id"] for c in comp["candidates"]} for comp in cands[key]]
        chosen = [p if p in ok else None for p, ok in
                  zip(picks.get(key, []) + [None] * 5, allowed)]
        nutrients, kept, fallback = compose(d["components"][:5], chosen)
        if not nutrients.get("kcal"):
            out[key] = {"ok": False, "error": "no component matched the database",
                        "final": False}
            continue
        conf = 0.6 if not fallback else 0.5
        guess = d.get("kcal_guess")
        if isinstance(guess, (int, float)) and guess > 0 and \
                abs(nutrients["kcal"] - guess) / guess > 0.5:
            conf = 0.4
        grams = sum(c["g"] for c in kept) or d.get("grams")
        out[key] = {"ok": True, "nutrients": nutrients,
                    "serving_desc": str(d.get("portion") or "1 serving"),
                    "serving_g": float(grams) if grams else None, "confidence": conf,
                    "detail": {"components": kept, "kcal_guess": guess,
                               "estimated_at": datetime.now().isoformat(timespec="seconds"),
                               "model": model_catalog.pick(FOOD_ESTIMATE_MODEL).model
                               or FOOD_ESTIMATE_MODEL}}
    return out


def estimate_batch(items: list[dict]) -> int:
    """Dining-hall menu items (rows from pending()) → usc_estimate foods.
    Returns rows written."""
    results = estimate_dishes([
        {"key": it["item_key"], "item": it["item"], "where": it["station"],
         "tags": json.loads(it["allergens"] or "[]") + json.loads(it["prefs"] or "[]")}
        for it in items])
    written = 0
    with db.session() as con, con:
        for it in items:
            key = it["item_key"]
            r = results[key]
            if not r["ok"]:
                record_failure(con, key, r["error"], final=r["final"])
                continue
            food.upsert_food(
                con, name=it["item"], source="usc_estimate", source_id=key,
                nutrients=r["nutrients"], serving_desc=r["serving_desc"],
                serving_g=r["serving_g"], confidence=r["confidence"], detail=r["detail"],
                allergens=json.loads(it["allergens"] or "[]"),
                prefs=json.loads(it["prefs"] or "[]"))
            con.execute("DELETE FROM food_estimates WHERE item_key=?", (key,))
            written += 1
    return written


def run_pending(limit: int = PER_RUN) -> int:
    """Estimate what's pending, BATCH at a time. Non-blocking: if another run
    holds the lock (the poller and a menus read racing), this one is a no-op."""
    if not _lock.acquire(blocking=False):
        return 0
    try:
        with db.session() as con:
            todo = pending(con, limit)
        done = 0
        for i in range(0, len(todo), BATCH):
            batch = todo[i:i + BATCH]
            try:
                done += estimate_batch(batch)
            except agent.AgentError:
                raise   # blocked/rate-limited: the poller reports it, next cycle retries
            except Exception as e:  # one bad batch, not the run
                with db.session() as con, con:
                    for it in batch:
                        record_failure(con, it["item_key"], f"batch failed: {e}")
        return done
    finally:
        _lock.release()


def run_pending_async() -> None:
    """Fire and forget, for a read that just pulled new menus."""
    threading.Thread(target=lambda: _safe(run_pending), daemon=True,
                     name="food-estimate").start()


def _safe(fn) -> None:
    try:
        fn()
    except Exception:  # background; the poller retries
        pass
