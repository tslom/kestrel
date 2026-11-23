"""Scheduled prompts: a prompt that opens a new Kestrel tab at a time, once or
on repeat ("Sunday 8pm: what's due this week, and unpaid expenses").

This is an agent doing a job at a time, which is the one kind of thing the
lazy-over-scheduled rule doesn't cover: a card can wait to be opened, a
digest you asked to *be handed* at 8pm can't. Still, nothing here runs a
model. The backend only says what's due; the page opens the tab, through the
same path an open_tab handoff takes (PlanApp's handOff), so the caps on
working tabs, the queue and the tab's model all apply unchanged.

**Due, claim, ran.** The page asks `/due` once a minute. To run one it
`claim`s it first (a conditional UPDATE, so two windows can't both open it),
opens the tab, then reports `ran`, which advances `next_at`. A claim that
never reports back (the window closed mid-open) lapses after CLAIM_S and the
prompt is due again.

**The lid.** A closed laptop is asleep, so nothing fires at 8pm in a bag.
The scheduled wake (wake.py) is deliberately not used: a dark wake keeps
the display off and the page's timers frozen, and an agent turn takes minutes,
longer than the wake lasts. What it does instead: a missed time runs **once**
on the first poll after the lid opens and Kestrel is up, however many times
it was missed, if it's within GRACE of when it was due; past that a repeating
prompt skips to its next time (`skipped`), and a one-off still runs late,
since it has no next time and you asked for it once. The time it was due goes
in the brief, so a late digest says it's late.

Times are local wall-clock (the machine's zone), like everything a person
sets here. Day keys are not involved: "Sunday 8pm" is a clock time, not a
day_key day.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from . import db

router = APIRouter(prefix="/api/schedules")

MODES = ("general", "study", "dev")
GRACE = timedelta(hours=12)
CLAIM_S = 300
DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
FULL_DAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
_HHMM = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")
_ONCE = re.compile(r"^\d{4}-\d{2}-\d{2}T([01]\d|2[0-3]):[0-5]\d$")


class ScheduleError(ValueError):
    pass


def _now() -> datetime:
    return datetime.now().replace(second=0, microsecond=0)


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="minutes")


def parse_repeat(repeat: str | None) -> tuple[str, set[int]] | None:
    """None (once), ('daily', all days) or ('weekly', {weekday…}, Mon=0)."""
    if repeat in (None, "", "once"):
        return None
    if repeat == "daily":
        return "daily", set(range(7))
    if repeat.startswith("weekly:"):
        try:
            days = {int(d) for d in repeat[7:].split(",") if d.strip()}
        except ValueError:
            days = set()
        if days and days <= set(range(7)):
            return "weekly", days
    raise ScheduleError("repeat is once, daily, or weekly:<days, Mon=0 … Sun=6>")


def next_time(at_time: str, repeat: str | None, after: datetime) -> datetime | None:
    """The first occurrence strictly after `after`. A one-off's `at_time` is
    the whole instant; a repeat's is HH:MM."""
    rule = parse_repeat(repeat)
    if rule is None:
        when = datetime.fromisoformat(at_time)
        return when if when > after else None
    m = _HHMM.match(at_time)
    if not m:
        raise ScheduleError("a repeating prompt's time is HH:MM")
    hh, mm = int(m.group(1)), int(m.group(2))
    _, days = rule
    day = after.replace(hour=hh, minute=mm, second=0, microsecond=0)
    for _ in range(8):
        if day > after and day.weekday() in days:
            return day
        day += timedelta(days=1)
    return None


def describe(row: dict) -> str:
    """'Sundays at 20:00', 'every day at 07:30', 'once, YYYY-MM-DD 09:00'."""
    rule = parse_repeat(row["repeat"])
    if rule is None:
        return f"once, {row['at_time'].replace('T', ' ')}"
    kind, days = rule
    if kind == "daily" or len(days) == 7:
        return f"every day at {row['at_time']}"
    if days == {0, 1, 2, 3, 4}:
        return f"weekdays at {row['at_time']}"
    if len(days) == 1:
        return f"{FULL_DAYS[next(iter(days))]}s at {row['at_time']}"
    names = ", ".join(DAYS[d].capitalize() for d in sorted(days))
    return f"{names} at {row['at_time']}"


def _row(r) -> dict:
    d = dict(r)
    d["enabled"] = bool(d["enabled"])
    d["when"] = describe(d)
    return d


