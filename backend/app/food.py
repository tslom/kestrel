"""§25 food tracker — what was eaten, against what a lean bulk needs, and what
on campus would close the gap (FOOD_PLAN.md is the spec).

app.db is truth for every row here: `foods` (the reusable thing you eat),
`food_log` (one row per thing eaten), `food_profile` / `food_targets` (the
inputs and any hand-set targets), `food_days` (a day closed by the user),
`weigh_ins`. The USC menus and venues are the usc_dining provider's derived
cache; SR28 is a read-only reference file checked into the repo.

foods.source is one of: sr28 (USDA reference, per 100 g), usc_estimate (a
dining-hall item, app/food_estimate.py), custom (a venue item or home food,
typed or from the model's knowledge), off (a barcode via Open Food Facts —
added after the migration's comment was written, which can't be edited).

Numbers carry their provenance. A food's nutrients are per serving; a nutrient
it doesn't have is absent, never 0, and day totals say how many entries were
missing it — "you got no vitamin D" and "nobody knows" are different facts.

Targets follow the decision of 2026-10-14: protein is fixed (g/kg every day),
energy is maintenance on a rest day and maintenance + the session's burn + a
surplus on a training day — a surplus is only built with when there's training
to build with. A training day is the Exercise habit checked (Fitbit's AZM
auto-checks it), or, before that's happened, a gym-shaped event on the
calendar.
"""

import gzip
import json
import re
import threading
from datetime import date, datetime, timedelta
from itertools import combinations
from pathlib import Path

from . import db

SR28_PATH = Path(__file__).resolve().parent / "food_data" / "sr28.json.gz"

# key -> (label, unit). Order is display order. Per serving everywhere.
NUTRIENTS = {
    "kcal": ("Energy", "kcal"),
    "protein": ("Protein", "g"),
    "carbs": ("Carbs", "g"),
    "fat": ("Fat", "g"),
    "sat_fat": ("Saturated fat", "g"),
    "fiber": ("Fiber", "g"),
    "sugar": ("Sugar", "g"),
    "sodium": ("Sodium", "mg"),
    "potassium": ("Potassium", "mg"),
    "calcium": ("Calcium", "mg"),
    "iron": ("Iron", "mg"),
    "magnesium": ("Magnesium", "mg"),
    "zinc": ("Zinc", "mg"),
    "vit_a": ("Vitamin A", "µg"),
    "vit_c": ("Vitamin C", "mg"),
    "vit_d": ("Vitamin D", "µg"),
    "vit_b12": ("Vitamin B12", "µg"),
    "folate": ("Folate", "µg"),
    "omega3": ("Omega-3", "g"),
}
# The ones scored as "fill this gap". Sugar is shown, never targeted.
MICROS = ("fiber", "potassium", "calcium", "iron", "magnesium", "zinc", "vit_a",
          "vit_c", "vit_d", "vit_b12", "folate", "omega3")
CAPS = ("sodium", "sat_fat")

# Adult (19–30) DRIs — RDA where one exists, else AI. Fiber is 14 g per
# 1000 kcal and computed from the energy target instead.
DRI = {
    "male": {"potassium": 3400, "calcium": 1000, "iron": 8, "magnesium": 400,
             "zinc": 11, "vit_a": 900, "vit_c": 90, "vit_d": 15,
             "vit_b12": 2.4, "folate": 400, "omega3": 1.6},
    "female": {"potassium": 2600, "calcium": 1000, "iron": 18, "magnesium": 310,
               "zinc": 8, "vit_a": 700, "vit_c": 75, "vit_d": 15,
               "vit_b12": 2.4, "folate": 400, "omega3": 1.1},
}
SODIUM_CAP_MG = 2300
ACTIVITY = {"sedentary": 1.2, "light": 1.375, "moderate": 1.55, "very": 1.725}
GOAL_SURPLUS = {"lean_bulk": 300, "maintain": 0, "cut": -400}
PROTEIN_G_PER_KG = 1.8
MEALS = ("breakfast", "lunch", "dinner", "snack")
PAID_WITH = ("dining_dollars", "card", "cash")
HABIT = "Log food"
PROTEIN_HABIT = "Hit protein"

PROFILE_KEYS = {
    "sex": str, "birth_year": int, "height_cm": float, "activity": str,
    "goal": str, "training_kcal": float, "dd_balance": float,
    "dd_balance_at": str, "semester_end": str, "door_price": float,
    "maint_adj": float, "maint_adj_at": str,
}
PROFILE_DEFAULTS = {"activity": "light", "goal": "lean_bulk",
                    "training_kcal": 300, "maint_adj": 0}

_GYM = re.compile(r"\b(gym|lift|lifting|workout|work out|training|leg day|"
                  r"push day|pull day|run|basketball|soccer|swim)\b", re.I)


class FoodError(ValueError):
    """A refusal phrased for the reader (bad id, ambiguous name)."""


# ---------- how far to trust a number ----------
#
# A guessed number shows as a range, not false precision. The half-width is
# a fraction of the value, from confidence: 0.8 → ±14%, 0.6 → ±28%, 0.4 →
# ±42%, capped at ±45%. A published or corrected number (≥ 0.95) has none.
# An entry's spread combines the food's (is the recipe right?) with the
# portion's (was it that much?) in quadrature, since the two errors are
# independent; a day's combines its entries the same way.

PORTION_GUESSED = 0.6     # amount read off a photo or a vague line
PORTION_ANSWERED = 0.9    # the one portion question was answered


def spread(conf: float | None) -> float:
    if conf is None or conf >= 0.95:
        return 0.0
    return min(0.45, round((1 - conf) * 0.7, 3))


def entry_spread(food_conf: float | None, portion_conf: float | None) -> float:
    return round((spread(food_conf) ** 2 + spread(portion_conf) ** 2) ** 0.5, 3)


def estimate_range(v: float, half: float) -> list[int]:
    """[lo, hi] rounded to 10 (to 1 under 50, for protein grams)."""
    step = 10 if v >= 50 else 1
    return [int(round(v * (1 - half) / step) * step),
            int(round(v * (1 + half) / step) * step)]


def trust(s: float) -> str:
    return "exact" if s == 0 else "close" if s < 0.2 else "rough"


# Dishes whose size varies a lot from one serving to the next — the only ones
# worth a portion question. A can, a bar, a cookie or a slice is what it is.
_AMORPHOUS = re.compile(
    r"\b(bowls?|soups?|stew|chili|curry|korma|masala|burritos?|plates?|platter|"
    r"combo|pasta|noodles?|lo mein|chow mein|rice|salad|casserole|stir[ -]?fry|"
    r"mac|poke|acai|açaí|mash(ed)?|enchiladas?|lasagna|fries|scoops?|nachos|"
    r"oatmeal|cereal|alfredo|ramen|pho)\b", re.I)
_SINGLE = re.compile(
    r"^\s*(1|one)\s+(can|bottle|bar|cookie|slice|piece|egg|packet|pack|bag|"
    r"cup \(\d+ ?(fl )?oz\)|\d+ ?(fl )?oz|sandwich|muffin|bagel|banana|apple|"
    r"donut|croissant|taco|wrap|burger|slider|egg roll|entrée)\b", re.I)
PORTION_OPTIONS = [("½", 0.5), ("¾", 0.75), ("as logged", 1.0), ("1½×", 1.5),
                   ("2×", 2.0)]


def portion_question(food: dict, servings: float,
                     portion_conf: float | None) -> dict | None:
    """One short question, only when the amount was guessed, the guess is
    loose, and the dish is one whose size varies. Never for an obvious single
    item."""
    if portion_conf is None or portion_conf >= 0.75:
        return None
    if entry_spread(food.get("confidence"), portion_conf) < 0.25:
        return None
    if not _AMORPHOUS.search(food["name"]) and not _AMORPHOUS.search(
            food.get("serving_desc") or ""):
        return None
    if _SINGLE.match(food.get("serving_desc") or "") and not _AMORPHOUS.search(food["name"]):
        return None
    desc = food.get("serving_desc") or "1 serving"
    return {"q": f"How much {food['name']}? Logged as "
                 + (desc if servings == 1 else f"{servings:g} × {desc}") + ".",
            "options": [{"label": lab, "x": x} for lab, x in PORTION_OPTIONS]}


# ---------- SR28 reference ----------

_sr = None
_sr_lock = threading.Lock()


def _norm_tokens(s: str) -> list[str]:
    out = []
    for t in re.split(r"[^a-z0-9]+", s.lower()):
        if not t:
            continue
        if len(t) > 3 and t.endswith("es") and not t.endswith("ses"):
            t = t[:-2] if t.endswith(("ches", "shes", "toes")) else t[:-1]
        elif len(t) > 3 and t.endswith("s") and not t.endswith("ss"):
            t = t[:-1]
        out.append(t)
    return out


