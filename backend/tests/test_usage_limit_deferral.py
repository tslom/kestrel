"""A spent usage window, from the callers of agent.ask: a refused model call is deferred work (not a failed
source, and stays pending), and callers skip expensive set-up while the breaker is open. Design: DESIGN.md → `app/agent.py`: one-shot model calls, breaker and budget gate."""

import time

import pytest

from app import agent, brain, brief, db
from app.providers import email, email_extract, email_triage, heartbeat, poller

LIMIT = "usage limit reached — resets 7:59 PM"


@pytest.fixture
def spent(monkeypatch):
    """The breaker open, as a 429 a minute ago would have left it."""
    monkeypatch.setattr(agent, "_tripped", (time.time() + 1800, LIMIT))


@pytest.fixture
def synced(monkeypatch):
    """email.refresh with the Gmail half stubbed to a successful sync."""
    monkeypatch.setattr(email, "access_token", lambda: "tok")
    monkeypatch.setattr(email, "_sync_inbox", lambda con, token, now: {"inbox": 3})
    monkeypatch.setattr(email, "_sync_days", lambda *a: 2)
    monkeypatch.setattr(email, "_sync_attachments", lambda *a: 0)


def _refused(*a, **k):
    raise agent.AgentError(LIMIT)


def test_a_refused_summary_is_deferred_not_a_failed_refresh(dbfile, synced,
                                                            monkeypatch):
    monkeypatch.setattr(email_triage, "summarize_pending", _refused)
    ran = []
    monkeypatch.setattr(email_extract, "refresh",
                        lambda token: ran.append(token) or {"packages": 0})
    info = email.refresh()
    assert info["inbox"] == 3 and info["fetched_days"] == 2
    assert info["summarized"] == {"skipped": LIMIT}
    assert ran == ["tok"]           # extraction still gets its turn


def test_a_sync_failure_still_fails_the_refresh(dbfile, synced, monkeypatch):
    def gmail_down(*a):
        raise RuntimeError("gmail API 503")
    monkeypatch.setattr(email, "_sync_days", gmail_down)
    with pytest.raises(RuntimeError, match="503"):
        email.refresh()


def test_a_non_model_summary_failure_still_fails_the_refresh(dbfile, synced,
                                                             monkeypatch):
    """Only the model saying no is deferrable; a bug is still a bug."""
    def broken(token):
        raise KeyError("label")
    monkeypatch.setattr(email_triage, "summarize_pending", broken)
    with pytest.raises(KeyError):
        email.refresh()


def test_the_poller_records_a_deferred_refresh_as_healthy(dbfile, synced,
                                                          monkeypatch):
    monkeypatch.setattr(email_triage, "summarize_pending", _refused)
    monkeypatch.setattr(email_extract, "refresh",
                        lambda token: {"packages": 0, "skipped": LIMIT})
    poller._refresh_sync("email", email.refresh)
    row = heartbeat.rows()["email"]
    assert row["ok"] and not row["error"]
    assert row["recent"].endswith("o")   # mail synced: a real run, not a skip


def test_a_refused_extraction_still_polls_flights(dbfile, monkeypatch):
    """Model-free flight and check-in tasks still run in a spent window."""
    calls = []
    monkeypatch.setattr(email_extract, "_run_extraction",
                        lambda con, token, kind, *a, **k: calls.append(kind) or _refused())
    monkeypatch.setattr(email_extract, "poll_flight_status", lambda con: 1)
    monkeypatch.setattr(email_extract, "ensure_checkin_tasks", lambda con: 2)
    monkeypatch.setattr(heartbeat, "log_event",
                        lambda *a: pytest.fail("a usage limit is not an OH failure"))
    out = email_extract.refresh("tok")
    assert out["skipped"] == LIMIT
    assert (out["flights_checked"], out["checkin_tasks"]) == (1, 2)
    assert calls == ["package"]         # the rest wait for next cycle


def test_breaker_open_extraction_fetches_nothing_and_stays_pending(dbfile, spent,
                                                                   monkeypatch):
    """A refused batch writes no email_extract row, so the candidate returns next cycle."""
    monkeypatch.setattr(email_triage, "body_excerpt",
                        lambda *a, **k: pytest.fail("fetched a body for a refused call"))
    cand = [{"msg_id": "m1", "from_addr": "a@b.c", "subject": "Your order shipped"}]
    with db.session() as con:
        with pytest.raises(agent.AgentError, match="usage limit"):
            email_extract._run_extraction(con, "tok", "package", cand,
                                          "system", lambda *a: None)
        assert con.execute("SELECT COUNT(*) FROM email_extract").fetchone()[0] == 0


def test_breaker_open_triage_fetches_nothing(dbfile, spent, monkeypatch):
    with db.session() as con, con:
        con.execute(
            "INSERT INTO emails (msg_id, day, received_at, from_addr, subject,"
            " refreshed_at, in_inbox, is_sent) VALUES"
            " ('m1','2026-10-05','2026-10-05T09:00:00','a@b.c','hi',"
            "  '2026-10-05T09:00:00',1,0)")
    monkeypatch.setattr(email_triage, "gmail_label_names", lambda token: [])
    monkeypatch.setattr(email_triage, "body_excerpt",
                        lambda *a, **k: pytest.fail("fetched a body for a refused call"))
    with pytest.raises(agent.AgentError, match="usage limit"):
        email_triage.summarize_pending("tok")
    with db.session() as con:
        assert [r["msg_id"] for r in email_triage.pending(con)] == ["m1"]


def test_breaker_open_brief_skips_gathering_and_says_why(dbfile, spent,
                                                         monkeypatch):
    day = db.day_key()
    monkeypatch.setattr(brief, "_gather",
                        lambda d: pytest.fail("gathered for a refused call"))
    assert brief._claim(day, False)
    brief._generate(day)
    r = brief._row(day)
    assert r["status"] == "error" and r["error"] == LIMIT


def test_breaker_open_brain_does_not_start_a_run(dbfile, spent, monkeypatch):
    monkeypatch.setattr(brain, "watermark", lambda: "2020-01-01")
    monkeypatch.setattr(brain, "_running", lambda: False)
    assert brain.maybe_start() is False
