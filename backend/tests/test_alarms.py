"""Alarms — the reconcile diff and the flight source.

plan() is pure and takes its clock as an argument, which is deliberate: the
dangerous half of this feature is deciding what to *withdraw*, and that decision
is worth testing without a database, a writer, or real time anywhere near it.
"""

from datetime import datetime, timedelta

import pytest

from app import alarms, db, keys, tz

NOW = datetime.fromisoformat("2026-08-07T12:00:00-04:00")


def mk(key="flight:X:1:2026-08-17:checkin", *, kind="flight",
       fire_at="2026-08-16T18:00:00-04:00", title="Check in AA1", body="b"):
    return alarms.Alarm(key=key, kind=kind, fire_at=fire_at, title=title,
                        body=body)


def written_row(alarm, **over):
    row = {"key": alarm.key, "kind": alarm.kind, "fire_at": alarm.fire_at,
           "title": alarm.title, "body": alarm.body, "external_id": "rem-1",
           "source_ref": "", "written_at": "x", "refreshed_at": "x"}
    row.update(over)
    return row


# ------------------------------------------------------------------ the diff

def test_new_alarm_is_created():
    a = mk()
    p = alarms.plan([alarms.SourceResult.of("flight", [a])], {}, now=NOW)
    assert p.create == [a]
    assert not p.withdraw and not p.update


def test_identical_alarm_is_unchanged():
    a = mk()
    p = alarms.plan([alarms.SourceResult.of("flight", [a])],
                    {a.key: written_row(a)}, now=NOW)
    assert p.unchanged == [a.key]
    assert p.is_noop()


def test_moved_fire_time_is_an_update_not_a_recreate():
    """A delayed flight must amend the alarm in place — a create would leave the
    original on the phone announcing a departure that moved."""
    old = mk()
    new = mk(fire_at="2026-08-16T20:30:00-04:00")
    p = alarms.plan([alarms.SourceResult.of("flight", [new])],
                    {old.key: written_row(old)}, now=NOW)
    assert p.update == [new]
    assert not p.create and not p.withdraw


def test_dropped_future_alarm_is_withdrawn():
    a = mk()
    p = alarms.plan([alarms.SourceResult.of("flight", [])],
                    {a.key: written_row(a)}, now=NOW)
    assert [r["key"] for r in p.withdraw] == [a.key]
    assert not p.reap


def test_alarm_that_already_fired_is_reaped_not_withdrawn():
    """It has done its job. Withdrawing a past alarm means nothing, and treating
    it as a withdrawal would make the writer chase an item the user has seen."""
    a = mk(fire_at="2026-08-01T09:00:00-04:00")
    p = alarms.plan([alarms.SourceResult.of("flight", [])],
                    {a.key: written_row(a)}, now=NOW)
    assert [r["key"] for r in p.reap] == [a.key]
    assert not p.withdraw


# ------------------------------------------- the three-state source protocol

def test_unknown_source_holds_its_alarms_instead_of_withdrawing():
    """The whole reason sources have three states. A failed read produces no
    alarms, and if that counted as "no longer wanted" one unreadable table would
    silently cancel a real alarm — _RETENTION_GATES, one store over."""
    a = mk()
    p = alarms.plan([alarms.SourceResult.unknown("flight", "disk on fire")],
                    {a.key: written_row(a)}, now=NOW)
    assert p.held == [a.key]
    assert not p.withdraw and not p.reap


def test_empty_but_known_source_does_withdraw():
    """The other half of the same rule: a confident nothing must still clean up,
    or a cancelled flight's alarm lives forever."""
    a = mk()
    p = alarms.plan([alarms.SourceResult.of("flight", [])],
                    {a.key: written_row(a)}, now=NOW)
    assert [r["key"] for r in p.withdraw] == [a.key]
    assert not p.held


