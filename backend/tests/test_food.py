"""§25 food tracker — app/food.py, the usc_dining provider, the estimator, the
console tools and the routes. Network and model calls are stubbed; menus and
the location listing come from fixtures captured 2026-10-14."""

import asyncio
import json
from datetime import date, timedelta
from pathlib import Path

import pytest

from app import console_tools as ct
from app import db, food, food_estimate
from app.providers import usc_dining

FIX = Path(__file__).parent / "fixtures" / "food"


def call(t, **kw):
    r = asyncio.run(t.handler(kw))
    return r["content"][0]["text"], bool(r.get("is_error"))


def _setup(kg=70.0, **extra):
    food.set_profile(sex="male", birth_year=2006, height_cm=178, **extra)
    food.weigh(kg=kg)


def _exercise(day, value=1):
    with db.session() as con, con:
        hid = con.execute("INSERT INTO habits (name, created_at) VALUES"
                          " ('Exercise', '2026-01-01')").lastrowid
    db.checkin(hid, day, value)


# ---------- SR28 ----------

def test_sr28_plain_words_find_the_plain_food():
    assert food.search_sr28("egg", 1)[0]["name"] == "Egg, whole, raw, fresh"
    assert food.search_sr28("potato cooked", 1)[0]["name"].startswith("Potatoes,")
    assert food.search_sr28("banana", 1)[0]["name"] == "Bananas, raw"


def test_sr28_a_missing_word_costs_but_doesnt_disqualify():
    """'collard greens' — SR28 says 'Collards'; 'greens' isn't in the name."""
    assert food.search_sr28("collard greens cooked", 1)[0]["name"].startswith("Collards")


def test_sr28_synonyms():
    assert "oats" in food.search_sr28("oatmeal", 1)[0]["name"].lower()


# ---------- log and totals ----------

def test_log_sr_portion_scales_from_100g(dbfile):
    e = food.log("sr:01123", portion="1 large", meal="breakfast")
    assert e["servings"] == pytest.approx(0.5)            # 50 g of a 100 g serving
    assert e["nutrients"]["protein"] == pytest.approx(6.28, abs=0.01)
    e2 = food.log("sr:01123", grams=150, meal="breakfast")
    assert e2["servings"] == pytest.approx(1.5)
    with db.session() as con:
        assert con.execute("SELECT COUNT(*) n FROM foods").fetchone()["n"] == 1


def test_unknown_portion_names_the_ones_it_has(dbfile):
    with pytest.raises(food.FoodError, match="1 large"):
        food.log("sr:01123", portion="1 bucket")


def test_totals_count_entries_missing_a_nutrient(dbfile):
    """A custom food with only macros must not read as 'zero vitamin C'."""
    f = food.create_custom("Orange Chicken", {"kcal": 490, "protein": 25},
                           venue="panda-express")
    food.log(f["ref"])
    food.log("sr:09040", portion="1 medium")              # banana: has vit C
    s = food.day_summary()
    assert s["missing"]["vit_c"] == 1
    assert s["totals"]["kcal"] == pytest.approx(490 + 105, abs=2)


def test_a_price_defaults_to_dining_dollars(dbfile):
    f = food.create_custom("Bowl", {"kcal": 600, "protein": 40}, venue="new-bowl-concept")
    assert food.log(f["ref"], price=13.5)["paid_with"] == "dining_dollars"
    assert food.log(f["ref"])["paid_with"] is None


def test_custom_food_is_one_row_per_venue_and_name(dbfile):
    a = food.create_custom("Orange Chicken", {"kcal": 490, "protein": 25},
                           venue="panda-express")
    b = food.create_custom("orange chicken", {"kcal": 500, "protein": 26},
                           venue="panda-express")
    assert a["id"] == b["id"] and b["nutrients"]["kcal"] == 500


def test_fix_food_marks_it_known_and_moves_past_entries(dbfile):
    f = food.create_custom("Wrap", {"kcal": 400, "protein": 20}, venue="seeds-marketplace")
    e = food.log(f["ref"])
    food.fix_food(f["id"], nutrients={"protein": 32})
    with db.session() as con:
        assert food.get_food(con, f["id"])["confidence"] == 1
        assert food._entry(con, e["id"])["nutrients"]["protein"] == 32


def test_delete_and_restore_keep_the_id(dbfile):
    e = food.log("sr:01123", portion="1 large")
    gone = food.delete_entry(e["id"])
    assert food.day_summary()["entries"] == []
    assert food.restore_entry(gone)["id"] == e["id"]


