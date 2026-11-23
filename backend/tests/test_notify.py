"""§21 step 4 — the at-desk channel.

The dangerous half of an emitter is saying something twice, so most of what is
worth testing here is dedupe and the two ways a queued row can leave the queue
without being seen: presence and staleness. Delivery itself is stubbed — the
whole reason senders enqueue instead of posting is that there is exactly one
path to the OS to fake.
"""

import json
import os
from datetime import datetime, timedelta

import pytest

from app import db, macos, notify

NOW = datetime.fromisoformat("2026-08-10T14:00:00-04:00")


@pytest.fixture
def desk(monkeypatch):
    """At the desk, with a delivery backend that records instead of posting.
    Returns the call list. Presence is stubbed in *every* test that flushes,
    because the real probe would answer about whoever is running pytest."""
    posted = []
    monkeypatch.setattr(notify, "at_desk", lambda: True)
    monkeypatch.setattr(notify, "_post",
                        lambda title, body, target=None: posted.append((title, body)))
    return posted


# ------------------------------------------------------------------ dedupe

def test_the_same_fact_queues_once(dbfile):
    con = db.connect()
    assert notify.send("package", "Amazon:1", "shipped",
                       title="Amazon shipped", con=con, now=NOW) is True
    assert notify.send("package", "Amazon:1", "shipped",
                       title="Amazon shipped", con=con, now=NOW) is False
    assert len(notify.recent(con)) == 1
    con.close()


def test_a_changed_value_is_a_different_fact(dbfile):
    """The poll cycle re-reads the same status every five minutes and must stay
    quiet; a package that actually moves has news."""
    con = db.connect()
    notify.send("package", "Amazon:1", "shipped", title="a", con=con, now=NOW)
    assert notify.send("package", "Amazon:1", "delivered", title="b",
                       con=con, now=NOW) is True
    assert len(notify.recent(con)) == 2
    con.close()


def test_a_sent_row_is_never_requeued_by_a_second_send(dbfile, desk):
    """DO NOTHING, not an upsert: re-sending after delivery must not resurrect
    the row and say it again."""
    con = db.connect()
    notify.send("flight", "PNR", "1", title="Flight 1", con=con, now=NOW)
    notify.flush(con, now=NOW)
    notify.send("flight", "PNR", "1", title="Flight 1 (again)", con=con, now=NOW)

    assert notify.flush(con, now=NOW)["sent"] == 0
    assert desk == [("Flight 1", "")]
    con.close()


def test_a_colon_in_a_value_is_escaped_not_rejected(dbfile):
    """An AeroAPI status is free text. Raising on 'Delayed until 15:30' would
    sink the flight poll from inside its transaction."""
    con = db.connect()
    assert notify.send("flight", "PNR", "1", "Delayed until 15:30",
                       title="t", con=con, now=NOW) is True
    key, = [r["key"] for r in notify.recent(con)]
    assert key == "flight:PNR:1:Delayed until 15%3A30"
    con.close()


def test_two_facts_cannot_collide_onto_one_key(dbfile):
    """Injectivity, the property escaping exists to preserve — a collision means
    one fact inherits the other's delivery record and never fires."""
    con = db.connect()
    notify.send("package", "a:b", "c", title="one", con=con, now=NOW)
    assert notify.send("package", "a", "b:c", title="two",
                       con=con, now=NOW) is True
    con.close()


def test_an_unknown_kind_is_rejected(dbfile):
    con = db.connect()
    with pytest.raises(ValueError):
        notify.send("groceries", "x", title="t", con=con, now=NOW)
    con.close()


# ---------------------------------------------------------------- delivery

def test_flush_posts_the_queue_and_records_it(dbfile, desk):
    con = db.connect()
    notify.send("package", "Amazon:1", "delivered",
                title="Amazon delivered", body="a lamp", con=con, now=NOW)
    info = notify.flush(con, now=NOW)

    assert info["sent"] == 1 and info["failed"] == 0
    assert desk == [("Amazon delivered", "a lamp")]
    assert notify.recent(con)[0]["sent_at"] is not None
    con.close()


def test_a_failed_post_stays_queued_for_the_next_cycle(dbfile, monkeypatch):
    def boom(title, body, target=None):
        raise RuntimeError("osascript is not available")

    monkeypatch.setattr(notify, "at_desk", lambda: True)
    monkeypatch.setattr(notify, "_post", boom)
    con = db.connect()
    notify.send("package", "Amazon:1", "shipped", title="t", con=con, now=NOW)

    info = notify.flush(con, now=NOW)
    assert info == {"sent": 0, "failed": 1, "expired": 0, "queued": 1}
    assert notify.recent(con)[0]["sent_at"] is None
    con.close()


