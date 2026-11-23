"""Usage — is the desk's ranking right, and does every card still earn its slot?

`lib/salience.js` sorts seventeen cards into three bands every morning and
nothing has ever checked its work. From inside the app a card you stopped
caring about in July looks exactly like one you read daily: both render, both
sit there, neither says anything.

The trap this has to avoid is measuring clicks and calling it value. Most good
cards here are read at a glance and never touched — a line saying "8 to
review" has done its whole job before you decide not to click it — so an
interaction count alone convicts precisely the cards that work best. Two
things keep the numbers honest:

  * **days, not totals.** "Still used?" is a question about habit, and a
    running total cannot tell fifty opens last March from five a week since.
  * **the band it was sitting in.** A card the desk folded into `quiet` every
    day for a month and that you never opened is dead weight. A card sitting
    in `needs`/`has` that you never opened is being read exactly as designed —
    the same shape as the "silence is a signal, but only if the source would
    have sent something" rule in DESIGN.md.

So this reports what it saw and refuses to compute a verdict out of the
interaction count alone. `summary()` returns both halves and the panel says
which cards are *candidates*, never which are worthless.

Same never-raise contract as `failures.py`: telemetry is not load-bearing.
"""

import sqlite3
from datetime import datetime

from fastapi import APIRouter
from pydantic import BaseModel, Field

from . import db

router = APIRouter(prefix="/api/usage")

KINDS = {"seen", "open", "sheet", "command"}
BANDS = {"needs", "has", "quiet", ""}

# One POST carries a whole flush, and the desk buffers ~20 impressions on
# load. A ceiling stops a stuck client turning a bug into unbounded writes.
MAX_BATCH = 200


def record(kind: str, label: str, band: str = "", count: int = 1) -> None:
    """Add `count` to today's (kind, label, band) tally. Never raises."""
    if kind not in KINDS or band not in BANDS or not label:
        return
    try:
        with db.session() as con, con:
            con.execute(
                "INSERT INTO usage (day, kind, label, band, count, last_at)"
                " VALUES (?,?,?,?,?,?)"
                " ON CONFLICT(day, kind, label, band) DO UPDATE SET"
                "   count=usage.count+excluded.count,"
                "   last_at=excluded.last_at",
                (db.day_key(), kind, label[:100], band, max(1, int(count)),
                 datetime.now().isoformat(timespec="seconds")))
    except (sqlite3.Error, ValueError, TypeError) as e:
        print(f"[usage] could not record {kind}/{label}: {e!r}")


def summary(days: int = 30) -> dict:
    """Per label, over the trailing `days` days of *recorded* history.

    The window is counted from the newest recorded day, not from today, so a
    backend that was off for a week doesn't quietly shrink every denominator
    and make the whole desk look abandoned.
    """
    try:
        with db.session() as con:
            newest = con.execute("SELECT MAX(day) AS d FROM usage").fetchone()["d"]
            if not newest:
                return {"labels": [], "commands": [],
                        "days_covered": 0, "since": None}

            since = con.execute(
                "SELECT day FROM (SELECT DISTINCT day FROM usage"
                "  WHERE day <= ? ORDER BY day DESC LIMIT ?)"
                " ORDER BY day LIMIT 1", (newest, days)).fetchone()["day"]

            rows = con.execute(
                "SELECT label,"
                # days the card was on the desk at all, and the split that
                # says whether it ever had anything to report
                "  COUNT(DISTINCT CASE WHEN kind='seen' THEN day END) AS days_seen,"
                "  COUNT(DISTINCT CASE WHEN kind='seen' AND band<>'quiet'"
                "        THEN day END) AS days_active,"
                # you unfolded a card the desk had folded — the ranking being
                # told it got this one wrong
                "  COUNT(DISTINCT CASE WHEN kind='open' THEN day END) AS days_opened,"
                "  COALESCE(SUM(CASE WHEN kind='open' THEN count END), 0) AS opens,"
                # the full sheet: engagement that works for every band, since
                # an unfoldable card is only ever one the desk folded
                "  COUNT(DISTINCT CASE WHEN kind='sheet' THEN day END) AS days_sheet,"
                "  COALESCE(SUM(CASE WHEN kind='sheet' THEN count END), 0) AS sheet_opens,"
                "  MAX(CASE WHEN kind<>'seen' THEN last_at END) AS last_touch"
                " FROM usage WHERE day >= ? AND kind NOT IN ('command', 'cleared')"
                " GROUP BY label", (since,)).fetchall()

            covered = con.execute(
                "SELECT COUNT(DISTINCT day) AS n FROM usage WHERE day >= ?",
                (since,)).fetchone()["n"]

            commands = [dict(r) for r in con.execute(
                "SELECT label, COALESCE(SUM(count),0) AS runs,"
                "  COUNT(DISTINCT day) AS days, MAX(last_at) AS last_at"
                " FROM usage WHERE day >= ? AND kind='command'"
                " GROUP BY label ORDER BY runs DESC", (since,))]
    except sqlite3.Error:
        return {"labels": [], "commands": [], "days_covered": 0, "since": None}

    labels = []
    for r in rows:
        d = dict(r)
        # Sheets with no card behind them (status, review, usage itself) log a
        # `sheet` row and nothing else. They are not desk cards, so scoring
        # them alongside the cards puts a permanent "0/0" row in a table whose
        # whole job is to be read as "days active out of days on the desk".
        if not d["days_seen"]:
            continue
        d["touches"] = d["opens"] + d["sheet_opens"]
        d["days_touched"] = max(d["days_opened"], d["days_sheet"])
        # The only claim worth making automatically, and it is deliberately
        # narrow: the desk judged this card had nothing to say on every single
        # day it saw it, and you never went looking either. Anything with a
        # day in needs/has is excluded — that card was being read.
        d["idle"] = (d["days_seen"] >= 7
                     and d["days_active"] == 0
                     and d["touches"] == 0)
        labels.append(d)

    # Least-touched first: the list exists to surface the bottom, and burying
    # that under the cards you obviously use is how a panel goes unread.
    labels.sort(key=lambda x: (x["touches"], -x["days_seen"]))
    return {"labels": labels, "commands": commands,
            "days_covered": covered, "since": since}


