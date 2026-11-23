"""The scheduled wake.

Two halves are worth testing and they are tested differently. `plan()` and
`_coalesce()` are pure and take everything as arguments, so the decision to
*cancel* an event — the only decision here that can touch state we don't own —
is tested with no subprocess and no clock anywhere near it. The parsing half
runs against real captured `pmset -g sched` output, because the format is the
one thing about this module that isn't ours to define.
"""

import time
from datetime import datetime

import pytest

from app import wake

NOW = datetime.fromisoformat("2026-08-11T12:00:00-04:00")


@pytest.fixture(autouse=True)
def eastern(monkeypatch):
    """Pin the process zone. `pmset -g sched` prints a bare wall clock, so
    parsing it means reading it as *local* time — which makes the expected
    instants in this file depend on where the machine is. Pinning is not
    cosmetic: TIMELINE moves this laptop to Pacific on 8/17, and a test that
    silently changes meaning on that date is worse than one that fails."""
    monkeypatch.setenv("TZ", "America/New_York")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()

# Real output, captured from `pmset -g sched` on this machine, with one of our
# own events spliced in. The system rows are the point: they are what a cancel
# must never reach.
REAL_SCHED = """Scheduled power events:
 [0]  wake at 08/11/2026 18:35:17 by 'com.apple.alarm.user-invisible-com.apple.calaccessd.travelEngine.periodicRefreshTimer'
 [1]  wake at 08/11/2026 19:46:28 by 'com.apple.alarm.user-invisible-com.apple.acmd.alarm'
 [2]  wake at 08/12/2026 00:00:00 by 'com.apple.alarm.user-visible-com.apple.donotdisturb.server.ScheduleLifetimeMonitor.timer' User visible: true
 [3]  wake at 08/12/2026 05:40:00 by 'com.kestrel.alarms'
"""


@pytest.fixture
def pmset(monkeypatch):
    """Capture pmset invocations and serve canned `-g sched` output."""
    class Calls(list):
        """A list of argv, with the privilege each call asked for parked
        alongside it — the existing assertions all read the argv."""
        def __init__(self):
            super().__init__()
            self.root = []

    calls = Calls()

    def fake_run(args, *, root=False):
        calls.append(args)
        calls.root.append(root)
        return REAL_SCHED if args[:2] == ["-g", "sched"] else ""

    monkeypatch.setattr(wake, "_run", fake_run)
    return calls


def at(iso):
    return datetime.fromisoformat(iso)


# ------------------------------------------------------------------ parsing

def test_only_our_owner_is_ever_seen(pmset):
    """The whole safety property of this module in one assertion. Three of the
    four scheduled events belong to macOS — the calendar travel engine, Do Not
    Disturb — and every cancel downstream is drawn from this list."""
    assert wake.scheduled(now=NOW) == [at("2026-08-12T05:40:00-04:00")]


def test_past_events_are_not_returned(pmset):
    """A wake whose time slid by while the machine was asleep is spent. pmset
    reaps it; we neither cancel it nor count it as covering an alarm."""
    later = datetime.fromisoformat("2026-08-13T12:00:00-04:00")
    assert wake.scheduled(now=later) == []


def test_a_trailing_user_visible_field_does_not_break_the_match(monkeypatch):
    """Row [2] carries `User visible: true` after the owner. The regex is
    unanchored at the end for exactly that, and a parse failure here would read
    as 'not scheduled' and re-add an event every cycle."""
    monkeypatch.setattr(wake, "WAKE_OWNER",
                        "com.apple.alarm.user-visible-com.apple.donotdisturb"
                        ".server.ScheduleLifetimeMonitor.timer")
    monkeypatch.setattr(wake, "_run", lambda args: REAL_SCHED)
    assert wake.scheduled(now=NOW) == [at("2026-08-12T00:00:00-04:00")]


