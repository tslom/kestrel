"""The scheduled wake, so the prediction is still true when it fires.

Steps 1-4 built a channel that reaches a shut laptop: the reconciler writes a
future-dated alarm to a surface iCloud syncs, and the phone fires it hours later
with nothing of ours running. That model carries an assumption it never states —
that the alarm written before the lid closed is still *correct* when it goes off.

It often isn't. alarms.py is a reconciler rather than an emitter for one reason:
a prediction can be revised, so a flight that slips must drag its alarm with it.
But revision only happens on a poll cycle, and a poll cycle only happens while
the machine is awake. The eight hours before a 6am alarm are precisely the hours
it is asleep, which makes the argument for a reconciler quietly stop holding
exactly when it matters most. The phone would fire a 5:40am check-in for a flight
that moved to noon, and every mechanism built to prevent that was unconscious.

So: ask the OS to wake the machine a little before each alarm. One cycle runs,
the sources are re-read, the alarm is amended or withdrawn, and it goes back to
sleep. That is the whole feature.

**This is a reconciler too, for a new reason.** `pmset schedule` is append-only
global state that outlives the process. Appending a wake every poll cycle would
leave hundreds of duplicate events by morning, so the desired set is diffed
against what is actually scheduled — the same shape as alarms.py, arrived at from
a different direction.

**The OS is the source of truth, not a table.** This is the one place alarm delivery does
*not* persist its own record, and the contrast is the point. alarms.py must keep
`external_id`, because a Reminders item is invisible from here and losing the
handle strands an alarm on the phone forever. A scheduled wake has no such
problem: `pmset -g sched` lists exactly what is scheduled, so a table could only
ever drift from it. Reading the OS also means a wake left behind by an older
build, or one the user cancelled by hand, is reconciled rather than
double-counted. No migration; nothing to back up.

**Scheduling needs root; reading does not.** `pmset schedule` checks euid and
refuses ("This operation must be run as root") even for an admin user — the
rights database's `system.preferences.energysaver` entry, which is class `user`
and group `admin`, governs the Energy Saver pane through the Admin framework and
is never consulted by this binary. So the two write verbs go through `sudo -n`
against a sudoers drop-in scoped to `/usr/bin/pmset schedule`, and `-g sched`
stays unprivileged. `-n` never prompts: with no rule installed the writes fail
the same way any other pmset failure does, which the next paragraph is about.

**A failed wake is safe, twice over.** The alarm is already on the phone and
fires with nothing of ours running, so a wake that never happens costs
*freshness*, never the alert itself. And if the machine wakes somewhere with no
network — in a bag, tethered to nothing — every provider fails, every source
returns `unknown`, and step 1's three-state protocol *holds* the existing alarms
rather than withdrawing them. A wake that accomplishes nothing degrades to
exactly the behaviour of not having woken at all. Both properties are inherited
rather than designed here, which is why this step can afford to fail soft
everywhere.
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

_PMSET = "/usr/bin/pmset"   # absolute: it is what the sudoers rule names, and a
                            # NOPASSWD rule matching a bare word off $PATH is the
                            # classic way to hand out more than was intended.
_TIMEOUT = 10           # pmset answers immediately or something is very wrong
_TYPE = "wake"          # never `wakeorpoweron`: a machine that was shut *down*
                        # was shut down on purpose, and booting it from a bag is
                        # a different and much ruder act than waking it.

# pmset is asymmetric about years and the man page only documents the input
# side: it *accepts* "MM/dd/yy HH:mm:ss" and *prints* MM/dd/yyyy. Writing the
# 4-digit form back is not worth the risk of it being read as year 2 0 2 6.
_WRITE_FMT = "%m/%d/%y %H:%M:%S"
_READ_FMT = "%m/%d/%Y %H:%M:%S"

# ` [0]  wake at 08/11/2026 18:35:17 by 'com.apple.alarm...' User visible: true`
# Unanchored at the end because of that trailing field. Repeating events print
# in a separate block with no `[N]` index, so they cannot match this.
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
    """`root=True` only for the two verbs that need it. `pmset schedule` checks
    euid before it does anything — the rights database's `energysaver` entry
    governs the Energy Saver pane through the Admin framework and never reaches
    this binary — while `-g sched` reads fine as the logged-in user. Privilege
    is asked for per-call rather than in one blanket wrapper so the read path
    keeps working on a machine where the sudoers drop-in was never installed.

    `sudo -n` never prompts: with no rule in place this fails immediately with
    "a password is required", which apply() logs and retries, exactly like any
    other pmset failure. A missed wake costs freshness, never the alarm.
    """
    cmd = ["sudo", "-n", _PMSET] if root else [_PMSET]
    proc = subprocess.run([*cmd, *args], capture_output=True, text=True,
                          timeout=_TIMEOUT)
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout or "pmset failed").strip())
    return proc.stdout


def scheduled(*, now: datetime) -> list[datetime]:
    """Our own future wake events, soonest first.

    Filtered to WAKE_OWNER on the way in, which is the only thing standing
    between this module and the system's own scheduled events. Everything
    downstream — including every cancel — reads from this list, so a bug that
    reached another owner's event would have to get past this line first.

    Past events are dropped rather than cancelled. pmset reaps them on its own,
    and a wake whose time slid by while the machine was asleep is already spent.
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
        # pmset prints a wall clock with no zone; it means local time on this
        # machine. `naive.astimezone()` reads it as local and attaches the offset
        # in force *on that date* — which is what makes it comparable to an
        # alarm's fire_at, an instant. Stamping `now`'s offset instead would be
        # an hour out for any wake on the far side of a DST boundary.
        when = when.astimezone()
        if when > now:
            found.append(when)
    return sorted(found)