def test_nothing_posts_while_nobody_is_at_the_desk(dbfile, monkeypatch):
    """Held, not dropped and not counted as sent — a banner posted at a locked
    screen would be recorded as delivered and never seen."""
    posted = []
    monkeypatch.setattr(notify, "at_desk", lambda: False)
    monkeypatch.setattr(notify, "_post",
                        lambda title, body, target=None: posted.append(title))
    con = db.connect()
    notify.send("package", "Amazon:1", "shipped", title="t", con=con, now=NOW)

    assert notify.flush(con, now=NOW)["held"] == 1
    assert posted == []
    assert notify.recent(con)[0]["sent_at"] is None
    con.close()


def test_it_goes_out_once_the_user_comes_back(dbfile, monkeypatch, desk):
    con = db.connect()
    notify.send("package", "Amazon:1", "shipped", title="t", con=con, now=NOW)
    monkeypatch.setattr(notify, "at_desk", lambda: False)
    notify.flush(con, now=NOW)

    monkeypatch.setattr(notify, "at_desk", lambda: True)
    assert notify.flush(con, now=NOW + timedelta(minutes=5))["sent"] == 1
    con.close()


def test_disabled_holds_the_queue_without_posting(dbfile, monkeypatch, desk):
    monkeypatch.setattr(notify, "NOTIFY_ENABLED", False)
    con = db.connect()
    notify.send("package", "Amazon:1", "shipped", title="t", con=con, now=NOW)

    assert notify.flush(con, now=NOW)["disabled"] is True
    assert desk == []
    con.close()


# ---------------------------------------------------------------- staleness

def test_a_stale_notification_expires_instead_of_firing_late(dbfile, desk):
    """The one place this channel deliberately loses data: 'out for delivery',
    delivered two hours late, is worse than silence."""
    con = db.connect()
    notify.send("package", "Amazon:1", "out_for_delivery", title="t",
                con=con, now=NOW)
    late = NOW + timedelta(minutes=notify.NOTIFY_STALE_MIN + 1)

    info = notify.flush(con, now=late)
    assert info == {"sent": 0, "failed": 0, "expired": 1, "queued": 0}
    assert desk == []
    con.close()


def test_an_expired_notification_is_kept_readable(dbfile, desk):
    """It's the only record the channel had something to say while you were
    away — deleting it would make a missed notification indistinguishable from
    one that was never queued."""
    con = db.connect()
    notify.send("package", "Amazon:1", "shipped", title="t", con=con, now=NOW)
    notify.flush(con, now=NOW + timedelta(minutes=notify.NOTIFY_STALE_MIN + 1))

    row, = notify.recent(con)
    assert row["expired_at"] is not None and row["sent_at"] is None
    con.close()


def test_expiry_does_not_touch_an_already_sent_row(dbfile, desk):
    con = db.connect()
    notify.send("package", "Amazon:1", "shipped", title="t", con=con, now=NOW)
    notify.flush(con, now=NOW)

    notify.flush(con, now=NOW + timedelta(days=2))
    row, = notify.recent(con)
    assert row["expired_at"] is None and row["sent_at"] is not None
    con.close()


# --------------------------------------------------------------- burst cap

def test_the_cycle_cap_defers_rather_than_refusing(dbfile, monkeypatch, desk):
    """Opposite call from ALARM_MAX_WITHDRAW_PER_CYCLE: half the news beats none
    of it, and the rest keep their turn."""
    monkeypatch.setattr(notify, "NOTIFY_MAX_PER_CYCLE", 2)
    con = db.connect()
    for i in range(5):
        notify.send("package", f"Amazon:{i}", "shipped", title=f"t{i}",
                    con=con, now=NOW + timedelta(seconds=i))

    info = notify.flush(con, now=NOW)
    assert info["sent"] == 2 and info["deferred"] == 3
    assert [t for t, _ in desk] == ["t0", "t1"]      # oldest first

    assert notify.flush(con, now=NOW)["sent"] == 2
    assert notify.flush(con, now=NOW)["sent"] == 1
    con.close()


# ----------------------------------------------------------------- posting

def test_text_is_passed_as_arguments_never_interpolated(monkeypatch):
    """The old `_notify` pasted json.dumps(body) into the script text, and
    osascript then refused to compile the script for any non-ASCII character
    (-2741) — silently, under check=False. Nothing user-shaped may appear in the
    script itself."""
    seen = {}

    class Proc:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(cmd, **kw):
        seen["cmd"] = cmd
        return Proc()

    monkeypatch.setattr(macos.subprocess, "run", fake_run)
    notify._post('Nespresso Café shipped', 'Vertuo pods — "Altissio" 30ct')

    cmd = seen["cmd"]
    script = cmd[cmd.index("-e") + 1]
    assert "Nespresso" not in script and "Altissio" not in script
    assert cmd[cmd.index("--") + 1:] == ['Nespresso Café shipped',
                                         'Vertuo pods — "Altissio" 30ct']