def test_repeat_copies_one_meal(dbfile):
    y = (date.fromisoformat(db.day_key()) - timedelta(days=1)).isoformat()
    food.log("sr:01123", portion="1 large", meal="breakfast", day=y)
    food.log("sr:09040", portion="1 medium", meal="lunch", day=y)
    out = food.repeat(y, meal="breakfast")
    assert [e["meal"] for e in out] == ["breakfast"]
    assert out[0]["day"] == db.day_key()


# ---------- targets ----------

def test_targets_wait_for_setup(dbfile):
    s = food.day_summary()
    assert s["targets"] is None
    assert s["setup_needed"] == ["sex", "birth_year", "height_cm", "weight"]


def test_training_day_carries_the_surplus_rest_day_doesnt(dbfile):
    _setup()
    today = db.day_key()
    rest = food.day_summary()["targets"]
    _exercise(today)
    train = food.day_summary()["targets"]
    assert not rest["training"] and train["training"]
    assert train["nutrients"]["kcal"]["target"] - rest["nutrients"]["kcal"]["target"] \
        == 300 + 300                                       # session burn + lean-bulk surplus
    assert rest["nutrients"]["kcal"]["target"] == rest["maintenance"]
    assert train["nutrients"]["protein"]["target"] == rest["nutrients"]["protein"]["target"] == 126


def test_unchecked_exercise_is_a_rest_day_even_with_gym_on_the_calendar(dbfile):
    _setup()
    today = db.day_key()
    with db.session() as con, con:
        con.execute("INSERT INTO events (day, event_id, calendar_id, summary,"
                    " start_at, end_at) VALUES (?,?,?,?,?,?)",
                    (today, "e1", "c", "Gym — push day", today + "T17:00",
                     today + "T18:00"))
        assert food.is_training_day(con, today)[0]          # the plan counts…
    _exercise(today, 0)
    with db.session() as con:
        assert not food.is_training_day(con, today)[0]      # …until the fact says no


def test_manual_target_wins(dbfile):
    _setup()
    food.set_target("protein", 160)
    assert food.day_summary()["targets"]["nutrients"]["protein"]["target"] == 160


# ---------- weight trend ----------

def _weigh_series(per_week_kg, weeks=3, start=70.0):
    today = date.fromisoformat(db.day_key())
    for i in range(weeks * 7, -1, -3):
        d = (today - timedelta(days=i)).isoformat()
        food.weigh(kg=start + per_week_kg * (weeks * 7 - i) / 7, day=d)


def test_gaining_too_fast_moves_maintenance_down_once_a_week(dbfile):
    _setup()
    _weigh_series(0.5)                                      # ~3%/month, far over 0.5%
    s = food.day_summary()
    assert s["maintenance_adjusted"]["step"] == -100
    assert food.get_profile()["maint_adj"] == -100
    assert food.day_summary()["maintenance_adjusted"] is None   # not again this week


def test_flat_weight_moves_maintenance_up(dbfile):
    _setup()
    _weigh_series(0.0)
    assert food.day_summary()["maintenance_adjusted"]["step"] == 100


# ---------- closing the day → habit ----------

def test_close_checks_off_log_food_and_reopen_unchecks(dbfile):
    food.log("sr:01123", portion="1 large")
    food.close_day()
    h = db.find_habit("Log food")
    with db.session() as con:
        row = con.execute("SELECT * FROM habit_checkins WHERE habit_id=?",
                          (h["id"],)).fetchone()
    assert row["value"] == 1 and "kcal" in row["note"]
    s = food.close_day(reopen=True)
    assert s["closed"] is None
    with db.session() as con:
        assert con.execute("SELECT value FROM habit_checkins WHERE habit_id=?",
                           (h["id"],)).fetchone()["value"] == 0


def test_close_refuses_an_empty_day(dbfile):
    with pytest.raises(food.FoodError, match="nothing logged"):
        food.close_day()


# ---------- Dining Dollars pace ----------

def test_dd_pace(dbfile):
    end = (date.fromisoformat(db.day_key()) + timedelta(days=9)).isoformat()
    food.set_profile(dd_balance=500, semester_end=end)
    f = food.create_custom("Bowl", {"kcal": 600, "protein": 40}, venue="new-bowl-concept")
    food.log(f["ref"], price=100)
    d = food.day_summary()["dd"]
    assert d["left"] == 400 and d["days_left"] == 10
    assert d["allowance_per_day"] == 40
    # $14.29/day over the last 7 against a $40/day allowance: the dollars
    # will be forfeited, which is what "under" warns about.
    assert d["per_day_7d"] == pytest.approx(100 / 7, abs=0.01) and d["pace"] == "under"


# ---------- usc_dining provider ----------