def test_one_failed_source_cannot_withdraw_anothers_alarms():
    """Partitioning by the key prefix is what contains a failure. Without it,
    `known_kinds` would be a single global flag and a broken flight read would
    take the package alarms down with it."""
    flight = mk()
    package = mk(key="package:ABC:eta", kind="package", title="Package")
    p = alarms.plan(
        [alarms.SourceResult.unknown("flight", "boom"),
         alarms.SourceResult.of("package", [])],
        {flight.key: written_row(flight), package.key: written_row(package)},
        now=NOW)
    assert p.held == [flight.key]
    assert [r["key"] for r in p.withdraw] == [package.key]


def test_kind_with_no_source_at_all_is_held():
    """`leave` is declared in KINDS but has no builder until step 3. An absent
    source must not read as a confident nothing, or shipping the kinds list
    ahead of the builders would withdraw alarms the next version will want."""
    a = mk(key="leave:evt-9", kind="leave", title="Leave for class")
    p = alarms.plan([alarms.SourceResult.of("flight", [])],
                    {a.key: written_row(a)}, now=NOW)
    assert p.held == [a.key]
    assert not p.withdraw


# ------------------------------------------------------------------ the keys

def test_key_carries_its_kind():
    assert alarms._alarm_key("flight", "FLZUNL", "2299") == "flight:FLZUNL:2299"
    assert alarms.kind_of("flight:FLZUNL:2299") == "flight"


def test_key_rejects_a_colon_in_a_part():
    """A colon inside a part forges a kind boundary on read-back, which is how
    one source's alarm gets attributed to another's partition."""
    with pytest.raises(ValueError):
        alarms._alarm_key("flight", "A:B")


def test_key_rejects_an_unknown_kind():
    with pytest.raises(ValueError):
        alarms._alarm_key("groceries", "x")


def test_alarm_rejects_a_naive_fire_at():
    """A wall clock means a different instant either side of a TIMELINE move,
    and an alarm row outlives one."""
    with pytest.raises(ValueError):
        alarms.Alarm(key="flight:a", kind="flight",
                     fire_at="2026-08-16T18:00:00", title="t")


def test_alarm_rejects_a_key_that_disagrees_with_its_kind():
    with pytest.raises(ValueError):
        alarms.Alarm(key="package:a", kind="flight",
                     fire_at="2026-08-16T18:00:00-04:00", title="t")


# ---------------------------------------------------------- the flight source

def seed_trip(con, **over):
    row = {"pnr": "FLZUNL", "flight_no": "2299", "dep_day": "2026-08-17",
           "carrier": "AA", "dep_airport": "RDU", "arr_airport": "LAX",
           "dep_local": "2026-08-17T18:00:00", "arr_local": "2026-08-17T20:19:00",
           "status": "confirmed", "updated_at": "2026-08-01T00:00:00"}
    row.update(over)
    with con:
        con.execute(
            "INSERT INTO trips (pnr, flight_no, dep_day, carrier, dep_airport,"
            " arr_airport, dep_local, arr_local, status, updated_at)"
            " VALUES (:pnr,:flight_no,:dep_day,:carrier,:dep_airport,"
            ":arr_airport,:dep_local,:arr_local,:status,:updated_at)", row)


def test_checkin_fires_24h_before_departure_in_the_airports_zone(dbfile):
    """The bug this pins: on departure day the user is in Los_Angeles by TIMELINE,
    but RDU's 18:00 departure is Eastern. Reading dep_local through zone_on()
    would place check-in three hours late."""
    con = db.connect()
    seed_trip(con)
    result = alarms.flight_alarms(con, now=NOW, horizon=NOW + timedelta(days=14))
    assert result.known
    (a,) = result.alarms
    assert a.fire_at == "2026-08-16T18:00:00-04:00"      # ET, not PT
    assert a.key == "flight:FLZUNL:2299:2026-08-17:checkin"
    assert a.title == "Check in AA2299"
    assert "RDU→LAX" in a.body and "ET" in a.body
    con.close()