def _schedule(at: datetime) -> None:
    _run(["schedule", _TYPE, at.astimezone().strftime(_WRITE_FMT), WAKE_OWNER],
         root=True)


def _cancel(at: datetime) -> None:
    """Cancel one event by exact type/date/owner. `pmset schedule cancelall`
    exists and is deliberately never used: it would take the calendar travel
    engine's wake and Do Not Disturb's with it."""
    _run(["schedule", "cancel", _TYPE,
          at.astimezone().strftime(_WRITE_FMT), WAKE_OWNER], root=True)


# ---------------------------------------------------------------- the desire

def desired(con, *, now: datetime) -> list[datetime]:
    """When to wake, derived from the alarms table the reconciler just wrote.

    Every future alarm inside the horizon wants a cycle to run shortly before
    it. Which alarm a given wake belongs to is not recorded anywhere, because it
    doesn't matter: a cycle refreshes *all* of them, so wakes are only ever a
    set of times.

    A wake time already in the past is dropped rather than clamped to now. It
    means the alarm fires within the lead window, and the cycle that would have
    checked it is the one running this instant.
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

    # Scheduled emails want the machine awake *at* send_at, not a lead ahead
    # of it — an earlier alarm wake doesn't keep the lid-shut machine up long
    # enough to cover them, so they join after coalescing, not inside it.
    from .email_schedule import wake_times
    sends = wake_times(con, now=now, horizon=horizon)
    return sorted(set(_coalesce(sorted(times)) + sends))[:WAKE_MAX_EVENTS]


def _coalesce(times: list[datetime]) -> list[datetime]:
    """Collapse wakes within WAKE_COALESCE_MIN of each other to the earliest.

    Keeping the *earliest* is the only safe direction: it is the one that lands
    before every alarm in the cluster, and a cycle run then covers the later
    ones too. Keeping the latest would wake the machine after the first alarm
    had already fired on the phone.
    """
    window = timedelta(minutes=WAKE_COALESCE_MIN)
    kept: list[datetime] = []
    for at in times:
        if not kept or at - kept[-1] > window:
            kept.append(at)
    return kept


# ---------------------------------------------------------------- the diff

def plan(want: list[datetime], have: list[datetime]) -> WakePlan:
    """Pure diff, with a deadband. No clock and no IO — same reasoning as
    alarms.plan(): this is where the decision to *cancel* is made.

    Matching is nearest-first within the tolerance rather than positional,
    because a single new alarm early in the list would otherwise shift every
    subsequent pairing and churn the whole set.
    """
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
    """Cancel first, then add. A cancel that fails leaves an event that wakes
    the machine for no reason, which is noise; an add that fails leaves an alarm
    unrefreshed, which is the thing this step exists to prevent. Doing the
    cheap-to-lose half first means a mid-batch failure costs the lesser one.

    Every failure is left on the floor and retried next cycle, exactly as in
    alarms.deliver() — the whole set is recomputed from the OS each time, so
    there is no partial state for a later cycle to be confused by.
    """
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
    """One wake-reconcile cycle. Runs after alarms.reconcile(), which is what
    populates the table this reads — reconciling first would schedule wakes for
    the previous cycle's alarms."""
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