def test_parse_menu_fixture():
    rows = usc_dining.parse_menu(json.loads((FIX / "menu_evk_20261014.json").read_text()))
    assert len(rows) == 81
    egg = next(r for r in rows if r["item"] == "Scrambled Eggs")
    assert egg["meal"] == "Breakfast" and egg["allergens"] == ["eggs"]
    assert egg["item_key"] == "scrambled egg"


def test_parse_listing_flags_alcohol_venues_off_dining_dollars():
    v = {r["slug"]: r for r in usc_dining.parse_listing(
        (FIX / "locations_upc.html").read_text())}
    assert v["everybodys-kitchen"]["kind"] == "dining_hall"
    assert v["panda-express"]["dining_dollars"] == 1
    assert v["mckays"]["kind"] == "restaurant" and v["mckays"]["dining_dollars"] == 0
    assert v["trojan-grounds-featuring-illy"]["shifts"][0][0] == 1791811800000


def _stub_menus(monkeypatch, payload=None):
    payload = payload or json.loads((FIX / "menu_evk_20261014.json").read_text())
    calls = []

    def fetch(key, d):
        calls.append((key, d))
        return payload if key == "evk" else {"meals": []}
    monkeypatch.setattr(usc_dining, "_fetch_menu", fetch)
    return calls


def test_refresh_menus_is_cached_and_records_empty_days(dbfile, monkeypatch):
    calls = _stub_menus(monkeypatch)
    d = date(2026, 10, 14)
    assert usc_dining.refresh_menus([d]) == {"hall_days": 3, "items": 81}
    assert usc_dining.refresh_menus([d])["hall_days"] == 0
    assert len(calls) == 3


def test_venue_refresh_keeps_a_hand_corrected_dd_flag(dbfile, monkeypatch):
    page = (FIX / "locations_upc.html").read_text()
    monkeypatch.setattr(usc_dining, "_fetch_listing",
                        lambda p: page if "upc" in p else "")
    usc_dining.refresh_venues(force=True)
    with db.session() as con, con:
        con.execute("UPDATE food_venues SET dining_dollars=0 WHERE slug='panda-express'")
    usc_dining.refresh_venues(force=True)
    with db.session() as con:
        assert con.execute("SELECT dining_dollars FROM food_venues WHERE"
                           " slug='panda-express'").fetchone()[0] == 0


# ---------- estimator ----------

def _decomp(key, comps, kcal_guess=None):
    return {"key": key, "portion": "1 plate", "grams": 300,
            "kcal_guess": kcal_guess, "components": comps}


def test_estimate_composes_picks_and_falls_back_to_model_macros(dbfile, monkeypatch):
    _stub_menus(monkeypatch)
    usc_dining.refresh_menus([date(2026, 10, 14)])
    with db.session() as con:
        todo = [t for t in food_estimate.pending(con, 500)
                if t["item"] in ("Scrambled Eggs", "Plant Based Breakfast Sausage Patty")]
    eggs = food.search_sr28("egg scrambled", food_estimate.CANDIDATES)[0]["id"]

    def ask(prompt, system):
        if system is food_estimate.DECOMPOSE_SYSTEM:
            return [_decomp("scrambled egg", [{"food": "egg scrambled", "g": 100}], 150),
                    _decomp("plant based breakfast sausage patty",
                            [{"food": "plant sausage patty", "g": 50, "kcal": 110,
                              "protein": 9, "carbs": 4, "fat": 7}], 110)]
        return [{"key": "scrambled egg", "picks": [eggs]},
                {"key": "plant based breakfast sausage patty", "picks": [None]}]
    monkeypatch.setattr(food_estimate, "_ask", ask)
    monkeypatch.setattr(food_estimate.agent, "raise_if_blocked", lambda: None)
    assert food_estimate.estimate_batch(todo) == 2
    with db.session() as con:
        rows = {r["source_id"]: food._food_row(r) for r in con.execute(
            "SELECT * FROM foods WHERE source='usc_estimate'")}
    egg = rows["scrambled egg"]
    assert egg["confidence"] == 0.6 and egg["nutrients"]["vit_d"] > 0
    patty = rows["plant based breakfast sausage patty"]
    assert patty["confidence"] == 0.5 and patty["nutrients"]["kcal"] == 110
    assert "vit_d" not in patty["nutrients"]                 # unknown, not zero
    assert patty["prefs"] == ["halal-ingredients", "vegan"]


