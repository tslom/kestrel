"""Due dates that name an hour (tasks.due_time): the 04:00 day-key rollover and when a timed task becomes overdue."""

from datetime import date, datetime, timedelta

import pytest

from app import db


def _day(offset=0):
    return (date.fromisoformat(db.day_key()) + timedelta(days=offset)).isoformat()


def test_split_due_accepts_a_bare_date():
    assert db.split_due("2026-09-21") == ("2026-09-21", None)


def test_split_due_accepts_a_combined_stamp():
    assert db.split_due("2026-09-21T17:30") == ("2026-09-21", "17:30")
    assert db.split_due("2026-09-21 17:30") == ("2026-09-21", "17:30")
    # Seconds are dropped, not rejected, so callers can echo an ISO stamp.
    assert db.split_due("2026-09-21T17:30:00") == ("2026-09-21", "17:30")


def test_split_due_empty_is_no_due():
    assert db.split_due(None) == (None, None)
    assert db.split_due("  ") == (None, None)


@pytest.mark.parametrize("bad", ["tomorrow", "2026-9-21", "2026-09-21X17:30",
                                 "2026-09-21T25:00", "2026-09-21T17:70", "17:30"])
def test_split_due_rejects_junk(bad):
    with pytest.raises(ValueError):
        db.split_due(bad)


def test_day_instant_inverts_day_key():
    # An evening time is that calendar day.
    assert db.day_instant("2026-09-21", "17:00") == datetime(2026, 9, 21, 17, 0)
    # After-midnight times are the next calendar day: 01:00 belongs to the day key that began the evening before.
    assert db.day_instant("2026-09-21", "01:00") == datetime(2026, 9, 22, 1, 0)


@pytest.mark.parametrize("hhmm", ["04:00", "09:30", "23:59", "00:30", "03:59"])
def test_day_instant_round_trips_through_day_key(hhmm):
    assert db.day_key(db.day_instant("2026-09-21", hhmm)) == "2026-09-21"


def test_create_stores_the_hour(dbfile):
    t = db.create_task("call the bank", due="2026-09-21", due_time="09:00")
    assert (t["due"], t["due_time"]) == ("2026-09-21", "09:00")
    assert t["due_at"] == "2026-09-21T09:00"


def test_create_accepts_the_hour_inside_due(dbfile):
    t = db.create_task("call the bank", due="2026-09-21T09:00")
    assert (t["due"], t["due_time"]) == ("2026-09-21", "09:00")


def test_all_day_task_has_no_instant(dbfile):
    t = db.create_task("tidy up", due="2026-09-21")
    assert t["due_time"] is None
    assert t["due_at"] is None


def test_an_hour_needs_a_date(dbfile):
    with pytest.raises(ValueError):
        db.create_task("floating", due_time="09:00")


def test_rescheduling_to_a_plain_date_clears_the_hour(dbfile):
    t = db.create_task("call the bank", due="2026-09-21T09:00")
    moved = db.update_task(t["id"], due="2026-09-25")
    assert moved["due_time"] is None, "yesterday's 9am must not follow the task"


def test_patching_only_the_hour_keeps_the_existing_date(dbfile):
    t = db.create_task("call the bank", due="2026-09-21")
    moved = db.update_task(t["id"], due_time="15:00")
    assert (moved["due"], moved["due_time"]) == ("2026-09-21", "15:00")


def test_patching_the_hour_off_keeps_the_date(dbfile):
    t = db.create_task("call the bank", due="2026-09-21T09:00")
    moved = db.update_task(t["id"], due_time=None)
    assert (moved["due"], moved["due_time"]) == ("2026-09-21", None)


def test_patching_neither_leaves_both_alone(dbfile):
    t = db.create_task("call the bank", due="2026-09-21T09:00")
    moved = db.update_task(t["id"], title="call the other bank")
    assert (moved["due"], moved["due_time"]) == ("2026-09-21", "09:00")


def test_bad_hour_is_rejected(dbfile):
    t = db.create_task("call the bank", due="2026-09-21")
    with pytest.raises(ValueError):
        db.update_task(t["id"], due_time="half five")


