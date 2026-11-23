"""§25: campus café menus the tracker knows before you've eaten there.

USC publishes no café menus or nutrition (checked 10-14), so this loads what
can be had, each row labeled with where its numbers came from:

  * food_data/coffee_bean.json — Coffee Bean & Tea Leaf's own nutrition PDF
    (scripts/build_coffee_bean.py). Exact: confidence 1.
  * food_data/panda_nutrition.txt — Panda Express's own nutrition page
    (macros, fiber, sugar, sodium: exact). Panda publishes no vitamins or
    minerals, so fill_micros() estimates only those, from an SR28
    decomposition of each dish, and says so on the row (detail.micros).
  * food_data/usc_dietitian.json — label numbers USC Hospitality's dietitian
    emailed on 10-16 (Taco Taco, C&G smoothies and açaí, Katora curry
    bowls). Exact: confidence 1.
  * food_data/campus_menus.json "published" — numbers with a stated origin,
    for any other venue that gets them.
  * food_data/campus_menus.json "estimate" — real item names (USC's Balanced
    Dining guide) with no numbers, estimated by app/food_estimate.py once per
    distinct name. "packaged" items are grab-and-go with a label: the row says
    so, and a barcode or a label photo makes them exact.

All rows are foods with source 'custom' keyed "venue|name" — the same key a
food_create for that venue and name produces, so the user's own entry and the
seed's are one row. The seed only ever writes a row it wrote itself
(detail.seed) that the user hasn't corrected; anything else is left alone.

    python -m app.food_seed            # published + estimates (model calls)
    python -m app.food_seed --no-estimate
"""

import json
import sys
from datetime import datetime
from pathlib import Path

from . import db, food, food_estimate

DATA = Path(__file__).resolve().parent / "food_data"
CBTL_VENUE = "coffee-bean-tea-leaf"


def _key(venue: str, name: str) -> str:
    return f"{venue}|{food.norm_name(name)}"


def _ours(con, venue: str, name: str) -> tuple[bool, bool]:
    """(exists, writable): writable iff absent, or seeded and uncorrected."""
    r = con.execute("SELECT detail FROM foods WHERE source='custom' AND source_id=?",
                    (_key(venue, name),)).fetchone()
    if r is None:
        return False, True
    d = json.loads(r["detail"] or "{}")
    return True, bool(d.get("seed")) and not d.get("corrected_at")


PANDA_VENUE = "panda-express"
OZ_G = 28.3495
# Panda sections whose items are food worth filling micros for (not sauces,
# fortune cookies or fountain drinks).
PANDA_FOOD = {"Base", "Vegetables", "Chicken", "Chicken Breast", "Beef",
              "Seafood", "Appetizers", "Soup"}
# Filled by estimate when a source publishes macros only. Fiber, sugar and
# sodium are on every US label, so those stay official.
MICRO_FILL = ("potassium", "calcium", "iron", "magnesium", "zinc", "vit_a",
              "vit_c", "vit_d", "vit_b12", "folate", "omega3")


def panda_items() -> list[dict]:
    """food_data/panda_nutrition.txt: a dish-name line, then a line of
    portion + 11 numbers (Panda's page, nutrition view)."""
    out, section, name = [], None, None
    for line in (DATA / "panda_nutrition.txt").read_text().splitlines():
        if line.startswith("## "):
            section = line[3:].strip()
            continue
        if not line.strip() or line.startswith("#"):
            continue
        cols = [c for c in line.split("    ") if c.strip()]
        if len(cols) < 12:
            name = line.strip()
            continue
        portion, v = cols[0].strip(), [float(c) for c in cols[1:12]]
        clean = (name.replace("®", "").replace("*", "").replace("The Original ", "")
                 .strip())
        try:
            oz = float(portion)
        except ValueError:
            oz = None
        if section == "Fountain":
            clean, desc = f"{clean} ({portion} oz)", f"{portion} oz cup"
        elif section == "Base":
            desc = f"1 side ({portion} oz)"
        elif section in {"Vegetables", "Chicken", "Chicken Breast", "Beef", "Seafood"}:
            desc = f"1 entrée ({portion} oz)"
        else:
            desc = portion if oz is None else f"{portion} oz"
        out.append({
            "venue": PANDA_VENUE, "name": clean, "serving_desc": desc,
            "serving_g": round(oz * OZ_G, 1) if oz else None,
            "n": {"kcal": v[0], "fat": v[2], "sat_fat": v[3], "sodium": v[6],
                  "carbs": v[7], "fiber": v[8], "sugar": v[9], "protein": v[10]},
            "confidence": 1.0, "allergens": [],
            "origin": "Panda Express nutrition page (pandaexpress.com), 2026-10-14",
            "fill_micros": section in PANDA_FOOD,
        })
    return out


