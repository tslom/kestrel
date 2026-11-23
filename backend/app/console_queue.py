"""Messages held for the next turn, per console tab (New Features #92).

Two ways to type into a turn that's running (the composer's "Enter
mid-turn" setting picks which Enter does, ⌥Enter the other):

- **steer**: straight to the CLI, which reads it at the next tool boundary
  (console.py, `queue_id`; the CLI's own queue). In the turn, can be taken
  back unread, can't be edited or reordered: the CLI already has it.
- **queue** (this module): held here until the turn ends, then sent as a
  turn of its own, oldest first. Editable, reorderable, and "steer now"
  hands one to the CLI mid-turn after all.

Held in app.db rather than in the page or the Link because a backend reload
ends both: the page reconnects under the same tab id and gets the list back
(the `held` frame is sticky, console_link.STICKY). A held message's pills
come back whole except images, which live in console._held_images only;
those go as "the image is gone, ask for it again", as any stale image does.

The drain (console.read_session → a `held_next` frame on the Link's inbox)
runs only after a turn that ended cleanly: not after a Stop (which hands the
list back to the prompt instead), an error or a usage cut, where the next
message would only hit the same wall. After a reload the list waits for a
turn to end or for "send now".
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime

from . import db

# A turn's queue, not a backlog: past this the page is told no.
MAX_HELD = 20
TEXT_MAX = 100_000


def _rows(con, tab: str) -> list[dict]:
    return [{"id": r["id"], **json.loads(r["payload"])}
            for r in con.execute("SELECT id, payload FROM console_queue WHERE tab=? "
                                 "ORDER BY pos, created_at", (tab,))]


def items(tab: str) -> list[dict]:
    """The held messages as the page shows them: id, text, pill labels."""
    with db.session() as con:
        return [{"id": r["id"], "text": r.get("text") or "",
                 "pills": [str(p.get("label") or "") for p in r.get("context") or []]}
                for r in _rows(con, tab)]


def count(tab: str) -> int:
    with db.session() as con:
        return con.execute("SELECT COUNT(*) FROM console_queue WHERE tab=?", (tab,)).fetchone()[0]


def add(tab: str, payload: dict) -> str | None:
    """Hold a message; its id (a uuid, so "steer now" can hand it to the CLI
    under it), or None when the queue is full."""
    keep = {k: payload.get(k) for k in ("text", "context", "typed", "verify_url", "reports")
            if payload.get(k) is not None}
    keep["text"] = str(keep.get("text") or "")[:TEXT_MAX]
    qid = str(uuid.uuid4())
    with db.session() as con, con:
        n, top = con.execute("SELECT COUNT(*), COALESCE(MAX(pos), 0) FROM console_queue "
                             "WHERE tab=?", (tab,)).fetchone()
        if n >= MAX_HELD:
            return None
        con.execute("INSERT INTO console_queue (id, tab, pos, payload, created_at) "
                    "VALUES (?,?,?,?,?)",
                    (qid, tab, top + 1, json.dumps(keep),
                     datetime.now().isoformat(timespec="seconds")))
    return qid


def edit(tab: str, qid: str, text: str) -> bool:
    with db.session() as con, con:
        row = con.execute("SELECT payload FROM console_queue WHERE id=? AND tab=?",
                          (qid, tab)).fetchone()
        if row is None:
            return False
        payload = {**json.loads(row["payload"]), "text": str(text)[:TEXT_MAX]}
        con.execute("UPDATE console_queue SET payload=? WHERE id=?", (json.dumps(payload), qid))
    return True


def move(tab: str, qid: str, to: int) -> bool:
    """Put `qid` at index `to` (clamped) and renumber."""
    with db.session() as con, con:
        ids = [r["id"] for r in con.execute(
            "SELECT id FROM console_queue WHERE tab=? ORDER BY pos, created_at", (tab,))]
        if qid not in ids:
            return False
        ids.remove(qid)
        ids.insert(max(0, min(int(to), len(ids))), qid)
        con.executemany("UPDATE console_queue SET pos=? WHERE id=?",
                        [(i + 1, x) for i, x in enumerate(ids)])
    return True


def take(tab: str, qid: str | None = None) -> dict | None:
    """Remove one and return its payload (with its id): `qid`, or the oldest."""
    with db.session() as con, con:
        rows = _rows(con, tab)
        row = next((r for r in rows if r["id"] == qid), None) if qid else (rows[0] if rows else None)
        if row is None:
            return None
        con.execute("DELETE FROM console_queue WHERE id=?", (row["id"],))
    return row


def clear(tab: str) -> list[dict]:
    """Remove them all; their payloads, oldest first (Stop hands the texts
    back to the prompt)."""
    with db.session() as con, con:
        rows = _rows(con, tab)
        con.execute("DELETE FROM console_queue WHERE tab=?", (tab,))
    return rows
