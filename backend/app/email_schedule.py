"""Scheduled send: the message is an ordinary Gmail draft, `scheduled_emails` holds the
when (source of truth), and send_due() calls `drafts.send` at send_at. The one place mail
goes out without a human pressing Send. Exactly-once: a row is claimed `scheduled` ->
`sending` before Gmail is called; a stuck `sending` becomes `failed`, never resent.
Design: DESIGN.md → §5 Email (Scheduled send).
"""

from __future__ import annotations

import time
import urllib.parse
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException

from . import db

# Transient failures back off 1, 2, 4 … 64 min (~2h), then fail loudly; the clock is in-process (restart retries at once).
MAX_ATTEMPTS = 8
_next_try: dict[int, float] = {}
STUCK_MIN = 10            # a `sending` row this old was interrupted mid-call
MAX_AHEAD_DAYS = 366
LATE_GRACE_S = 120        # "send at 3:00" asked at 3:00:30 is not an error


def _utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def parse_when(raw: str, *, now: datetime | None = None) -> datetime:
    """An ISO datetime, naive = local. Refuses the past (beyond grace) and over a year out: likely a wrong date."""
    now = now or datetime.now().astimezone()
    try:
        when = datetime.fromisoformat((raw or "").strip())
    except ValueError:
        raise HTTPException(400, f"send_at must be an ISO datetime like "
                                 f"2026-10-24T08:00, got {raw!r}")
    if when.tzinfo is None:
        when = when.astimezone()          # local, the way the user said it
    if when < now - timedelta(seconds=LATE_GRACE_S):
        raise HTTPException(400, f"send_at {when.isoformat(timespec='minutes')} "
                                 "is in the past")
    if when > now + timedelta(days=MAX_AHEAD_DAYS):
        raise HTTPException(400, "send_at is more than a year away")
    return when


def _row(con, sid: int) -> dict:
    r = con.execute("SELECT * FROM scheduled_emails WHERE id=?", (sid,)).fetchone()
    if r is None:
        raise HTTPException(404, f"no scheduled email #{sid}")
    return _public(r)


def _public(r) -> dict:
    d = dict(r)
    # UTC is stored; local for display
    d["send_at_local"] = (datetime.fromisoformat(d["send_at"]).astimezone()
                          .isoformat(timespec="minutes"))
    return d


def _draft_headers(draft_id: str) -> dict:
    """To/Subject of an existing Gmail draft."""
    from .providers import email as email_provider
    from .routes import email as email_routes
    d = email_routes.api_call(
        f"{email_routes.API_BASE}/drafts/{urllib.parse.quote(draft_id, safe='')}"
        "?format=metadata", email_routes.access_token())
    payload = d.get("message", {}).get("payload", {})
    return {"to": email_provider.header(payload, "To") or "",
            "subject": email_provider.header(payload, "Subject")}


def schedule(*, send_at: str, draft_id: str | None = None,
             body: str | None = None, to: str | None = None,
             subject: str | None = None, reply_to: str | None = None,
             cc: str | None = None, attach: list[int] | None = None) -> dict:
    """Schedule a draft at `send_at`: name `draft_id` or pass what email_draft takes.
    The time is validated before Gmail is touched (no orphan draft on a bad date)."""
    when = parse_when(send_at)
    if draft_id:
        draft_id = draft_id.strip()
        head = _draft_headers(draft_id)
        if not head["to"]:
            raise HTTPException(400, f"draft {draft_id} has no recipient yet")
        to_addr, subj = head["to"], head["subject"]
    else:
        from .routes import email as email_routes
        made = email_routes.create_draft(body=body or "", to=to, subject=subject,
                                         reply_to=reply_to, cc=cc, attach=attach)
        draft_id, to_addr, subj = made["draft_id"], made["to"], made["subject"]
    now = datetime.now().astimezone()
    with db.session() as con, con:
        # one schedule per draft: rescheduling moves the row (a second send would 404 as a phantom cancel)
        cur = con.execute(
            "UPDATE scheduled_emails SET send_at=?, to_addr=?, subject=?,"
            " error=NULL, attempts=0 WHERE draft_id=? AND status='scheduled'",
            (_utc(when), to_addr, subj, draft_id))
        if cur.rowcount:
            sid = con.execute("SELECT id FROM scheduled_emails WHERE draft_id=?"
                              " AND status='scheduled'", (draft_id,)).fetchone()[0]
        else:
            sid = con.execute(
                "INSERT INTO scheduled_emails (draft_id, to_addr, subject,"
                " send_at, created_at) VALUES (?,?,?,?,?)",
                (draft_id, to_addr, subj, _utc(when),
                 now.isoformat(timespec="seconds"))).lastrowid
        out = _row(con, sid)
    out["rescheduled"] = bool(cur.rowcount)
    _next_try.pop(sid, None)              # a new time is a fresh start
    return out


def cancel(sid: int) -> dict:
    """Stop a pending send; the draft stays in Gmail."""
    with db.session() as con, con:
        row = _row(con, sid)
        if row["status"] != "scheduled":
            raise HTTPException(409, f"scheduled email #{sid} is already "
                                     f"{row['status']}")
        con.execute("UPDATE scheduled_emails SET status='cancelled',"
                    " error='cancelled before sending' WHERE id=?", (sid,))
        return _row(con, sid)