def test_departure_time_in_the_body_is_the_airports_clock(dbfile):
    """Reading it through the user's zone would print 3:00 PM for a flight that
    leaves at 6."""
    con = db.connect()
    seed_trip(con)
    (a,) = alarms.flight_alarms(
        con, now=NOW, horizon=NOW + timedelta(days=14)).alarms
    assert "6:00 PM" in a.body
    con.close()


def test_flight_beyond_the_horizon_gets_no_alarm(dbfile):
    con = db.connect()
    seed_trip(con, pnr="SBAOBC", dep_day="2026-11-29",
              dep_local="2026-11-29T18:06:00")
    result = alarms.flight_alarms(con, now=NOW, horizon=NOW + timedelta(days=14))
    assert result.known and result.alarms == ()
    con.close()


def test_checkin_already_open_gets_no_alarm(dbfile):
    """An alarm in the past cannot fire; ensure_checkin_tasks() already covers
    this case on the task board."""
    con = db.connect()
    seed_trip(con, dep_day="2026-08-07", dep_local="2026-08-07T18:00:00")
    result = alarms.flight_alarms(con, now=NOW, horizon=NOW + timedelta(days=14))
    assert result.known and result.alarms == ()
    con.close()


def test_cancelled_flight_is_dropped_so_its_alarm_withdraws(dbfile):
    con = db.connect()
    seed_trip(con, status="Cancelled")
    result = alarms.flight_alarms(con, now=NOW, horizon=NOW + timedelta(days=14))
    assert result.known and result.alarms == ()
    con.close()


def test_missing_trips_table_reports_unknown_not_empty(dbfile):
    """The failure mode the three-state protocol exists for, end to end."""
    con = db.connect()
    with con:
        con.execute("DROP TABLE trips")
    result = alarms.flight_alarms(con, now=NOW, horizon=NOW + timedelta(days=14))
    assert result.known is False
    assert result.alarms == ()
    con.close()


def test_unknown_airport_falls_back_to_the_users_zone():
    zone = tz.airport_zone("XXX", "2026-08-20")
    assert str(zone) == "America/Los_Angeles"       # after the move
    assert tz.airport_zone_known("XXX") is False
    assert tz.airport_zone_known("rdu") is True     # case-insensitive


# --------------------------------------------------------- the package source

def seed_package(con, **over):
    row = {"order_key": "Campus Ready:41415", "merchant": "Campus Ready",
           "item_desc": "Twin XL Mattress Protector", "order_id": "41415",
           "tracking_no": "T1", "carrier": "fedex", "eta_day": "2026-08-18",
           "status": "shipped", "updated_at": "2026-08-01T00:00:00"}
    row.update(over)
    with con:
        con.execute(
            "INSERT INTO packages (order_key, merchant, item_desc, order_id,"
            " tracking_no, carrier, eta_day, status, updated_at)"
            " VALUES (:order_key,:merchant,:item_desc,:order_id,:tracking_no,"
            ":carrier,:eta_day,:status,:updated_at)", row)


def test_package_fires_on_its_eta_morning_in_the_users_zone(dbfile):
    """The mirror of the flight test: an ETA day has no clock of its own, and
    the user is in Los_Angeles by the ETA day — 9am there, not 9am Eastern."""
    con = db.connect()
    seed_package(con)
    result = alarms.package_alarms(con, now=NOW, horizon=NOW + timedelta(days=14))
    assert result.known
    (a,) = result.alarms
    assert a.fire_at == "2026-08-18T09:00:00-07:00"
    assert a.title == "Campus Ready arrives today"
    assert "Twin XL Mattress Protector" in a.body and "fedex" in a.body
    con.close()


def test_delivered_package_is_dropped_so_its_alarm_withdraws(dbfile):
    con = db.connect()
    seed_package(con, status="delivered")
    result = alarms.package_alarms(con, now=NOW, horizon=NOW + timedelta(days=14))
    assert result.known and result.alarms == ()
    con.close()


