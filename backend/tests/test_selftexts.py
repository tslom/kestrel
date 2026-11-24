"""Texts to yourself: grouped into bursts, routed by the model, every write
logged with an undo (app/selftexts.py, app/activity.py)."""

import json
from datetime import datetime, timedelta

import pytest

from app import activity, agent, db, selftexts

ME = "+12025550000"


@pytest.fixture
def me(dbfile, monkeypatch):
    monkeypatch.setattr(selftexts, "MESSAGES_SELF_HANDLES", [ME])
    # Routing starts at "now" on first run; pin the start mark an hour back for texts seeded in the past.
    since = (datetime.now() - timedelta(hours=1)).astimezone().isoformat(timespec="seconds")
    with db.session() as con, con:
        con.execute("INSERT INTO sync_state (key, value) VALUES (?,?)",
                    (selftexts.SINCE_KEY, since))


_n = [0]


def text(body, mins_ago=10, *, from_me=1, reaction=0, handle="2025550000"):
    _n[0] += 1
    ts = (datetime.now() - timedelta(minutes=mins_ago)).astimezone()
    guid = f"g{_n[0]}"
    with db.session() as con, con:
        con.execute(
            "INSERT INTO messages (guid, day, ts, chat_guid, handle, is_from_me,"
            " text, refreshed_at, is_reaction) VALUES (?,?,?,?,?,?,?,?,?)",
            (guid, db.day_key(), ts.isoformat(timespec="seconds"), "any;-;" + ME,
             handle, from_me, body, "x", reaction))
    return guid


def reply(monkeypatch, *actions, seen=None):
    async def ask(prompt, **kw):
        if seen is not None:
            seen.append(prompt)
        return json.dumps({"actions": list(actions)})
    monkeypatch.setattr(selftexts.agent, "ask", ask)


def bursts():
    with db.session() as con:
        return [dict(r) for r in con.execute("SELECT * FROM selftext_bursts ORDER BY id")]


def log():
    return activity.recent()["items"][::-1]   # oldest first


# ---------- bursts ----------

def test_texts_close_together_are_one_burst(me):
    text("(photo)", 30)
    text("for the party friday", 28)
    text("Flip flops", 10)
    assert selftexts.collect() == 2
    with db.session() as con:
        sizes = [r[0] for r in con.execute(
            "SELECT COUNT(*) FROM selftext_messages GROUP BY burst_id ORDER BY burst_id")]
    assert sizes == [2, 1]


def test_the_newest_burst_waits_for_a_caption(me):
    text("(photo)", 1)
    assert selftexts.collect() == 0
    assert selftexts.collect(datetime.now().astimezone() + timedelta(minutes=5)) == 1


def test_reactions_inbound_copies_and_others_are_not_routed(me):
    text('Liked "buy milk"', reaction=1)
    text("buy milk", from_me=0)                 # the self-chat's inbound copy
    text("hey", handle="3105550000")            # a text to someone else
    text("before routing existed", 120)
    assert selftexts.collect() == 0


def test_a_burst_is_collected_once(me):
    text("Flip flops")
    assert selftexts.collect() == 1
    assert selftexts.collect() == 0


def test_no_handles_means_nothing_happens(dbfile, monkeypatch):
    monkeypatch.setattr(selftexts, "MESSAGES_SELF_HANDLES", [])
    text("Flip flops")
    assert selftexts.collect() == 0


# ---------- routing and filing ----------

def test_actions_are_applied_and_logged(me, monkeypatch):
    shopping = db.find_list("Shopping")   # seeded by the lists migration
    text("There will be blood add to watchlist")
    text("and flip flops", 9)
    reply(monkeypatch,
          {"kind": "watch", "title": "There Will Be Blood", "year": 2007, "type": "movie"},
          {"kind": "list_item", "list": "Shopping", "name": "Flip flops"})
    selftexts.collect()
    selftexts.process(bursts()[0])

    assert [w["title"] for w in db.list_rows("watchlist")] == ["There Will Be Blood"]
    assert [i["name"] for i in db.list_items_for(shopping["id"])] == ["Flip flops"]
    rows = log()
    assert [r["kind"] for r in rows] == ["watch", "list_item"]
    assert rows[1]["summary"] == "Added Flip flops to Shopping"
    assert all(r["source"] == "selftext" and r["undoable"] for r in rows)
    assert rows[0]["origin_text"]["text"] == "There will be blood add to watchlist\nand flip flops"
    assert bursts()[0]["status"] == "done"