def test_repeating_block_is_not_parsed_as_an_event(monkeypatch):
    """`pmset -g sched` prints repeating events in a second block with no [N]
    index. Matching one would invent an event we would then try to cancel."""
    monkeypatch.setattr(wake, "_run", lambda args: (
        REAL_SCHED + "Repeating power events:\n"
        "  wakeorpoweron at 7:00AM every day\n"))
    assert wake.scheduled(now=NOW) == [at("2026-08-12T05:40:00-04:00")]


# ---------------------------------------------------------------- the desire

def _seed(con, *fire_ats):
    for i, fire_at in enumerate(fire_ats):
        con.execute(
            "INSERT INTO alarms (key, kind, fire_at, title, body, external_id,"
            " source_ref, written_at, refreshed_at)"
            " VALUES (?,?,?,?,'',NULL,'',NULL,'x')",
            (f"flight:X:{i}", "flight", fire_at, f"alarm {i}"))
    con.commit()


def test_wake_is_scheduled_one_lead_before_the_alarm(dbfile):
    from app import db
    con = db.connect()
    _seed(con, "2026-08-12T06:00:00-04:00")
    assert wake.desired(con, now=NOW) == [at("2026-08-12T05:40:00-04:00")]
    con.close()


def test_alarms_beyond_the_horizon_get_no_wake(dbfile):
    """ALARM_HORIZON_DAYS is 14 and WAKE_HORIZON_HOURS is 36 — deliberately not
    the same span. A Thanksgiving flight has an alarm and must not have a wake;
    the laptop will be open many times before then."""
    from app import db
    con = db.connect()
    _seed(con, "2026-11-26T06:00:00-05:00")
    assert wake.desired(con, now=NOW) == []
    con.close()


def test_an_alarm_inside_the_lead_window_gets_no_wake(dbfile):
    """Its wake time is already in the past — the cycle that would check it is
    the one running right now. Clamping to `now` would schedule a wake for a
    moment that has passed and churn it every cycle."""
    from app import db
    con = db.connect()
    _seed(con, "2026-08-11T12:05:00-04:00")
    assert wake.desired(con, now=NOW) == []
    con.close()


def test_past_alarms_get_no_wake(dbfile):
    from app import db
    con = db.connect()
    _seed(con, "2026-08-10T06:00:00-04:00")
    assert wake.desired(con, now=NOW) == []
    con.close()


def test_clustered_alarms_collapse_to_one_wake(dbfile):
    """A check-in and a leave_by for the same trip. One cycle refreshes both, so
    the second wake would be a machine spinning up to learn nothing."""
    from app import db
    con = db.connect()
    _seed(con, "2026-08-12T06:00:00-04:00", "2026-08-12T06:15:00-04:00")
    assert wake.desired(con, now=NOW) == [at("2026-08-12T05:40:00-04:00")]
    con.close()


def test_coalescing_keeps_the_earliest_not_the_latest():
    """The direction matters: the earliest is the one that lands before every
    alarm in the cluster. Keeping the latest would wake the machine after the
    first alarm had already gone off on the phone."""
    times = [at("2026-08-12T05:40:00-04:00"), at("2026-08-12T05:55:00-04:00")]
    assert wake._coalesce(times) == [times[0]]


def test_coalescing_does_not_chain_across_the_window():
    """Three alarms 25 minutes apart are not one cluster just because each is
    within the window of its neighbour — the second is measured against the
    wake we actually kept, so the span it covers stays bounded."""
    times = [at("2026-08-12T05:00:00-04:00"), at("2026-08-12T05:25:00-04:00"),
             at("2026-08-12T05:50:00-04:00")]
    assert wake._coalesce(times) == [times[0], times[2]]