def test_package_whose_eta_already_passed_gets_no_alarm(dbfile):
    con = db.connect()
    seed_package(con, eta_day="2026-08-06")
    result = alarms.package_alarms(con, now=NOW, horizon=NOW + timedelta(days=14))
    assert result.known and result.alarms == ()
    con.close()


def test_order_key_keeps_its_colon_out_of_the_alarm_key(dbfile):
    """`order_key` is literally merchant:order_id, and ':' is the kind
    delimiter — unescaped, the natural identity for a package is unusable."""
    con = db.connect()
    seed_package(con)
    (a,) = alarms.package_alarms(
        con, now=NOW, horizon=NOW + timedelta(days=14)).alarms
    assert a.key == "package:Campus Ready%3A41415"
    assert alarms.kind_of(a.key) == "package"
    assert a.source_ref == "Campus Ready:41415"     # the real key, unescaped
    con.close()


def test_escaping_keeps_two_near_identical_order_keys_apart():
    """Injectivity, which is the whole reason for escaping over stripping: two
    packages must never collapse onto one alarm."""
    assert keys.escape_part("a:b") != keys.escape_part("a%3Ab")


def test_missing_packages_table_reports_unknown_not_empty(dbfile):
    con = db.connect()
    with con:
        con.execute("DROP TABLE packages")
    result = alarms.package_alarms(con, now=NOW, horizon=NOW + timedelta(days=14))
    assert result.known is False and result.alarms == ()
    con.close()


# ----------------------------------------------------------- the leave source

def leave_row(**over):
    row = {"event": "Dentist", "event_id": "ev1", "location": "123 Main St",
           "start": "2026-08-07T15:00:00-04:00",
           "leave_by": "2026-08-07T14:20:00-04:00", "drive_min": 30,
           "origin": "home"}
    row.update(over)
    return row


def fake_leaves(monkeypatch, rows=None, raises=None):
    def fn(day=None, *, strict=False):
        assert strict is True, "the reconciler must not read an outage as calm"
        if raises is not None:
            raise raises
        return list(rows or [])
    monkeypatch.setattr(alarms.leave, "compute_leaves", fn)


def test_leave_alarm_fires_at_leave_by(monkeypatch):
    fake_leaves(monkeypatch, [leave_row()])
    result = alarms.leave_alarms(None, now=NOW, horizon=NOW + timedelta(days=14))
    assert result.known
    (a,) = result.alarms
    assert a.fire_at == "2026-08-07T14:20:00-04:00"     # no extra lead: the
    assert a.title == "Leave for Dentist"               # buffer is inside it
    assert a.body == "123 Main St · starts 3:00 PM"
    assert a.key == "leave:ev1"


def test_calendar_outage_is_unknown_not_an_empty_afternoon(monkeypatch):
    """compute_leaves() swallows a calendar failure into [] for the card's
    sake. Read that way here, one auth hiccup withdraws every leave alarm."""
    fake_leaves(monkeypatch, raises=RuntimeError("no calendar auth"))
    result = alarms.leave_alarms(None, now=NOW, horizon=NOW + timedelta(days=14))
    assert result.known is False and result.alarms == ()


def test_an_event_that_will_not_route_is_held_not_withdrawn(monkeypatch):
    """The case kind-level unknown can't express: the calendar answered, so
    the other event's alarm is still trustworthy."""
    fake_leaves(monkeypatch, [leave_row(),
                              leave_row(event="Lunch", event_id="ev2",
                                        leave_by=None, drive_min=None)])
    result = alarms.leave_alarms(None, now=NOW, horizon=NOW + timedelta(days=14))
    assert result.known
    assert [a.key for a in result.alarms] == ["leave:ev1"]
    assert result.hold == ("leave:ev2",)