def test_a_task_keeps_its_source_message(me, monkeypatch):
    g = text("reschedule level 3 training")
    reply(monkeypatch, {"kind": "task", "title": "Reschedule level 3 training",
                        "due": "2026-10-30"})
    selftexts.collect()
    selftexts.process(bursts()[0])
    (t,) = db.list_tasks()
    assert (t["title"], t["due"], t["source_kind"], t["source_ref"]) == \
        ("Reschedule level 3 training", "2026-10-30", "message", g)
    assert log()[0]["summary"] == "New task: Reschedule level 3 training — due Fri Oct 30"


def test_a_failed_action_is_kept_with_the_reason(me, monkeypatch):
    text("Buy eargasm")
    reply(monkeypatch, {"kind": "task", "title": ""})
    selftexts.collect()
    selftexts.process(bursts()[0])
    (row,) = log()
    assert row["kind"] == "keep" and "couldn't make it a task" in row["summary"]
    assert "watch" in row["redo_kinds"]


def test_a_model_failure_retries_then_gives_up_visibly(me, monkeypatch):
    async def ask(prompt, **kw):
        return "sorry, no"
    monkeypatch.setattr(selftexts.agent, "ask", ask)
    text("Stoddard falls")
    selftexts.collect()
    for _ in range(selftexts.MAX_ATTEMPTS - 1):
        assert selftexts.process(bursts()[0]) == []
        assert bursts()[0]["status"] == "pending"
    selftexts.process(bursts()[0])
    assert bursts()[0]["status"] == "failed"
    assert log()[0]["kind"] == "keep"


def test_a_usage_limit_leaves_the_burst_pending(me, monkeypatch):
    async def ask(prompt, **kw):
        raise agent.AgentError("usage limit reached")
    monkeypatch.setattr(selftexts.agent, "ask", ask)
    monkeypatch.setattr(selftexts.agent, "blocked", lambda: None)
    text("Stoddard falls")
    out = selftexts.run()
    assert out["skipped"] == "usage limit reached"
    assert bursts()[0]["status"] == "pending" and bursts()[0]["attempts"] == 0


def test_the_earlier_burst_is_context(me, monkeypatch):
    text("Buy eargasm", 25)
    reply(monkeypatch, {"kind": "list_item", "list": "Shopping", "name": "Eargasm"})
    selftexts.collect()
    selftexts.process(bursts()[0])
    text("And a white monster", 10)
    seen = []
    reply(monkeypatch, {"kind": "list_item", "list": "Shopping", "name": "White Monster"},
          seen=seen)
    selftexts.collect()
    selftexts.process(bursts()[1])
    assert "Buy eargasm" in seen[0] and "Added Eargasm to Shopping" in seen[0]


def test_attachment_text_is_fenced(me, monkeypatch):
    g = text("(photo)")
    with db.session() as con, con:
        aid = con.execute(
            "INSERT INTO attachments (source, source_ref, ord, day, filename, mime,"
            " is_sticker, seen_at) VALUES ('message', ?, 0, ?, 'IMG_1.heic',"
            " 'image/heic', 0, 'x')", (g, db.day_key())).lastrowid
        con.execute("INSERT INTO attachment_text (attachment_id, status, method, text,"
                    " chars, extracted_at) VALUES (?, 'ok', 'vision',"
                    " 'ACM speaker night Thu 7pm SGM 101', 30, 'x')", (aid,))
    seen = []
    reply(monkeypatch, {"kind": "keep"}, seen=seen)
    selftexts.collect()
    selftexts.process(bursts()[0])
    assert "<untrusted_attachment name='IMG_1.heic'>" in seen[0]
    assert "ACM speaker night" in seen[0]


def _photo(guid, status):
    with db.session() as con, con:
        aid = con.execute(
            "INSERT INTO attachments (source, source_ref, ord, day, filename, mime,"
            " is_sticker, seen_at) VALUES ('message', ?, 0, ?, 'IMG_2.heic',"
            " 'image/heic', 0, 'x')", (guid, db.day_key())).lastrowid
        con.execute("INSERT INTO attachment_text (attachment_id, status, method, text,"
                    " chars, extracted_at) VALUES (?, ?, NULL, '', 0, 'x')",
                    (aid, status))
    return aid