def published_items() -> list[dict]:
    out = list(panda_items())
    cb = json.loads((DATA / "coffee_bean.json").read_text())
    for it in cb["items"]:
        out.append({"venue": CBTL_VENUE, "name": it["name"], "n": it["nutrients"],
                    "serving_desc": it["serving_desc"], "confidence": 1.0,
                    "allergens": it.get("allergens") or [],
                    "origin": f"Coffee Bean nutrition PDF ({cb['source']})"})
    rd = json.loads((DATA / "usc_dietitian.json").read_text())
    for it in rd["items"]:
        out.append({"venue": it["venue"], "name": it["name"], "n": it["n"],
                    "serving_desc": it["serving_desc"], "confidence": 1.0,
                    "allergens": [],
                    "origin": f"USC Hospitality dietitian handout, emailed 2026-10-16"
                              f" ({rd['docs'][it['doc']]})"})
    menus = json.loads((DATA / "campus_menus.json").read_text())["venues"]
    for venue, v in menus.items():
        for it in v.get("published", []):
            out.append({"venue": venue, "name": it["name"], "n": it["n"],
                        "serving_desc": it.get("serving_desc", "1 serving"),
                        "confidence": v.get("confidence", 0.8), "allergens": [],
                        "origin": v["origin"]})
    return out


def estimate_items() -> list[dict]:
    """Items to estimate, with the guide's diet marks as tags — without them
    the vegan Beet Poke box came back with 32 g of protein."""
    data = json.loads((DATA / "campus_menus.json").read_text())
    vegan, veg = set(data.get("vegan", [])), set(data.get("vegetarian", []))
    out = []
    for venue, v in data["venues"].items():
        packaged = set(v.get("packaged", []))
        for name in v.get("estimate", []):
            tags = ["vegan"] if name in vegan else ["vegetarian"] if name in veg else []
            out.append({"venue": venue, "name": name, "packaged": name in packaged,
                        "tags": tags})
    return out


def seed_published() -> int:
    """Load published rows; drop seeded rows whose item left the data (a
    rebuilt file renamed or removed it) unless corrected or ever logged."""
    n = 0
    items = published_items()
    keep = {_key(it["venue"], it["name"]) for it in items}
    keep |= {_key(it["venue"], it["name"]) for it in estimate_items()}
    with db.session() as con, con:
        for r in con.execute("SELECT id, source_id, detail FROM foods WHERE source='custom'"
                             " AND id NOT IN (SELECT food_id FROM food_log)").fetchall():
            d = json.loads(r["detail"] or "{}")
            if d.get("seed") and not d.get("corrected_at") and r["source_id"] not in keep:
                con.execute("DELETE FROM foods WHERE id=?", (r["id"],))
        for it in items:
            exists, writable = _ours(con, it["venue"], it["name"])
            if not writable:
                continue
            nutrients, detail = dict(it["n"]), {"seed": True, "origin": it["origin"]}
            if it.get("fill_micros"):
                # Keep micros a previous fill_micros() estimated: the label
                # numbers are rewritten every pass, the estimate isn't redone.
                prev = con.execute("SELECT nutrients, detail FROM foods WHERE"
                                   " source='custom' AND source_id=?",
                                   (_key(it["venue"], it["name"]),)).fetchone()
                pd = json.loads(prev["detail"] or "{}") if prev else {}
                if pd.get("micros"):
                    pn = json.loads(prev["nutrients"] or "{}")
                    nutrients.update({k: pn[k] for k in MICRO_FILL if k in pn})
                    detail["micros"] = pd["micros"]
                else:
                    detail["micros"] = None    # pending: fill_micros() picks it up
            food.upsert_food(
                con, name=it["name"], source="custom",
                source_id=_key(it["venue"], it["name"]), nutrients=nutrients,
                serving_desc=it["serving_desc"], serving_g=it.get("serving_g"),
                venue=it["venue"], confidence=it["confidence"],
                allergens=it["allergens"], detail=detail)
            n += 0 if exists else 1
    return n