def test_a_held_key_survives_the_diff_while_its_neighbours_withdraw():
    held = mk(key="leave:ev2", kind="leave", fire_at="2026-08-07T13:00:00-04:00",
              title="Leave for Lunch")
    gone = mk(key="leave:ev3", kind="leave", fire_at="2026-08-07T13:30:00-04:00",
              title="Leave for Gym")
    p = alarms.plan([alarms.SourceResult.of("leave", [], hold=["leave:ev2"])],
                    {held.key: written_row(held), gone.key: written_row(gone)},
                    now=NOW)
    assert p.held == ["leave:ev2"]
    assert [r["key"] for r in p.withdraw] == ["leave:ev3"]


def test_a_source_may_not_hold_another_kinds_alarm():
    with pytest.raises(ValueError):
        alarms.SourceResult.of("leave", [], hold=["flight:X:1:2026-08-17:checkin"])


def test_leave_by_already_past_gets_no_alarm(monkeypatch):
    fake_leaves(monkeypatch, [leave_row(leave_by="2026-08-07T11:00:00-04:00")])
    result = alarms.leave_alarms(None, now=NOW, horizon=NOW + timedelta(days=14))
    assert result.known and result.alarms == ()


def test_leave_falls_back_to_summary_and_start_without_an_event_id(monkeypatch):
    fake_leaves(monkeypatch, [leave_row(event_id=None)])
    (a,) = alarms.leave_alarms(
        None, now=NOW, horizon=NOW + timedelta(days=14)).alarms
    assert a.key.startswith("leave:Dentist@")
    assert alarms.kind_of(a.key) == "leave"     # the start's colons, escaped


# ------------------------------------------------------------- the deadband

def test_traffic_jitter_does_not_rewrite_the_alarm():
    """An amendment withdraws the alarm off the phone and writes it back. A
    leave_by that moved two minutes is not worth that."""
    before = mk(key="leave:ev1", kind="leave", title="Leave for Dentist",
                fire_at="2026-08-07T14:20:00-04:00")
    after = mk(key="leave:ev1", kind="leave", title="Leave for Dentist",
               fire_at="2026-08-07T14:22:00-04:00")
    p = alarms.plan([alarms.SourceResult.of("leave", [after])],
                    {before.key: written_row(before)}, now=NOW)
    assert p.unchanged == ["leave:ev1"] and not p.update


def test_traffic_that_really_moved_does_amend_it():
    before = mk(key="leave:ev1", kind="leave", title="Leave for Dentist",
                fire_at="2026-08-07T14:20:00-04:00")
    after = mk(key="leave:ev1", kind="leave", title="Leave for Dentist",
               fire_at="2026-08-07T14:05:00-04:00")
    p = alarms.plan([alarms.SourceResult.of("leave", [after])],
                    {before.key: written_row(before)}, now=NOW)
    assert p.update == [after]


def test_the_deadband_is_only_for_the_kind_that_measures_its_time():
    """A flight's check-in window is stated, not estimated: two minutes of
    movement is a real amendment, not noise."""
    before = mk(fire_at="2026-08-16T18:00:00-04:00")
    after = mk(fire_at="2026-08-16T18:02:00-04:00")
    p = alarms.plan([alarms.SourceResult.of("flight", [after])],
                    {before.key: written_row(before)}, now=NOW)
    assert p.update == [after]


# ------------------------------------------------------------------ reconcile

def test_dry_run_records_the_plan_without_an_external_id(dbfile):
    """A dry run has to be inspectable, not merely logged — the rehearsal is
    reading these rows. NULL external_id is also the writer's queue in step 2."""
    con = db.connect()
    seed_trip(con)
    info = alarms.reconcile(con, dry_run=True, now=NOW)
    assert info["create"] == 1 and info["dry_run"] is True

    row, = alarms.pending(con)
    assert row["key"] == "flight:FLZUNL:2299:2026-08-17:checkin"
    assert row["external_id"] is None
    assert row["written_at"] is None
    con.close()


