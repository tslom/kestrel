"""Usage: is the desk's ranking right, and does every card still earn its slot?

Tallies per day of `seen` / `open` / `sheet` / `command` (plus the `cleared` set). Reports what
it saw and never a verdict from interaction counts alone (glance-read cards are never clicked).
Never raises: telemetry. Design: DESIGN.md → Usage.
"""
import sqlite3
from datetime import datetime

from fastapi import APIRouter
from pydantic import BaseModel, Field

from . import db

router = APIRouter(prefix="/api/usage")

KINDS = {"seen", "open", "sheet", "command"}
BANDS = {"needs", "has", "quiet", ""}

# One POST is a whole flush (~20 impressions on load); the ceiling bounds a stuck client.
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
    """Per label, over the trailing `days` days of recorded history (counted from the newest recorded day, not today)."""
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
                # days on the desk, and days it had anything to report
                "  COUNT(DISTINCT CASE WHEN kind='seen' THEN day END) AS days_seen,"
                "  COUNT(DISTINCT CASE WHEN kind='seen' AND band<>'quiet'"
                "        THEN day END) AS days_active,"
                # unfolded a card the desk had folded
                "  COUNT(DISTINCT CASE WHEN kind='open' THEN day END) AS days_opened,"
                "  COALESCE(SUM(CASE WHEN kind='open' THEN count END), 0) AS opens,"
                # the full sheet: works for every band
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
        # Sheets with no card (status, review, usage) are not desk cards; skip them (they'd read "0/0").
        if not d["days_seen"]:
            continue
        d["touches"] = d["opens"] + d["sheet_opens"]
        d["days_touched"] = max(d["days_opened"], d["days_sheet"])
        # The one automatic verdict, deliberately narrow: quiet every day seen and never touched.
        d["idle"] = (d["days_seen"] >= 7
                     and d["days_active"] == 0
                     and d["touches"] == 0)
        labels.append(d)

    # Least-touched first: the list exists to surface the bottom.
    labels.sort(key=lambda x: (x["touches"], -x["days_seen"]))
    return {"labels": labels, "commands": commands,
            "days_covered": covered, "since": since}


# ---------------------------------------------------------------- cleared
# A per-day *set* of asks finished by acting (kind `cleared`), not a tally: these write the row
# outright instead of record()'s count-adding upsert, and the generic POST can't send the kind.

def set_cleared(ask_id: str, cleared: bool = True) -> int:
    """Mark (or, for an undo, unmark) an ask cleared today. Returns today's count. Never raises."""
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
