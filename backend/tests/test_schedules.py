"""Scheduled prompts (schedules.py): next times, the due/claim/ran cycle,
and what a closed lid does to them."""

from datetime import datetime, timedelta

import pytest

from app import schedules as s

SUN_8PM = s.ScheduleIn(title="digest", prompt="what's due", at_time="20:00", repeat="weekly:6")


def test_next_time_weekly_daily_once():
    fri = datetime(2026, 11, 20, 12, 0)   # a Friday
    assert s.next_time("20:00", "weekly:6", fri) == datetime(2026, 11, 22, 20, 0)
    assert s.next_time("20:00", "weekly:6", datetime(2026, 11, 22, 20, 0)) == datetime(2026, 11, 29, 20, 0)
    assert s.next_time("07:30", "daily", fri) == datetime(2026, 11, 21, 7, 30)
    assert s.next_time("2026-11-21T09:00", None, fri) == datetime(2026, 11, 21, 9, 0)
    assert s.next_time("2026-11-19T09:00", None, fri) is None


@pytest.mark.parametrize("repeat", ["weekly:", "weekly:9", "hourly"])
def test_bad_repeat(repeat):
    with pytest.raises(s.ScheduleError):
        s.parse_repeat(repeat)


def test_describe():
    assert s.describe({"repeat": "weekly:6", "at_time": "20:00"}) == "Sundays at 20:00"
    assert s.describe({"repeat": "weekly:0,2", "at_time": "09:00"}) == "Mon, Wed at 09:00"
    assert s.describe({"repeat": "weekly:0,1,2,3,4", "at_time": "08:00"}) == "weekdays at 08:00"
    assert s.describe({"repeat": "daily", "at_time": "07:30"}) == "every day at 07:30"


def test_cycle_claims_once_and_advances(con):
    fri = datetime(2026, 11, 20, 12, 0)
    row = s.create(SUN_8PM, now=fri, con=con)
    assert row["next_at"] == "2026-11-22T20:00"
    assert s.due(now=fri, con=con) == []
    at = datetime(2026, 11, 22, 20, 1)
    assert [d["id"] for d in s.due(now=at, con=con)] == [row["id"]]
    got = s.claim(row["id"], now=at, con=con)
    assert got and "what's due" in got["brief"] and "late" not in got["brief"]
    # A second window can't take it while the claim is live.
    assert s.claim(row["id"], now=at, con=con) is None
    assert s.due(now=at, con=con) == []
    done = s.ran(row["id"], "tab1", now=at, con=con)
    assert done["next_at"] == "2026-11-29T20:00" and done["runs"] == 1 and done["enabled"]


def test_a_lapsed_claim_comes_back(con):
    row = s.create(SUN_8PM, now=datetime(2026, 11, 20, 12, 0), con=con)
    at = datetime(2026, 11, 22, 20, 1)
    assert s.claim(row["id"], now=at, con=con)
    later = at + timedelta(seconds=s.CLAIM_S + 60)
    assert [d["id"] for d in s.due(now=later, con=con)] == [row["id"]]


def test_lid_closed_runs_once_late_then_skips_when_stale(con):
    row = s.create(SUN_8PM, now=datetime(2026, 11, 20, 12, 0), con=con)
    # Opened Monday 7am: 11h late, within grace — runs once, says it's late.
    mon = datetime(2026, 11, 23, 7, 0)
    got = s.claim(row["id"], now=mon, con=con)
    assert got and "11h late" in got["brief"]
    assert s.ran(row["id"], None, now=mon, con=con)["next_at"] == "2026-11-29T20:00"
    # Away a whole week and then some: skipped to the next time, not run.
    back = datetime(2026, 12, 1, 12, 0)
    assert s.due(now=back, con=con) == []
    assert s.listing(con=con)[0]["next_at"] == "2026-12-06T20:00"


def test_one_off_runs_late_and_finishes(con):
    row = s.create(s.ScheduleIn(title="t", prompt="p", at_time="2026-11-21T09:00"),
                   now=datetime(2026, 11, 20, 12, 0), con=con)
    way_later = datetime(2026, 11, 23, 9, 0)
    assert s.claim(row["id"], now=way_later, con=con)
    done = s.ran(row["id"], "t", now=way_later, con=con)
    assert done["next_at"] is None and not done["enabled"]


def test_resume_starts_from_the_next_time(con):
    row = s.create(SUN_8PM, now=datetime(2026, 11, 20, 12, 0), con=con)
    s.set_enabled(row["id"], False, con=con)
    back = s.set_enabled(row["id"], True, now=datetime(2026, 11, 25, 12, 0), con=con)
    assert back["next_at"] == "2026-11-29T20:00"


def test_create_validates(con):
    with pytest.raises(s.ScheduleError):
        s.create(s.ScheduleIn(title="t", prompt="p", at_time="8pm", repeat="daily"), con=con)
    with pytest.raises(s.ScheduleError):
        s.create(s.ScheduleIn(title="t", prompt="p", mode="dev", at_time="20:00", repeat="daily"), con=con)


def test_routes(router_client):
    c = router_client(s.router)
    r = c.post("/api/schedules", json={"title": "d", "prompt": "p", "at_time": "20:00", "repeat": "daily"})
    assert r.status_code == 200, r.text
    rid = r.json()["id"]
    assert c.get("/api/schedules").json()["schedules"][0]["id"] == rid
    assert c.post(f"/api/schedules/{rid}/claim").json()["ok"] is False   # not due yet
    assert c.post("/api/schedules", json={"title": "d", "prompt": "p", "at_time": "x",
                                          "repeat": "daily"}).status_code == 400
    assert c.delete(f"/api/schedules/{rid}").json() == {"ok": True}
    assert c.delete(f"/api/schedules/{rid}").status_code == 404