def test_second_cycle_is_a_noop(dbfile):
    """Churn is the thing a dry run is watching for: if a stable trip produced a
    create or an update every five minutes, the writer would rewrite the phone's
    alarm all day."""
    con = db.connect()
    seed_trip(con)
    alarms.reconcile(con, dry_run=True, now=NOW)
    info = alarms.reconcile(con, dry_run=True, now=NOW + timedelta(minutes=5))
    assert info["create"] == 0 and info["update"] == 0
    assert info["unchanged"] == 1
    con.close()


def test_dry_run_keeps_a_withdrawal_it_cannot_perform(dbfile):
    """With no writer there is no way to pull an alarm off the phone, so
    deleting our only record of it would strand it — un-withdrawable forever."""
    con = db.connect()
    seed_trip(con)
    alarms.reconcile(con, dry_run=True, now=NOW)
    with con:
        con.execute("DELETE FROM trips")

    info = alarms.reconcile(con, dry_run=True, now=NOW)
    assert info["withdraw"] == 1
    assert len(alarms.pending(con)) == 1        # row kept, still ours to retract
    con.close()


def test_reaped_row_is_removed_even_in_dry_run(dbfile):
    """Safe with no writer precisely because a fired alarm needs no retraction."""
    con = db.connect()
    seed_trip(con)
    alarms.reconcile(con, dry_run=True, now=NOW)
    with con:
        con.execute("DELETE FROM trips")

    after = datetime.fromisoformat("2026-08-20T12:00:00-07:00")
    info = alarms.reconcile(con, dry_run=True, now=after)
    assert info["reap"] == 1 and info["withdraw"] == 0
    assert alarms.pending(con) == []
    con.close()


# ------------------------------------------------------------------- delivery

class FakeBackend:
    """Stands in for Reminders. Records calls so ordering can be asserted —
    withdraw-before-write is a correctness property, not an implementation
    detail (see deliver's docstring)."""

    def __init__(self, *, fail_write=False, fail_withdraw=False):
        self.calls = []
        self.next_id = 0
        self.fail_write = fail_write
        self.fail_withdraw = fail_withdraw

    def write(self, *, title, body, fire_at):
        self.calls.append(("write", title, fire_at))
        if self.fail_write:
            raise RuntimeError("Reminders is not running")
        self.next_id += 1
        return f"rem-{self.next_id}"

    def withdraw(self, external_id):
        self.calls.append(("withdraw", external_id))
        if self.fail_withdraw:
            raise RuntimeError("osascript timed out")
        return True


def test_delivery_writes_the_queue_and_records_the_id(dbfile):
    con = db.connect()
    seed_trip(con)
    be = FakeBackend()
    info = alarms.reconcile(con, dry_run=False, now=NOW, backend=be)

    assert info["delivery"] == {"written": 1, "withdrawn": 0, "failed": 0}
    row, = alarms.pending(con)
    assert row["external_id"] == "rem-1"
    assert row["written_at"] is not None
    con.close()


def test_delivered_alarm_is_not_rewritten_next_cycle(dbfile):
    """The churn guard, now with a real writer behind it: a stable trip must not
    re-push the phone's alarm every five minutes."""
    con = db.connect()
    seed_trip(con)
    be = FakeBackend()
    alarms.reconcile(con, dry_run=False, now=NOW, backend=be)
    alarms.reconcile(con, dry_run=False, now=NOW + timedelta(minutes=5),
                     backend=be)
    assert [c[0] for c in be.calls] == ["write"]
    con.close()


def test_amended_alarm_replaces_the_one_on_the_phone(dbfile):
    """A delayed flight already has an external_id, so keying delivery off that
    would read it as done and leave the phone holding the old time. Clearing
    written_at is what re-queues it, and the stale alarm comes off first."""
    con = db.connect()
    seed_trip(con)
    be = FakeBackend()
    alarms.reconcile(con, dry_run=False, now=NOW, backend=be)

    with con:
        con.execute("UPDATE trips SET dep_local='2026-08-17T21:30:00'")
    alarms.reconcile(con, dry_run=False, now=NOW, backend=be)

    assert be.calls == [
        ("write", "Check in AA2299", "2026-08-16T18:00:00-04:00"),
        ("withdraw", "rem-1"),
        ("write", "Check in AA2299", "2026-08-16T21:30:00-04:00"),
    ]
    row, = alarms.pending(con)
    assert row["external_id"] == "rem-2"
    con.close()