def test_a_failed_photo_read_is_retried_not_trusted(me, monkeypatch):
    """A cached vision "error" is retried, not filed as "couldn't be read"."""
    from app.providers import attachments
    aid = _photo(text("(photo)"), "error")
    calls = []

    def extract(i, force=False):
        calls.append((i, force))
        with db.session() as con, con:
            con.execute("UPDATE attachment_text SET status='ok', method='vision',"
                        " text='A plate of chicken tikka masala with rice'"
                        " WHERE attachment_id=?", (i,))
    monkeypatch.setattr(attachments, "extract", extract)
    seen = []
    reply(monkeypatch, {"kind": "keep"}, seen=seen)
    selftexts.collect()
    selftexts.process(bursts()[0])
    assert calls == [(aid, True)]
    assert "chicken tikka masala" in seen[0]


def test_an_unreadable_photo_waits_then_routes_on_the_last_try(me, monkeypatch):
    from app.providers import attachments
    _photo(text("(photo)"), "error")
    monkeypatch.setattr(attachments, "extract", lambda i, force=False: None)
    seen = []
    reply(monkeypatch, {"kind": "keep", "why": "photo unreadable"}, seen=seen)
    selftexts.collect()
    for n in range(1, selftexts.MAX_ATTEMPTS):
        assert selftexts.process(bursts()[0]) == []
        b = bursts()[0]
        assert b["status"] == "pending" and b["attempts"] == n
        assert "IMG_2.heic" in b["error"]
    assert seen == []                       # nothing routed while it might still read
    selftexts.process(bursts()[0])
    assert len(seen) == 1 and log()[0]["kind"] == "keep"


def test_food_is_estimated_logged_and_undone(me, monkeypatch):
    from app import food, food_estimate
    text("(photo)")
    reply(monkeypatch, {"kind": "food", "meal": "lunch",
                        "items": [{"item": "Chicken burrito", "where": "Chipotle"}]})
    monkeypatch.setattr(food_estimate, "estimate_dishes", lambda items: {
        "0": {"ok": True, "nutrients": {"kcal": 900, "protein": 50},
              "serving_desc": "1 burrito", "serving_g": 500, "confidence": 0.6}})
    selftexts.collect()
    selftexts.process(bursts()[0])
    with db.session() as con:
        (e,) = food.entries(con, db.day_key())
    assert (e["meal"], e["name"], e["source"]) == ("lunch", "Chicken burrito", "selftext")
    # A guessed amount of an estimated burrito: a range, and one question.
    assert e["portion_conf"] == food.PORTION_GUESSED and e["ask"]
    (row,) = log()
    assert row["summary"] == ("Logged lunch: Chicken burrito (540–1,260 kcal)"
                              " · how much Chicken burrito? answer on the Food card")
    activity.undo(row["id"])
    with db.session() as con:
        assert food.entries(con, db.day_key()) == []


def test_a_known_food_photo_logs_the_known_food_not_a_new_estimate(me, monkeypatch):
    """A corrected food returns at its corrected numbers and usual amount, nothing estimated."""
    from app import food, food_estimate
    f = food.create_custom("Teriyaki Salmon Bowl", {"kcal": 700, "protein": 38},
                           venue="Teriyaki Bowls")
    food.fix_food(f["id"], nutrients={"kcal": 820})
    food.log(f["ref"], servings=1.5, day="2026-01-01")
    text("(photo)")
    reply(monkeypatch, {"kind": "food", "meal": "lunch", "items": [
        {"item": "teriyaki salmon bowl", "where": "Teriyaki Bowls"}]})
    monkeypatch.setattr(food_estimate, "estimate_dishes",
                        lambda items: pytest.fail("estimated a known food"))
    selftexts.collect()
    selftexts.process(bursts()[0])
    with db.session() as con:
        (e,) = food.entries(con, db.day_key())
    assert e["food_id"] == f["id"] and e["servings"] == 1.5
    assert e["nutrients"]["kcal"] == 1230 and e["range"] is None and not e["ask"]