def test_estimate_not_a_dish_is_final_and_a_pick_outside_candidates_is_ignored(
        dbfile, monkeypatch):
    _stub_menus(monkeypatch)
    usc_dining.refresh_menus([date(2026, 10, 14)])
    with db.session() as con:
        todo = [t for t in food_estimate.pending(con, 500)
                if t["item"] in ("MADE TO ORDER OMELETES", "Scrambled Eggs")]

    def ask(prompt, system):
        if system is food_estimate.DECOMPOSE_SYSTEM:
            return [_decomp("made to order omelete", []),
                    _decomp("scrambled egg", [{"food": "egg scrambled", "g": 100}])]
        return [{"key": "scrambled egg", "picks": ["sr:99999"]}]   # not offered
    monkeypatch.setattr(food_estimate, "_ask", ask)
    monkeypatch.setattr(food_estimate.agent, "raise_if_blocked", lambda: None)
    assert food_estimate.estimate_batch(todo) == 0
    with db.session() as con:
        errs = {r["item_key"]: dict(r) for r in con.execute("SELECT * FROM food_estimates")}
        assert errs["made to order omelete"]["attempts"] == food_estimate.MAX_ATTEMPTS
        assert "matched" in errs["scrambled egg"]["error"]
        left = {t["item_key"] for t in food_estimate.pending(con, 500)}
    assert "made to order omelete" not in left and "scrambled egg" not in left  # retry later


# ---------- suggestions ----------

def _hall_with(dbfile, items, meal="Lunch", venue="everybodys-kitchen"):
    day = db.day_key()
    with db.session() as con, con:
        con.execute("INSERT OR IGNORE INTO food_venues (slug, name, kind, campus,"
                    " dining_dollars, refreshed_at) VALUES (?,?,?,?,1,?)",
                    (venue, venue, "dining_hall", "upc", "2026-10-14T00:00"))
        for name, n in items.items():
            key = food.norm_name(name)
            con.execute("INSERT INTO usc_menu_items (date, venue, meal, station,"
                        " item, item_key) VALUES (?,?,?,?,?,?)",
                        (day, venue, meal, "Grill", name, key))
            food.upsert_food(con, name=name, source="usc_estimate", source_id=key,
                             nutrients=n, confidence=0.6)


def test_suggest_puts_protein_on_the_plate(dbfile):
    _setup()
    _hall_with(dbfile, {
        "Grilled Chicken": {"kcal": 280, "protein": 50, "sodium": 400, "fat": 6},
        "Fries": {"kcal": 450, "protein": 5, "sodium": 600, "fat": 22},
        "Broccoli": {"kcal": 50, "protein": 4, "fiber": 5, "vit_c": 90},
        "Cookie": {"kcal": 400, "protein": 4, "sodium": 300, "fat": 20},
    })
    out = food.suggest(meal="lunch", at=False)
    names = {i["name"] for i in out["plates"][0]["items"]}
    assert "Grilled Chicken" in names and "Cookie" not in names
    assert out["plates"][0]["kind"] == "dining_hall"


def test_suggest_respects_the_sodium_cap(dbfile):
    _setup()
    f = food.create_custom("Ramen", {"kcal": 300, "protein": 10, "sodium": 2200})
    food.log(f["ref"], meal="breakfast")
    _hall_with(dbfile, {
        "Salty Chicken": {"kcal": 400, "protein": 45, "sodium": 1800},
        "Plain Chicken": {"kcal": 400, "protein": 42, "sodium": 150},
    })
    plate = food.suggest(meal="lunch", at=False)["plates"][0]
    assert [i["name"] for i in plate["items"]] == ["Plain Chicken"]


def test_suggest_plans_a_meal_not_the_whole_day(dbfile):
    """The first version, on an empty day, put three entrées and ~117 g of
    protein on one lunch plate, because the *day* still needed it."""
    _setup()
    _hall_with(dbfile, {
        "Steak": {"kcal": 350, "protein": 45, "sodium": 300},
        "Roast Chicken": {"kcal": 330, "protein": 42, "sodium": 350},
        "Pork Loin": {"kcal": 340, "protein": 40, "sodium": 380},
        "Rice": {"kcal": 240, "protein": 5, "sodium": 5},
        "Broccoli": {"kcal": 50, "protein": 4, "fiber": 5, "potassium": 450},
    })
    plate = food.suggest(meal="lunch", at=False)["plates"][0]
    mains = [i for i in plate["items"] if i["name"] in ("Steak", "Roast Chicken", "Pork Loin")]
    assert len(mains) == 1
    assert plate["adds"]["protein"] < 70


def test_suggest_without_setup_says_why(dbfile):
    out = food.suggest(meal="lunch")
    assert out["plates"] == [] and "set up" in out["why_empty"]


# ---------- console tools ----------

