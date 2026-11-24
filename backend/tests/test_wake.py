"""The scheduled wake: pure plan()/_coalesce() tests, plus parsing against real `pmset -g sched` output.

Design: DESIGN.md → `tests/test_wake.py`."""

import time
from datetime import datetime

import pytest

from app import wake

NOW = datetime.fromisoformat("2026-08-11T12:00:00-04:00")


@pytest.fixture(autouse=True)
def eastern(monkeypatch):
    """Pin TZ to New York: pmset prints bare local wall-clock times."""
    monkeypatch.setenv("TZ", "America/New_York")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()

# Real `pmset -g sched` output with one of our events spliced in; the system rows are what a cancel must never reach.
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
        """argv list with the root flag of each call in `.root`."""
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
    """Three of the four events belong to macOS; every cancel is drawn from this list."""
    assert wake.scheduled(now=NOW) == [at("2026-08-12T05:40:00-04:00")]


def test_past_events_are_not_returned(pmset):
    """A wake whose time slid by while asleep is spent: neither cancelled nor counted."""
    later = datetime.fromisoformat("2026-08-13T12:00:00-04:00")
    assert wake.scheduled(now=later) == []


def test_a_trailing_user_visible_field_does_not_break_the_match(monkeypatch):
    """Row [2] carries `User visible: true` after the owner; the regex is unanchored at the end for that."""
    monkeypatch.setattr(wake, "WAKE_OWNER",
                        "com.apple.alarm.user-visible-com.apple.donotdisturb"
                        ".server.ScheduleLifetimeMonitor.timer")
    monkeypatch.setattr(wake, "_run", lambda args: REAL_SCHED)
    assert wake.scheduled(now=NOW) == [at("2026-08-12T00:00:00-04:00")]


def test_repeating_block_is_not_parsed_as_an_event(monkeypatch):
    """The repeating-events block has no [N] index; matching one would invent a cancel."""
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
    """Alarm horizon is 14 days, wake horizon 36 hours on purpose."""
    from app import db
    con = db.connect()
    _seed(con, "2026-11-26T06:00:00-05:00")
    assert wake.desired(con, now=NOW) == []
    con.close()


def test_an_alarm_inside_the_lead_window_gets_no_wake(dbfile):
    """A wake already in the past is dropped, not clamped to now."""
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
    """A check-in and a leave_by for one trip share one wake."""
    from app import db
    con = db.connect()
    _seed(con, "2026-08-12T06:00:00-04:00", "2026-08-12T06:15:00-04:00")
    assert wake.desired(con, now=NOW) == [at("2026-08-12T05:40:00-04:00")]
    con.close()


def test_coalescing_keeps_the_earliest_not_the_latest():
    """The earliest wake of a cluster is kept, not the latest."""
    times = [at("2026-08-12T05:40:00-04:00"), at("2026-08-12T05:55:00-04:00")]
    assert wake._coalesce(times) == [times[0]]


def test_coalescing_does_not_chain_across_the_window():
    """Clustering is measured against the wake kept, so a chain of 25-minute alarms stays bounded."""
    times = [at("2026-08-12T05:00:00-04:00"), at("2026-08-12T05:25:00-04:00"),
             at("2026-08-12T05:50:00-04:00")]
    assert wake._coalesce(times) == [times[0], times[2]]


def test_the_cap_keeps_the_soonest(dbfile, monkeypatch):
    """Over the cap, the least urgent are truncated (not refused); a later cycle picks them up."""
    from app import db
    monkeypatch.setattr(wake, "WAKE_MAX_EVENTS", 2)
    con = db.connect()
    _seed(con, "2026-08-12T02:00:00-04:00", "2026-08-12T06:00:00-04:00",
          "2026-08-12T10:00:00-04:00")
    assert wake.desired(con, now=NOW) == [at("2026-08-12T01:40:00-04:00"),
                                          at("2026-08-12T05:40:00-04:00")]
    con.close()


def test_an_unparseable_fire_at_is_skipped_not_fatal(dbfile):
    """One hand-edited bad fire_at must not cost every other alarm its wake."""
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
    """Reconciler, not re-adder: pmset schedule is append-only, so re-adding leaves duplicates."""
    t = at("2026-08-12T05:40:00-04:00")
    p = wake.plan([t], [t])
    assert p.keep == [t] and not p.add and not p.cancel
    assert p.is_noop()


def test_a_wake_no_longer_wanted_is_cancelled():
    """A withdrawn alarm's wake is cancelled too."""
    stale = at("2026-08-12T05:40:00-04:00")
    p = wake.plan([], [stale])
    assert p.cancel == [stale] and not p.add


def test_a_small_drift_is_inside_the_deadband():
    """A leave_by jitters by a minute or two; the deadband stops cancel/re-add churn."""
    p = wake.plan([at("2026-08-12T05:42:00-04:00")],
                  [at("2026-08-12T05:40:00-04:00")])
    assert p.is_noop() and p.keep == [at("2026-08-12T05:40:00-04:00")]


def test_a_real_move_is_a_cancel_and_an_add():
    """A delayed flight moves its wake."""
    p = wake.plan([at("2026-08-12T07:40:00-04:00")],
                  [at("2026-08-12T05:40:00-04:00")])
    assert p.add == [at("2026-08-12T07:40:00-04:00")]
    assert p.cancel == [at("2026-08-12T05:40:00-04:00")]


def test_matching_is_nearest_first_not_positional():
    """A new early alarm must not re-pair later wakes positionally and churn them."""
    early, late = at("2026-08-12T02:00:00-04:00"), at("2026-08-12T05:40:00-04:00")
    p = wake.plan([early, late], [late])
    assert p.add == [early]
    assert p.keep == [late]
    assert not p.cancel


# ---------------------------------------------------------------- applying

def test_dry_run_touches_nothing(dbfile, pmset):
    """A dry run reads `-g sched` but makes no `pmset schedule` call."""
    from app import db
    con = db.connect()
    _seed(con, "2026-08-12T09:00:00-04:00")   # wants 08:40; canned 05:40 is stale
    info = wake.reconcile(con, dry_run=True, now=NOW)
    con.close()
    assert (info["add"], info["cancel"], info["dry_run"]) == (1, 1, True)
    assert [c for c in pmset if c[0] == "schedule"] == []
    assert pmset == [["-g", "sched"]]


def test_apply_cancels_before_adding(dbfile, pmset):
    """Cancels run before adds: a failed cancel is the cheap loss."""
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
    """Only `pmset schedule` asks for root; the `-g sched` read must work without the sudoers drop-in."""
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
    """pmset is named by absolute path with `-n`: a bare-word NOPASSWD rule is unsafe and a password prompt would block the poll."""
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
    """A pmset refusal is counted as failed, never raised into the poll cycle."""
    from app import db

    def boom(args, *, root=False):
        if args[:2] == ["-g", "sched"]:
            return REAL_SCHED
        # Verbatim message when the sudoers drop-in is missing.
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
