"""Build backend/app/food/sr28.json.gz from the USDA SR28 ASCII release.

SR28 (2015, re-published by USDA as "SR Legacy") is the food database behind
§25's food tracker: ~8.8k generic foods with full micronutrients and household
portions. The file it produces is checked in, so this runs only to rebuild it:

    python scripts/build_sr28.py /path/to/sr28/dir

The dir holds FOOD_DES.txt, NUTR_DEF.txt, NUT_DATA.txt, WEIGHT.txt and
FD_GROUP.txt — the ASCII release (fdc.nal.usda.gov's SR Legacy zip, or the
mirror at github.com/alyssaq/usda-sqlite/tree/master/data). Fields are
`^`-separated, text fields wrapped in `~`.

Only the nutrients in app.food.NUTRIENTS are kept (per 100 g), plus the
household portions ("1 large", "1 cup, chopped") with their gram weights.
Omega-3 is the sum of ALA + EPA + DHA, which is how a person means it.
"""

import gzip
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.food import NUTRIENTS, SR28_PATH

# SR28 nutrient number -> our key. Omega-3 is three numbers summed.
SR_NUM = {
    "208": "kcal", "203": "protein", "205": "carbs", "204": "fat",
    "606": "sat_fat", "291": "fiber", "269": "sugar", "307": "sodium",
    "306": "potassium", "301": "calcium", "303": "iron", "304": "magnesium",
    "309": "zinc", "320": "vit_a", "401": "vit_c", "328": "vit_d",
    "418": "vit_b12", "435": "folate",
}
OMEGA3 = {"851", "629", "621"}   # ALA, EPA, DHA


def rows(path: Path):
    with open(path, encoding="latin-1") as f:
        for line in f:
            yield [c.strip("~") for c in line.rstrip("\r\n").split("^")]


def main(src: Path) -> None:
    assert set(SR_NUM.values()) | {"omega3"} == set(NUTRIENTS), \
        "SR_NUM and app.food.NUTRIENTS disagree"
    groups = {r[0]: r[1] for r in rows(src / "FD_GROUP.txt")}
    foods = {}
    for r in rows(src / "FOOD_DES.txt"):
        foods[r[0]] = {"id": f"sr:{r[0]}", "name": r[2],
                       "group": groups.get(r[1], ""), "n": {}, "portions": []}
    for r in rows(src / "NUT_DATA.txt"):
        fid, num, val = r[0], r[1], r[2]
        if fid not in foods or val == "":
            continue
        n = foods[fid]["n"]
        if num in SR_NUM:
            n[SR_NUM[num]] = round(float(val), 3)
        elif num in OMEGA3:
            n["omega3"] = round(n.get("omega3", 0) + float(val), 3)
    for r in rows(src / "WEIGHT.txt"):
        fid, amount, desc, grams = r[0], r[2], r[3], r[4]
        if fid in foods and grams:
            amt = float(amount)
            label = f"{amt:g} {desc}" if amt != 1 else f"1 {desc}"
            foods[fid]["portions"].append([label, round(float(grams), 1)])
    out = sorted(foods.values(), key=lambda f: f["id"])
    SR28_PATH.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(SR28_PATH, "wt", encoding="utf-8") as f:
        json.dump(out, f, separators=(",", ":"))
    print(f"{len(out)} foods -> {SR28_PATH} ({SR28_PATH.stat().st_size // 1024} KB)")


if __name__ == "__main__":
    main(Path(sys.argv[1]))