def test_console_log_flow(dbfile):
    _setup(dd_balance=1000)
    text, err = call(ct.food_create, name="Orange Chicken", venue="panda-express",
                     serving_desc="1 entrée", kcal=490, protein=25, published=True)
    assert not err and text.startswith("created f")
    ref = text.split()[1]
    text, err = call(ct.food_log, food=ref, price=6.5, meal="lunch")
    assert not err and "logged #" in text and "490/" in text and "rest day" in text
    text, err = call(ct.food_search, query="orange chicken")
    assert text.splitlines()[0].startswith(ref)            # your own food ranks first
    text, err = call(ct.food_day)
    assert "Dining Dollars: $993.50 left" in text


def test_console_logs_a_meal_in_one_call(dbfile):
    """A meal's foods as `items`: one call, every entry, the day line once,
    and a guessed id among them refused without losing the others."""
    _setup(dd_balance=1000)
    a = call(ct.food_create, name="Rice", kcal=200, protein=4, published=True)[0].split()[1]
    b = call(ct.food_create, name="Chicken", kcal=300, protein=40, published=True)[0].split()[1]
    text, err = call(ct.food_log, meal="dinner", items=[
        {"food": a}, {"food": b, "servings": 2}, {"food": "broccoli"}])
    assert not err, text
    assert text.count("logged #") == 2 and text.count("\nday ") == 1
    assert "FAILED: broccoli:" in text and "food_search" in text
    assert "dinner: 2 × Chicken" in text


def test_console_log_refuses_a_guessed_id(dbfile):
    text, err = call(ct.food_log, food="orange chicken")
    assert err and "food_search" in text


def test_console_profile_and_weigh(dbfile):
    text, err = call(ct.food_profile, sex="male", birth_year=2006, height_cm=178)
    assert not err and "setup missing: weight" in text
    text, err = call(ct.food_weigh, lb=154)
    assert not err and "69.85 kg" in text


# ---------- café seed ----------

def test_seed_published_is_idempotent_and_never_touches_a_correction(dbfile):
    from app import food_seed
    n = food_seed.seed_published()
    assert n > 300
    assert food_seed.seed_published() == 0
    with db.session() as con:
        r = con.execute("SELECT * FROM foods WHERE source_id=?",
                        ("panda-express|orange chicken",)).fetchone()
    assert json.loads(r["nutrients"])["protein"] == 19 and r["confidence"] == 1.0
    food.fix_food(r["id"], nutrients={"protein": 26})
    food_seed.seed_published()
    with db.session() as con:
        f = food.get_food(con, r["id"])
    assert f["nutrients"]["protein"] == 26


def test_fill_micros_adds_only_micros_scaled_and_survives_reseed(dbfile, monkeypatch):
    """Panda publishes macros only: estimate the vitamins and minerals, never
    touch the label's numbers, and don't redo the estimate every cycle."""
    from app import food_seed
    food_seed.seed_published()
    calls = []

    def fake(items):
        calls.extend(i["key"] for i in items)
        return {i["key"]: {"ok": True, "serving_g": 88.0, "confidence": 0.6,
                           "serving_desc": "x", "detail": {"components": []},
                           "nutrients": {"kcal": 999, "protein": 99, "iron": 2.0,
                                         "vit_c": 10.0}} for i in items}
    monkeypatch.setattr(food_seed.food_estimate, "estimate_dishes", fake)
    assert food_seed.fill_micros() > 0
    assert "panda-express|coca cola 12 oz" not in calls            # drinks skipped
    with db.session() as con:
        oc = food.get_food(con, con.execute(
            "SELECT id FROM foods WHERE source_id='panda-express|orange chicken'").fetchone()[0])
    assert oc["nutrients"]["kcal"] == 560 and oc["nutrients"]["protein"] == 19
    assert oc["nutrients"]["iron"] == pytest.approx(2.0 * 175.8 / 88, abs=0.01)
    assert "estimated" in oc["detail"]["micros"]
    food_seed.seed_published()
    with db.session() as con:
        again = food.get_food(con, oc["id"])
    assert again["nutrients"]["iron"] == oc["nutrients"]["iron"]
    calls.clear()
    assert food_seed.fill_micros() == 0 and calls == []