def fill_micros(limit: int = 60) -> int:
    """Estimate vitamins and minerals for seeded foods whose source gives
    macros only (Panda). The dish is decomposed like a dining-hall item; only
    MICRO_FILL is taken, scaled from the estimate's portion to the label's
    serving. Label numbers are never touched."""
    cutoff = (datetime.now() - food_estimate.RETRY_AFTER).isoformat(timespec="seconds")
    with db.session() as con:
        rows = [dict(r) for r in con.execute(
            "SELECT * FROM foods WHERE source='custom'"
            " AND json_extract(detail, '$.seed') AND json_type(detail, '$.micros') = 'null'")]
        held = {r["item_key"] for r in con.execute(
            "SELECT item_key FROM food_estimates WHERE item_key LIKE 'micro|%'"
            " AND (attempts >= ? OR attempted_at >= ?)",
            (food_estimate.MAX_ATTEMPTS, cutoff))}
    rows = [r for r in rows if f"micro|{r['source_id']}" not in held][:limit]
    done = 0
    for i in range(0, len(rows), food_estimate.BATCH):
        batch = rows[i:i + food_estimate.BATCH]
        results = food_estimate.estimate_dishes([
            {"key": r["source_id"], "item": f"{r['name']} ({r['serving_desc']})",
             "where": (r["venue"] or "").replace("-", " "), "tags": []} for r in batch])
        with db.session() as con, con:
            for r in batch:
                res = results[r["source_id"]]
                if not res["ok"]:
                    food_estimate.record_failure(con, f"micro|{r['source_id']}",
                                                  res["error"], final=res["final"])
                    continue
                scale = (r["serving_g"] / res["serving_g"]
                         if r["serving_g"] and res["serving_g"] else 1.0)
                n = json.loads(r["nutrients"])
                n.update({k: round(v * scale, 3) for k, v in res["nutrients"].items()
                          if k in MICRO_FILL})
                d = json.loads(r["detail"])
                d["micros"] = "estimated from an SR28 decomposition of the dish"
                d["micro_components"] = res["detail"]["components"]
                con.execute("UPDATE foods SET nutrients=?, detail=?, updated_at=? WHERE id=?",
                            (json.dumps(n), json.dumps(d),
                             datetime.now().isoformat(timespec="seconds"), r["id"]))
                done += 1
    return done


def seed_estimates(limit: int = 200) -> int:
    """Estimate seed items with no row yet. One estimate per distinct name,
    copied to every venue that sells it (the Positive Foods bowls are at
    three cafés)."""
    # Failures share food_estimates (and its retry rules) with the menus,
    # under a "seed|" key, so a name the model can't do isn't re-asked every
    # poller cycle.
    cutoff = (datetime.now() - food_estimate.RETRY_AFTER).isoformat(timespec="seconds")
    with db.session() as con:
        todo = [it for it in estimate_items() if not _ours(con, it["venue"], it["name"])[0]]
        held = {r["item_key"] for r in con.execute(
            "SELECT item_key FROM food_estimates WHERE item_key LIKE 'seed|%'"
            " AND (attempts >= ? OR attempted_at >= ?)",
            (food_estimate.MAX_ATTEMPTS, cutoff))}
    by_name: dict[str, list[dict]] = {}
    for it in todo:
        k = food.norm_name(it["name"])
        if f"seed|{k}" not in held:
            by_name.setdefault(k, []).append(it)
    names = list(by_name)[:limit]
    written = 0
    for i in range(0, len(names), food_estimate.BATCH):
        batch = names[i:i + food_estimate.BATCH]
        results = food_estimate.estimate_dishes([
            {"key": k, "item": by_name[k][0]["name"],
             "where": by_name[k][0]["venue"].replace("-", " "),
             "tags": by_name[k][0]["tags"]}
            for k in batch])
        with db.session() as con, con:
            for k in batch:
                r = results[k]
                if not r["ok"]:
                    food_estimate.record_failure(con, f"seed|{k}", r["error"],
                                                  final=r["final"])
                    continue
                for it in by_name[k]:
                    detail = {**r["detail"], "seed": True,
                              "origin": "estimated from the item name (USC Balanced Dining guide)"}
                    if it["packaged"]:
                        detail["packaged"] = ("grab-and-go with a nutrition label: scan the"
                                              " barcode or photograph the label to make it exact")
                    food.upsert_food(
                        con, name=it["name"], source="custom",
                        source_id=_key(it["venue"], it["name"]),
                        nutrients=r["nutrients"], serving_desc=r["serving_desc"],
                        serving_g=r["serving_g"], venue=it["venue"],
                        confidence=r["confidence"], detail=detail)
                    written += 1
                con.execute("DELETE FROM food_estimates WHERE item_key=?", (f"seed|{k}",))
    return written


def ensure(estimate: bool = True) -> dict:
    """Idempotent; cheap when everything is already there. Called from the
    usc_dining poller refresh."""
    out = {"published": seed_published()}
    if estimate:
        # A usage limit isn't this refresh failing — the menus above landed.
        # Say so and let a later cycle estimate.
        try:
            out["estimated"] = seed_estimates()
            out["micros"] = fill_micros()
        except food_estimate.agent.AgentError as e:
            out["estimated"] = f"held: {e}"
    return out


if __name__ == "__main__":
    with food_estimate.agent.interactive():   # a deliberate run, not the poller
        print(ensure(estimate="--no-estimate" not in sys.argv))
