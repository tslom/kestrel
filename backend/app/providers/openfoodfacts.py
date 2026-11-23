"""Open Food Facts — barcode → label nutrition for packaged food. Keyless.

Not a provider in REGISTRY: nothing to poll and no days to read, same shape as
venues.py. One call per barcode, the first time it's looked up; the result
becomes a foods row (source 'off', source_id the barcode), so a second scan of
the same bar never reaches the network.

OFF reports every nutrient per 100 g in grams — 0.035 calcium means 35 mg —
so each is scaled to the unit app/food.NUTRIENTS uses.
"""

from . import http

URL = "https://world.openfoodfacts.org/api/v2/product/{code}.json"
FIELDS = "product_name,brands,serving_size,serving_quantity,nutriments"

# our key → (OFF nutriment, factor from OFF's grams to our unit)
MAP = {
    "kcal": ("energy-kcal", 1), "protein": ("proteins", 1),
    "carbs": ("carbohydrates", 1), "fat": ("fat", 1),
    "sat_fat": ("saturated-fat", 1), "fiber": ("fiber", 1),
    "sugar": ("sugars", 1), "sodium": ("sodium", 1000),
    "potassium": ("potassium", 1000), "calcium": ("calcium", 1000),
    "iron": ("iron", 1000), "magnesium": ("magnesium", 1000),
    "zinc": ("zinc", 1000), "vit_a": ("vitamin-a", 1e6),
    "vit_c": ("vitamin-c", 1000), "vit_d": ("vitamin-d", 1e6),
    "vit_b12": ("vitamin-b12", 1e6), "folate": ("folates", 1e6),
    "omega3": ("omega-3-fat", 1),
}


def _fetch(code: str) -> dict:
    """GET one product. Tests monkeypatch this name."""
    return http.json_request(URL.format(code=code) + f"?fields={FIELDS}",
                             label="open food facts", timeout=15,
                             headers={"User-Agent": "kestrel-dashboard/1.0 (personal)"})


def lookup(code: str) -> dict | None:
    """→ {name, serving_desc, serving_g, nutrients (per serving)} or None
    when OFF doesn't know the barcode or has no calories for it."""
    code = "".join(ch for ch in str(code) if ch.isdigit())
    if not 6 <= len(code) <= 14:
        raise ValueError("a barcode is 6–14 digits")
    payload = _fetch(code)
    p = payload.get("product") if payload.get("status") == 1 else None
    if not p:
        return None
    nm = p.get("nutriments") or {}
    per100 = {k: float(nm[f"{off}_100g"]) * f for k, (off, f) in MAP.items()
              if isinstance(nm.get(f"{off}_100g"), (int, float))}
    if "kcal" not in per100:
        return None
    grams = p.get("serving_quantity")
    try:
        grams = float(grams) if grams else None
    except (TypeError, ValueError):
        grams = None
    scale = (grams or 100) / 100
    name = " ".join(x for x in (p.get("brands", "").split(",")[0].strip(),
                                (p.get("product_name") or "").strip()) if x)
    return {"code": code, "name": name or f"barcode {code}",
            "serving_desc": p.get("serving_size") or "100 g",
            "serving_g": grams or 100,
            "nutrients": {k: round(v * scale, 3) for k, v in per100.items()}}
