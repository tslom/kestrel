"""Alarms — alerts that survive a closed laptop.

The dashboard computes plenty that is only useful *before* it happens (a flight
check-in opening, a package arriving, a time to leave) and has had no way to say
so: every channel it owns needs a process running and a screen open. A live
notifier does not fix that — the machine is asleep in a bag. So the model here
is that **the laptop computes and the phone delivers**: while the lid is open,
the reconciler writes future-dated alarms to a surface iCloud syncs, and the
phone fires them hours later with nothing of ours running at all.

Which means this is a *reconciler*, not an emitter. An emitter fires once and
cannot take it back, so a flight that slips leaves a phone announcing a
departure that moved. Every cycle instead recomputes the whole desired set,
diffs it against what was written, and applies the difference.

The hazard that shape creates is deletion. A source that fails to read produces
no alarms, an empty desired set withdraws everything, and one unreadable table
silently cancels a real 6am alarm. That is features.py's `_RETENTION_GATES` one
store over: absence of data from a source that has stopped *covering* is not
evidence of absence. So a source reports one of three things and never two —
alarms, a confident nothing, or `unknown` — and `unknown` holds its existing
alarms untouched. Sources are partitioned by the `kind:` prefix on every key, so
a failing source can only ever hold its own. A source that can answer for most
of its alarms but not all of them names the exceptions instead (`SourceResult
.hold`), which is the same protection one alarm wide.

`reconcile()` records what it would deliver as rows with a NULL `written_at`,
which is both the dry-run rehearsal and the writer's work queue; `deliver()`
(app/reminders.py) is what actually reaches the phone, and runs only when
ALARMS_DRY_RUN is off.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from . import db, keys, leave, reminders, tz
from .config import (ALARM_HORIZON_DAYS, ALARM_MAX_WITHDRAW_PER_CYCLE,
                     ALARMS_DRY_RUN, FLIGHT_CHECKIN_LEAD_HOURS,
                     LEAVE_ALARM_TOLERANCE_MIN, PACKAGE_ALARM_HOUR)
from .keys import kind_of  # re-exported: this module's readers reach for it here

# Kinds this module knows how to build. A written row whose kind is absent here
# is held, never withdrawn — an unrecognised kind means this code is older than
# the row (a rollback), and the alarm on the phone is real either way.
KINDS = ("flight", "package", "leave")


def _alarm_key(kind: str, *parts: str) -> str:
    """The one place an alarm key is built. `kind` leads because the reconciler
    partitions by it — see the module docstring on failed reads. The rule itself
    lives in keys.py, shared with the at-desk channel."""
    return keys.join(kind, *parts, allowed=KINDS, label="alarm")


@dataclass(frozen=True)
class Alarm:
    key: str
    kind: str
    fire_at: str            # ISO 8601 with offset — an instant, not a wall clock
    title: str
    body: str = ""
    source_ref: str = ""

    def __post_init__(self) -> None:
        if kind_of(self.key) != self.kind:
            raise ValueError(f"key {self.key!r} does not carry kind {self.kind!r}")
        if datetime.fromisoformat(self.fire_at).tzinfo is None:
            # A naive fire_at is the bug this catches: it reads as a different
            # instant either side of a TIMELINE move, and the row outlives one.
            raise ValueError(f"fire_at must carry an offset: {self.fire_at!r}")


@dataclass(frozen=True)
class SourceResult:
    """One source's contribution. `known=False` means the read failed and this
    source's existing alarms must be left alone — see the module docstring.

    `hold` is the same protection at key granularity. Step 1 could answer only
    per kind, which is all a flight needs: the trips table reads or it doesn't.
    Leave is the case that doesn't fit — the calendar answers, so the source
    *is* known, but one venue out of three fails to geocode and that event
    alone can't be spoken for. Kind-level unknown would throw away the two
    good answers; saying nothing about it would withdraw a live alarm. So the
    source names the keys it is holding and stays known about the rest.
    """
    kind: str
    alarms: tuple[Alarm, ...] = ()
    known: bool = True
    detail: str = ""
    hold: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        # Same partition rule the keys enforce, one level up: a source may
        # hold its own alarms and no one else's. A source that gets this
        # wrong raises here, and `desired()` turns that into unknown — it
        # loses its own alarms' worth of accuracy, never another source's.
        for key in self.hold:
            if kind_of(key) != self.kind:
                raise ValueError(
                    f"{self.kind!r} source may not hold {key!r}")

    @staticmethod
    def of(kind: str, alarms, hold=()) -> SourceResult:
        return SourceResult(kind=kind, alarms=tuple(alarms), known=True,
                            hold=tuple(hold))

    @staticmethod
    def unknown(kind: str, detail: str) -> SourceResult:
        return SourceResult(kind=kind, alarms=(), known=False, detail=detail)


@dataclass
class Plan:
    create: list[Alarm] = field(default_factory=list)
    update: list[Alarm] = field(default_factory=list)
    withdraw: list[dict] = field(default_factory=list)   # future, no longer wanted
    reap: list[dict] = field(default_factory=list)       # already fired, done with
    unchanged: list[str] = field(default_factory=list)
    held: list[str] = field(default_factory=list)        # source unknown this cycle

    def summary(self) -> dict:
        return {"create": len(self.create), "update": len(self.update),
                "withdraw": len(self.withdraw), "reap": len(self.reap),
                "unchanged": len(self.unchanged), "held": len(self.held)}

    def is_noop(self) -> bool:
        return not (self.create or self.update or self.withdraw or self.reap)


# ---------------------------------------------------------------- sources

def flight_alarms(con, *, now: datetime, horizon: datetime) -> SourceResult:
    """One alarm per upcoming flight, firing when online check-in opens.

    `dep_local` is local to the *departure airport*, not to the user — so it is
    read through `tz.airport_zone()` and not `tz.zone_on()`. On a cross-country moving day those
    disagree by three hours (see tz.AIRPORT_ZONES), and this is the flight that
    matters most.

    A check-in window that has *already* opened gets no alarm: an alarm in the
    past cannot fire, and that case is already covered — `ensure_checkin_tasks()`
    puts it on the task board.
    """
    try:
        rows = con.execute(
            "SELECT pnr, flight_no, dep_day, carrier, dep_airport, arr_airport,"
            " dep_local, status FROM trips WHERE dep_local IS NOT NULL"
        ).fetchall()
    except sqlite3.Error as e:
        return SourceResult.unknown("flight", repr(e))

    out = []
    for r in rows:
        row = dict(r)
        if (row.get("status") or "").lower().startswith("cancel"):
            continue
        zone = tz.airport_zone(row.get("dep_airport"), row["dep_day"])
        dep = datetime.fromisoformat(row["dep_local"]).replace(tzinfo=zone)
        fire = dep - timedelta(hours=FLIGHT_CHECKIN_LEAD_HOURS)
        if not (now <= fire <= horizon):
            continue
        ident = f"{row.get('carrier') or ''}{row['flight_no']}"
        leg = f"{row.get('dep_airport') or '?'}→{row.get('arr_airport') or '?'}"
        dep_label = dep.strftime("%-I:%M %p")
        out.append(Alarm(
            key=_alarm_key("flight", row["pnr"], str(row["flight_no"]),
                           row["dep_day"], "checkin"),
            kind="flight",
            fire_at=fire.isoformat(),
            title=f"Check in {ident}",
            # The zone label is not decoration: a bare departure time is
            # unreadable on a day the user is not in the airport's zone.
            body=f"{leg} departs {dep_label} "
                 f"{tz.label_for_zone(zone)} on {row['dep_day']}",
            source_ref=f"{row['pnr']}:{row['flight_no']}:{row['dep_day']}",
        ))
    return SourceResult.of("flight", out)


def package_alarms(con, *, now: datetime, horizon: datetime) -> SourceResult:
    """One alarm per package due to arrive, firing the morning of its ETA.

    A package has an `eta_day` and no clock at all, so the hour comes from
    config and the zone from `tz.zone_on()` — the *user's* zone on that day.
    The contrast with `flight_alarms` is the whole point: a departure time
    belongs to the airport, an arrival day belongs to wherever the user is
    reading it.

    `status` only ever moves forward (see the v18 schema note), so 'delivered'
    is terminal — dropping those here is what withdraws the alarm once the
    box lands, without needing to hear about it a second time.
    """
    try:
        rows = con.execute(
            "SELECT order_key, merchant, item_desc, carrier, tracking_no,"
            " eta_day, status FROM packages WHERE eta_day IS NOT NULL"
        ).fetchall()
    except sqlite3.Error as e:
        return SourceResult.unknown("package", repr(e))

    out = []
    for r in rows:
        row = dict(r)
        if (row.get("status") or "") == "delivered":
            continue
        try:
            zone = tz.zone_on(row["eta_day"])
            fire = datetime.fromisoformat(row["eta_day"]).replace(
                hour=PACKAGE_ALARM_HOUR, tzinfo=zone)
        except ValueError:
            continue        # an eta_day the extractor wrote in some other shape
        if not (now <= fire <= horizon):
            continue
        merchant = row.get("merchant") or "Package"
        what = (row.get("item_desc") or "").strip()
        if len(what) > 120:
            what = what[:119].rstrip() + "…"
        # The carrier is worth a line only when it says where to go: a
        # counter pickup is an errand, a FedEx van is not.
        via = row.get("carrier") or ""
        detail = " · ".join(p for p in (what, via) if p)
        out.append(Alarm(
            key=_alarm_key("package", keys.escape_part(row["order_key"])),
            kind="package",
            fire_at=fire.isoformat(),
            title=f"{merchant} arrives today",
            body=detail or f"ETA {row['eta_day']}",
            source_ref=row["order_key"],
        ))
    return SourceResult.of("package", out)


def leave_alarms(con, *, now: datetime, horizon: datetime) -> SourceResult:
    """One alarm per located event still ahead today, firing at its leave_by.

    Unlike the other two this reads no table — leave.py computes leaves live from
    the calendar and a traffic-aware route, so the source is a call into
    `leave.compute_leaves()` and inherits its failure modes:

    * the calendar itself failing is kind-level unknown (hence `strict=True`,
      which is the only reason that argument exists);
    * a single event that won't geocode or route comes back with `leave_by`
      null, and that is a per-key hold — the route is unknown for that one
      event, and an alarm already written for it is the best answer anyone
      has until the next cycle.

    `leave_by` already carries LEAVE_BUFFER_MIN, so the alarm fires *at* it:
    the buffer is the lead. `horizon` is unused and can't bind — a leave is
    today by construction, and ALARM_HORIZON_DAYS is measured in days.
    """
    try:
        rows = leave.compute_leaves(strict=True)
    except Exception as e:  # calendar unreachable, not "no events"
        return SourceResult.unknown("leave", repr(e))

    out, hold = [], []
    for row in rows:
        # Falls back to the summary only if the provider ever stops carrying
        # an id: two events can share a title, but not a title and a start.
        ident = row.get("event_id") or f"{row.get('event')}@{row['start']}"
        key = _alarm_key("leave", keys.escape_part(str(ident)))
        if not row.get("leave_by"):
            hold.append(key)
            continue
        # Truncated, not rounded: the writer sets seconds to 0 anyway, so
        # keeping them would store a fire_at that never matches the alarm
        # actually delivered — and truncation errs toward leaving early.
        fire = datetime.fromisoformat(row["leave_by"]).replace(
            second=0, microsecond=0)
        if fire <= now:
            continue        # already the time to go; an alarm can't say it late
        start_label = datetime.fromisoformat(row["start"]).strftime("%-I:%M %p")
        out.append(Alarm(
            key=key,
            kind="leave",
            fire_at=fire.isoformat(),
            title=f"Leave for {row.get('event') or 'your next event'}",
            # Deliberately no drive time here. It is recomputed from live
            # traffic every cycle, and a body that changes on its own would
            # defeat the deadband that keeps this alarm off the write path.
            body=f"{row['location']} · starts {start_label}",
            source_ref=str(ident),
        ))
    return SourceResult.of("leave", out, hold=hold)


# kind -> builder. A kind with no builder is absent rather than stubbed,
# because a stub returning () is a *confident nothing* and would withdraw its
# alarms. Absent means held.
SOURCES = {"flight": flight_alarms, "package": package_alarms,
           "leave": leave_alarms}


def desired(con, *, now: datetime | None = None) -> list[SourceResult]:
    """Every source's contribution for this cycle. A source that raises outside
    its own guard still degrades to `unknown` rather than to silence."""
    now = now or datetime.now().astimezone()
    horizon = now + timedelta(days=ALARM_HORIZON_DAYS)
    results = []
    for kind, fn in SOURCES.items():
        try:
            results.append(fn(con, now=now, horizon=horizon))
        except Exception as e:  # a broken source must not withdraw
            results.append(SourceResult.unknown(kind, repr(e)))
    return results


# ---------------------------------------------------------------- the diff

# Per-kind deadband on fire_at. A kind is listed here only if its time is
# *measured* rather than stated: a flight's check-in window and a package's
# ETA hour are exact numbers that either changed or didn't, while a leave_by
# is a traffic estimate that is never twice the same. Amendment is not free —
# it withdraws the alarm off the phone and writes it back — so a kind that
# jitters needs a threshold below which it isn't news.
_FIRE_DEADBAND = {"leave": timedelta(minutes=LEAVE_ALARM_TOLERANCE_MIN)}


def _fire_moved(prior_iso: str, alarm: Alarm) -> bool:
    band = _FIRE_DEADBAND.get(alarm.kind)
    if band is None:
        return prior_iso != alarm.fire_at
    try:
        drift = datetime.fromisoformat(alarm.fire_at) - datetime.fromisoformat(prior_iso)
    except ValueError:
        return True         # unparseable stored time: rewrite rather than trust it
    return abs(drift) > band


def read_written(con) -> dict[str, dict]:
    rows = con.execute("SELECT * FROM alarms").fetchall()
    return {r["key"]: dict(r) for r in rows}


def plan(results: list[SourceResult], written: dict[str, dict],
         *, now: datetime) -> Plan:
    """Pure diff: desired vs written. No IO, no clock of its own — which is the
    whole reason the risky logic lives here and not in reconcile()."""
    p = Plan()
    known_kinds = {r.kind for r in results if r.known}
    held_keys = {key for r in results for key in r.hold}

    wanted: dict[str, Alarm] = {}
    for result in results:
        for alarm in result.alarms:
            wanted[alarm.key] = alarm

    for key, alarm in wanted.items():
        prior = written.get(key)
        if prior is None:
            p.create.append(alarm)
        elif (_fire_moved(prior["fire_at"], alarm)
              or prior["title"] != alarm.title
              or (prior["body"] or "") != alarm.body):
            p.update.append(alarm)
        else:
            p.unchanged.append(key)

    for key, row in written.items():
        if key in wanted:
            continue
        if key in held_keys:
            # The source answered, but not about this one. Same reasoning as
            # an unknown kind, one alarm wide.
            p.held.append(key)
        elif kind_of(key) not in known_kinds:
            # The source could not say whether this is still wanted. Holding a
            # stale alarm costs one wrong notification; withdrawing a live one
            # costs the alert entirely, and silently.
            p.held.append(key)
        elif datetime.fromisoformat(row["fire_at"]) <= now:
            p.reap.append(row)      # already fired; withdrawing it means nothing
        else:
            p.withdraw.append(row)  # would fire, and should not
    return p


# ---------------------------------------------------------------- apply

def _apply(con, p: Plan, *, now_iso: str) -> None:
    """Persist the plan. Delivery is a separate pass — this only records what
    *should* be true, so a reconcile with no writer (or a failing one) still
    leaves a correct queue behind.

    `written_at` is the delivery marker, not `external_id`: an update to an
    already-delivered alarm has a perfectly good external_id and still needs
    re-pushing, so clearing written_at is what marks it. external_id survives
    because it is the only handle on the alarm already sitting on the phone —
    blanking it there would strand that alarm beyond withdrawal.

    Withdrawal rows are never deleted here. Dropping the row is what makes an
    alarm unreachable, so it happens only after the writer confirms the alarm
    is off the phone (see deliver()).
    """
    with con:
        for alarm in p.create:
            con.execute(
                "INSERT INTO alarms (key, kind, fire_at, title, body,"
                " external_id, source_ref, written_at, refreshed_at)"
                " VALUES (?,?,?,?,?, NULL, ?, NULL, ?)",
                (alarm.key, alarm.kind, alarm.fire_at, alarm.title, alarm.body,
                 alarm.source_ref, now_iso))
        for alarm in p.update:
            con.execute(
                "UPDATE alarms SET fire_at=?, title=?, body=?, source_ref=?,"
                " written_at=NULL, refreshed_at=? WHERE key=?",
                (alarm.fire_at, alarm.title, alarm.body, alarm.source_ref,
                 now_iso, alarm.key))
        for key in p.unchanged:
            con.execute("UPDATE alarms SET refreshed_at=? WHERE key=?",
                        (now_iso, key))
        for row in p.reap:
            # It fired. Nothing to withdraw, so this is safe with no writer.
            con.execute("DELETE FROM alarms WHERE key=?", (row["key"],))


def deliver(con, p: Plan, *, now_iso: str, backend=None) -> dict:
    """Push the queue to the phone: withdraw what should go, write what's due.

    Every failure is left on the floor deliberately — a row that doesn't
    deliver keeps `written_at` NULL and is retried next cycle, and a withdrawal
    that fails keeps its row so it is retried too. Self-healing beats a partial
    state nothing revisits.

    Withdrawals run *before* writes so an update (withdraw the old, write the
    new) can't briefly leave two alarms for one fact on the phone.
    """
    backend = backend or reminders
    out = {"written": 0, "withdrawn": 0, "failed": 0}

    if len(p.withdraw) > ALARM_MAX_WITHDRAW_PER_CYCLE:
        # The three-state protocol should have prevented this, so reaching it
        # means a source lied about being `known`. Skip the lot: a stale alarm
        # is a wrong notification, a wrongly withdrawn one is silence.
        print(f"[alarms] REFUSING to withdraw {len(p.withdraw)} alarms in one "
              f"cycle (cap {ALARM_MAX_WITHDRAW_PER_CYCLE}) — treating as a bug")
        out["withdraw_refused"] = len(p.withdraw)
    else:
        for row in p.withdraw:
            try:
                if row["external_id"]:
                    backend.withdraw(row["external_id"])
                with con:
                    con.execute("DELETE FROM alarms WHERE key=?", (row["key"],))
                out["withdrawn"] += 1
            except Exception as e:  # retried next cycle
                print(f"[alarms] withdraw failed for {row['key']}: {e!r}")
                out["failed"] += 1

    for row in _undelivered(con):
        try:
            if row["external_id"]:
                # An amended alarm. The old one has to come off first, or the
                # phone ends up holding both versions of the same fact.
                backend.withdraw(row["external_id"])
            rid = backend.write(title=row["title"], body=row["body"] or "",
                                fire_at=row["fire_at"])
            with con:
                con.execute(
                    "UPDATE alarms SET external_id=?, written_at=? WHERE key=?",
                    (rid, now_iso, row["key"]))
            out["written"] += 1
        except Exception as e:  # retried next cycle
            print(f"[alarms] write failed for {row['key']}: {e!r}")
            out["failed"] += 1
    return out


def reconcile(con=None, *, dry_run: bool | None = None,
              now: datetime | None = None, backend=None) -> dict:
    """One reconcile cycle. Returns a poller-style info dict.

    Called after the providers land, not alongside them: alarms are derived from
    the stores, so reconciling mid-cycle would diff against half-updated trips.
    """
    dry_run = ALARMS_DRY_RUN if dry_run is None else dry_run
    now = now or datetime.now().astimezone()
    now_iso = now.isoformat(timespec="seconds")
    delivery = None
    with db.borrow(con) as con:
        results = desired(con, now=now)
        p = plan(results, read_written(con), now=now)
        _apply(con, p, now_iso=now_iso)
        if not dry_run:
            delivery = deliver(con, p, now_iso=now_iso, backend=backend)

    info = p.summary()
    info["dry_run"] = dry_run
    if delivery is not None:
        info["delivery"] = delivery
    unknown = [r.kind for r in results if not r.known]
    if unknown:
        info["unknown_sources"] = unknown
    if not p.is_noop():
        print(f"[alarms] {'(dry run) ' if dry_run else ''}{info}")
        for alarm in p.create:
            print(f"[alarms]   + {alarm.fire_at}  {alarm.title} — {alarm.body}")
        for alarm in p.update:
            print(f"[alarms]   ~ {alarm.fire_at}  {alarm.title}")
        for row in p.withdraw:
            print(f"[alarms]   - {row['fire_at']}  {row['title']}"
                  f"{' (dry run: kept)' if dry_run else ''}")
    return info


def _undelivered(con) -> list[dict]:
    """The writer's queue: recorded as desired, not yet on the phone in this
    form. Keyed on `written_at` rather than `external_id` so an *amended* alarm
    — which already has an id — is queued for re-push instead of being read as
    delivered and silently left stale."""
    rows = con.execute(
        "SELECT * FROM alarms WHERE written_at IS NULL ORDER BY fire_at"
    ).fetchall()
    return [dict(r) for r in rows]


def pending(con) -> list[dict]:
    """Everything the reconciler is holding, delivered or not — the inspection
    view a dry run is read through."""
    rows = con.execute("SELECT * FROM alarms ORDER BY fire_at").fetchall()
    return [dict(r) for r in rows]