def sr28() -> dict:
    """{'list': [...], 'by_id': {...}} — loaded once, ~8.8k foods."""
    global _sr
    if _sr is None:
        with _sr_lock:
            if _sr is None:
                with gzip.open(SR28_PATH, "rt", encoding="utf-8") as f:
                    foods = json.load(f)
                for it in foods:
                    head, _, rest = it["name"].partition(",")
                    it["_head"] = set(_norm_tokens(head))
                    it["_all"] = set(_norm_tokens(it["name"]))
                _sr = {"list": foods, "by_id": {f["id"]: f for f in foods}}
    return _sr


# Groups that are almost never what a person means by a bare word.
_SR_DEMOTE = {"Baby Foods": 3.0, "American Indian/Alaska Native Foods": 2.0,
              "Restaurant Foods": 0.5}


def _match_score(q: list[str], head: set[str], all_: set[str],
                 name: str) -> float | None:
    """All words matching is the strong case; a missing word costs 2.5 rather
    than disqualifying, so "collard greens" still finds "Collards, cooked"
    — but at least half the words have to land."""
    score = 0.0
    hit = 0
    for t in q:
        if t in head:
            score += 3
        elif t in all_:
            score += 2
        elif len(t) > 2 and any(w.startswith(t) for w in all_):
            score += 1
        else:
            score -= 2.5
            continue
        hit += 1
    if hit == 0 or hit * 2 < len(q):
        return None
    return score - 0.015 * len(name)


# Forms a bare word doesn't mean: "egg" is not dried yolk, "banana" is not
# banana powder. Each costs a point unless the query asked for it.
_SR_FORMS = {"dried", "dehydrated", "powder", "flour", "canned", "frozen",
             "unprepared", "dry", "mix", "concentrate", "imitation", "babyfood",
             "sheep", "goat", "buffalo", "low", "reduced", "free", "substitute",
             "fluid", "sodium", "meatless", "deli", "bits", "paste", "oil",
             "skin", "leaves", "stalks", "yolk", "beverage", "homemade",
             "relish", "rolls", "cookies", "bread", "fried"}
_BRAND = re.compile(r"[A-Z]{3,}")     # KEEBLER, McDONALD'S


# Words SR28 spells its own way.
_SR_SYNONYMS = {"oatmeal": "oats", "ketchup": "catsup", "fries": "french fried",
                "soda": "carbonated beverage", "chickpea": "chickpeas garbanzo",
                "garbanzo": "chickpeas garbanzo", "shrimp": "crustaceans shrimp",
                "scallion": "onions spring", "zucchini": "squash zucchini"}


def search_sr28(query: str, limit: int = 10) -> list[dict]:
    q = _norm_tokens(" ".join(_SR_SYNONYMS.get(t, t) for t in _norm_tokens(query)))
    if not q:
        return []
    qs = set(q)
    hits = []
    for it in sr28()["list"]:
        s = _match_score(q, it["_head"], it["_all"], it["name"])
        if s is None:
            continue
        s -= _SR_DEMOTE.get(it["group"], 0)
        s -= 0.015 * len(it["name"])          # on top of _match_score's: shorter is plainer
        if it["_head"] <= qs:
            s += 3                            # "Potatoes, …" for "potato cooked", not "Sweet potato"
        if _BRAND.search(it["name"]):
            s -= 2                            # KEEBLER, SILK: a product, not the food
        s -= len((it["_all"] - qs) & _SR_FORMS)
        hits.append((s, it))
    hits.sort(key=lambda h: -h[0])
    return [_sr_public(it) for _, it in hits[:limit]]


def _sr_public(it: dict) -> dict:
    return {"id": it["id"], "name": it["name"], "group": it["group"],
            "serving_desc": "100 g", "serving_g": 100, "nutrients": it["n"],
            "portions": it["portions"], "source": "sr28", "confidence": 1.0}


def scale(nutrients: dict, factor: float) -> dict:
    return {k: round(v * factor, 3) for k, v in nutrients.items()
            if v is not None}


def add_nutrients(parts: list[dict]) -> dict:
    """Sum nutrient dicts. A key present in some parts but not all is summed
    over the parts that have it — callers that care track the gap."""
    out: dict[str, float] = {}
    for p in parts:
        for k, v in p.items():
            if v is not None:
                out[k] = round(out.get(k, 0) + v, 3)
    return out


# ---------- foods ----------

def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _food_row(r) -> dict:
    d = dict(r)
    d["nutrients"] = json.loads(d["nutrients"] or "{}")
    d["detail"] = json.loads(d["detail"]) if d.get("detail") else None
    d["allergens"] = json.loads(d["allergens"]) if d.get("allergens") else []
    d["prefs"] = json.loads(d["prefs"]) if d.get("prefs") else []
    d["ref"] = f"f{d['id']}"
    return d


def norm_name(name: str) -> str:
    return " ".join(_norm_tokens(name))


def upsert_food(con, *, name: str, source: str, source_id: str | None,
                nutrients: dict, serving_desc: str = "1 serving",
                serving_g: float | None = None, venue: str | None = None,
                confidence: float = 1.0, detail: dict | None = None,
                allergens: list | None = None, prefs: list | None = None) -> int:
    """Insert or update by (source, source_id). Returns the food id."""
    now = _now()
    clean = {k: round(float(v), 3) for k, v in nutrients.items()
             if k in NUTRIENTS and v is not None}
    row = con.execute("SELECT id FROM foods WHERE source=? AND source_id=?",
                      (source, source_id)).fetchone() if source_id else None
    vals = (name.strip(), venue, serving_desc, serving_g, json.dumps(clean),
            source, source_id, confidence,
            json.dumps(detail) if detail is not None else None,
            json.dumps(allergens or []), json.dumps(prefs or []))
    if row:
        con.execute(
            "UPDATE foods SET name=?, venue=?, serving_desc=?, serving_g=?,"
            " nutrients=?, source=?, source_id=?, confidence=?, detail=?,"
            " allergens=?, prefs=?, updated_at=? WHERE id=?",
            (*vals, now, row["id"]))
        return row["id"]
    cur = con.execute(
        "INSERT INTO foods (name, venue, serving_desc, serving_g, nutrients,"
        " source, source_id, confidence, detail, allergens, prefs, created_at,"
        " updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", (*vals, now, now))
    return cur.lastrowid


def get_food(con, food_id: int) -> dict | None:
    r = con.execute("SELECT * FROM foods WHERE id=?", (food_id,)).fetchone()
    return _food_row(r) if r else None


def create_custom(name: str, nutrients: dict, *, venue: str | None = None,
                  serving_desc: str = "1 serving", serving_g: float | None = None,
                  confidence: float = 0.8, note: str | None = None) -> dict:
    """A food you buy or make that no database has: "Seeds — chicken caesar
    wrap". Keyed by venue + name, so logging it again finds this row."""
    if not name.strip():
        raise FoodError("a food needs a name")
    if "kcal" not in nutrients and "protein" not in nutrients:
        raise FoodError("give at least kcal or protein — a food with no numbers"
                        " can't count toward anything")
    with db.session() as con, con:
        # A food corrected once stays right: a later estimate under the same
        # name (a re-texted photo, the model re-creating it) must not write
        # over a correction or a better-sourced row. Equal confidence still
        # updates, so retyping the same kind of number is an edit, and the
        # user's own typed numbers (confidence 1) always land.
        old = con.execute("SELECT * FROM foods WHERE source='custom' AND source_id=?",
                          (f"{venue or ''}|{norm_name(name)}",)).fetchone()
        if old is not None and confidence < 1:
            of = _food_row(old)
            if (of["detail"] or {}).get("corrected_at") or of["confidence"] > confidence:
                return {**of, "kept": True}
        fid = upsert_food(
            con, name=name, source="custom",
            source_id=f"{venue or ''}|{norm_name(name)}", nutrients=nutrients,
            serving_desc=serving_desc, serving_g=serving_g, venue=venue,
            confidence=confidence, detail={"note": note} if note else None)
        return get_food(con, fid)


def from_barcode(code: str) -> dict:
    """A packaged food by its barcode: app.db first, then Open Food Facts."""
    from .providers import openfoodfacts
    digits = "".join(ch for ch in str(code) if ch.isdigit())
    with db.session() as con:
        r = con.execute("SELECT * FROM foods WHERE source='off' AND source_id=?",
                        (digits,)).fetchone()
        if r:
            return _food_row(r)
    try:
        hit = openfoodfacts.lookup(digits)
    except ValueError as e:
        raise FoodError(str(e)) from None
    if hit is None:
        raise FoodError(f"Open Food Facts doesn't know {digits} (or has no"
                        " calories for it) — read the label into food_create")
    with db.session() as con, con:
        fid = upsert_food(con, name=hit["name"], source="off", source_id=hit["code"],
                          nutrients=hit["nutrients"], serving_desc=hit["serving_desc"],
                          serving_g=hit["serving_g"], confidence=0.95,
                          detail={"barcode": hit["code"]})
        return get_food(con, fid)