def test_food_create_merges_with_the_seeded_row(dbfile):
    """Kestrel creating 'Orange Chicken' at Panda must land on the seeded row,
    not beside it. Panda's own label (confidence 1) outranks the model's
    recall (0.95), so the label's numbers stay; the user's typed numbers
    (confidence 1) land, and from then on the seed leaves them alone."""
    from app import food_seed
    food_seed.seed_published()
    f = food.create_custom("Orange Chicken", {"kcal": 500, "protein": 26},
                           venue="panda-express", confidence=0.95)
    assert f["kept"] and f["nutrients"]["protein"] == 19     # Panda's label
    with db.session() as con:
        n = con.execute("SELECT COUNT(*) FROM foods WHERE venue='panda-express'"
                        " AND name LIKE 'orange chicken'").fetchone()[0]
    assert n == 1
    food.create_custom("Orange Chicken", {"kcal": 510, "protein": 26},
                       venue="panda-express", confidence=1.0)
    food_seed.seed_published()
    with db.session() as con:
        assert food.get_food(con, f["id"])["nutrients"]["kcal"] == 510


def test_an_estimate_never_overwrites_a_correction(dbfile):
    f = food.create_custom("Wrap", {"kcal": 400, "protein": 20}, venue="seeds-marketplace")
    food.fix_food(f["id"], nutrients={"kcal": 520})
    again = food.create_custom("wrap", {"kcal": 380, "protein": 18},
                               venue="seeds-marketplace", confidence=0.8)
    assert again["kept"] and again["id"] == f["id"] and again["nutrients"]["kcal"] == 520


def test_a_kcal_correction_keeps_the_macros_consistent(dbfile):
    """The BBQ chicken of 10-15: kcal fixed to 230 left 24 g fat and 17 g
    carbs beside it (400 kcal of macros). The unstated macros now share what
    the stated ones leave, and the history is kept."""
    f = food.create_custom("BBQ chicken", {"kcal": 400, "protein": 32, "fat": 23.8,
                                           "carbs": 16.5, "sat_fat": 9, "sugar": 13,
                                           "sodium": 800})
    g = food.fix_food(f["id"], nutrients={"kcal": 230, "protein": 30})
    n = g["nutrients"]
    assert abs(4 * n["protein"] + 4 * n["carbs"] + 9 * n["fat"] - 230) < 1
    assert n["sat_fat"] <= n["fat"] and n["sugar"] <= n["carbs"]
    assert n["sodium"] == pytest.approx(800 * 230 / 400)
    (c,) = g["detail"]["corrections"]
    assert c["before"]["kcal"] == 400 and c["given"] == {"kcal": 230, "protein": 30}


def test_relogging_uses_the_usual_amount(dbfile):
    f = food.create_custom("Chow Mein", {"kcal": 520, "protein": 14}, venue="panda-express")
    e = food.log(f["ref"], day="2026-01-01")
    food.update_entry(e["id"], servings=0.5)
    again = food.log(f["ref"])
    assert again["servings"] == 0.5 and again["servings_from"] == "last time"
    assert food.log(f["ref"], servings=2)["servings"] == 2


def test_recents_and_yesterdays_meals(dbfile):
    f = food.create_custom("Bowl", {"kcal": 600, "protein": 40}, venue="taco-taco")
    y = (date.fromisoformat(db.day_key()) - timedelta(days=1)).isoformat()
    food.log(f["ref"], servings=1.5, day=y, meal="lunch")
    (r,) = food.recents(meal="lunch")
    assert r["ref"] == f["ref"] and r["servings"] == 1.5 and r["kcal"] == 900
    (m,) = food.repeatable_meals()
    assert m["meal"] == "lunch" and m["names"] == ["Bowl"]
    text, err = call(ct.food_search)
    assert not err and "usually 1.5 × 1 serving" in text and f"same_as={y}" in text
    text, err = call(ct.food_log, same_as=y, meal="lunch")
    assert not err and "logged #" in text


def test_ranges_follow_confidence_and_portion(dbfile):
    exact = food.create_custom("Label bar", {"kcal": 200, "protein": 20}, confidence=1.0)
    rough = food.create_custom("Mystery bowl", {"kcal": 700, "protein": 30},
                               serving_desc="1 bowl", confidence=0.6)
    a = food.log(exact["ref"])
    b = food.log(rough["ref"], amount_guessed=True)
    assert a["range"] is None and a["trust"] == "exact"
    assert b["trust"] == "rough" and b["range"]["kcal"][0] < 700 < b["range"]["kcal"][1]
    s = food.day_summary()
    lo, hi = s["range"]["kcal"]
    assert lo < 900 < hi and hi - lo < b["range"]["kcal"][1] - b["range"]["kcal"][0] + 1