# ---------------------------------------------------------------- cleared
#
# "N cleared today" in the plan view's list header: asks finished by acting,
# not by snoozing. Stored in the same table as kind `cleared`, but as a *set*
# per day rather than a tally — acting on one ask twice (tick a habit, untick,
# tick) is one ask cleared, and an undo has to be able to take it back out.
# So these two write the row outright instead of going through record()'s
# count-adding upsert, and the generic POST can't send the kind at all.

def set_cleared(ask_id: str, cleared: bool = True) -> int:
    """Mark (or, for an undo, unmark) an ask cleared today. Returns today's
    count. Never raises."""
    try:
        with db.session() as con, con:
            if cleared:
                con.execute(
                    "INSERT OR REPLACE INTO usage (day, kind, label, band, count, last_at)"
                    " VALUES (?, 'cleared', ?, '', 1, ?)",
                    (db.day_key(), ask_id[:100],
                     datetime.now().isoformat(timespec="seconds")))
            else:
                con.execute("DELETE FROM usage WHERE day=? AND kind='cleared' AND label=?",
                            (db.day_key(), ask_id[:100]))
    except sqlite3.Error as e:
        print(f"[usage] could not mark {ask_id} cleared={cleared}: {e!r}")
    return cleared_today()["count"]


def cleared_today() -> dict:
    day = db.day_key()
    try:
        with db.session() as con:
            ids = [r["label"] for r in con.execute(
                "SELECT label FROM usage WHERE day=? AND kind='cleared'"
                " ORDER BY last_at, rowid",
                (day,))]
    except sqlite3.Error:
        ids = []
    return {"day": day, "count": len(ids), "ask_ids": ids}


class ClearedIn(BaseModel):
    ask_id: str = Field(min_length=1, max_length=100)
    undo: bool = False


@router.get("/cleared")
def get_cleared() -> dict:
    return cleared_today()


@router.post("/cleared")
def post_cleared(body: ClearedIn) -> dict:
    set_cleared(body.ask_id, not body.undo)
    return cleared_today()


class Event(BaseModel):
    kind: str = Field(pattern="^(seen|open|sheet|command)$")
    label: str = Field(min_length=1, max_length=100)
    band: str = Field(default="", pattern="^(needs|has|quiet|)$")
    count: int = Field(default=1, ge=1, le=1000)


class Batch(BaseModel):
    events: list[Event] = Field(max_length=MAX_BATCH)


@router.post("")
def report(body: Batch) -> dict:
    """The desk flushes a buffer here — one request per flush, not per click."""
    for e in body.events:
        record(e.kind, e.label, e.band, e.count)
    return {"ok": True, "recorded": len(body.events)}


@router.get("/summary")
def usage_summary(days: int = 30) -> dict:
    return summary(max(1, min(days, 365)))


@router.delete("")
def clear() -> dict:
    try:
        with db.session() as con, con:
            return {"cleared": con.execute("DELETE FROM usage").rowcount}
    except sqlite3.Error:
        return {"cleared": 0}
