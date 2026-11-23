"""Which tabs had work in flight, so work cut off by a quit can be resumed.

A tab with a turn running, or background agents and shells still going, has
a row in `console_work`, stamped with this server process's BOOT id; the row
goes when the work does. Quitting Kestrel (or the backend dying) leaves the
rows where they were, so on the next launch a row from another boot is a tab
that was interrupted, and what it was doing. The page offers one Resume for
all of them (and per tab), which sends each a "continue" naming what was cut
off; nothing resumes without the click. Work after the relaunch is recorded
under the new boot, apart from the old row, which only Resume or Dismiss
clears.

Written from console_link (every turn start and end, every change to the
agents strip), so the row is never staler than the last thing the page saw.
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


# A row is deleted this long after its work ends, not at once. Quitting kills
# the CLI too, and on its way out the CLI marks its background tasks killed
# — which reads exactly like the work ending. Within this delay the server is
# gone and the timer with it, so the row survives to be offered back; a
# server that lives on deletes it a moment later.
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
                # Keyed by (tab, boot): this launch's work never overwrites
                # what an earlier one left. The CLI starts a turn of its own on
                # resume (its "stopped" notifications), and that turn used to
                # erase the very record of what was cut off.
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
    """Which tab and mode a session ran in (`console_sessions`), kept after
    the work row goes: the usage page's only way to tell a study tab's
    tokens from a general one's, since both run in the vault. Bookkeeping,
    so it never raises."""
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