def test_withdrawal_deletes_the_row_only_after_the_backend_confirms(dbfile):
    con = db.connect()
    seed_trip(con)
    be = FakeBackend()
    alarms.reconcile(con, dry_run=False, now=NOW, backend=be)
    with con:
        con.execute("DELETE FROM trips")

    info = alarms.reconcile(con, dry_run=False, now=NOW, backend=be)
    assert info["delivery"]["withdrawn"] == 1
    assert ("withdraw", "rem-1") in be.calls
    assert alarms.pending(con) == []
    con.close()


def test_failed_write_leaves_the_row_queued(dbfile):
    """Self-healing beats a partial state nothing revisits."""
    con = db.connect()
    seed_trip(con)
    info = alarms.reconcile(con, dry_run=False, now=NOW,
                            backend=FakeBackend(fail_write=True))
    assert info["delivery"] == {"written": 0, "withdrawn": 0, "failed": 1}
    row, = alarms.pending(con)
    assert row["written_at"] is None and row["external_id"] is None

    be = FakeBackend()
    alarms.reconcile(con, dry_run=False, now=NOW, backend=be)
    assert alarms.pending(con)[0]["external_id"] == "rem-1"
    con.close()


def test_failed_withdrawal_keeps_the_row_for_a_retry(dbfile):
    """Deleting the row on a failed withdrawal strands the alarm on the phone —
    it fires anyway and nothing is left that can retract it."""
    con = db.connect()
    seed_trip(con)
    be = FakeBackend()
    alarms.reconcile(con, dry_run=False, now=NOW, backend=be)
    with con:
        con.execute("DELETE FROM trips")

    be.fail_withdraw = True
    info = alarms.reconcile(con, dry_run=False, now=NOW, backend=be)
    assert info["delivery"]["failed"] == 1
    assert alarms.pending(con)[0]["external_id"] == "rem-1"
    con.close()


def test_mass_withdrawal_is_refused_wholesale(dbfile):
    """If the three-state protocol ever fails, this is the last guard. Partial
    application would be worse than none — half the alarms gone, no way to tell
    which."""
    con = db.connect()
    now_iso = NOW.isoformat()
    with con:
        for i in range(6):
            con.execute(
                "INSERT INTO alarms (key, kind, fire_at, title, external_id,"
                " written_at, refreshed_at) VALUES (?,?,?,?,?,?,?)",
                (f"flight:P{i}:1:2026-08-17:checkin", "flight",
                 "2026-08-16T18:00:00-04:00", f"t{i}", f"rem-{i}",
                 now_iso, now_iso))

    be = FakeBackend()
    info = alarms.reconcile(con, dry_run=False, now=NOW, backend=be)
    assert info["delivery"]["withdraw_refused"] == 6
    assert info["delivery"]["withdrawn"] == 0
    assert be.calls == []
    assert len(alarms.pending(con)) == 6
    con.close()


def test_dry_run_delivers_nothing(dbfile):
    con = db.connect()
    seed_trip(con)
    be = FakeBackend()
    info = alarms.reconcile(con, dry_run=True, now=NOW, backend=be)
    assert be.calls == []
    assert "delivery" not in info
    assert alarms.pending(con)[0]["external_id"] is None
    con.close()


# ------------------------------------------------------- the Reminders script