def fix_food(food_id: int, *, nutrients: dict | None = None,
             serving_desc: str | None = None, serving_g: float | None = None,
             name: str | None = None) -> dict:
    """A correction. Merges the given nutrients over the old ones and marks
    the food as known (confidence 1) — for a dining-hall estimate this fixes
    every future day the item appears, which is the point."""
    with db.session() as con, con:
        f = get_food(con, food_id)
        if f is None:
            raise FoodError(f"no food f{food_id}")
        given = {k: float(v) for k, v in (nutrients or {}).items()
                 if k in NUTRIENTS and v is not None}
        merged = reconcile(f["nutrients"], given)
        before = {k: f["nutrients"][k] for k in ("kcal", "protein", "carbs", "fat")
                  if k in f["nutrients"]}
        detail = {**(f["detail"] or {}), "corrected_at": _now()}
        # The history is how "is the estimator biased?" gets answered later.
        detail["corrections"] = (detail.get("corrections") or [])[-9:] + [{
            "at": _now(), "before": before, "was_conf": f["confidence"],
            "given": given, "serving_desc": serving_desc, "serving_g": serving_g}]
        con.execute(
            "UPDATE foods SET nutrients=?, serving_desc=?, serving_g=?, name=?,"
            " confidence=1, detail=?, updated_at=? WHERE id=?",
            (json.dumps(merged), serving_desc or f["serving_desc"],
             serving_g if serving_g is not None else f["serving_g"],
             (name or f["name"]).strip(), json.dumps(detail), _now(), food_id))
        return get_food(con, food_id)


ENERGY = {"protein": 4, "carbs": 4, "fat": 9}


def reconcile(old: dict, given: dict) -> dict:
    """The old nutrients with a correction laid over them, kept consistent.

    A correction usually names kcal (and maybe protein) and nothing else. Laid
    over the old row as-is, that left "230 kcal" next to 24 g fat and 17 g
    carbs, which is 400 kcal of macros (the BBQ chicken, 10-15). So when kcal
    is given, the unstated macros share what's left after the stated ones, in
    their old proportions, and everything else that scales with the amount
    (sugar, sat fat, fiber, sodium, micros) moves by the kcal ratio — most
    corrections are "it was more/less food", which moves everything."""
    out = {**old, **given}
    if "kcal" not in given or not old.get("kcal"):
        return out
    ratio = given["kcal"] / old["kcal"]
    free = [k for k in ENERGY if k not in given and old.get(k)]
    if free:
        left = given["kcal"] - sum(ENERGY[k] * given[k] for k in ENERGY if k in given)
        old_e = sum(ENERGY[k] * old[k] for k in free)
        if left > 0 and old_e > 0:
            for k in free:
                out[k] = round(old[k] * left / old_e, 2)
        elif left <= 0:
            for k in free:
                out[k] = 0.0
    tied = {"sat_fat": "fat", "sugar": "carbs"}   # a part can't outgrow its whole
    for k, v in old.items():
        if k in given or k in ENERGY or k == "kcal":
            continue
        parent = tied.get(k)
        r = (out[parent] / old[parent] if parent and old.get(parent) else ratio)
        out[k] = round(v * r, 3)
    return out


def _materialize_sr(con, sr_id: str) -> int:
    it = sr28()["by_id"].get(sr_id)
    if it is None:
        raise FoodError(f"no SR28 food {sr_id}")
    return upsert_food(con, name=it["name"], source="sr28", source_id=sr_id,
                       nutrients=it["n"], serving_desc="100 g", serving_g=100,
                       detail={"portions": it["portions"], "group": it["group"]})


def resolve_ref(con, ref: str) -> int:
    """'f12' / '12' → foods.id; 'sr:01001' → materialized foods.id."""
    ref = str(ref).strip()
    if ref.startswith("sr:"):
        return _materialize_sr(con, ref)
    m = re.fullmatch(r"[fF]?(\d+)", ref)
    if not m:
        raise FoodError(f"{ref!r} isn't a food id — use one from food_search"
                        " (f12, or sr:01001)")
    fid = int(m.group(1))
    if not con.execute("SELECT 1 FROM foods WHERE id=?", (fid,)).fetchone():
        raise FoodError(f"no food f{fid}")
    return fid


def search(query: str, *, venue: str | None = None, day: str | None = None,
           limit: int = 12) -> list[dict]:
    """Everything a name could mean, best first: foods already in app.db
    (your venue foods, past logs, dining-hall estimates) and then SR28.
    Dining-hall items on today's menu rank above the same name off-menu."""
    day = day or db.day_key()
    q = _norm_tokens(query)
    if not q:
        return []
    with db.session() as con:
        on_menu = {norm_name(r["item"]) for r in con.execute(
            "SELECT DISTINCT item FROM usc_menu_items WHERE date=?", (day,))}
        uses = {r["food_id"]: r["n"] for r in con.execute(
            "SELECT food_id, COUNT(*) n FROM food_log GROUP BY food_id")}
        mine = []
        for r in con.execute("SELECT * FROM foods"):
            f = _food_row(r)
            toks = set(_norm_tokens(f["name"]))
            s = _match_score(q, toks, toks | set(_norm_tokens(f["venue"] or "")),
                             f["name"])
            if s is None:
                continue
            s += 4 + min(uses.get(f["id"], 0), 10) * 0.3
            if venue and f["venue"] == venue:
                s += 3
            if f["source"] == "usc_estimate" and f["source_id"] in on_menu:
                s += 2
            mine.append((s, f))
    mine.sort(key=lambda h: -h[0])
    out = [{**f, "uses": uses.get(f["id"], 0)} for _, f in mine[:limit]]
    seen = {f["source_id"] for f in out if f["source"] == "sr28"}
    out += [s for s in search_sr28(query, limit) if s["id"] not in seen]
    return out[:limit + 6]


# ---------- log ----------

def default_meal(at: datetime | None = None) -> str:
    """Late night (22:00–03:59, still the previous day-key) is a snack."""
    h = (at or datetime.now()).hour
    if 4 <= h < 11:
        return "breakfast"
    if 11 <= h < 16:
        return "lunch"
    if 16 <= h < 22:
        return "dinner"
    return "snack"


def _servings_for(food: dict, servings: float | None, grams: float | None,
                  portion: str | None) -> tuple[float, str | None]:
    if grams is not None:
        if not food.get("serving_g"):
            raise FoodError(f"{food['name']} has no gram weight — log it in"
                            f" servings ({food['serving_desc']})")
        return grams / food["serving_g"], f"{grams:g} g"
    if portion:
        portions = (food.get("detail") or {}).get("portions") or []
        want = portion.lower().strip()
        hit = next((p for p in portions if p[0].lower() == want), None) or \
            next((p for p in portions if want in p[0].lower()), None)
        if hit is None:
            have = ", ".join(p[0] for p in portions[:8]) or "none"
            raise FoodError(f"no portion {portion!r} for {food['name']}; it has:"
                            f" {have}")
        return hit[1] / (food.get("serving_g") or 100) * (servings or 1), \
            f"{servings or 1:g} × {hit[0]}"
    return float(servings if servings is not None else 1), None


def log(food: str | int, *, servings: float | None = None,
        grams: float | None = None, portion: str | None = None,
        meal: str | None = None, venue: str | None = None,
        price: float | None = None, paid_with: str | None = None,
        day: str | None = None, note: str | None = None,
        source: str = "ui", amount_guessed: bool = False) -> dict:
    """Log one food. With no amount given, a food logged before comes back at
    the amount it was last logged at (after any fix to that entry) — your
    usual portion, learned. `amount_guessed` marks an amount read off a photo
    or a vague line: the entry's range widens, and for a dish whose size
    varies it carries one portion question (`ask`)."""
    meal = (meal or default_meal()).lower()
    if meal not in MEALS:
        raise FoodError(f"meal must be one of {', '.join(MEALS)}")
    if paid_with and paid_with not in PAID_WITH:
        raise FoodError(f"paid_with must be one of {', '.join(PAID_WITH)}")
    with db.session() as con, con:
        fid = resolve_ref(con, str(food))
        f = get_food(con, fid)
        learned, pconf = None, PORTION_GUESSED if amount_guessed else None
        if servings is None and grams is None and not portion:
            last = last_servings(con, fid)
            if last:
                # A guess repeated is still a guess.
                learned, pconf = last["servings"], pconf or last["portion_conf"]
        sv, how = _servings_for(f, learned if learned else servings, grams, portion)
        if sv <= 0:
            raise FoodError("servings must be positive")
        venue = venue or f["venue"]
        if paid_with is None and price is not None:
            paid_with = "dining_dollars"   # Trojan plan: DD unless said otherwise
        ask = portion_question(f, sv, pconf)
        cur = con.execute(
            "INSERT INTO food_log (day, eaten_at, meal, food_id, servings, venue,"
            " paid_with, price, note, source, portion_conf, ask)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (day or db.day_key(), _now(), meal, fid, round(sv, 4), venue,
             paid_with, price, note or how, source, pconf,
             json.dumps(ask) if ask else None))
        e = _entry(con, cur.lastrowid)
        if learned:
            e["servings_from"] = "last time"
        return e


