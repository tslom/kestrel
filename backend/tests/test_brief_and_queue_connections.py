"""Connection scoping: the mail queue (one per request) and the brief worker (none held across the model call)."""

from app import brief, db
from app.routes import email as email_route


def test_the_queue_renders_a_card(dbfile):
    day = db.day_key()
    with db.session() as con, con:
        con.execute(
            "INSERT INTO emails (msg_id, day, received_at, from_addr, subject,"
            " refreshed_at, in_inbox) VALUES ('m1',?,?,?,?,?,1)",
            (day, f"{day}T09:00:00", "a@b.c", "hi", f"{day}T09:00:00"))
    out = email_route.get_queue()
    assert out["count"] == 1 and out["cards"][0]["attachments"] == []


def test_the_brief_writes_ready_then_error(dbfile, monkeypatch):
    day = db.day_key()
    monkeypatch.setattr(brief, "_gather", lambda d: {"day": d, "context": {"a": 1}, "failed": {}})

    async def ask(*a, **k):
        return "BODY"
    monkeypatch.setattr(brief.agent, "ask", ask)
    assert brief._claim(day, False)
    assert not brief._claim(day, False)
    brief._generate(day)
    assert brief._row(day)["status"] == "ready" and brief._row(day)["body"] == "BODY"

    async def bad(*a, **k):
        raise RuntimeError("boom")
    monkeypatch.setattr(brief.agent, "ask", bad)
    assert brief._claim(day, True)
    brief._generate(day)
    r = brief._row(day)
    assert r["status"] == "error" and r["error"] == "boom"
