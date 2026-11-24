"""The scheduled wake: wake the machine shortly before each alarm so a poll cycle can revise it.

A reconciler over `pmset -g sched` (the OS is the source of truth; no table), diffed against
the desired wake set since `pmset schedule` is append-only global state. Writes need root via
`sudo -n` (sudoers drop-in); the read does not. A failed wake costs freshness, never the alarm.
Design: DESIGN.md → Reaching the user (§21).
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from . import db
from .config import (WAKE_COALESCE_MIN, WAKE_DRY_RUN, WAKE_ENABLED,
                     WAKE_HORIZON_HOURS, WAKE_LEAD_MIN, WAKE_MAX_EVENTS,
                     WAKE_OWNER, WAKE_TOLERANCE_MIN)

_PMSET = "/usr/bin/pmset"   # absolute: the sudoers rule names this path
_TIMEOUT = 10
_TYPE = "wake"          # never `wakeorpoweron`: don't boot a machine shut down on purpose

# pmset accepts "MM/dd/yy HH:mm:ss" but prints MM/dd/yyyy; write the 2-digit form.
_WRITE_FMT = "%m/%d/%y %H:%M:%S"
_READ_FMT = "%m/%d/%Y %H:%M:%S"

# ` [0]  wake at 08/11/2026 18:35:17 by 'com.apple.alarm...' User visible: true`
# Repeating events print without a `[N]` index, so they can't match.
_SCHED_RE = re.compile(
    r"^\s*\[\d+\]\s+(?P<type>\w+)\s+at\s+"
    r"(?P<when>\d{2}/\d{2}/\d{4}\s+\d{2}:\d{2}:\d{2})\s+by\s+'(?P<owner>[^']*)'")


@dataclass
class WakePlan:
    add: list[datetime] = field(default_factory=list)
    cancel: list[datetime] = field(default_factory=list)
    keep: list[datetime] = field(default_factory=list)

    def summary(self) -> dict:
        return {"add": len(self.add), "cancel": len(self.cancel),
                "keep": len(self.keep)}

    def is_noop(self) -> bool:
        return not (self.add or self.cancel)


# ------------------------------------------------------------------ the OS

def _run(args: list[str], *, root: bool = False) -> str:
    """`root=True` only for the two write verbs; `-g sched` stays unprivileged so a
    machine without the sudoers drop-in can still read. `sudo -n` fails fast with no
    rule installed, which apply() logs and retries.
    """
    cmd = ["sudo", "-n", _PMSET] if root else [_PMSET]
    proc = subprocess.run([*cmd, *args], capture_output=True, text=True,
                          timeout=_TIMEOUT)
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout or "pmset failed").strip())
    return proc.stdout


def scheduled(*, now: datetime) -> list[datetime]:
    """Our own future wake events, soonest first.

    The WAKE_OWNER filter is all that stands between this module (every cancel reads from
    this list) and the system's own events. Past events are dropped, not cancelled.
    """
    out = _run(["-g", "sched"])
    found = []
    for line in out.splitlines():
        m = _SCHED_RE.match(line)
        if not m or m.group("owner") != WAKE_OWNER or m.group("type") != _TYPE:
            continue
        try:
            when = datetime.strptime(m.group("when"), _READ_FMT)
        except ValueError:
            continue        # a format we don't know is not one we should cancel
        # Naive local wall clock: astimezone() attaches the offset in force on that date (DST-safe).
        when = when.astimezone()
        if when > now:
            found.append(when)
    return sorted(found)


def _schedule(at: datetime) -> None:
    _run(["schedule", _TYPE, at.astimezone().strftime(_WRITE_FMT), WAKE_OWNER],
         root=True)


def _cancel(at: datetime) -> None:
    """Cancel one event by exact type/date/owner; never `cancelall` (it would take other owners' wakes)."""
    _run(["schedule", "cancel", _TYPE,
          at.astimezone().strftime(_WRITE_FMT), WAKE_OWNER], root=True)


# ---------------------------------------------------------------- the desire

def desired(con, *, now: datetime) -> list[datetime]:
    """When to wake: a lead before every future alarm in the horizon, from the alarms table.

    Wakes are only a set of times (a cycle refreshes all alarms). Past wake times are dropped, not clamped.
    """
    horizon = now + timedelta(hours=WAKE_HORIZON_HOURS)
    lead = timedelta(minutes=WAKE_LEAD_MIN)
    rows = con.execute(
        "SELECT fire_at FROM alarms ORDER BY fire_at").fetchall()

    times = []
    for row in rows:
        try:
            fire_at = datetime.fromisoformat(row["fire_at"])
        except (ValueError, TypeError):
            continue        # alarms.Alarm validates this; a bad row is not ours to fix
        if fire_at <= now or fire_at > horizon:
            continue
        at = fire_at - lead
        if at > now:
            times.append(at)

    # Scheduled emails need the machine awake *at* send_at, so they join after coalescing.
    from .email_schedule import wake_times
    sends = wake_times(con, now=now, horizon=horizon)
    return sorted(set(_coalesce(sorted(times)) + sends))[:WAKE_MAX_EVENTS]


def _coalesce(times: list[datetime]) -> list[datetime]:
    """Collapse wakes within WAKE_COALESCE_MIN to the earliest (the later alarms get covered too)."""
    window = timedelta(minutes=WAKE_COALESCE_MIN)
    kept: list[datetime] = []
    for at in times:
        if not kept or at - kept[-1] > window:
            kept.append(at)
    return kept


# ---------------------------------------------------------------- the diff

def plan(want: list[datetime], have: list[datetime]) -> WakePlan:
    """Pure diff with a deadband. Matching is nearest-first within tolerance, not positional,
    so one new early alarm doesn't churn every pairing."""
    tolerance = timedelta(minutes=WAKE_TOLERANCE_MIN)
    p = WakePlan()
    unmatched = sorted(have)

    for at in sorted(want):
        near = [(abs(at - s), s) for s in unmatched if abs(at - s) <= tolerance]
        if near:
            _, match = min(near, key=lambda pair: pair[0])
            unmatched.remove(match)
            p.keep.append(match)
        else:
            p.add.append(at)

    p.cancel = unmatched
    return p


# ---------------------------------------------------------------- apply

def apply(p: WakePlan) -> dict:
    """Cancel first, then add: a failed cancel is only noise, a failed add leaves an alarm
    unrefreshed. Failures are retried next cycle (the set is recomputed from the OS)."""
    out = {"added": 0, "cancelled": 0, "failed": 0}
    for at in p.cancel:
        try:
            _cancel(at)
            out["cancelled"] += 1
        except Exception as e:  # retried next cycle
            print(f"[wake] cancel failed for {at.isoformat()}: {e!r}")
            out["failed"] += 1
    for at in p.add:
        try:
            _schedule(at)
            out["added"] += 1
        except Exception as e:  # retried next cycle
            print(f"[wake] schedule failed for {at.isoformat()}: {e!r}")
            out["failed"] += 1
    return out


def reconcile(con=None, *, dry_run: bool | None = None,
              now: datetime | None = None) -> dict:
    """One wake-reconcile cycle; runs after alarms.reconcile(), which populates the table it reads."""
    if not WAKE_ENABLED:
        return {"enabled": False}
    dry_run = WAKE_DRY_RUN if dry_run is None else dry_run
    now = now or datetime.now().astimezone()
    with db.borrow(con) as con:
        have = scheduled(now=now)
        p = plan(desired(con, now=now), have)

    info = p.summary()
    info["dry_run"] = dry_run
    if not dry_run:
        info["applied"] = apply(p)
    if not p.is_noop():
        print(f"[wake] {'(dry run) ' if dry_run else ''}{info}")
        for at in p.add:
            print(f"[wake]   + {at.isoformat(timespec='seconds')}")
        for at in p.cancel:
            print(f"[wake]   - {at.isoformat(timespec='seconds')}"
                  f"{' (dry run: kept)' if dry_run else ''}")
    return info


def status(*, now: datetime | None = None) -> dict:
    """What is actually scheduled right now — the inspection view, and the only
    read that answers whether pmset accepted anything at all."""
    now = now or datetime.now().astimezone()
    try:
        return {"owner": WAKE_OWNER,
                "scheduled": [at.isoformat(timespec="seconds")
                              for at in scheduled(now=now)]}
    except Exception as e:
        return {"owner": WAKE_OWNER, "error": repr(e)}