def test_portion_question_only_for_guessed_amorphous_dishes(dbfile):
    bowl = food.create_custom("Teriyaki Chicken Bowl", {"kcal": 680, "protein": 45},
                              serving_desc="1 bowl (about 450 g)", confidence=0.8)
    can = food.create_custom("Monster Ultra", {"kcal": 10, "protein": 0},
                             serving_desc="1 can (16 fl oz)", confidence=0.8)
    cookie = food.create_custom("Sugar Cookie", {"kcal": 250, "protein": 2},
                                serving_desc="1 cookie", confidence=0.6)
    assert food.log(bowl["ref"], amount_guessed=True)["ask"]
    assert not food.log(bowl["ref"], servings=1)["ask"]          # stated amount
    assert not food.log(can["ref"], amount_guessed=True)["ask"]
    assert not food.log(cookie["ref"], amount_guessed=True)["ask"]
    e = food.log(bowl["ref"], servings=1, amount_guessed=True)
    (q,) = [q for q in food.day_summary()["questions"] if q["id"] == e["id"]]
    assert [o["x"] for o in q["options"]] == [0.5, 0.75, 1.0, 1.5, 2.0]
    done = food.answer_portion(e["id"], 1.5)
    assert done["servings"] == 1.5 and not done["ask"]
    assert done["portion_conf"] == food.PORTION_ANSWERED


def test_weigh_checks_the_habit_and_targets_use_the_7_day_average(dbfile):
    with db.session() as con, con:
        con.execute("INSERT INTO habits (name, created_at) VALUES ('Weigh in', '2026-01-01')")
    food.set_profile(sex="male", birth_year=2006, height_cm=178)
    today = date.fromisoformat(db.day_key())
    food.weigh(kg=70, day=(today - timedelta(days=2)).isoformat())
    w = food.weigh(kg=72)
    assert w["avg7_kg"] == 71
    with db.session() as con:
        r = con.execute("SELECT c.value, c.source FROM habit_checkins c JOIN habits h"
                        " ON h.id=c.habit_id WHERE h.name='Weigh in' AND c.day=?",
                        (db.day_key(),)).fetchone()
        assert r["value"] == 1 and r["source"] == "auto"
        assert food.targets(con, db.day_key())["kg"] == 71
    assert food.day_summary()["weight"]["avg7_kg"] == 71


def test_a_weeks_old_weigh_in_is_not_a_7_day_average(dbfile):
    food.weigh(kg=70, day=(date.fromisoformat(db.day_key()) - timedelta(days=20)).isoformat())
    w = food.day_summary()["weight"]
    assert w["kg"] == 70 and w["avg7_kg"] is None


def _calibration_history(kcal_per_day, kg_per_week, days=28):
    """`days` fully logged days at kcal_per_day and weekly weigh-ins on a
    straight trend, ending yesterday."""
    food.set_profile(sex="male", birth_year=2006, height_cm=178, activity="light")
    f = food.create_custom("Day of food", {"kcal": kcal_per_day / 2, "protein": 60},
                           confidence=1.0)
    today = date.fromisoformat(db.day_key())
    for i in range(1, days + 1):
        d = (today - timedelta(days=i)).isoformat()
        food.log(f["ref"], servings=1, day=d, meal="lunch")
        food.log(f["ref"], servings=1, day=d, meal="dinner")
    for i in range(0, days + 1, 4):
        food.weigh(kg=70 - kg_per_week * i / 7,
                   day=(today - timedelta(days=i)).isoformat())


def test_calibration_says_logging_runs_low_and_feeds_maintenance(dbfile):
    """Logging 2000 kcal/day while gaining 0.25 kg/week means the body got
    ~2275: logging runs low (or the burn is high). Maintenance follows the
    scale, in the units he logs in."""
    _calibration_history(2000, 0.25)
    with db.session() as con:
        c = food.calibration(con, db.day_key())
        tg = food.targets(con, db.day_key())
    assert c["ready"] and c["logged_days"] == 28
    assert c["observed_tdee"] == pytest.approx(2000 - 0.25 / 7 * 7700, abs=15)
    # Formula (light, 70 kg, 178 cm, age 20) ≈ 2400 > observed ≈ 1725.
    assert c["verdict"] == "high" and c["gap_kcal"] < -150
    assert tg["maintenance_from"] == "scale"
    assert tg["maintenance"] == c["maint_rest"] and tg["maintenance"] < tg["formula_maintenance"]
    line = food.calibration_line(c)
    assert "runs ~" in line and "high" in line


def test_calibration_waits_for_enough_data(dbfile):
    food.set_profile(sex="male", birth_year=2006, height_cm=178)
    food.weigh(kg=70)
    with db.session() as con:
        c = food.calibration(con, db.day_key())
        tg = food.targets(con, db.day_key())
    assert not c["ready"] and len(c["need"]) == 2
    assert tg["maintenance_from"] == "formula"
    assert "needs" in food.calibration_line(c)


