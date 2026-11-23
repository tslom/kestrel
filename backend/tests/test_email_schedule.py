"""Scheduled send (app/email_schedule.py, the console's email_schedule tool).

The draft lives in Gmail and the time lives here; what's worth testing is the
handoff at send_at — that it sends exactly once, that a draft deleted by hand
reads as a cancellation rather than a retry loop, and that nothing goes early.
"""

from datetime import datetime, timedelta

import pytest
from fastapi import HTTPException

from app import db, email_schedule as sched
from app.routes import email as email_routes


@pytest.fixture
def gmail(monkeypatch, dbfile):
    """Stand in for Gmail: drafts.create returns d1, drafts.send records."""
    calls = []

    def fake_api(url, token, method="GET", body=None):
        calls.append({"url": url, "method": method, "body": body})
        if url.endswith("/drafts/send"):
            return {"id": "sent1", "threadId": "t1"}
        if url.endswith("/drafts"):
            return {"id": "d1", "message": {"id": "m1", "threadId": "t1"}}
        if "/drafts/" in url:
            return {"message": {"payload": {"headers": [
                {"name": "To", "value": "sam@example.org"},
                {"name": "Subject", "value": "hand-written"}]}}}
        raise AssertionError(url)

    monkeypatch.setattr(email_routes, "api_call", fake_api)
    monkeypatch.setattr(email_routes, "access_token", lambda: "tok")
    sched._next_try.clear()
    return calls


def _sends(calls):
    return [c for c in calls if c["url"].endswith("/drafts/send")]


def _in(minutes):
    return (datetime.now() + timedelta(minutes=minutes)).isoformat(timespec="seconds")


def test_schedule_makes_a_draft_and_waits(gmail):
    out = sched.schedule(send_at=_in(60), to="sam@example.org",
                         subject="hi", body="see you then")
    assert out["status"] == "scheduled" and out["draft_id"] == "d1"
    assert sched.send_due()["sent"] == 0
    assert _sends(gmail) == []                        # nothing goes early


def test_due_mail_sends_once(gmail):
    out = sched.schedule(send_at=_in(1), to="sam@example.org", body="x")
    later = datetime.now().astimezone() + timedelta(minutes=2)
    assert sched.send_due(now=later)["sent"] == 1
    assert sched.send_due(now=later)["sent"] == 0     # not twice
    assert _sends(gmail) == [{"url": f"{email_routes.API_BASE}/drafts/send",
                              "method": "POST", "body": {"id": "d1"}}]
    with db.session() as con:
        row = con.execute("SELECT status, message_id FROM scheduled_emails"
                          " WHERE id=?", (out["id"],)).fetchone()
    assert tuple(row) == ("sent", "sent1")


def test_an_existing_draft_takes_its_headers_from_gmail(gmail):
    out = sched.schedule(send_at=_in(30), draft_id="r99")
    assert (out["to_addr"], out["subject"]) == ("sam@example.org", "hand-written")
    assert not [c for c in gmail if c["url"].endswith("/drafts")]   # none made


def test_rescheduling_a_draft_moves_it(gmail):
    a = sched.schedule(send_at=_in(30), draft_id="r99")
    b = sched.schedule(send_at=_in(90), draft_id="r99")
    assert b["id"] == a["id"] and b["rescheduled"]
    assert len(sched.pending()) == 1


def test_a_past_time_is_refused_before_any_draft_is_made(gmail):
    with pytest.raises(HTTPException) as e:
        sched.schedule(send_at=_in(-60), to="sam@example.org", body="x")
    assert "past" in e.value.detail
    assert gmail == []


def test_a_deleted_draft_cancels_rather_than_retries(gmail, monkeypatch):
    sched.schedule(send_at=_in(1), to="sam@example.org", body="x")

    def gone(url, token, method="GET", body=None):
        raise HTTPException(502, "gmail API 404: Requested entity was not found.")
    monkeypatch.setattr(email_routes, "api_call", gone)
    later = datetime.now().astimezone() + timedelta(minutes=2)
    assert sched.send_due(now=later)["cancelled"] == 1
    assert sched.pending() == []


def test_a_transient_failure_backs_off_then_gives_up(gmail, monkeypatch):
    out = sched.schedule(send_at=_in(1), to="sam@example.org", body="x")
    tries = []

    def down(url, token, method="GET", body=None):
        tries.append(url)
        raise HTTPException(502, "gmail API 500: backend error")
    monkeypatch.setattr(email_routes, "api_call", down)
    later = datetime.now().astimezone() + timedelta(minutes=2)
    assert sched.send_due(now=later)["retry"] == 1
    sched.send_due(now=later)
    assert len(tries) == 1                            # backing off, not hammering
    for _ in range(sched.MAX_ATTEMPTS):
        sched._next_try.clear()
        sched.send_due(now=later)
    with db.session() as con:
        assert con.execute("SELECT status FROM scheduled_emails WHERE id=?",
                           (out["id"],)).fetchone()[0] == "failed"
    assert len(tries) == sched.MAX_ATTEMPTS


def test_a_send_interrupted_mid_call_is_never_resent(gmail):
    out = sched.schedule(send_at=_in(1), to="sam@example.org", body="x")
    with db.session() as con, con:
        con.execute("UPDATE scheduled_emails SET status='sending' WHERE id=?",
                    (out["id"],))
    sched.send_due(now=datetime.now().astimezone() + timedelta(minutes=30))
    assert _sends(gmail) == []
    assert sched.recent()[0]["status"] == "failed"


def test_cancel_leaves_the_draft_and_stops_the_send(gmail):
    out = sched.schedule(send_at=_in(1), to="sam@example.org", body="x")
    assert sched.cancel(out["id"])["status"] == "cancelled"
    sched.send_due(now=datetime.now().astimezone() + timedelta(minutes=5))
    assert _sends(gmail) == []
    with pytest.raises(HTTPException):
        sched.cancel(out["id"])


def test_pending_sends_ask_for_a_wake(gmail):
    sched.schedule(send_at=_in(120), to="sam@example.org", body="x")
    now = datetime.now().astimezone()
    with db.session() as con:
        times = sched.wake_times(con, now=now, horizon=now + timedelta(hours=36))
    assert len(times) == 1
    assert timedelta(minutes=118) < times[0] - now < timedelta(minutes=120)