def test_write_converts_the_instant_to_local_components(monkeypatch):
    """fire_at is an instant; AppleScript date components are wall clock. The
    conversion is what keeps the alarm at the right moment — passing the ISO
    string's own digits through would fire it in the wrong zone."""
    from app import reminders

    seen = {}

    def fake_run(script, *args):
        seen["args"] = args
        return "x-apple-reminder://ABC"

    monkeypatch.setattr(reminders, "_run", fake_run)
    rid = reminders.write(title="Check in AA2299", body="b",
                          fire_at="2026-08-16T18:00:00-04:00")
    assert rid == "x-apple-reminder://ABC"

    local = datetime.fromisoformat("2026-08-16T18:00:00-04:00").astimezone()
    acct, lst, title, body, y, mo, dy, hh, mi = seen["args"]
    assert (y, mo, dy) == (str(local.year), str(local.month), str(local.day))
    assert (hh, mi) == (str(local.hour), str(local.minute))
    assert title == "Check in AA2299"


def test_withdraw_treats_an_already_gone_alarm_as_success(monkeypatch):
    """A user who deleted it by hand made the same call. A permanent failure
    here would block the row from ever being cleaned up."""
    from app import reminders
    monkeypatch.setattr(reminders, "_run", lambda *a: "NOTFOUND")
    assert reminders.withdraw("x-apple-reminder://GONE") is True


def _osascript_failing(monkeypatch, errors):
    """macos.osascript raising each of `errors` in turn, then answering."""
    from app import reminders
    calls = []

    def fake(script, *args, timeout, error):
        calls.append(script)
        if len(calls) <= len(errors):
            raise error(errors[len(calls) - 1])
        return "x-apple-reminder://OK"

    monkeypatch.setattr(reminders.macos, "osascript", fake)
    monkeypatch.setattr(reminders.time, "sleep", lambda s: None)
    return calls


def test_a_reminders_instance_shutting_down_is_retried(monkeypatch):
    """Windowless Reminders quits seconds after a script; the next one can
    meet it mid-exit. That is a relaunch away, not a failed delivery."""
    from app import reminders
    calls = _osascript_failing(monkeypatch, [
        "382:418: execution error: Reminders got an error: Connection is invalid. (-609)",
        "Reminders got an error: Application isn’t running. (-600)",
    ])
    assert reminders._run("S") == "x-apple-reminder://OK"
    assert len(calls) == 3


def test_a_real_script_error_is_not_retried(monkeypatch):
    from app import reminders
    calls = _osascript_failing(monkeypatch, ["Not authorized to send Apple events (-1743)"])
    with pytest.raises(reminders.ReminderError, match="-1743"):
        reminders._run("S")
    assert len(calls) == 1


def test_the_relaunch_retry_gives_up(monkeypatch):
    from app import reminders
    restarts = []
    monkeypatch.setattr(reminders, "_restart_reminders", lambda: restarts.append(1))
    calls = _osascript_failing(monkeypatch, ["Connection is invalid. (-609)"] * 9)
    with pytest.raises(reminders.ReminderError, match="-609"):
        reminders._run("S")
    assert len(calls) == reminders._RETRIES + 2
    assert restarts == [1]


def test_a_wedged_reminders_is_force_restarted(monkeypatch):
    """A wedged instance answers -609 to every retry, cycle after cycle, and a
    leave alarm never reaches the phone. After the retries, kill + relaunch."""
    from app import reminders
    restarts = []
    monkeypatch.setattr(reminders, "_restart_reminders", lambda: restarts.append(1))
    calls = _osascript_failing(monkeypatch,
                               ["Connection is invalid. (-609)"] * (reminders._RETRIES + 1))
    assert reminders._run("S") == "x-apple-reminder://OK"
    assert restarts == [1]
    assert len(calls) == reminders._RETRIES + 2


def test_write_raises_when_reminders_returns_no_id(monkeypatch):
    from app import reminders
    monkeypatch.setattr(reminders, "_run", lambda *a: "")
    with pytest.raises(reminders.ReminderError):
        reminders.write(title="t", body="", fire_at="2026-08-16T18:00:00-04:00")