def last_servings(con, food_id: int) -> dict | None:
    """{servings, portion_conf} this food was last logged at, if ever."""
    r = con.execute("SELECT servings, portion_conf FROM food_log WHERE food_id=?"
                    " ORDER BY day DESC, id DESC LIMIT 1", (food_id,)).fetchone()
    return dict(r) if r else None


def _entry(con, log_id: int) -> dict | None:
    r = con.execute(
        "SELECT l.*, f.name, f.serving_desc, f.serving_g, f.nutrients, f.source"
        " food_source, f.confidence, f.venue food_venue FROM food_log l"
        " JOIN foods f ON f.id=l.food_id WHERE l.id=?", (log_id,)).fetchone()
    if r is None:
        return None
    d = dict(r)
    d["nutrients"] = scale(json.loads(d["nutrients"] or "{}"), d["servings"])
    d["ask"] = json.loads(d["ask"]) if d.get("ask") else None
    s = entry_spread(d["confidence"], d.get("portion_conf"))
    d["spread"], d["trust"] = s, trust(s)
    d["range"] = ({k: estimate_range(d["nutrients"][k], s) for k in ("kcal", "protein")
                   if k in d["nutrients"]} if s >= 0.05 else None)
    return d


def update_entry(log_id: int, **fields) -> dict:
    """A servings change is the user stating the amount: the entry's portion
    is known from then on, any pending question is moot, and the next log of
    this food starts from the new amount."""
    allowed = {"servings", "meal", "price", "paid_with", "note", "day"}
    sets = {k: v for k, v in fields.items() if k in allowed and v is not None}
    if "meal" in sets and sets["meal"] not in MEALS:
        raise FoodError(f"meal must be one of {', '.join(MEALS)}")
    if "servings" in sets and float(sets["servings"]) <= 0:
        raise FoodError("servings must be positive — delete the entry instead")
    with db.session() as con, con:
        if not con.execute("SELECT 1 FROM food_log WHERE id=?", (log_id,)).fetchone():
            raise FoodError(f"no log entry {log_id}")
        if sets:
            con.execute(
                f"UPDATE food_log SET {', '.join(f'{k}=?' for k in sets)} WHERE id=?",
                (*sets.values(), log_id))
        if "servings" in sets:
            con.execute("UPDATE food_log SET portion_conf=NULL, ask=NULL WHERE id=?",
                        (log_id,))
        return _entry(con, log_id)


def answer_portion(log_id: int, x: float | None) -> dict:
    """The portion question's answer: × the logged amount (1 = as logged).
    None dismisses it, leaving the guess and its range as they were."""
    with db.session() as con, con:
        e = _entry(con, log_id)
        if e is None:
            raise FoodError(f"no log entry {log_id}")
        if x is None:
            con.execute("UPDATE food_log SET ask=NULL WHERE id=?", (log_id,))
        else:
            if not 0 < float(x) <= 4:
                raise FoodError("the answer is a multiple of the logged amount, 0–4")
            con.execute("UPDATE food_log SET servings=?, portion_conf=?, ask=NULL"
                        " WHERE id=?", (round(e["servings"] * float(x), 4),
                                        PORTION_ANSWERED, log_id))
        return _entry(con, log_id)


def delete_entry(log_id: int) -> dict:
    with db.session() as con, con:
        e = _entry(con, log_id)
        if e is None:
            raise FoodError(f"no log entry {log_id}")
        con.execute("DELETE FROM food_log WHERE id=?", (log_id,))
        return e


def restore_entry(entry: dict) -> dict:
    """Undo of a delete: the same row back, same id."""
    with db.session() as con, con:
        ask = entry.get("ask")
        con.execute(
            "INSERT INTO food_log (id, day, eaten_at, meal, food_id, servings,"
            " venue, paid_with, price, note, source, portion_conf, ask)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (*(entry[k] for k in ("id", "day", "eaten_at", "meal", "food_id",
                                  "servings", "venue", "paid_with", "price",
                                  "note", "source")),
             entry.get("portion_conf"),
             json.dumps(ask) if isinstance(ask, dict) else ask))
        return _entry(con, entry["id"])


def repeat(from_day: str, to_day: str | None = None,
           meal: str | None = None) -> list[dict]:
    """"Same breakfast as yesterday": copy a day's (or one meal's) entries."""
    with db.session() as con:
        rows = con.execute(
            "SELECT * FROM food_log WHERE day=?" + (" AND meal=?" if meal else "")
            + " ORDER BY id", (from_day, meal) if meal else (from_day,)).fetchall()
    if not rows:
        raise FoodError(f"nothing logged {'for ' + meal + ' ' if meal else ''}"
                        f"on {from_day}")
    return [log(r["food_id"], servings=r["servings"], meal=r["meal"],
                venue=r["venue"], price=r["price"], paid_with=r["paid_with"],
                day=to_day, source="ui") for r in rows]


def entries(con, day: str) -> list[dict]:
    ids = [r["id"] for r in con.execute(
        "SELECT id FROM food_log WHERE day=? ORDER BY eaten_at, id", (day,))]
    return [_entry(con, i) for i in ids]


def totals(items: list[dict]) -> tuple[dict, dict]:
    """(totals, missing) — missing[k] counts entries that don't know k."""
    tot = add_nutrients([e["nutrients"] for e in items])
    missing = {k: sum(1 for e in items if k not in e["nutrients"])
               for k in NUTRIENTS}
    return tot, {k: v for k, v in missing.items() if v}


def day_range(items: list[dict]) -> dict | None:
    """{kcal: [lo, hi], protein: [lo, hi], exact_share} for the day, or None
    when every entry is exact. Entry errors add in quadrature."""
    out = {}
    for k in ("kcal", "protein"):
        v = sum(e["nutrients"].get(k, 0) for e in items)
        half = sum((e["nutrients"].get(k, 0) * e.get("spread", 0)) ** 2
                   for e in items) ** 0.5
        if v and half / v >= 0.03:
            out[k] = estimate_range(v, half / v)
    if not out:
        return None
    kc = sum(e["nutrients"].get("kcal", 0) for e in items) or 1
    out["exact_share"] = round(sum(e["nutrients"].get("kcal", 0) for e in items
                                   if not e.get("spread")) / kc, 2)
    return out


def recents(limit: int = 8, meal: str | None = None, days: int = 45) -> list[dict]:
    """The foods you actually eat, for one-tap re-logging: ranked by how
    often (recent weeks count more), with this meal's regulars first, each
    with the amount it was last logged at."""
    meal = meal or default_meal()
    today = date.fromisoformat(db.day_key())
    since = (today - timedelta(days=days)).isoformat()
    score: dict[int, float] = {}
    with db.session() as con:
        rows = con.execute("SELECT food_id, day, meal FROM food_log WHERE day>=?",
                           (since,)).fetchall()
        for r in rows:
            age = (today - date.fromisoformat(r["day"])).days
            score[r["food_id"]] = score.get(r["food_id"], 0) + (
                (2 if r["meal"] == meal else 1) * (0.5 ** (age / 21)))
        out = []
        for fid in sorted(score, key=lambda i: -score[i])[:limit]:
            f = get_food(con, fid)
            last = last_servings(con, fid)
            n = scale(f["nutrients"], last["servings"])
            out.append({"ref": f["ref"], "id": fid, "name": f["name"],
                        "venue": f["venue"], "serving_desc": f["serving_desc"],
                        "servings": last["servings"], "confidence": f["confidence"],
                        "kcal": round(n.get("kcal", 0)),
                        "protein": round(n.get("protein", 0))})
    return out


def repeatable_meals(day: str | None = None) -> list[dict]:
    """Yesterday's meals, as "same lunch as yesterday" candidates."""
    y = (date.fromisoformat(day or db.day_key()) - timedelta(days=1)).isoformat()
    with db.session() as con:
        items = entries(con, y)
    out = []
    for m in MEALS:
        es = [e for e in items if e["meal"] == m]
        if es:
            out.append({"day": y, "meal": m, "names": [e["name"] for e in es],
                        "kcal": round(sum(e["nutrients"].get("kcal", 0) for e in es))})
    return out


