"""What the backend changed on its own, and how to take each change back.

Several things write while nobody is looking: a bill's due date opens a task
(providers/finance.py), a flight opens a check-in task
(providers/email_extract.py), a text to yourself becomes an event or a food
entry or a list item (app/selftexts.py). Each used to write and say nothing,
so the only way to find out was to notice the result. Every such write now
goes through `record()`, and the desk's "While you were away" card reads the
log back with an Undo on each row.

`record()` is the obligation, not a convenience: an unattended write that
skips it is a change nobody is told about. test_activity.py fails on a
`db.create_task(` call site outside the user-initiated ones that doesn't also
call it.

Undo is per kind and only reverses what the row made. It refuses rather than
guesses when the thing has moved on since — a task you already finished is
yours now, and undoing the automation must not erase that.
"""

import json
from datetime import datetime, timedelta

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from . import db
from .config import VAULT_PATH

router = APIRouter(prefix="/api")

SEEN_KEY = "activity_seen_at"
WINDOW_DAYS = 7          # what the card lists; older rows stay in the table

# kind → (noun for the summary line, can it be undone)
KINDS = {
    "task": "task",
    "event": "calendar event",
    "food": "food log",
    "watch": "watchlist",
    "list_item": "list item",
    "note": "note",
    "keep": "kept",
}


class UndoError(ValueError):
    """Shown as-is on the card: why this one can't be taken back."""


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def record(source: str, kind: str, summary: str, ref: dict | None = None,
           origin: str | None = None) -> int:
    """Log one unattended write. Call it after the write has landed."""
    if kind not in KINDS:
        raise ValueError(f"unknown activity kind {kind!r}")
    with db.session() as con, con:
        return con.execute(
            "INSERT INTO activity (at, source, kind, summary, ref, origin)"
            " VALUES (?,?,?,?,?,?)",
            (_now(), source, kind, summary.strip()[:300],
             json.dumps(ref) if ref else None, origin)).lastrowid


def _row(r) -> dict:
    out = dict(r)
    out["ref"] = json.loads(r["ref"]) if r["ref"] else None
    out["undoable"] = out["undone_at"] is None and out["kind"] != "keep"
    return out


def get(row_id: int) -> dict | None:
    with db.session() as con:
        r = con.execute("SELECT * FROM activity WHERE id=?", (row_id,)).fetchone()
    return _row(r) if r else None


def seen_through(con) -> int:
    """The newest row id you've said "looks fine" to. An id rather than a
    time: a row written in the same second as the click would otherwise be
    marked seen without ever having been on screen."""
    r = con.execute("SELECT value FROM sync_state WHERE key=?", (SEEN_KEY,)).fetchone()
    return int(r["value"]) if r else 0


def mark_seen(through: int | None = None) -> int:
    with db.session() as con, con:
        if through is None:
            through = con.execute("SELECT COALESCE(MAX(id), 0) FROM activity").fetchone()[0]
        con.execute("INSERT INTO sync_state (key, value) VALUES (?,?)"
                    " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (SEEN_KEY, str(through)))
    return through


def recent(days: int = WINDOW_DAYS) -> dict:
    """The card's payload: the last `days` of rows, newest first, each marked
    unseen when it landed after the last "looks fine"."""
    since = (datetime.now() - timedelta(days=days)).isoformat(timespec="seconds")
    with db.session() as con:
        seen = seen_through(con)
        rows = [_row(r) for r in con.execute(
            "SELECT * FROM activity WHERE at >= ? ORDER BY id DESC", (since,))]
    from . import selftexts   # import here: selftexts imports this module
    selftexts.decorate(rows)
    for r in rows:
        r["unseen"] = r["id"] > seen
    return {"items": rows, "seen_through": seen,
            "unseen": sum(r["unseen"] for r in rows)}


# ---------- undo ----------

def _undo_task(ref: dict) -> None:
    with db.session() as con:
        t = con.execute("SELECT status FROM tasks WHERE id=?",
                        (ref["task_id"],)).fetchone()
    if t is None:
        return                       # already deleted by hand: nothing to undo
    if t["status"] == "done":
        raise UndoError("you've already finished that task")
    if t["status"] != "dropped":
        db.set_task_status(ref["task_id"], "dropped")


def _undo_event(ref: dict) -> None:
    from fastapi import HTTPException as _H

    from .routes import calendar as cal
    try:
        cal.delete_event(event_id=ref["event_id"], calendar_id=ref["calendar_id"])
    except _H as e:
        # Gone already (deleted in Google Calendar) is the outcome undo wanted.
        if e.status_code not in (404, 410):
            raise UndoError(str(e.detail))


def _undo_food(ref: dict) -> None:
    from . import food
    for log_id in ref["log_ids"]:
        try:
            food.delete_entry(log_id)
        except food.FoodError:
            pass                     # removed by hand already


def _undo_row(table: str):
    def undo(ref: dict) -> None:
        db.delete_row(table, ref["id"])
    return undo


def _undo_note(ref: dict) -> None:
    path = (VAULT_PATH / ref["path"]).resolve()
    if VAULT_PATH not in path.parents:
        raise UndoError("that note isn't in the vault")
    if not path.exists():
        return
    import frontmatter
    if frontmatter.load(path).metadata.get("reviewed"):
        raise UndoError("you've already reviewed that note — delete it in the vault")
    path.unlink()


_UNDO = {
    "task": _undo_task,
    "event": _undo_event,
    "food": _undo_food,
    "watch": _undo_row("watchlist"),
    "list_item": _undo_row("list_items"),
    "note": _undo_note,
}


def undo(row_id: int) -> dict:
    row = get(row_id)
    if row is None:
        raise KeyError(row_id)
    if row["undone_at"]:
        return row
    if row["kind"] == "keep":
        raise UndoError("nothing was written, so there's nothing to undo")
    _UNDO[row["kind"]](row["ref"] or {})
    with db.session() as con, con:
        con.execute("UPDATE activity SET undone_at=? WHERE id=?", (_now(), row_id))
    return get(row_id)


# ---------- routes ----------

class RedoIn(BaseModel):
    kind: str


@router.get("/activity")
def get_activity(days: int = WINDOW_DAYS):
    return recent(days)


class SeenIn(BaseModel):
    # The newest id the card was showing — not "everything", so a row that
    # landed between the fetch and the click stays unseen.
    through: int | None = None


@router.post("/activity/seen")
def post_seen(body: SeenIn | None = None):
    return {"seen_through": mark_seen(body.through if body else None)}


@router.post("/activity/{row_id}/undo")
def post_undo(row_id: int):
    try:
        return undo(row_id)
    except KeyError:
        raise HTTPException(404, "no such activity")
    except UndoError as e:
        raise HTTPException(409, str(e))


@router.post("/activity/{row_id}/redo")
def post_redo(row_id: int, body: RedoIn):
    """Undo this row and route its self-text again, as `kind` this time."""
    from . import selftexts
    try:
        return selftexts.redo(row_id, body.kind)
    except KeyError:
        raise HTTPException(404, "no such activity")
    except (UndoError, ValueError) as e:
        raise HTTPException(409, str(e))
