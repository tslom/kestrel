"""Alarms: future-dated alerts the laptop computes and the phone (Reminders) fires.

A reconciler, not an emitter: each cycle recomputes the desired set and diffs it
against the `alarms` table. Rows with NULL `written_at` are both the dry-run view
and the writer's queue; `deliver()` pushes them unless ALARMS_DRY_RUN.
Trap: a source that can't read must return `unknown` (held), never an empty list
(withdraws every alarm). Keys carry a `kind:` prefix so a failure holds only its own.
Design: DESIGN.md → Reaching the user (§21).
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

# A written row whose kind is absent here is held, never withdrawn (code older than the row).
KINDS = ("flight", "package", "leave")


def _alarm_key(kind: str, *parts: str) -> str:
    """The one place an alarm key is built; `kind` leads because the reconciler partitions by it."""
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
            # A naive fire_at reads as a different instant after a timezone move.
            raise ValueError(f"fire_at must carry an offset: {self.fire_at!r}")


@dataclass(frozen=True)
class SourceResult:
    """One source's contribution. `known=False`: the read failed, keep its alarms.
    `hold`: the same protection per key (a source stays known but names the
    alarms it can't speak for, e.g. one event that won't geocode)."""
    kind: str
    alarms: tuple[Alarm, ...] = ()
    known: bool = True
    detail: str = ""
    hold: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        # A source may hold only its own keys; a violation raises, and desired() makes it unknown.
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

    `dep_local` is the departure airport's local time: read it via `tz.airport_zone()`,
    not `tz.zone_on()`. A window already open gets none (`ensure_checkin_tasks()` covers it).
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
            # The zone label matters when the user isn't in the airport's zone.
            body=f"{leg} departs {dep_label} "
                 f"{tz.label_for_zone(zone)} on {row['dep_day']}",
            source_ref=f"{row['pnr']}:{row['flight_no']}:{row['dep_day']}",
        ))
    return SourceResult.of("flight", out)


def package_alarms(con, *, now: datetime, horizon: datetime) -> SourceResult:
    """One alarm per package due to arrive, firing the morning of its ETA.

    Hour from config, zone from `tz.zone_on()` (the user's, unlike flights).
    'delivered' is terminal, so dropping it here withdraws the alarm.
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
        # Carrier shown for errands (counter pickup), not for a courier van.
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

    Reads `leave.compute_leaves(strict=True)`: calendar failure is kind-level
    unknown; an event with null `leave_by` (won't geocode/route) is a per-key hold.
    `leave_by` already includes LEAVE_BUFFER_MIN; `horizon` is unused (leaves are today).
    """
    try:
        rows = leave.compute_leaves(strict=True)
    except Exception as e:  # calendar unreachable, not "no events"
        return SourceResult.unknown("leave", repr(e))

    out, hold = [], []
    for row in rows:
        # Fallback id if the provider drops event_id: title+start is unique.
        ident = row.get("event_id") or f"{row.get('event')}@{row['start']}"
        key = _alarm_key("leave", keys.escape_part(str(ident)))
        if not row.get("leave_by"):
            hold.append(key)
            continue
        # Truncated: the writer zeroes seconds anyway, and truncating errs early.
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
            # No drive time: it changes every cycle and would defeat the deadband.
            body=f"{row['location']} · starts {start_label}",
            source_ref=str(ident),
        ))
    return SourceResult.of("leave", out, hold=hold)


# kind -> builder. Never stub a kind with `()`: that is a confident nothing and withdraws.
SOURCES = {"flight": flight_alarms, "package": package_alarms,
           "leave": leave_alarms}


def desired(con, *, now: datetime | None = None) -> list[SourceResult]:
    """Every source's contribution; a source that raises becomes `unknown`."""
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

# Per-kind fire_at deadband; only kinds whose time is measured (traffic), not stated.
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
    """Pure diff: desired vs written. No IO, no clock of its own."""
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
            # Source answered, but not about this one.
            p.held.append(key)
        elif kind_of(key) not in known_kinds:
            # Unknown source: a stale alarm beats a silently withdrawn live one.
            p.held.append(key)
        elif datetime.fromisoformat(row["fire_at"]) <= now:
            p.reap.append(row)      # already fired; withdrawing it means nothing
        else:
            p.withdraw.append(row)  # would fire, and should not
    return p


# ---------------------------------------------------------------- apply

def _apply(con, p: Plan, *, now_iso: str) -> None:
    """Record the plan (delivery is a separate pass). An update clears `written_at`
    but keeps `external_id` (the only handle on the phone's copy). Withdraw rows
    are deleted only by deliver(), after the writer confirms."""
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
            con.execute("DELETE FROM alarms WHERE key=?", (row["key"],))


def deliver(con, p: Plan, *, now_iso: str, backend=None) -> dict:
    """Push the queue to the phone: withdraw what should go, then write what's due.

    Failures are left for the next cycle (row keeps `written_at` NULL / its row).
    Withdrawals run first so an update never leaves two alarms on the phone.
    """
    backend = backend or reminders
    out = {"written": 0, "withdrawn": 0, "failed": 0}

    if len(p.withdraw) > ALARM_MAX_WITHDRAW_PER_CYCLE:
        # Reaching this means a source lied about being `known`; skip the lot.
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
                # Amended alarm: the old one comes off first.
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
    """One reconcile cycle; returns a poller-style info dict. Run after the providers land, not alongside."""
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
    """The writer's queue, keyed on `written_at` (not `external_id`) so amended alarms re-push."""
    rows = con.execute(
        "SELECT * FROM alarms WHERE written_at IS NULL ORDER BY fire_at"
    ).fetchall()
    return [dict(r) for r in rows]


def pending(con) -> list[dict]:
    """Everything the reconciler is holding, delivered or not — the inspection
    view a dry run is read through."""
    rows = con.execute("SELECT * FROM alarms ORDER BY fire_at").fetchall()
    return [dict(r) for r in rows]