def test_the_cap_keeps_the_soonest(dbfile, monkeypatch):
    """Truncation, not refusal — the opposite call from
    ALARM_MAX_WITHDRAW_PER_CYCLE, because what falls off the end here is the
    least urgent and a later cycle picks it up."""
    from app import db
    monkeypatch.setattr(wake, "WAKE_MAX_EVENTS", 2)
    con = db.connect()
    _seed(con, "2026-08-12T02:00:00-04:00", "2026-08-12T06:00:00-04:00",
          "2026-08-12T10:00:00-04:00")
    assert wake.desired(con, now=NOW) == [at("2026-08-12T01:40:00-04:00"),
                                          at("2026-08-12T05:40:00-04:00")]
    con.close()


def test_an_unparseable_fire_at_is_skipped_not_fatal(dbfile):
    """alarms.Alarm validates fire_at on the way in, so this can only come from
    a hand-edited row — but one bad row must not cost every other alarm its
    wake."""
    from app import db
    con = db.connect()
    _seed(con, "not-a-date", "2026-08-12T06:00:00-04:00")
    assert wake.desired(con, now=NOW) == [at("2026-08-12T05:40:00-04:00")]
    con.close()


# ---------------------------------------------------------------- the diff

def test_an_unscheduled_wake_is_added():
    p = wake.plan([at("2026-08-12T05:40:00-04:00")], [])
    assert p.add == [at("2026-08-12T05:40:00-04:00")]
    assert not p.cancel


def test_an_already_scheduled_wake_is_kept_not_re_added():
    """The reason this module is a reconciler at all. pmset schedule is
    append-only, so re-adding every cycle would leave hundreds of duplicate
    events by morning."""
    t = at("2026-08-12T05:40:00-04:00")
    p = wake.plan([t], [t])
    assert p.keep == [t] and not p.add and not p.cancel
    assert p.is_noop()


def test_a_wake_no_longer_wanted_is_cancelled():
    """The alarm was withdrawn — a cancelled flight. Its wake goes too, or the
    machine wakes at 5:40am for a trip that isn't happening."""
    stale = at("2026-08-12T05:40:00-04:00")
    p = wake.plan([], [stale])
    assert p.cancel == [stale] and not p.add


def test_a_small_drift_is_inside_the_deadband():
    """A leave_by is recomputed from live traffic and jitters by a minute or
    two. Without the deadband every cycle would cancel and re-add."""
    p = wake.plan([at("2026-08-12T05:42:00-04:00")],
                  [at("2026-08-12T05:40:00-04:00")])
    assert p.is_noop() and p.keep == [at("2026-08-12T05:40:00-04:00")]


def test_a_real_move_is_a_cancel_and_an_add():
    """A flight delayed by two hours. The old wake is useless and the new one is
    the entire point of the step."""
    p = wake.plan([at("2026-08-12T07:40:00-04:00")],
                  [at("2026-08-12T05:40:00-04:00")])
    assert p.add == [at("2026-08-12T07:40:00-04:00")]
    assert p.cancel == [at("2026-08-12T05:40:00-04:00")]


def test_matching_is_nearest_first_not_positional():
    """A new early alarm must not re-pair every later wake and churn the whole
    set. Positional matching would pair 02:00 with 05:40, call it a move, and
    then cancel-and-re-add a wake that was already correct."""
    early, late = at("2026-08-12T02:00:00-04:00"), at("2026-08-12T05:40:00-04:00")
    p = wake.plan([early, late], [late])
    assert p.add == [early]
    assert p.keep == [late]
    assert not p.cancel


# ---------------------------------------------------------------- applying

def test_dry_run_touches_nothing(dbfile, pmset):
    """A plan with real work in it — one add and one stale cancel — and not a
    single `pmset schedule` call behind it. `-g sched` is still read, because
    computing the plan is the whole of what a dry run does."""
    from app import db
    con = db.connect()
    _seed(con, "2026-08-12T09:00:00-04:00")   # wants 08:40; canned 05:40 is stale
    info = wake.reconcile(con, dry_run=True, now=NOW)
    con.close()
    assert (info["add"], info["cancel"], info["dry_run"]) == (1, 1, True)
    assert [c for c in pmset if c[0] == "schedule"] == []
    assert pmset == [["-g", "sched"]]


