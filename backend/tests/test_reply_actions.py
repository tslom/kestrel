"""Short of replying: remind (a task you asked for) or draft (text you copy). Suggestion-only, nothing sends."""

from datetime import datetime

import pytest

from app import db, people, reply_draft


def _req(last_at="2026-03-10T09:00:00", **kw):
    return people.Reminder(kind="message", key="+15551212", last_at=last_at,
                           name="Felix", preview="you around sunday?", **kw)


def test_remind_creates_a_task_due_today(con):
    out = people.post_remind(_req())
    assert out["created"] is True
    task = out["task"]
    assert task["title"] == "Reply to Felix"
    assert task["due"] == db.day_key()          # never date.today() (CLAUDE.md)
    assert task["owed_to"] == "Felix"
    assert "you around sunday?" in task["note"]
    assert task["source_kind"] == "waiting"


def test_remind_is_idempotent_for_one_message(con):
    people.post_remind(_req())
    again = people.post_remind(_req())
    assert again["created"] is False
    n = con.execute("SELECT COUNT(*) c FROM tasks WHERE source_kind='waiting'").fetchone()["c"]
    assert n == 1


def test_a_newer_message_earns_a_new_reminder(con):
    """source_ref carries the message timestamp: person-keyed dedupe would block reminders forever (it matches done and dropped tasks)."""
    people.post_remind(_req(last_at="2026-03-10T09:00:00"))
    with db.session() as c:
        c.execute("UPDATE tasks SET status='done' WHERE source_kind='waiting'")
    out = people.post_remind(_req(last_at="2026-03-14T18:30:00"))
    assert out["created"] is True


def test_draft_reads_the_thread_oldest_first(con):
    now = datetime.now().isoformat(timespec="seconds")
    with con:
        for i, (mine, text) in enumerate([(0, "yo"), (1, "hey"), (0, "sunday?")]):
            con.execute(
                "INSERT INTO messages (guid, day, ts, chat_guid, handle,"
                " contact_name, is_from_me, text, refreshed_at, is_reaction)"
                " VALUES (?,?,?,?,?,?,?,?,?,0)",
                (f"g{i}", "2026-03-10", f"2026-03-10T09:0{i}:00", "c1",
                 None if mine else "5551212", None, mine, text, now))
    turns = reply_draft.message_thread(con, "c1", 20)
    assert [t["who"] for t in turns] == ["them", "me", "them"]
    assert [t["text"] for t in turns] == ["yo", "hey", "sunday?"]


def test_draft_with_no_thread_asks_the_model_nothing(con, monkeypatch):
    """An empty thread returns no drafts and makes no model call."""
    def boom(*a, **kw):
        raise AssertionError("called the model with no context")
    monkeypatch.setattr(reply_draft.agent, "ask", boom)
    out = reply_draft.drafts_for(kind="message", name="Felix",
                                 chat_guid="nonexistent", limit=20, count=3)
    assert out == {"drafts": [], "turns": 0}


def test_draft_parses_plain_and_fenced_arrays():
    assert reply_draft._parse('["sure", "cant sunday"]') == ["sure", "cant sunday"]
    assert reply_draft._parse('```json\n["yep"]\n```') == ["yep"]
    # a stray {"reply": ...} shape instead of bare strings
    assert reply_draft._parse('[{"reply": "on my way"}]') == ["on my way"]
    with pytest.raises(ValueError):
        reply_draft._parse("I'd suggest replying warmly.")