def test_dietitian_handouts_are_seeded_exact(dbfile):
    from app import food_seed
    food_seed.seed_published()
    hits = {h["name"]: h for h in food.search("pb chocolate acai")}
    big = hits["PB Chocolate Acai Cup (24 oz)"]
    assert big["nutrients"]["kcal"] == 986 and big["confidence"] == 1.0
    assert "dietitian" in big["detail"]["origin"]
    text, _ = call(ct.food_search, query="chicken burrito", venue="taco-taco")
    assert "published: USC Hospitality dietitian handout" in text
    assert "from memory" not in call(ct.food_search, query="orange chicken")[0]


def test_seed_estimates_once_per_name_across_venues(dbfile, monkeypatch):
    from app import food_seed
    asked = []

    def fake(items):
        asked.extend(i["item"] for i in items)
        return {i["key"]: {"ok": True, "nutrients": {"kcal": 400, "protein": 30},
                           "serving_desc": "1 bowl", "serving_g": 350,
                           "confidence": 0.6, "detail": {"components": []}}
                for i in items}
    monkeypatch.setattr(food_seed.food_estimate, "estimate_dishes", fake)
    food_seed.seed_estimates()
    assert asked.count("Spicy Chicken Quinoa Bowl") == 1
    with db.session() as con:
        rows = [food._food_row(r) for r in con.execute(
            "SELECT * FROM foods WHERE name='Spicy Chicken Quinoa Bowl'")]
    assert {r["venue"] for r in rows} == {"seeds-marketplace", "law-school-cafe"}
    assert all("barcode" in r["detail"]["packaged"] for r in rows)
    asked.clear()
    food_seed.seed_estimates()
    assert asked == []


def test_seed_failure_is_not_reasked_every_cycle(dbfile, monkeypatch):
    from app import food_seed
    calls = []

    def fail(items):
        calls.append(len(items))
        return {i["key"]: {"ok": False, "error": "model skipped the item", "final": False}
                for i in items}
    monkeypatch.setattr(food_seed.food_estimate, "estimate_dishes", fail)
    food_seed.seed_estimates()
    first = sum(calls)
    food_seed.seed_estimates()
    assert first > 0 and sum(calls) == first


# ---------- barcode ----------

def test_barcode_converts_off_units_and_caches(dbfile, monkeypatch):
    from app.providers import openfoodfacts
    calls = []

    def fetch(code):
        calls.append(code)
        return {"status": 1, "product": {
            "product_name": "Classic Potato Chips", "brands": "Lay's",
            "serving_size": "1 portion (28.3 g)", "serving_quantity": 28.3,
            "nutriments": {"energy-kcal_100g": 565.37, "proteins_100g": 7.07,
                           "sodium_100g": 0.4947, "calcium_100g": 0.0353,
                           "vitamin-c_100g": 0.0212}}}
    monkeypatch.setattr(openfoodfacts, "_fetch", fetch)
    f = food.from_barcode("0284 0009 0858")
    assert f["name"] == "Lay's Classic Potato Chips" and f["source"] == "off"
    assert f["nutrients"]["kcal"] == pytest.approx(160, abs=0.5)      # per 28.3 g
    assert f["nutrients"]["sodium"] == pytest.approx(140, abs=0.5)    # g → mg
    assert f["nutrients"]["vit_c"] == pytest.approx(6.0, abs=0.1)
    assert food.from_barcode("028400090858")["id"] == f["id"]
    assert calls == ["028400090858"]                                   # second scan: no fetch


def test_unknown_barcode_says_to_read_the_label(dbfile, monkeypatch):
    from app.providers import openfoodfacts
    monkeypatch.setattr(openfoodfacts, "_fetch", lambda c: {"status": 0})
    with pytest.raises(food.FoodError, match="food_create"):
        food.from_barcode("12345678")


# ---------- routes ----------

def test_routes(router_client, monkeypatch):
    from app.routes import food as food_routes
    _stub_menus(monkeypatch)
    monkeypatch.setattr(usc_dining, "_fetch_listing", lambda p: "")
    monkeypatch.setattr(food_estimate, "run_pending_async", lambda: None)
    c = router_client(food_routes.router)
    r = c.post("/api/food/log", json={"food": "sr:01123", "portion": "1 large"})
    assert r.status_code == 200
    assert c.get("/api/food/day").json()["entries"][0]["name"].startswith("Egg")
    assert c.post("/api/food/log", json={"food": "nope"}).status_code == 400
    m = c.get("/api/food/menus", params={"day": "2026-10-14"}).json()
    assert m["halls"][0]["meals"][0]["meal"] == "Breakfast" and m["estimating"]
    assert c.post("/api/food/close", json={}).json()["closed"]