class ScheduleIn(BaseModel):
    title: str = Field(min_length=1, max_length=80)
    prompt: str = Field(min_length=1, max_length=8000)
    mode: str = "general"
    project: str | None = None
    model: str | None = None
    at_time: str
    repeat: str | None = None


def _check(body: ScheduleIn) -> None:
    if body.mode not in MODES:
        raise ScheduleError(f"mode is one of {', '.join(MODES)}")
    if body.mode == "dev" and not (body.project or "").strip():
        raise ScheduleError("a dev prompt names its project")
    if parse_repeat(body.repeat) is None:
        if not _ONCE.match(body.at_time):
            raise ScheduleError("a one-off's time is YYYY-MM-DDTHH:MM")
    elif not _HHMM.match(body.at_time):
        raise ScheduleError("a repeating prompt's time is HH:MM")


def create(body: ScheduleIn, now: datetime | None = None, con=None) -> dict:
    _check(body)
    now = now or _now()
    nxt = next_time(body.at_time, body.repeat, now - timedelta(minutes=1))
    if nxt is None:
        raise ScheduleError("that time has already passed")
    with db.borrow(con) as c:
        with c:
            rid = c.execute(
                "INSERT INTO scheduled_prompts (title, prompt, mode, project, model,"
                " at_time, repeat, next_at, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (body.title.strip(), body.prompt.strip(), body.mode,
                 (body.project or "").strip() or None, (body.model or "").strip() or None,
                 body.at_time, body.repeat or None, _iso(nxt), _iso(now))).lastrowid
        return _row(c.execute("SELECT * FROM scheduled_prompts WHERE id=?", (rid,)).fetchone())


def listing(con=None) -> list[dict]:
    with db.borrow(con) as c:
        return [_row(r) for r in c.execute(
            "SELECT * FROM scheduled_prompts ORDER BY enabled DESC,"
            " next_at IS NULL, next_at, id")]


def _skip_stale(c, now: datetime) -> list[int]:
    """Repeating prompts missed by more than GRACE move to their next time
    without running; returns their ids."""
    skipped = []
    for r in c.execute("SELECT * FROM scheduled_prompts WHERE enabled=1"
                       " AND next_at IS NOT NULL AND repeat IS NOT NULL AND next_at < ?",
                       (_iso(now - GRACE),)).fetchall():
        nxt = next_time(r["at_time"], r["repeat"], now)
        c.execute("UPDATE scheduled_prompts SET next_at=?, claimed_at=NULL WHERE id=?",
                  (_iso(nxt) if nxt else None, r["id"]))
        skipped.append(r["id"])
    return skipped


def due(now: datetime | None = None, con=None) -> list[dict]:
    """What should open now: enabled, due, not held by a live claim."""
    now = now or _now()
    lapsed = _iso(now - timedelta(seconds=CLAIM_S))
    with db.borrow(con) as c:
        with c:
            _skip_stale(c, now)
        rows = c.execute(
            "SELECT * FROM scheduled_prompts WHERE enabled=1 AND next_at IS NOT NULL"
            " AND next_at <= ? AND (claimed_at IS NULL OR claimed_at < ?)"
            " ORDER BY next_at", (_iso(now), lapsed)).fetchall()
    return [_row(r) for r in rows]


def brief(row: dict, now: datetime | None = None) -> str:
    """The new tab's first message: the prompt, with when it was meant for."""
    now = now or _now()
    meant = datetime.fromisoformat(row["next_at"])
    late = now - meant
    when = describe(row)
    head = f"Scheduled prompt “{row['title']}” ({when})"
    if late > timedelta(minutes=10):
        hours = late.total_seconds() / 3600
        ago = f"{round(hours)}h" if hours >= 1 else f"{round(late.total_seconds() / 60)} min"
        head += (f", due {meant:%a %H:%M} and running {ago} late"
                 " (Kestrel wasn't open then)")
    return f"{head}. Nobody is waiting on this tab live, so finish with a short report.\n\n{row['prompt']}"