def test_today_with_a_past_hour_is_overdue(dbfile, monkeypatch):
    """A 9am deadline must not sit in "due today" all afternoon as fresh as a 5pm one."""
    monkeypatch.setattr(db, "day_key", lambda dt=None: "2026-09-21")
    monkeypatch.setattr(db, "datetime", _clock(datetime(2026, 9, 21, 14, 0)))
    past = db.create_task("9am standup", due="2026-09-21T09:00")
    soon = db.create_task("5pm deploy", due="2026-09-21T17:00")
    allday = db.create_task("tidy up", due="2026-09-21")

    out = db.tasks_today()
    assert [t["id"] for t in out["overdue"]] == [past["id"]]
    # An untimed task is due by end of day, so it stays put until the day rolls.
    assert [t["id"] for t in out["due_today"]] == [soon["id"], allday["id"]]


def test_after_midnight_hour_is_not_yet_overdue(dbfile, monkeypatch):
    """23:30 on day key D read at 00:30 is the next calendar day but still key D, before the deadline."""
    monkeypatch.setattr(db, "day_key", lambda dt=None: "2026-09-21")
    monkeypatch.setattr(db, "datetime", _clock(datetime(2026, 9, 22, 0, 30)))
    t = db.create_task("1am cron check", due="2026-09-21T01:00")
    out = db.tasks_today()
    assert [x["id"] for x in out["due_today"]] == [t["id"]]
    assert out["overdue"] == []
    assert t["due_at"] == "2026-09-22T01:00"


def test_due_today_is_in_clock_order_with_untimed_last(dbfile, monkeypatch):
    monkeypatch.setattr(db, "day_key", lambda dt=None: "2026-09-21")
    monkeypatch.setattr(db, "datetime", _clock(datetime(2026, 9, 21, 5, 0)))
    late = db.create_task("1am wrap", due="2026-09-21T01:00")
    noon = db.create_task("noon call", due="2026-09-21T12:00")
    allday = db.create_task("tidy up", due="2026-09-21")
    evening = db.create_task("8pm gym", due="2026-09-21T20:00")
    # 01:00 sorts last of the timed three: it is the end of this day key.
    assert [t["id"] for t in db.tasks_today()["due_today"]] == [
        noon["id"], evening["id"], late["id"], allday["id"]]


def test_tasks_today_reports_the_day_key(dbfile):
    assert db.tasks_today()["today"] == db.day_key()


def test_list_tasks_carries_the_instant(dbfile):
    db.create_task("call the bank", due=_day(2) + "T09:00")
    (t,) = db.list_tasks()
    assert t["due_at"] == f"{_day(2)}T09:00"


def test_subtasks_carry_the_instant_too(dbfile):
    parent = db.create_task("parent", due=_day(1))
    db.create_task("child", due=_day(1) + "T08:00", parent_id=parent["id"])
    (top,) = db.list_tasks()
    assert top["subtasks"][0]["due_at"] == f"{_day(1)}T08:00"
    for group in ("overdue", "due_today", "available"):
        for t in db.tasks_today()[group]:
            for s in t["subtasks"]:
                assert "due_at" in s


def test_api_round_trips_the_hour(client, dbfile):
    made = client.post("/api/tasks", json={"title": "deploy", "due": "2026-09-21",
                                           "due_time": "17:00"}).json()
    assert made["due_time"] == "17:00"
    got = client.get("/api/tasks").json()
    assert got[0]["due_at"] == "2026-09-21T17:00"
    moved = client.patch(f"/api/tasks/{made['id']}", json={"due_time": "18:30"})
    assert moved.json()["due_at"] == "2026-09-21T18:30"


def test_api_rejects_a_bad_hour(client, dbfile):
    r = client.post("/api/tasks", json={"title": "deploy", "due": "2026-09-21",
                                        "due_time": "tea time"})
    assert r.status_code == 400


def test_generated_view_prints_the_hour(dbfile):
    from app import vault_views
    line = vault_views._task_line(
        {"id": 1, "title": "deploy", "due": "2026-09-21", "due_time": "17:00"})
    assert "due 2026-09-21 17:00" in line
    assert "due 2026-09-21 " not in vault_views._task_line(
        {"id": 2, "title": "tidy", "due": "2026-09-21"})


def _clock(at):
    """Stand-in for db.datetime with a fixed now(); the rest of the class is intact."""
    class _Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return at
    return _Frozen