def match_known(name: str, venue: str | None = None) -> dict | None:
    """A food already in app.db that this name surely means — corrected,
    published, or logged before — so a photo of the usual bowl logs the
    usual bowl instead of a fresh estimate. Exact normalized-name match only
    (at the venue when one is given); a near miss is a new food, not a
    guess."""
    key = norm_name(name)
    if not key:
        return None
    with db.session() as con:
        uses = {r["food_id"]: r["n"] for r in con.execute(
            "SELECT food_id, COUNT(*) n FROM food_log GROUP BY food_id")}
        best = None
        for r in con.execute("SELECT * FROM foods WHERE source IN"
                             " ('custom','usc_estimate','off')"):
            f = _food_row(r)
            if norm_name(f["name"]) != key:
                continue
            if venue and f["venue"] and f["venue"] != venue:
                continue
            known = ((f["detail"] or {}).get("corrected_at") or f["confidence"] >= 0.95
                     or uses.get(f["id"]))
            if not known:
                continue
            rank = (bool((f["detail"] or {}).get("corrected_at")), f["confidence"],
                    uses.get(f["id"], 0), bool(venue and f["venue"] == venue))
            if best is None or rank > best[0]:
                best = (rank, f)
    return best[1] if best else None


# ---------- profile & targets ----------

def get_profile(con=None) -> dict:
    with db.borrow(con) as c:
        raw = {r["key"]: r["value"] for r in c.execute("SELECT * FROM food_profile")}
    out = dict(PROFILE_DEFAULTS)
    for k, v in raw.items():
        typ = PROFILE_KEYS.get(k)
        if typ is None:
            continue
        try:
            out[k] = typ(v)
        except ValueError:
            pass
    return out