def pending(con=None) -> list[dict]:
    with db.borrow(con) as con:
        return [_public(r) for r in con.execute(
            "SELECT * FROM scheduled_emails WHERE status IN ('scheduled','sending')"
            " ORDER BY send_at")]


def recent(con=None, *, limit: int = 20) -> list[dict]:
    """Pending first, then the latest outcomes (including an overnight failure)."""
    with db.borrow(con) as con:
        done = [_public(r) for r in con.execute(
            "SELECT * FROM scheduled_emails WHERE status NOT IN ('scheduled','sending')"
            " ORDER BY COALESCE(sent_at, send_at) DESC LIMIT ?", (limit,))]
        return pending(con) + done


def send_due(*, now: datetime | None = None, api=None, token=None) -> dict:
    """Send every scheduled row whose time has come. `api`/`token` are the
    Gmail call seam for tests; by default they are routes/email.py's."""
    from .routes import email as email_routes
    api = api or email_routes.api_call
    now = now or datetime.now().astimezone()
    out = {"sent": 0, "cancelled": 0, "failed": 0, "retry": 0}
    with db.session() as con:
        with con:
            stuck = con.execute(
                "UPDATE scheduled_emails SET status='failed', error=?"
                " WHERE status='sending' AND send_at <= ?",
                ("interrupted mid-send — check Gmail Sent before re-sending",
                 _utc(now - timedelta(minutes=STUCK_MIN)))).rowcount
            out["failed"] += stuck
        due = con.execute(
            "SELECT id, draft_id FROM scheduled_emails"
            " WHERE status='scheduled' AND send_at <= ? ORDER BY send_at",
            (_utc(now),)).fetchall()
        if not due:
            return out
        try:
            token = token or email_routes.access_token()
        except HTTPException as e:
            # unauthorized: stay scheduled for the next tick, but record why
            with con:
                con.execute("UPDATE scheduled_emails SET error=? WHERE"
                            " status='scheduled' AND send_at <= ?",
                            (f"gmail: {e.detail}", _utc(now)))
            out["retry"] += len(due)
            return out
        for r in due:
            if _next_try.get(r["id"], 0) > time.monotonic():
                out["retry"] += 1
                continue
            with con:
                claimed = con.execute(
                    "UPDATE scheduled_emails SET status='sending',"
                    " attempts=attempts+1 WHERE id=? AND status='scheduled'",
                    (r["id"],)).rowcount
            if not claimed:
                continue                      # another cycle got it
            try:
                sent = api(f"{email_routes.API_BASE}/drafts/send", token,
                           method="POST", body={"id": r["draft_id"]})
            except HTTPException as e:
                detail = str(e.detail)
                if " 404:" in detail:
                    status, error = ("cancelled", "draft no longer in Gmail — "
                                     "sent or deleted by hand")
                else:
                    attempts = con.execute(
                        "SELECT attempts FROM scheduled_emails WHERE id=?",
                        (r["id"],)).fetchone()[0]
                    status = "failed" if attempts >= MAX_ATTEMPTS else "scheduled"
                    error = detail[:500]
                    _next_try[r["id"]] = time.monotonic() + 60 * 2 ** (attempts - 1)
                with con:
                    con.execute("UPDATE scheduled_emails SET status=?, error=?"
                                " WHERE id=?", (status, error, r["id"]))
                out["retry" if status == "scheduled" else status] += 1
                if status == "failed":
                    # a promised mail that didn't go belongs in the error log
                    from . import failures
                    failures.record("email_schedule", "send", f"#{r['id']}",
                                    detail=detail[:500])
                continue
            with con:
                con.execute(
                    "UPDATE scheduled_emails SET status='sent', sent_at=?,"
                    " message_id=?, thread_id=?, error=NULL WHERE id=?",
                    (datetime.now().astimezone().isoformat(timespec="seconds"),
                     sent.get("id"), sent.get("threadId"), r["id"]))
            out["sent"] += 1
    if out["sent"] or out["cancelled"] or out["failed"]:
        print(f"[email_schedule] {out}")
    return out


def wake_times(con, *, now: datetime, horizon: datetime) -> list[datetime]:
    """When wake.py should rouse the machine: a minute before each pending
    send inside the horizon. A shut lid otherwise holds a "send at 8am" until
    whenever it opens."""
    times = []
    for r in con.execute("SELECT send_at FROM scheduled_emails"
                         " WHERE status='scheduled' AND send_at > ? AND send_at <= ?",
                         (_utc(now), _utc(horizon))):
        at = datetime.fromisoformat(r["send_at"]).astimezone() - timedelta(minutes=1)
        if at > now:
            times.append(at)
    return times


SEND_TICK_S = 30


async def send_loop() -> None:
    """Its own loop rather than a poller POST_STEP: the provider cycle is five
    minutes plus however long the slowest provider takes, and "send at 8:00"
    arriving at 8:07 is late in a way a stale widget isn't. A tick is one
    indexed read when nothing is due."""
    import asyncio
    from . import failures
    while True:
        try:
            await asyncio.to_thread(send_due)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # the loop outlives one bad tick
            print(f"[email_schedule] tick FAILED: {e!r}")
            failures.record("email_schedule", "tick", "send_due", detail=repr(e))
        await asyncio.sleep(SEND_TICK_S)