def test_a_nonzero_exit_raises_so_the_row_stays_queued(monkeypatch):
    class Proc:
        returncode = 1
        stderr = "Application isn’t running"

    monkeypatch.setattr(macos.subprocess, "run", lambda cmd, **kw: Proc())
    with pytest.raises(RuntimeError, match="isn’t running"):
        notify._post("t", "b")


def _no_osascript(monkeypatch):
    def refuse(*a, **kw):
        raise AssertionError("posted through osascript, not the window")
    monkeypatch.setattr(macos.subprocess, "run", refuse)


def test_under_the_app_a_banner_goes_to_the_window_with_its_tab(dbfile, monkeypatch):
    """Kestrel.app hands the server a pipe; the window posts the banner as
    Kestrel and a click opens `target`. One JSON line per banner, and long
    non-ASCII text arrives whole (macOS PIPE_BUF is 512)."""
    r, w = os.pipe()
    monkeypatch.setattr(notify, "WINDOW_FD", w)
    monkeypatch.setattr(notify, "at_desk", lambda: True)
    _no_osascript(monkeypatch)
    con = db.connect()
    long = "Café — " * 120
    notify.send("console-turn", "tab1", "1", title="Kestrel finished",
                body=long, target="tab1", con=con, now=NOW)
    notify.send("package", "Amazon:1", "shipped", title="Amazon shipped",
                con=con, now=NOW)
    assert notify.flush(con, now=NOW)["sent"] == 2
    os.close(w)
    with os.fdopen(r) as f:
        lines = [json.loads(x) for x in f.read().splitlines()]
    assert lines == [
        {"title": "Kestrel finished", "body": long, "target": "tab1"},
        {"title": "Amazon shipped", "body": "", "target": None},
    ]
    con.close()


def test_an_fd_that_isnt_a_pipe_is_never_written(monkeypatch, tmp_path):
    f = open(tmp_path / "unrelated.txt", "w+")
    monkeypatch.setattr(notify, "WINDOW_FD", f.fileno())
    seen = []
    monkeypatch.setattr(notify, "_osascript_post", lambda title, body: seen.append(title))
    notify._post("t", "b", "tab1")
    f.seek(0)
    assert f.read() == "" and seen == ["t"]
    f.close()


def test_a_closed_window_pipe_falls_back_to_osascript(monkeypatch):
    r, w = os.pipe()
    os.close(r)
    monkeypatch.setattr(notify, "WINDOW_FD", w)
    seen = []
    monkeypatch.setattr(notify, "_osascript_post",
                        lambda title, body: seen.append(title))
    notify._post("t", "b", "tab1")
    os.close(w)
    assert seen == ["t"]


# ---------------------------------------------------------------- presence

def test_at_desk_fails_open_when_the_probe_cannot_answer(monkeypatch):
    """The direction that matters: a presence check that silently swallows every
    banner is `_notify`'s never-fired bug in better clothes."""
    monkeypatch.setattr(notify, "_probe", lambda cmd: None)
    assert notify.at_desk() is True


def test_a_locked_screen_is_not_at_the_desk(monkeypatch):
    """Typing a password is input, so idle time alone reads a lock screen as
    presence."""
    monkeypatch.setattr(
        notify, "_probe",
        lambda cmd: '"IOConsoleUsers" = ({"CGSSessionScreenIsLocked"=Yes})'
        if "Root" in cmd else '"HIDIdleTime" = 1000000')
    assert notify.at_desk() is False


def test_a_long_idle_is_not_at_the_desk(monkeypatch):
    idle_ns = (notify.NOTIFY_IDLE_MAX_SEC + 60) * 1_000_000_000
    monkeypatch.setattr(
        notify, "_probe",
        lambda cmd: '"IOConsoleUsers" = ({"kCGSSessionOnConsoleKey"=Yes})'
        if "Root" in cmd else f'"HIDIdleTime" = {idle_ns}')
    assert notify.at_desk() is False


def test_recent_input_at_an_unlocked_screen_is_at_the_desk(monkeypatch):
    monkeypatch.setattr(
        notify, "_probe",
        lambda cmd: '"IOConsoleUsers" = ({"kCGSSessionOnConsoleKey"=Yes})'
        if "Root" in cmd else '"HIDIdleTime" = 2000000000')
    assert notify.at_desk() is True
