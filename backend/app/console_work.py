"""Which tabs had work in flight, so work cut off by a quit can be resumed.

A tab with a turn or background agents running has a `console_work` row stamped
with this process's BOOT id, removed when the work ends. A row from another boot
is an interrupted tab; one Resume (or per tab) sends it a "continue", never
automatically. Written from console_link. Design: DESIGN.md → `app/console_work.py`.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timedelta

from . import db

BOOT = uuid.uuid4().hex
KEEP_DAYS = 7          # an interrupted row nobody resumed or dismissed
_DONE = frozenset({"completed", "failed", "stopped", "killed"})


def snapshot(turn: bool, agents: list[dict]) -> dict | None:
    """What a tab has in flight, or None for nothing."""
    running = [{"id": a.get("id"), "description": a.get("description") or "",
                "kind": a.get("kind") or "local_agent"}
               for a in agents if a.get("status") not in _DONE]
    if not turn and not running:
        return None
    return {"turn": turn, "agents": running}


# Delete this long after work ends, not at once: a quitting CLI marks its tasks killed,
# which looks like the work ending. The server dies within the delay, so the row survives.
DELETE_DELAY_S = 4.0
_pending: dict[str, asyncio.TimerHandle] = {}


def _delete(tab: str, boot: str) -> None:
    _pending.pop(tab, None)
    try:
        with db.borrow(None) as con:
            with con:
                con.execute("DELETE FROM console_work WHERE tab=? AND boot=?", (tab, boot))
    except Exception as e:   # bookkeeping
        print(f"[console] couldn't clear the work row for {tab}: {e!r}")


def mark(tab: str | None, *, session_id: str | None, profile: str | None,
         project: str | None, label: str | None, work: dict | None, con=None) -> None:
    if not tab:
        return
    if session_id:
        remember_session(session_id, tab=tab, profile=profile, project=project,
                         label=label, con=con)
    timer = _pending.pop(tab, None)
    if timer is not None:
        timer.cancel()
    if work is None:
        if con is not None:            # a caller's connection: at once (tests)
            with con:
                con.execute("DELETE FROM console_work WHERE tab=? AND boot=?", (tab, BOOT))
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:           # no server loop to outlive: at once
            _delete(tab, BOOT)
            return
        _pending[tab] = loop.call_later(DELETE_DELAY_S, _delete, tab, BOOT)
        return
    with db.borrow(con) as con:
        with con:
            con.execute(
                "INSERT INTO console_work (tab, session_id, profile, project, label,"
                " turn, agents, boot, updated_at) VALUES (?,?,?,?,?,?,?,?,?)"
                # Keyed (tab, boot): a resumed CLI starts a turn of its own, which must
                # not overwrite the record of what was cut off.
                " ON CONFLICT(tab, boot) DO UPDATE SET session_id=excluded.session_id,"
                " profile=excluded.profile, project=excluded.project,"
                " label=COALESCE(excluded.label, console_work.label),"
                " turn=excluded.turn, agents=excluded.agents,"
                " updated_at=excluded.updated_at",
                (tab, session_id, profile, project, label, int(bool(work["turn"])),
                 json.dumps(work["agents"]), BOOT,
                 datetime.now().astimezone().isoformat(timespec="seconds")))


def remember_session(session_id: str, *, tab: str | None, profile: str | None,
                     project: str | None, label: str | None = None, con=None) -> None:
    """Which tab and mode a session ran in (`console_sessions`), kept after the work
    row goes (the usage page tells study from general by it). Never raises."""
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    try:
        with db.borrow(con) as c:
            with c:
                c.execute(
                    "INSERT INTO console_sessions (session_id, tab, profile, project,"
                    " label, first_at, last_at) VALUES (?,?,?,?,?,?,?)"
                    " ON CONFLICT(session_id) DO UPDATE SET"
                    " tab=COALESCE(excluded.tab, console_sessions.tab),"
                    " profile=COALESCE(excluded.profile, console_sessions.profile),"
                    " project=COALESCE(excluded.project, console_sessions.project),"
                    " label=COALESCE(excluded.label, console_sessions.label),"
                    " last_at=excluded.last_at",
                    (session_id, tab, profile, project, label, now, now))
    except Exception as e:   # bookkeeping
        print(f"[console] couldn't record session {session_id}: {e!r}")


def interrupted(con=None) -> list[dict]:
    """Rows a previous server process left: work that was cut off."""
    cutoff = (datetime.now().astimezone() - timedelta(days=KEEP_DAYS)).isoformat(timespec="seconds")
    with db.borrow(con) as con:
        with con:
            con.execute("DELETE FROM console_work WHERE boot != ? AND updated_at < ?",
                        (BOOT, cutoff))
        rows = con.execute("SELECT * FROM console_work WHERE boot != ?"
                           " ORDER BY updated_at", (BOOT,)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["agents"] = json.loads(d["agents"] or "[]")
        except ValueError:
            d["agents"] = []
        d["turn"] = bool(d["turn"])
        out.append(d)
    return out


def dismiss(tab: str, con=None) -> bool:
    with db.borrow(con) as con:
        with con:
            cur = con.execute("DELETE FROM console_work WHERE tab=? AND boot != ?", (tab, BOOT))
    return cur.rowcount > 0
