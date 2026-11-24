"""USC dining provider: the three residential halls' menus and the campus venue list. No key.

Menus come from the hospitality site's own wp-json endpoint, one hall-day per call, with
allergen/diet tags but **no calories, macros or portions** (estimated in app/food_estimate.py).
Venues come from the location listing pages; dining_dollars = type != restaurants, an
inference from USC's published rule, not a field. Menus are published ahead; reads are lazy (MENU_TTL).
Design: DESIGN.md → Food (§25).

    python -m app.providers usc_dining --range today --query chicken --format brief
"""

import html
import json
import re
from datetime import date, datetime, timedelta

from .. import db
from . import http, ics, offline
from .ranges import Range

NAME = "usc_dining"
METRICS = None
DEFAULT_METRIC = None
ALLOW_FUTURE = True

BASE = "https://hospitality.usc.edu"
# menu API key → the hall's location slug (what the venues table is keyed by)
HALLS = {"evk": "everybodys-kitchen",
         "parkside": "parkside-restaurant-grill",
         "university-village": "usc-village-dining-hall"}
LISTINGS = {"upc": "/dining-locations-upc/", "hsc": "/dining-locations-hsc/"}
KIND = {"dining-halls": "dining_hall", "retail-dining": "retail",
        "restaurants": "restaurant"}
MENU_TTL = timedelta(hours=6)
VENUE_TTL = timedelta(hours=12)
MAX_DAYS = 14      # a range read never fetches more than this many days


def _fetch_menu(api_key: str, d: date) -> dict:
    """GET one hall-day. Tests monkeypatch this name."""
    return http.json_request(
        f"{BASE}/wp-json/hsp-api/v1/get-res-dining-menus/{api_key}"
        f"?y={d.year}&m={d.month:02d}&d={d.day:02d}",
        label=f"usc menu {api_key}", timeout=20)


def _fetch_listing(path: str) -> str:
    """GET a location listing page. Tests monkeypatch this name."""
    return ics.fetch(BASE + path)


def parse_menu(payload: dict) -> list[dict]:
    """→ [{meal, station, item, allergens, prefs}]. Meals without stations
    (Brunch on weekdays) are simply absent."""
    from ..food import norm_name
    out = []
    for meal in payload.get("meals") or []:
        for st in meal.get("stations") or []:
            for it in st.get("menu") or []:
                name = (it.get("item") or "").strip()
                if not name:
                    continue
                out.append({"meal": meal["name"], "station": st["station"].strip(),
                            "item": name, "item_key": norm_name(name),
                            "allergens": it.get("allergens") or [],
                            "prefs": it.get("preferences") or []})
    return out


_TILE = re.compile(
    r'data-types="([^"]*)".*?href="https://hospitality\.usc\.edu/dining-locations/'
    r'([^/"]+)/".*?class="title">([^<]*)<.*?data-shifts="([^"]*)"', re.S)


def parse_listing(page: str) -> list[dict]:
    out = []
    for m in _TILE.finditer(page):
        types = json.loads(html.unescape(m.group(1)))
        kind = next((KIND[t] for t in types if t in KIND), "retail")
        out.append({"slug": m.group(2), "name": html.unescape(m.group(3)).strip(),
                    "kind": kind, "dining_dollars": int(kind != "restaurant"),
                    "shifts": [[int(a), int(b)] for a, b in
                               json.loads(html.unescape(m.group(4)) or "[]")]})
    return out


def refresh_venues(force: bool = False) -> int:
    with db.session() as con:
        last = con.execute("SELECT MAX(refreshed_at) t FROM food_venues").fetchone()["t"]
    today = db.day_key()
    if not force and last and datetime.now() - datetime.fromisoformat(last) < VENUE_TTL \
            and last[:10] >= today:
        return 0
    now = datetime.now().isoformat(timespec="seconds")
    n = 0
    for campus, path in LISTINGS.items():
        rows = parse_listing(_fetch_listing(path))
        with db.session() as con, con:
            for v in rows:
                con.execute(
                    "INSERT INTO food_venues (slug, name, kind, campus,"
                    " dining_dollars, shifts, shifts_day, refreshed_at)"
                    " VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(slug) DO UPDATE SET"
                    " name=excluded.name, kind=excluded.kind, campus=excluded.campus,"
                    " shifts=excluded.shifts, shifts_day=excluded.shifts_day,"
                    " refreshed_at=excluded.refreshed_at",
                    # dining_dollars is set on insert only: hand corrections survive.
                    (v["slug"], v["name"], v["kind"], campus, v["dining_dollars"],
                     json.dumps(v["shifts"]), today, now))
                n += 1
    return n


