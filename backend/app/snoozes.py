"""Snoozes: push an ask out of the "needs you" list until a time.

Times are local wall-clock ISO datetimes. "tonight", "tomorrow" and "the day
before a deadline" resolve here against `db.day_key()` (4am rollover), never
the browser clock. Design: DESIGN.md → The "needs you" list.
"""

from datetime import datetime, timedelta

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, model_validator

from . import db

router = APIRouter(prefix="/api/snoozes")

# Preset times on a day key: tonight = HabitsWidget's `evening`; the others a working morning, not 4am.
TONIGHT = "20:00"
MORNING = "08:00"


def _now() -> datetime:
    return datetime.now().replace(microsecond=0)


def resolve(preset: str | None = None, day: str | None = None,
            until: str | None = None, now: datetime | None = None) -> datetime:
    """The instant a snooze ends. Exactly one of the three is given.

    Raises ValueError for anything that isn't in the future: a snooze that
    has already ended would leave the row where it was and look like the
    button did nothing.
    """
    now = now or _now()
    today = db.day_key(now)
    if preset == "tonight":
        at = db.day_instant(today, TONIGHT)
        if at <= now:
            raise ValueError("it's already tonight")
    elif preset == "tomorrow":
        nxt = (datetime.fromisoformat(today) + timedelta(days=1)).date().isoformat()
        at = db.day_instant(nxt, MORNING)
    elif day is not None:
        at = db.day_instant(day, MORNING)
    elif until is not None:
        at = datetime.fromisoformat(until).replace(microsecond=0)
        if at.tzinfo is not None:  # a browser toISOString(): convert to local
            at = at.astimezone().replace(tzinfo=None)
    else:
        raise ValueError("say when: a preset, a day or an until")
    if at <= now:
        raise ValueError(f"{at.isoformat()} has already passed")
    return at


def active(con=None, now: datetime | None = None) -> dict[str, str]:
    """{ask_id: until} for snoozes still running. Prunes the rest."""
    cutoff = (now or _now()).isoformat()
    with db.borrow(con) as c, c:
        c.execute("DELETE FROM snoozes WHERE until <= ?", (cutoff,))
        return {r["ask_id"]: r["until"]
                for r in c.execute("SELECT ask_id, until FROM snoozes ORDER BY until")}


def snooze(ask_id: str, at: datetime, con=None) -> dict:
    with db.borrow(con) as c, c:
        c.execute(
            "INSERT INTO snoozes (ask_id, until, created) VALUES (?,?,?)"
            " ON CONFLICT(ask_id) DO UPDATE SET until=excluded.until,"
            " created=excluded.created",
            (ask_id, at.isoformat(), _now().isoformat()))
    return {"ask_id": ask_id, "until": at.isoformat()}


def wake(ask_id: str, con=None) -> bool:
    """End a snooze now (undo, or "bring it back"). True if one was running."""
    with db.borrow(con) as c, c:
        return c.execute("DELETE FROM snoozes WHERE ask_id=?", (ask_id,)).rowcount > 0


class SnoozeIn(BaseModel):
    ask_id: str = Field(min_length=1, max_length=200)
    preset: str | None = Field(default=None, pattern="^(tonight|tomorrow)$")
    day: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    until: str | None = None

    @model_validator(mode="after")
    def one_when(self):
        if sum(x is not None for x in (self.preset, self.day, self.until)) != 1:
            raise ValueError("give exactly one of preset, day, until")
        return self


class WakeIn(BaseModel):
    ask_id: str = Field(min_length=1, max_length=200)


@router.get("")
def get_snoozes() -> dict:
    return {"snoozes": active(), "day": db.day_key()}


@router.post("")
def post_snooze(body: SnoozeIn) -> dict:
    try:
        at = resolve(body.preset, body.day, body.until)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return snooze(body.ask_id, at)


# Body, not path segment: ask ids carry colons and slashes (note paths).
@router.post("/wake")
def post_wake(body: WakeIn) -> dict:
    return {"ask_id": body.ask_id, "woke": wake(body.ask_id)}