def test_apply_cancels_before_adding(dbfile, pmset):
    """A cancel that fails leaves a machine waking for no reason; an add that
    fails leaves an alarm unrefreshed, which is the thing this step exists to
    prevent. The cheap-to-lose half goes first."""
    from app import db
    con = db.connect()
    _seed(con, "2026-08-12T09:00:00-04:00")   # wants 08:40; 05:40 is stale
    wake.reconcile(con, dry_run=False, now=NOW)
    con.close()
    verbs = [c[:2] for c in pmset if c[0] == "schedule"]
    assert verbs == [["schedule", "cancel"], ["schedule", "wake"]]


def test_cancel_names_the_exact_event_and_never_cancelall(dbfile, pmset):
    from app import db
    con = db.connect()
    _seed(con, "2026-08-12T09:00:00-04:00")
    wake.reconcile(con, dry_run=False, now=NOW)
    con.close()
    cancels = [c for c in pmset if c[:2] == ["schedule", "cancel"]]
    assert cancels == [["schedule", "cancel", "wake", "08/12/26 05:40:00",
                        "com.kestrel.alarms"]]
    assert not any("cancelall" in part for c in pmset for part in c)


def test_only_the_write_verbs_ask_for_root(dbfile, pmset):
    """`pmset schedule` checks euid; `-g sched` does not. Asking for root on the
    read would make the whole module depend on the sudoers drop-in, so a machine
    without it could not even report what is scheduled — and status() is the one
    call that answers whether pmset ever accepted anything."""
    from app import db
    con = db.connect()
    _seed(con, "2026-08-12T09:00:00-04:00")
    wake.reconcile(con, dry_run=False, now=NOW)
    con.close()
    asked = {tuple(c[:2]): r for c, r in zip(pmset, pmset.root)}
    assert asked[("-g", "sched")] is False
    assert asked[("schedule", "cancel")] is True
    assert asked[("schedule", "wake")] is True


def test_sudo_only_wraps_the_privileged_call(monkeypatch):
    """The argv actually handed to subprocess. pmset is named by absolute path:
    a NOPASSWD rule matching a bare word off $PATH is the classic way to hand
    out more than was intended, and `-n` must be there so a missing rule fails
    instead of blocking the poll cycle on a password prompt."""
    seen = []

    class Proc:
        returncode = 0
        stdout = stderr = ""

    monkeypatch.setattr(wake.subprocess, "run",
                        lambda argv, **kw: (seen.append(argv), Proc())[1])
    wake._run(["-g", "sched"])
    wake._run(["schedule", "wake", "08/12/26 05:40:00", "x"], root=True)
    assert seen[0] == ["/usr/bin/pmset", "-g", "sched"]
    assert seen[1][:3] == ["sudo", "-n", "/usr/bin/pmset"]


def test_a_failing_pmset_does_not_raise(dbfile, monkeypatch):
    """Power management can refuse, and this runs inside the poll cycle. A wake
    that cannot be scheduled costs freshness; an exception here would cost the
    cycle."""
    from app import db

    def boom(args, *, root=False):
        if args[:2] == ["-g", "sched"]:
            return REAL_SCHED
        # What a missing sudoers drop-in actually says, verbatim.
        raise RuntimeError("sudo: a password is required")

    monkeypatch.setattr(wake, "_run", boom)
    con = db.connect()
    _seed(con, "2026-08-12T09:00:00-04:00")
    info = wake.reconcile(con, dry_run=False, now=NOW)
    con.close()
    assert info["applied"]["failed"] == 2 and info["applied"]["added"] == 0


def test_disabled_does_nothing_at_all(dbfile, monkeypatch, pmset):
    monkeypatch.setattr(wake, "WAKE_ENABLED", False)
    assert wake.reconcile(now=NOW) == {"enabled": False}
    assert pmset == []