def set_profile(**fields) -> dict:
    bad = [k for k in fields if k not in PROFILE_KEYS]
    if bad:
        raise FoodError(f"unknown profile field(s): {', '.join(bad)}")
    if fields.get("sex") not in (None, "male", "female"):
        raise FoodError("sex is male or female (it picks the DRI table)")
    if fields.get("activity") not in (None, *ACTIVITY):
        raise FoodError(f"activity is one of {', '.join(ACTIVITY)}")
    if fields.get("goal") not in (None, *GOAL_SURPLUS):
        raise FoodError(f"goal is one of {', '.join(GOAL_SURPLUS)}")
    with db.session() as con, con:
        for k, v in fields.items():
            if v is None or v == "":
                con.execute("DELETE FROM food_profile WHERE key=?", (k,))
                continue
            PROFILE_KEYS[k](v)   # validates the type before storing
            con.execute("INSERT INTO food_profile (key, value) VALUES (?,?)"
                        " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                        (k, str(v)))
        if "dd_balance" in fields and "dd_balance_at" not in fields:
            con.execute("INSERT INTO food_profile (key, value) VALUES"
                        " ('dd_balance_at', ?) ON CONFLICT(key) DO UPDATE SET"
                        " value=excluded.value", (_now(),))
    return get_profile()


def weigh(kg: float | None = None, lb: float | None = None,
          day: str | None = None) -> dict:
    if kg is None and lb is None:
        raise FoodError("give kg or lb")
    kg = kg if kg is not None else lb * 0.45359237
    if not 30 <= kg <= 300:
        raise FoodError(f"{kg:.1f} kg doesn't look like a body weight")
    day = day or db.day_key()
    now = datetime.now()
    with db.session() as con, con:
        con.execute("INSERT INTO weigh_ins (day, kg, at) VALUES (?,?,?)"
                    " ON CONFLICT(day) DO UPDATE SET kg=excluded.kg, at=excluded.at",
                    (day, round(kg, 2), now.isoformat(timespec="seconds")))
        avg = trend_kg(con)
    lb = round(kg / 0.45359237, 1)
    # Weighing is the Weigh in habit's whole point, so it checks it — never
    # over a check or un-check the user made by hand.
    db.auto_checkin(WEIGH_HABIT, day, note=f"{lb} lb")
    out = {"day": day, "kg": round(kg, 2), "lb": lb,
           "avg7_kg": avg, "avg7_lb": round(avg / 0.45359237, 1) if avg else None}
    if day == db.day_key() and not 4 <= now.hour < 11:
        out["hint"] = WEIGH_HOW
    return out


WEIGH_HABIT = "Weigh in"
WEIGH_HOW = ("best first thing in the morning, after the bathroom and before eating"
             " or drinking; one reading means little, the 7-day average is what counts")


def weigh_ins(con, days: int = 90) -> list[dict]:
    since = (date.fromisoformat(db.day_key()) - timedelta(days=days)).isoformat()
    return [dict(r) for r in con.execute(
        "SELECT day, kg, at FROM weigh_ins WHERE day>=? ORDER BY day", (since,))]


def trend_kg(con, end: str | None = None) -> float | None:
    """Body weight for targets: the mean of the weigh-ins in the 7 days up to
    the latest one (up to `end`), so a salty dinner's 1 kg of water doesn't
    move protein or maintenance. With one weigh-in, that one."""
    q = "SELECT day FROM weigh_ins" + (" WHERE day<=?" if end else "") + \
        " ORDER BY day DESC LIMIT 1"
    r = con.execute(q, (end,) if end else ()).fetchone()
    if r is None:
        return None
    since = (date.fromisoformat(r["day"]) - timedelta(days=6)).isoformat()
    v = con.execute("SELECT AVG(kg) a FROM weigh_ins WHERE day BETWEEN ? AND ?",
                    (since, r["day"])).fetchone()["a"]
    return round(v, 2)


def _latest_kg(con) -> float | None:
    return trend_kg(con)


def is_training_day(con, day: str) -> tuple[bool, str]:
    """(training?, why). The checked habit is the fact; the calendar is the
    plan, used only while the habit hasn't been checked either way."""
    r = con.execute(
        "SELECT c.value FROM habit_checkins c JOIN habits h ON h.id=c.habit_id"
        " WHERE h.name='Exercise' AND c.day=?", (day,)).fetchone()
    if r is not None:
        return (r["value"] > 0,
                "Exercise checked" if r["value"] > 0 else "Exercise unchecked")
    for e in con.execute("SELECT summary FROM events WHERE day=?", (day,)):
        if e["summary"] and _GYM.search(e["summary"]):
            return True, f"planned: {e['summary']}"
    return False, "no workout yet"


def weight_trend(ins: list[dict]) -> dict | None:
    """Least-squares slope over the last 28 days of weigh-ins, as % body
    weight per month. Needs ≥3 weigh-ins spanning ≥10 days."""
    if not ins:
        return None
    end = date.fromisoformat(ins[-1]["day"])
    pts = [(-(end - date.fromisoformat(w["day"])).days, w["kg"]) for w in ins
           if (end - date.fromisoformat(w["day"])).days <= 28]
    if len(pts) < 3 or pts[-1][0] - pts[0][0] < 10:
        return None
    n = len(pts)
    mx = sum(x for x, _ in pts) / n
    my = sum(y for _, y in pts) / n
    den = sum((x - mx) ** 2 for x, _ in pts)
    slope = sum((x - mx) * (y - my) for x, y in pts) / den   # kg/day
    return {"kg_per_week": round(slope * 7, 3),
            "pct_per_month": round(slope * 30 / my * 100, 2),
            "points": n, "span_days": pts[-1][0] - pts[0][0]}


# Lean bulk: 0.25–0.5% of body weight a month.
BULK_RATE = (0.25, 0.5)


def maybe_adjust(con, profile: dict) -> dict | None:
    """At most weekly, nudge maintenance by ±100 kcal from the weight trend.
    Faster than the band → down; flat or below → up. Lean bulk only."""
    if profile.get("goal") != "lean_bulk":
        return None
    at = profile.get("maint_adj_at")
    today = date.fromisoformat(db.day_key())
    if at and (today - date.fromisoformat(at[:10])).days < 7:
        return None
    if calibration(con, today.isoformat(), profile)["ready"]:
        return None
    trend = weight_trend(weigh_ins(con, 35))
    if trend is None or trend["span_days"] < 14:
        return None
    rate = trend["pct_per_month"]
    step = -100 if rate > BULK_RATE[1] else 100 if rate < BULK_RATE[0] else 0
    with con:
        if step:
            new = float(profile.get("maint_adj") or 0) + step
            con.execute("INSERT INTO food_profile (key, value) VALUES ('maint_adj', ?)"
                        " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                        (str(new),))
            profile["maint_adj"] = new
        con.execute("INSERT INTO food_profile (key, value) VALUES ('maint_adj_at', ?)"
                    " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (today.isoformat(),))
    return {"step": step, "rate_pct_month": rate}


# ---------- the scale as ground truth ----------
#
# Logged intake minus the energy the weight trend says went into (or came out
# of) the body is what you actually burn, measured in the units you log in.
# Compared with the formula's burn, the gap is either logging running low or
# high, or a burn the formula has wrong — the two can't be told apart from
# here, and it doesn't matter for targets: maintenance taken from your own
# log is right in your log's units, whichever it is. So the gap is shown
# ("logging runs ~300 kcal/day low, or you burn more than the formula
# thinks") and the observed number replaces the formula, phased in as the
# data firms up. Water weight swings a reading by a kilo or more, so the
# trend is a least-squares slope over up to four weeks of weigh-ins, never
# two readings.

KCAL_PER_KG = 7700
CAL_WINDOW = 28
CAL_MIN_WEIGH_INS = 4
CAL_MIN_SPAN = 14
CAL_MIN_DAYS = 7


def _formula_maint(kg: float, profile: dict, day: str) -> float:
    age = int(day[:4]) - int(profile["birth_year"])
    bmr = 10 * kg + 6.25 * float(profile["height_cm"]) - 5 * age + (
        5 if profile["sex"] == "male" else -161)
    return bmr * ACTIVITY.get(profile.get("activity"), 1.375)


def _slope(pts: list[tuple[int, float]]) -> float:
    n = len(pts)
    mx = sum(x for x, _ in pts) / n
    my = sum(y for _, y in pts) / n
    den = sum((x - mx) ** 2 for x, _ in pts) or 1
    return sum((x - mx) * (y - my) for x, y in pts) / den


def calibration(con, day: str, profile: dict | None = None) -> dict:
    """Logged intake vs the weight trend over the CAL_WINDOW days before
    `day`. Always returns a dict; `ready` says whether it means anything yet,
    and `need` says what's missing.

    Only complete days count — closed with "done for today", or with at least
    two meals and 1000 kcal — since a half-logged day would read as eating
    less, which is the very bias being measured."""
    profile = profile or get_profile(con)
    d0 = date.fromisoformat(day)
    end = (d0 - timedelta(days=1)).isoformat()
    start = (d0 - timedelta(days=CAL_WINDOW)).isoformat()
    ins = [dict(r) for r in con.execute(
        "SELECT day, kg FROM weigh_ins WHERE day BETWEEN ? AND ? ORDER BY day",
        (start, day))]
    span = ((date.fromisoformat(ins[-1]["day"]) - date.fromisoformat(ins[0]["day"])).days
            if ins else 0)
    closed = {r["day"] for r in con.execute(
        "SELECT day FROM food_days WHERE closed_at IS NOT NULL AND day BETWEEN ? AND ?",
        (start, end))}
    rows = con.execute(
        "SELECT l.day, SUM(COALESCE(json_extract(f.nutrients, '$.kcal'), 0) * l.servings) k,"
        " COUNT(DISTINCT l.meal) m FROM food_log l JOIN foods f ON f.id=l.food_id"
        " WHERE l.day BETWEEN ? AND ? GROUP BY l.day", (start, end)).fetchall()
    days = {r["day"]: r["k"] for r in rows
            if r["day"] in closed or (r["m"] >= 2 and r["k"] >= 1000)}
    out = {"ready": False, "window": [start, end], "weigh_ins": len(ins),
           "span_days": span, "logged_days": len(days)}
    need = []
    if len(ins) < CAL_MIN_WEIGH_INS or span < CAL_MIN_SPAN:
        need.append(f"{CAL_MIN_WEIGH_INS}+ weigh-ins over {CAL_MIN_SPAN}+ days"
                    f" (have {len(ins)} over {span})")
    if len(days) < CAL_MIN_DAYS:
        need.append(f"{CAL_MIN_DAYS}+ fully logged days (have {len(days)})")
    if missing_setup(profile, ins[-1]["kg"] if ins else _latest_kg(con)):
        need.append("targets setup")
    if need:
        out["need"] = need
        return out
    e0 = date.fromisoformat(ins[0]["day"])
    slope = _slope([((date.fromisoformat(w["day"]) - e0).days, w["kg"]) for w in ins])
    intake = sum(days.values()) / len(days)
    observed = intake - slope * KCAL_PER_KG
    kg = trend_kg(con, day)
    tk = float(profile.get("training_kcal") or 0)
    trained = sum(1 for d in days if is_training_day(con, d)[0])
    base = _formula_maint(kg, profile, day)
    model = base + tk * trained / len(days)
    gap = observed - model
    verdict = ("consistent" if abs(gap) < max(150, 0.07 * model)
               else "low" if gap > 0 else "high")
    # Phased in: a 2-week slope on a few readings is mostly water; four weeks
    # and two weeks of logging is worth trusting fully.
    weight = round(min(1.0, span / CAL_WINDOW) * min(1.0, len(days) / 14), 2)
    rest = observed - tk * trained / len(days)
    out.update(
        ready=True, intake=round(intake), kg_per_week=round(slope * 7, 3),
        observed_tdee=round(observed), formula_tdee=round(model),
        gap_kcal=round(gap), gap_pct=round(gap / intake * 100) if intake else None,
        verdict=verdict, weight=weight,
        maint_rest=round(min(max(rest, base * 0.75), base * 1.35)),
        clamped=not base * 0.75 <= rest <= base * 1.35)
    return out


def calibration_line(c: dict) -> str:
    if not c.get("ready"):
        return "scale check: needs " + "; ".join(c.get("need") or [])
    head = (f"scale check ({c['logged_days']} logged days, {c['weigh_ins']} weigh-ins):"
            f" you log ~{c['intake']:,} kcal/day and weight moves"
            f" {c['kg_per_week']:+.2f} kg/week, so you burn ~{c['observed_tdee']:,}"
            f" in logged calories; the formula says ~{c['formula_tdee']:,}.")
    if c["verdict"] == "consistent":
        return head + " Logging and the scale agree."
    word = "low" if c["verdict"] == "low" else "high"
    other = "more" if word == "low" else "less"
    return (head + f" Either logging runs ~{abs(c['gap_kcal']):,} kcal/day {word}"
            f" (~{abs(c['gap_pct'])}%), or you burn {other} than the formula thinks."
            f" Targets lean {round(c['weight'] * 100)}% on the scale's number.")


def missing_setup(profile: dict, kg: float | None) -> list[str]:
    need = [k for k in ("sex", "birth_year", "height_cm") if not profile.get(k)]
    if kg is None:
        need.append("weight")
    return need


def targets(con, day: str, profile: dict | None = None) -> dict | None:
    """{nutrient: {target, kind}} for `day`, or None until set up."""
    profile = profile or get_profile(con)
    kg = _latest_kg(con)
    if missing_setup(profile, kg):
        return None
    base = _formula_maint(kg, profile, day)
    cal = calibration(con, day, profile)
    if cal["ready"]:
        # The scale's number, phased in; the weekly ±100 nudge is the cruder
        # version of the same idea and stands down.
        maint = (1 - cal["weight"]) * base + cal["weight"] * cal["maint_rest"]
    else:
        maint = base + float(profile.get("maint_adj") or 0)
    training, why = is_training_day(con, day)
    surplus = GOAL_SURPLUS.get(profile.get("goal"), 0)
    kcal = maint + (float(profile.get("training_kcal") or 0) + max(surplus, 0)
                    if training else min(surplus, 0))
    out = {
        "kcal": {"target": round(kcal), "kind": "range", "band": 0.08},
        "protein": {"target": round(PROTEIN_G_PER_KG * kg), "kind": "min"},
        "fat": {"target": round(kcal * 0.28 / 9), "kind": "range", "band": 0.25},
        "fiber": {"target": round(14 * kcal / 1000), "kind": "min"},
        "sodium": {"target": SODIUM_CAP_MG, "kind": "max"},
        "sat_fat": {"target": round(kcal * 0.10 / 9), "kind": "max"},
    }
    out["carbs"] = {"target": round((kcal - out["protein"]["target"] * 4
                                     - out["fat"]["target"] * 9) / 4),
                    "kind": "range", "band": 0.25}
    for k, v in DRI[profile["sex"]].items():
        out[k] = {"target": v, "kind": "min"}
    for r in con.execute("SELECT nutrient, target FROM food_targets"):
        if r["nutrient"] in out:
            out[r["nutrient"]] = {**out[r["nutrient"]], "target": r["target"],
                                  "manual": True}
    return {"nutrients": out, "training": training, "training_why": why,
            "maintenance": round(maint), "formula_maintenance": round(base),
            "maintenance_from": "scale" if cal["ready"] else "formula",
            "kg": kg, "calibration": cal}


def set_target(nutrient: str, target: float | None) -> None:
    """Pin a target by hand (None clears it back to computed)."""
    if nutrient not in NUTRIENTS:
        raise FoodError(f"unknown nutrient {nutrient!r}")
    with db.session() as con, con:
        if target is None:
            con.execute("DELETE FROM food_targets WHERE nutrient=?", (nutrient,))
        else:
            con.execute("INSERT INTO food_targets (nutrient, target) VALUES (?,?)"
                        " ON CONFLICT(nutrient) DO UPDATE SET target=excluded.target",
                        (nutrient, float(target)))


def coverage(tot: dict, tg: dict | None) -> dict:
    """{nutrient: fraction of target} — for caps, fraction of the cap used."""
    if not tg:
        return {}
    return {k: round(tot.get(k, 0) / t["target"], 3)
            for k, t in tg["nutrients"].items() if t["target"]}


def gaps(tot: dict, tg: dict | None, n: int = 4) -> list[str]:
    """The lowest-covered micros (plus protein when short)."""
    if not tg:
        return []
    cov = coverage(tot, tg)
    keys = [k for k in MICROS if k in cov]
    keys.sort(key=lambda k: cov[k])
    return [k for k in keys if cov[k] < 0.9][:n]


# ---------- dining dollars ----------

def dd_pace(con, profile: dict, today: str) -> dict | None:
    bal = profile.get("dd_balance")
    if bal is None:
        return None
    at = profile.get("dd_balance_at") or "0000"
    spent = con.execute(
        "SELECT COALESCE(SUM(price),0) s FROM food_log WHERE paid_with="
        "'dining_dollars' AND price IS NOT NULL AND eaten_at>=?", (at,)).fetchone()["s"]
    left = round(float(bal) - spent, 2)
    week_ago = (date.fromisoformat(today) - timedelta(days=6)).isoformat()
    week = con.execute(
        "SELECT COALESCE(SUM(price),0) s FROM food_log WHERE paid_with="
        "'dining_dollars' AND price IS NOT NULL AND day BETWEEN ? AND ?",
        (week_ago, today)).fetchone()["s"]
    out = {"left": left, "spent_since_set": round(spent, 2),
           "per_day_7d": round(week / 7, 2)}
    end = profile.get("semester_end")
    if end:
        days_left = (date.fromisoformat(end) - date.fromisoformat(today)).days + 1
        if days_left > 0:
            allow = left / days_left
            out.update(days_left=days_left, allowance_per_day=round(allow, 2))
            diff = out["per_day_7d"] - allow
            out["pace"] = ("on pace" if abs(diff) <= max(3, allow * 0.1) else
                           "over" if diff > 0 else "under")
    return out


# ---------- the day ----------

def day_summary(day: str | None = None) -> dict:
    day = day or db.day_key()
    with db.session() as con:
        profile = get_profile(con)
        adjusted = maybe_adjust(con, profile) if day == db.day_key() else None
        items = entries(con, day)
        tot, missing = totals(items)
        tg = targets(con, day, profile)
        closed = con.execute("SELECT * FROM food_days WHERE day=?", (day,)).fetchone()
        kg = _latest_kg(con)
        last = con.execute("SELECT day, kg, at FROM weigh_ins ORDER BY day DESC"
                           " LIMIT 1").fetchone()
        trend = weight_trend(weigh_ins(con, 35))
        pace = dd_pace(con, profile, day)
        cal = tg["calibration"] if tg else calibration(con, day, profile)
    return {
        "day": day, "today": db.day_key(), "entries": items, "totals": tot,
        "range": day_range(items),
        "questions": [{"id": e["id"], "name": e["name"], **e["ask"]}
                      for e in items if e.get("ask")],
        "missing": missing, "targets": tg, "coverage": coverage(tot, tg),
        "gaps": gaps(tot, tg), "closed": dict(closed) if closed else None,
        "setup_needed": missing_setup(profile, kg), "profile": profile,
        # A "7-day average" of a reading weeks old would be a stale number
        # passed off as current, so it's only given while one is recent.
        "weight": {"kg": kg, "avg7_kg": kg if last and (
                       date.fromisoformat(day) - date.fromisoformat(last["day"])).days <= 7
                   else None, "last": dict(last) if last else None,
                   "weighed_today": bool(last and last["day"] == day),
                   "how": WEIGH_HOW, "trend": trend},
        "calibration": cal, "calibration_line": calibration_line(cal), "dd": pace,
        "maintenance_adjusted": adjusted, "nutrients": NUTRIENTS,
        "meals": MEALS,
    }


def summary_line(s: dict) -> str:
    """'2,140/2,900 kcal · protein 96/140 g · low: vit D, fiber'"""
    tot, tg = s["totals"], s["targets"]
    if not tg:
        return (f"{round(tot.get('kcal', 0)):,} kcal · protein "
                f"{round(tot.get('protein', 0))} g (targets not set up)")
    n = tg["nutrients"]
    r = (s.get("range") or {}).get("kcal")
    line = (f"{round(tot.get('kcal', 0)):,}/{n['kcal']['target']:,} kcal"
            + (f" (likely {r[0]:,}–{r[1]:,})" if r else "")
            + f" · protein {round(tot.get('protein', 0))}/{n['protein']['target']} g")
    if s["gaps"]:
        line += " · low: " + ", ".join(NUTRIENTS[k][0] for k in s["gaps"][:3])
    return line


def _ensure_habit(con, name: str) -> int:
    r = con.execute("SELECT id, archived FROM habits WHERE name=?", (name,)).fetchone()
    if r:
        if r["archived"]:
            con.execute("UPDATE habits SET archived=0 WHERE id=?", (r["id"],))
        return r["id"]
    return con.execute(
        "INSERT INTO habits (name, cadence, target, created_at) VALUES (?,?,?,?)",
        (name, "daily", 1, _now())).lastrowid


def ensure_habit() -> None:
    with db.session() as con, con:
        _ensure_habit(con, HABIT)


def close_day(day: str | None = None, *, reopen: bool = False,
              source: str = "ui") -> dict:
    """The user's "done for today": freezes the summary into the Log food
    check-in. Reopening un-checks it (a tombstone, like any un-check)."""
    day = day or db.day_key()
    s = day_summary(day)
    with db.session() as con, con:
        hid = _ensure_habit(con, HABIT)
        if reopen:
            con.execute("DELETE FROM food_days WHERE day=?", (day,))
        else:
            if not s["entries"]:
                raise FoodError(f"nothing logged on {day} — log something first")
            con.execute("INSERT INTO food_days (day, closed_at, summary) VALUES"
                        " (?,?,?) ON CONFLICT(day) DO UPDATE SET"
                        " closed_at=excluded.closed_at, summary=excluded.summary",
                        (day, _now(), summary_line(s)))
    db.checkin(hid, day, 0 if reopen else 1,
               None if reopen else summary_line(s), source=source)
    if not reopen and s["targets"]:
        p = s["totals"].get("protein", 0)
        if p >= s["targets"]["nutrients"]["protein"]["target"]:
            db.auto_checkin(PROTEIN_HABIT, day, note=f"{round(p)} g")
    return day_summary(day)


def week(end: str | None = None, days: int = 7) -> dict:
    end = end or db.day_key()
    d0 = date.fromisoformat(end)
    out = []
    for i in range(days - 1, -1, -1):
        d = (d0 - timedelta(days=i)).isoformat()
        s = day_summary(d)
        out.append({"day": d, "totals": s["totals"], "coverage": s["coverage"],
                    "targets": {k: v["target"] for k, v in
                                (s["targets"] or {}).get("nutrients", {}).items()},
                    "training": (s["targets"] or {}).get("training"),
                    "closed": bool(s["closed"]), "logged": len(s["entries"]),
                    "dd_spent": round(sum(e["price"] or 0 for e in s["entries"]
                                          if e["paid_with"] == "dining_dollars"), 2)})
    with db.session() as con:
        ins = weigh_ins(con, 90)
    return {"days": out, "weigh_ins": ins, "nutrients": NUTRIENTS}


def brief_context(day: str) -> dict | None:
    """Yesterday, for the morning brief — only once a day has been closed."""
    y = (date.fromisoformat(day) - timedelta(days=1)).isoformat()
    s = day_summary(y)
    if not s["entries"]:
        return None
    return {"day": y, "line": summary_line(s), "closed": bool(s["closed"])}


# ---------- suggestions ----------

MEAL_SHARE = {"breakfast": 0.25, "lunch": 0.35, "dinner": 0.4, "snack": 0.15}


def _remaining_meals(meal: str) -> list[str]:
    order = ["breakfast", "lunch", "dinner"]
    return order[order.index(meal):] if meal in order else ["snack"]


def _open_at(shifts: list, at: datetime) -> bool | None:
    """Open at `at`, from the listing's shifts — the shifts the page showed
    for the calendar day it was fetched. They only answer for that calendar
    date: yesterday's shifts say nothing about today (a ±1-day window marked
    every venue closed at 12:30 the next day), and USC's page at 2am already
    shows the new date's shifts, which a day_key comparison got wrong.
    Otherwise unknown."""
    if not shifts:
        return None
    dates = {datetime.fromtimestamp(int(a) / 1000).date() for a, _ in shifts}
    if at.date() not in dates:
        return None
    ms = at.timestamp() * 1000
    return any(int(a) <= ms <= int(b) for a, b in shifts)


def candidates(con, day: str, meal: str, *, at: datetime | None = None) -> dict:
    """{venue_slug: {"venue": row, "items": [food...]}} — what could be eaten
    for this meal: the halls' menus for it, and the foods you've bought at
    Dining Dollar–eligible retail venues."""
    venues = {r["slug"]: dict(r) for r in con.execute("SELECT * FROM food_venues")}
    out: dict[str, dict] = {}
    check_hours = at is not None and day == db.day_key()
    for v in venues.values():
        v["shifts"] = json.loads(v["shifts"]) if v.get("shifts") else []
        v["open"] = _open_at(v["shifts"], at) if check_hours else None
    hall_meal = {"snack": None}.get(meal, meal.capitalize())
    if hall_meal:
        rows = con.execute(
            "SELECT m.venue, m.item, m.station, f.* FROM usc_menu_items m JOIN"
            " foods f ON f.source='usc_estimate' AND f.source_id=m.item_key"
            " WHERE m.date=? AND m.meal IN (?, 'Brunch')", (day, hall_meal)).fetchall()
        for r in rows:
            v = venues.get(r["venue"])
            if v is None or v["open"] is False:
                continue
            f = _food_row(r)
            f["station"] = r["station"]
            slot = out.setdefault(r["venue"], {"venue": v, "items": {}})
            slot["items"][f["id"]] = f
    for r in con.execute(
            "SELECT f.* FROM foods f JOIN food_venues v ON v.slug=f.venue"
            " WHERE v.kind='retail' AND v.dining_dollars=1"):
        v = venues[r["venue"]]
        if v["open"] is False:
            continue
        f = _food_row(r)
        slot = out.setdefault(r["venue"], {"venue": v, "items": {}})
        slot["items"][f["id"]] = f
    return {k: {"venue": s["venue"], "items": list(s["items"].values())}
            for k, s in out.items()}


MAIN_PROTEIN_G = 20   # an item with this much protein is an entrée


def _plate_score(plate: list[dict], meal_need: dict, budget: float,
                 focus: list[str], caps_meal: dict) -> float:
    """Everything is judged against *this meal's* share of what's left, not
    the day's: the first version filled lunch with three entrées and 117 g of
    protein, because the day still needed it."""
    n = add_nutrients([f["nutrients"] for f in plate])
    score = 0.0
    p_need = max(meal_need.get("protein", 0), 1)
    score += 3 * min(n.get("protein", 0), p_need) / p_need
    kcal = n.get("kcal", 0)
    if budget > 50:
        # Overshoot costs; a quarter under is free (there's a snack later), so
        # the plate isn't padded to budget with a cookie.
        off = (kcal - budget) / budget
        if off > 0:
            score -= 3 * off
        elif off < -0.25:
            score -= 1.2 * (-off - 0.25)
    for k in focus:
        want = meal_need.get(k, 0)
        if want > 0:
            score += 2.0 / len(focus) * min(n.get(k, 0), want) / want
    for k in CAPS:
        cap = max(caps_meal.get(k, 0), 1)
        if n.get(k, 0) > cap:
            score -= 2 * (n[k] - cap) / cap
    mains = sum(1 for f in plate if f["nutrients"].get("protein", 0) >= MAIN_PROTEIN_G)
    conf = sum(f["confidence"] for f in plate) / len(plate)
    score -= 0.8 * max(mains - 1, 0) + 0.3 * (1 - conf) + 0.15 * len(plate)
    return score


def suggest(day: str | None = None, meal: str | None = None, *,
            at: datetime | None = None, k: int = 3) -> dict:
    """Top plates for the next meal, one venue each. Deterministic; the
    console phrases it."""
    day = day or db.day_key()
    now = at or datetime.now()
    meal = meal or default_meal(now)
    s = day_summary(day)
    tg = s["targets"]
    if not tg:
        need = [k.replace("_cm", "").replace("_", " ") for k in s["setup_needed"]]
        return {"day": day, "meal": meal, "plates": [],
                "why_empty": "targets aren't set up yet (needs " + ", ".join(need) + ")"}
    tot = s["totals"]
    tn = tg["nutrients"]
    need = {k2: tn[k2]["target"] - tot.get(k2, 0) for k2 in tn}
    # A normal meal of this kind, as a share of the day (snacks on top).
    nominal = MEAL_SHARE[meal] / (1 - MEAL_SHARE["snack"])
    if day > db.day_key():
        # Planning ahead: the earlier meals will happen, they just haven't.
        share = nominal
    else:
        rem_meals = _remaining_meals(meal)
        share = MEAL_SHARE[meal] / sum(MEAL_SHARE[m] for m in rem_meals)
    # This meal's share of what's left, never more than 1.4× a normal meal of
    # that kind — an empty day doesn't make dinner the whole day's food.
    meal_need = {k2: min(max(v, 0) * share, tn[k2]["target"] * nominal * 1.4)
                 for k2, v in need.items()}
    budget = meal_need["kcal"]
    focus = s["gaps"] or [k2 for k2 in MICROS if k2 in tn][:4]
    caps_meal = {k2: min(max(need[k2], 0) * share, tn[k2]["target"] * nominal * 1.2)
                 for k2 in CAPS}
    with db.session() as con:
        cands = candidates(con, day, meal,
                           at=now if day == db.day_key() else None)
        door = get_profile(con).get("door_price")
    plates = []
    for slug, c in cands.items():
        items = [f for f in c["items"] if f["nutrients"].get("kcal")]
        if not items:
            continue
        # Prune to the 22 best singles before combining (22C3 ≈ 1.5k).
        items.sort(key=lambda f: -_plate_score([f], meal_need, budget, focus,
                                               caps_meal))
        items = items[:22]
        best = None
        # A hall plate is a few things off the line; a café order is one or
        # two — three packaged bowls isn't a meal anyone buys.
        for size in ((1, 2, 3) if c["venue"]["kind"] == "dining_hall" else (1, 2)):
            for combo in combinations(items, size):
                sc = _plate_score(list(combo), meal_need, budget, focus, caps_meal)
                if best is None or sc > best[0]:
                    best = (sc, combo)
        if best:
            plates.append((best[0], slug, c["venue"], list(best[1])))
    plates.sort(key=lambda p: -p[0])
    # The same Positive Foods bowl is sold at three cafés: one plate, not
    # three. And when a hall serves this meal, its best plate keeps a slot —
    # three café bowls hid every dining hall on the first run.
    picked, seen_items = [], set()
    for p in plates:
        items = tuple(sorted(f["name"].lower() for f in p[3]))
        if items not in seen_items:
            seen_items.add(items)
            picked.append(p)
    best_hall = next((p for p in picked if p[2]["kind"] == "dining_hall"), None)
    top = picked[:k]
    if best_hall and best_hall not in top and top:
        top[-1] = best_hall
    out = []
    for sc, slug, v, combo in top:
        n = add_nutrients([f["nutrients"] for f in combo])
        after = add_nutrients([tot, n])
        closes = [k2 for k2 in focus if after.get(k2, 0) >= 0.9 * tn[k2]["target"]
                  > tot.get(k2, 0)]
        price = door if v["kind"] == "dining_hall" else None
        out.append({
            "venue": slug, "venue_name": v["name"], "kind": v["kind"],
            "open": v.get("open"), "score": round(sc, 3),
            "items": [{"ref": f["ref"], "id": f["id"], "name": f["name"],
                       "serving_desc": f["serving_desc"],
                       "station": f.get("station"),
                       "confidence": f["confidence"]} for f in combo],
            "adds": {k2: round(n.get(k2, 0), 1) for k2 in
                     ("kcal", "protein", "carbs", "fat", "fiber", "sodium")},
            "closes": closes, "price": price,
        })
    return {"day": day, "meal": meal, "kcal_budget": round(budget),
            "protein_left": round(max(need["protein"], 0)), "focus": focus,
            "plates": out,
            "why_empty": None if out else
            ("nothing on campus with numbers is open for a snack right now"
             if meal == "snack" else
             "no menus or venue foods with numbers yet for this meal")}


def plate_line(p: dict) -> str:
    items = " + ".join(i["name"] for i in p["items"])
    a = p["adds"]
    line = (f"{p['venue_name']}: {items} → {round(a['kcal'])} kcal,"
            f" +{round(a['protein'])} g protein")
    if p["closes"]:
        line += " · closes " + ", ".join(NUTRIENTS[k][0] for k in p["closes"])
    if any(i["confidence"] < 0.9 for i in p["items"]):
        line += " (est.)"
    return line