def claim(rid: int, now: datetime | None = None, con=None) -> dict | None:
    """Hold a due prompt for this page. None if it isn't due or another page
    has it. The answer carries the brief to open the tab with."""
    now = now or _now()
    lapsed = _iso(now - timedelta(seconds=CLAIM_S))
    with db.borrow(con) as c:
        with c:
            cur = c.execute(
                "UPDATE scheduled_prompts SET claimed_at=? WHERE id=? AND enabled=1"
                " AND next_at IS NOT NULL AND next_at <= ?"
                " AND (claimed_at IS NULL OR claimed_at < ?)",
                (_iso(now), rid, _iso(now), lapsed))
            if cur.rowcount != 1:
                return None
        row = _row(c.execute("SELECT * FROM scheduled_prompts WHERE id=?", (rid,)).fetchone())
    return {**row, "brief": brief(row, now)}


def ran(rid: int, tab: str | None, now: datetime | None = None, con=None) -> dict:
    """The tab opened: move to the next time (none for a one-off)."""
    now = now or _now()
    with db.borrow(con) as c:
        r = c.execute("SELECT * FROM scheduled_prompts WHERE id=?", (rid,)).fetchone()
        if r is None:
            raise KeyError(rid)
        nxt = (next_time(r["at_time"], r["repeat"], now)
               if r["repeat"] else None)
        with c:
            c.execute(
                "UPDATE scheduled_prompts SET next_at=?, claimed_at=NULL, last_run_at=?,"
                " last_tab=?, runs=runs+1, enabled=? WHERE id=?",
                (_iso(nxt) if nxt else None, _iso(now), tab,
                 int(bool(r["enabled"]) and nxt is not None), rid))
        return _row(c.execute("SELECT * FROM scheduled_prompts WHERE id=?", (rid,)).fetchone())


def set_enabled(rid: int, enabled: bool, now: datetime | None = None, con=None) -> dict:
    """Pause or resume. Resuming starts from the next time after now, so a
    paused week isn't run on the spot."""
    now = now or _now()
    with db.borrow(con) as c:
        r = c.execute("SELECT * FROM scheduled_prompts WHERE id=?", (rid,)).fetchone()
        if r is None:
            raise KeyError(rid)
        nxt = r["next_at"]
        if enabled:
            when = next_time(r["at_time"], r["repeat"], now)
            if when is None:
                raise ScheduleError("that one-off's time has passed; make a new one")
            nxt = _iso(when)
        with c:
            c.execute("UPDATE scheduled_prompts SET enabled=?, next_at=?, claimed_at=NULL"
                      " WHERE id=?", (int(enabled), nxt, rid))
        return _row(c.execute("SELECT * FROM scheduled_prompts WHERE id=?", (rid,)).fetchone())


def delete(rid: int, con=None) -> bool:
    with db.borrow(con) as c:
        with c:
            return c.execute("DELETE FROM scheduled_prompts WHERE id=?", (rid,)).rowcount > 0


# --- routes ---------------------------------------------------------------

def _guard(fn, *a):
    try:
        return fn(*a)
    except ScheduleError as e:
        raise HTTPException(400, str(e))
    except KeyError:
        raise HTTPException(404, "no such scheduled prompt")


@router.get("")
def get_all() -> dict:
    return {"schedules": listing()}


@router.post("")
def post(body: ScheduleIn) -> dict:
    return _guard(create, body)


@router.get("/due")
def get_due() -> dict:
    return {"due": [{"id": r["id"], "title": r["title"], "next_at": r["next_at"]} for r in due()]}


@router.post("/{rid}/claim")
def post_claim(rid: int) -> dict:
    """The tab's spec, held for this page. A dev prompt's project resolves
    here, by the name open_tab takes, to the path the page opens."""
    from . import console_profiles
    got = claim(rid)
    if got and got["mode"] == "dev":
        try:
            got["project_path"] = str(console_profiles.resolve_project(got["project"]))
        except console_profiles.ProfileError as e:
            return {"ok": False, "reason": str(e), "schedule": None}
    return {"ok": got is not None, "schedule": got}


class RanIn(BaseModel):
    tab: str | None = None


@router.post("/{rid}/ran")
def post_ran(rid: int, body: RanIn) -> dict:
    return _guard(ran, rid, body.tab)


class EnabledIn(BaseModel):
    enabled: bool


@router.post("/{rid}/enabled")
def post_enabled(rid: int, body: EnabledIn) -> dict:
    return _guard(set_enabled, rid, body.enabled)


@router.delete("/{rid}")
def remove(rid: int) -> dict:
    if not delete(rid):
        raise HTTPException(404, "no such scheduled prompt")
    return {"ok": True}
