"""Build backend/app/food_data/coffee_bean.json from Coffee Bean & Tea Leaf's
published nutrition PDF (§25 — the one campus venue whose full menu has
official numbers fetchable from here).

    curl -sL -o cbtl.pdf "$(curl -sIL https://www.coffeebean.com/nutrition | ...)"
    python scripts/build_coffee_bean.py cbtl.pdf [recovered.json]

(coffeebean.com/nutrition redirects to a Shopify CDN PDF; the URL changes with
each revision, so follow the redirect rather than pinning it.)

Each row of the PDF is a name, eleven nutrient columns, four %DV columns and
an allergen list; a long name wraps onto its own line above the numbers.
%DV is converted with the FDA 2020 daily values (vitamin D 20 µg, calcium
1300 mg, iron 18 mg, potassium 4700 mg). Drinks and add-ins only — the PDF
has no food items.
"""

import json
import re
import sys
from pathlib import Path

import pypdf

OUT = Path(__file__).resolve().parents[1] / "app" / "food_data" / "coffee_bean.json"
NUM = r"(\d+(?:\.\d+)?)"
ROW = re.compile(r"^(?P<name>.*?)\s*" + r"\s+".join([NUM] * 15) + r"\s+(?P<allergens>.+)$")
DV = {"vit_d": 20, "calcium": 1300, "iron": 18, "potassium": 4700}
SCRAMBLED = re.compile(r"\b[A-Za-z] [a-z] [a-z] [a-z]\b|\d{4,}")


def plausible(n: dict) -> bool:
    """Atwater check: 4·protein + 4·carbs + 9·fat within 35% (or 30 kcal) of
    the stated calories. Catches a row whose digits landed in the wrong
    columns — the failure the digit-multiset check can't see."""
    est = 4 * n["protein"] + 4 * n["carbs"] + 9 * n["fat"]
    return abs(est - n["kcal"]) <= max(30, 0.35 * n["kcal"])


def parse(text: str) -> list[dict]:
    out, section, carry = [], None, ""
    for raw in text.splitlines():
        line = re.sub(r"\s+", " ", raw.replace("‐", "-").replace("\xa0", " ")).strip()
        if not line or line.startswith("Beverage Name"):
            continue
        if line.isupper() and len(line) < 45 and not re.search(r"\d", line):
            section, carry = line.title().replace("®", ""), ""
            continue
        # A scrambled line ("Iced C o f f e e 1 0 0000 5 0 …") still fits ROW,
        # one digit per column: refuse it here, recovered() supplies the row.
        if SCRAMBLED.search(line):
            carry = ""
            continue
        m = ROW.match(line)
        if not m:
            carry = f"{carry} {line}".strip()   # a wrapped name
            continue
        name = f"{carry} {m.group('name')}".strip() if carry else m.group("name")
        carry = ""
        name = re.sub(r"\s*\((New addition|Felbro \d{4})\)", "", name).strip()
        v = [float(m.group(i)) for i in range(2, 17)]
        n = {"kcal": v[0], "fat": v[1], "sat_fat": v[2], "sodium": v[5],
             "carbs": v[6], "fiber": v[7], "sugar": v[8], "protein": v[10]}
        for key, pct in zip(("vit_d", "calcium", "iron", "potassium"), v[11:15]):
            n[key] = round(pct / 100 * DV[key], 2)
        size = re.match(r"(\d+ oz)\s+(.*)", name)
        out.append({
            "name": name, "section": section,
            "serving_desc": size.group(1) if size else "1 serving",
            "nutrients": n,
            "allergens": [a.strip().lower() for a in m.group("allergens").split(",")
                          if a.strip() and a.strip() != "None"],
        })
    return out


def recovered(path: str) -> list[dict]:
    """Rows whose text layer is scrambled (pages 4–8 of the 2025 PDF: digits
    come out in the wrong order) — transcribed from page images and accepted
    only where the digits match the scrambled line's digits exactly. The file
    is {"accepted": [[name, 15 numbers, allergens], ...]}."""
    out = []
    for r in json.loads(Path(path).read_text())["accepted"]:
        line = " ".join(str(x) for x in r[:16]) + " " + (r[16] if len(r) > 16 else "None")
        out += parse(line)
    return out


def main(pdf: str, extra: str | None = None) -> None:
    text = "\n".join(p.extract_text() or "" for p in pypdf.PdfReader(pdf).pages)
    rows = parse(text)
    if extra:
        have = {r["name"] for r in rows}
        rows += [r for r in recovered(extra) if r["name"] not in have]
    dropped = [r["name"] for r in rows if not plausible(r["nutrients"])]
    rows = [r for r in rows if plausible(r["nutrients"])]
    if dropped:
        print(f"dropped {len(dropped)} implausible: {dropped}")
    OUT.write_text(json.dumps({"source": "coffeebean.com/nutrition (CBTL_Nutrition.pdf)",
                               "items": rows}, indent=1))
    print(f"{len(rows)} items -> {OUT}")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else None)