def test_a_label_photo_logs_exact_numbers_and_a_misread_one_is_refused(me, monkeypatch):
    from app import food, food_estimate
    text("(photo)")
    reply(monkeypatch, {"kind": "food", "meal": "snack", "items": [
        {"item": "Quest Bar Cookies & Cream", "label": {
            "serving": "1 bar (60 g)", "kcal": 200, "protein": 21, "carbs": 22,
            "fat": 8}},
        {"item": "Mystery chips", "label": {
            "serving": "1 bag", "kcal": 150, "protein": 2, "carbs": 150, "fat": 9}}]})
    monkeypatch.setattr(food_estimate, "estimate_dishes", lambda items: {
        it["key"]: {"ok": True, "nutrients": {"kcal": 160, "protein": 2},
                    "serving_desc": "1 bag", "serving_g": 28, "confidence": 0.6}
        for it in items})
    selftexts.collect()
    selftexts.process(bursts()[0])
    with db.session() as con:
        es = food.entries(con, db.day_key())
    by = {e["name"]: e for e in es}
    assert by["Quest Bar Cookies & Cream"]["confidence"] == 0.95
    assert by["Quest Bar Cookies & Cream"]["nutrients"]["kcal"] == 200
    assert by["Mystery chips"]["confidence"] == 0.6      # label failed Atwater → estimated


def test_an_event_goes_on_the_calendar_and_comes_off(me, monkeypatch):
    from app.routes import calendar as cal
    made, gone = [], []
    monkeypatch.setattr(cal, "create_event", lambda **kw: made.append(kw) or {
        "event_id": "ev1", "calendar_id": "primary", "summary": kw["summary"],
        "location": kw["location"]})
    monkeypatch.setattr(cal, "delete_event", lambda **kw: gone.append(kw))
    text("Chess Club GA tomorrow 7pm @ the Union")
    reply(monkeypatch, {"kind": "event", "summary": "Chess Club GA",
                        "start": "2026-10-31T19:00", "location": "the Union"})
    selftexts.collect()
    selftexts.process(bursts()[0])
    assert "attendees" not in made[0]
    (row,) = log()
    assert row["summary"] == "Added “Chess Club GA” to your calendar — Sat Oct 31, 7:00pm at the Union"
    activity.undo(row["id"])
    assert gone == [{"event_id": "ev1", "calendar_id": "primary"}]


def test_a_note_is_written_and_undo_removes_it(me, monkeypatch, tmp_path):
    monkeypatch.setattr(selftexts, "VAULT_PATH", tmp_path)
    monkeypatch.setattr(activity, "VAULT_PATH", tmp_path)
    text("Stoddard falls")
    reply(monkeypatch, {"kind": "note", "title": "Stoddard Falls",
                        "body": "A place to visit."})
    selftexts.collect()
    selftexts.process(bursts()[0])
    (row,) = log()
    path = tmp_path / row["ref"]["path"]
    assert path.parent.name == "06-Generated"
    assert "generated_by: selftext" in path.read_text()
    activity.undo(row["id"])
    assert not path.exists()


# ---------- redo ----------

def test_redo_undoes_and_refiles_as_the_chosen_kind(me, monkeypatch):
    text("Flip flops")
    reply(monkeypatch, {"kind": "list_item", "list": "Shopping", "name": "Flip flops"})
    selftexts.collect()
    selftexts.process(bursts()[0])
    (first,) = log()

    seen = []
    reply(monkeypatch, {"kind": "task", "title": "Buy flip flops"}, seen=seen)
    new = selftexts.redo(first["id"], "task")
    assert "filed as a task" in seen[0]
    assert new["kind"] == "task" and new["origin"] == first["origin"]
    assert activity.get(first["id"])["undone_at"]
    assert db.list_items_for(db.find_list("Shopping")["id"]) == []
    assert [t["title"] for t in db.list_tasks()] == ["Buy flip flops"]


def test_redo_refuses_a_kind_the_model_did_not_give(me, monkeypatch):
    text("Flip flops")
    reply(monkeypatch, {"kind": "keep"})
    selftexts.collect()
    selftexts.process(bursts()[0])
    (row,) = log()
    reply(monkeypatch, {"kind": "note", "title": "x"})
    with pytest.raises(ValueError):
        selftexts.redo(row["id"], "event")