def refresh_menus(days: list[date], force: bool = False) -> dict:
    """Fetch each hall-day not fetched within MENU_TTL. A day the API
    answers with nothing is still recorded, so it isn't asked again."""
    now = datetime.now()
    with db.session() as con:
        have = {(r["date"], r["venue"]): r["fetched_at"] for r in con.execute(
            "SELECT * FROM usc_menu_days WHERE date BETWEEN ? AND ?",
            (min(days).isoformat(), max(days).isoformat()))}
    fetched = items = 0
    for d in days:
        for api_key, slug in HALLS.items():
            at = have.get((d.isoformat(), slug))
            if not force and at and now - datetime.fromisoformat(at) < MENU_TTL:
                continue
            rows = parse_menu(_fetch_menu(api_key, d))
            with db.session() as con, con:
                con.execute("DELETE FROM usc_menu_items WHERE date=? AND venue=?",
                            (d.isoformat(), slug))
                con.executemany(
                    "INSERT OR IGNORE INTO usc_menu_items (date, venue, meal,"
                    " station, item, item_key, allergens, prefs)"
                    " VALUES (?,?,?,?,?,?,?,?)",
                    [(d.isoformat(), slug, r["meal"], r["station"], r["item"],
                      r["item_key"], json.dumps(r["allergens"]),
                      json.dumps(r["prefs"])) for r in rows])
                con.execute(
                    "INSERT INTO usc_menu_days (date, venue, fetched_at) VALUES"
                    " (?,?,?) ON CONFLICT(date, venue) DO UPDATE SET"
                    " fetched_at=excluded.fetched_at",
                    (d.isoformat(), slug, now.isoformat(timespec="seconds")))
            fetched += 1
            items += len(rows)
    return {"hall_days": fetched, "items": items}


def refresh(force: bool = False) -> dict:
    """Poller: venues, today + tomorrow's menus, estimate new items, ensure café menus are seeded."""
    from .. import food_estimate, food_seed
    today = date.fromisoformat(db.day_key())
    info = {"venues": refresh_venues(force),
            **refresh_menus([today, today + timedelta(days=1)], force)}
    # A usage limit holds the estimates; it doesn't fail the menus that landed.
    try:
        info["estimated"] = food_estimate.run_pending()
    except food_estimate.agent.AgentError as e:
        info["estimated"] = f"held: {e}"
    info["cafes"] = food_seed.ensure()
    return info


def get(rng: Range, metric: str | None = None) -> dict:
    days = []
    d = date.fromisoformat(rng.start_key)
    while d.isoformat() <= rng.end_key and len(days) < MAX_DAYS:
        days.append(d)
        d += timedelta(days=1)
    with offline.best_effort("usc menus"):
        refresh_menus(days)
    with db.session() as con:
        rows = [dict(r) for r in con.execute(
            "SELECT m.*, v.name venue_name, f.id food_id, f.nutrients,"
            " f.serving_desc FROM usc_menu_items m"
            " LEFT JOIN food_venues v ON v.slug=m.venue"
            " LEFT JOIN foods f ON f.source='usc_estimate' AND f.source_id=m.item_key"
            " WHERE m.date BETWEEN ? AND ? ORDER BY m.date, m.venue, m.meal,"
            " m.station, m.item", (rng.start_key, rng.end_key))]
        last = con.execute(
            "SELECT MAX(fetched_at) t FROM usc_menu_days WHERE date BETWEEN ? AND ?",
            (rng.start_key, rng.end_key)).fetchone()["t"]
    for r in rows:
        r["allergens"] = json.loads(r["allergens"] or "[]")
        r["prefs"] = json.loads(r["prefs"] or "[]")
        n = json.loads(r.pop("nutrients") or "{}")
        r["kcal"] = round(n["kcal"]) if "kcal" in n else None
        r["protein"] = round(n["protein"]) if "protein" in n else None
        r["venue_name"] = r["venue_name"] or r["venue"]
    return {"data": rows, "refreshed_at": last}


def brief_line(it: dict) -> str:
    """2026-10-14 Everybody's Kitchen Lunch · Hot Line: Grilled Chicken
    [halal] ~310 kcal 34 g protein (f88)"""
    line = (f"{it['date']} {it['venue_name']} {it['meal']} · {it['station']}:"
            f" {it['item']}")
    if it["prefs"]:
        line += f" [{', '.join(it['prefs'])}]"
    if it.get("kcal") is not None:
        line += f" ~{it['kcal']} kcal {it['protein']} g protein (f{it['food_id']})"
    else:
        line += " (not estimated yet)"
    return line
