""""When am I free": calendar.free_slots and the free_time tool. Traps: an event that started the night
before, a day in another zone, and an all-day banner marking the whole day busy.
"""

from datetime import time
from zoneinfo import ZoneInfo

import pytest

from app import db, tz
from app.providers import calendar as calendar_provider
from app.providers.ranges import parse_range

DAY = "2026-08-20"


@pytest.fixture
def cal(dbfile, monkeypatch):
    """A cached day with the API refusing calls (a silent sync would test Google)."""
    def no_api(*a, **k):
        raise AssertionError("free_slots must not hit the API")

    monkeypatch.setattr(calendar_provider, "_sync", no_api)
    # Pin the zone: fixtures are Eastern clock times, the real zone table says west coast.
    monkeypatch.setattr(tz, "zone_on", lambda day: ZoneInfo("America/New_York"))

    def seed(rows, day=DAY):
        con = db.connect()
        with con:
            con.execute("INSERT INTO calendar_days (day, refreshed_at)"
                        " VALUES (?, '2026-08-20T08:00:00')", (day,))
            for i, (summary, start, end, all_day) in enumerate(rows):
                con.execute(
                    "INSERT INTO events (day, event_id, calendar_id,"
                    " calendar_name, summary, start_at, end_at, all_day, location)"
                    " VALUES (?,?,?,?,?,?,?,?,NULL)",
                    (day, f"ev{i}", "cal-a", "Personal", summary, start, end,
                     int(all_day)))
        con.close()
    return seed


def slots(day=DAY, **kw):
    out = calendar_provider.free_slots(parse_range(day), **kw)
    return [(g["start"], g["end"]) for g in out["days"][0]["free"]]


def test_an_empty_day_is_free_all_window(cal):
    cal([])
    assert slots() == [("09:00", "21:00")]


def test_events_are_subtracted(cal):
    cal([("Lecture", "2026-08-20T10:00:00-04:00", "2026-08-20T11:30:00-04:00", 0),
         ("Lab", "2026-08-20T14:00:00-04:00", "2026-08-20T16:00:00-04:00", 0)])

    assert slots() == [("09:00", "10:00"), ("11:30", "14:00"), ("16:00", "21:00")]


def test_overlapping_events_are_one_busy_block(cal):
    cal([("Lecture", "2026-08-20T10:00:00-04:00", "2026-08-20T12:00:00-04:00", 0),
         ("Office hours", "2026-08-20T11:00:00-04:00", "2026-08-20T13:00:00-04:00", 0)])

    assert slots() == [("09:00", "10:00"), ("13:00", "21:00")]


def test_an_event_swallowed_by_a_longer_one_does_not_reopen_the_gap(cal):
    """Merging needs max(): a meeting nested in a long one must not move the cursor backwards."""
    cal([("Retreat", "2026-08-20T10:00:00-04:00", "2026-08-20T17:00:00-04:00", 0),
         ("Standup", "2026-08-20T11:00:00-04:00", "2026-08-20T11:15:00-04:00", 0)])

    assert slots() == [("09:00", "10:00"), ("17:00", "21:00")]


def test_an_event_from_the_night_before_only_blocks_its_own_tail(cal):
    """The 10pm–1am row on day two is spill-over, not a 10pm start."""
    cal([("Party", "2026-08-19T22:00:00-04:00", "2026-08-20T10:30:00-04:00", 0)])

    assert slots() == [("10:30", "21:00")]


def test_an_all_day_event_does_not_block_the_day(cal):
    cal([("Rowan's birthday", "2026-08-20", "2026-08-21", 1),
         ("Lecture", "2026-08-20T10:00:00-04:00", "2026-08-20T11:00:00-04:00", 0)])

    out = calendar_provider.free_slots(parse_range(DAY))
    assert out["days"][0]["all_day"] == ["Rowan's birthday"]
    assert slots() == [("09:00", "10:00"), ("11:00", "21:00")]


def test_a_timed_event_over_a_day_is_a_banner_not_busy(cal):
    """A timed event over a day long frames the day like an all-day one (a mis-dated 1h event inflated Load to 696h a week)."""
    cal([("Wrist appt", "2026-08-18T15:30:00-04:00", "2026-08-25T16:30:00-04:00", 0),
         ("Lecture", "2026-08-20T10:00:00-04:00", "2026-08-20T11:00:00-04:00", 0)])

    out = calendar_provider.free_slots(parse_range(DAY))
    assert out["days"][0]["all_day"] == ["Wrist appt"]
    assert slots() == [("09:00", "10:00"), ("11:00", "21:00")]

    rows = {e["summary"]: e for e in calendar_provider.get(parse_range(DAY))["data"]}
    assert rows["Wrist appt"]["all_day"] and rows["Wrist appt"]["span"]
    assert rows["Wrist appt"]["tz"] is None
    assert not rows["Lecture"]["all_day"] and "span" not in rows["Lecture"]
    assert "multi-day" in calendar_provider.brief_line(rows["Wrist appt"])


def test_an_event_of_exactly_a_day_still_blocks(cal):
    cal([("Shift", "2026-08-19T21:00:00-04:00", "2026-08-20T21:00:00-04:00", 0)])

    assert slots() == []


def test_gaps_shorter_than_asked_for_are_not_offered(cal):
    cal([("Lecture", "2026-08-20T09:30:00-04:00", "2026-08-20T11:00:00-04:00", 0)])

    assert slots(min_minutes=60) == [("11:00", "21:00")]
    assert slots(min_minutes=30) == [("09:00", "09:30"), ("11:00", "21:00")]


def test_the_window_can_be_narrowed(cal):
    cal([])
    assert slots(earliest=time(8, 0), latest=time(12, 0)) == [("08:00", "12:00")]


def test_a_day_with_no_room_left_says_so(cal):
    cal([("All-day workshop", "2026-08-20T08:00:00-04:00",
          "2026-08-20T22:00:00-04:00", 0)])

    assert slots() == []


def test_times_are_in_the_zone_of_the_day_not_the_machine(cal, monkeypatch):
    """The cached instant is the same; which clock it's read on isn't."""
    monkeypatch.setattr(tz, "zone_on", lambda day: ZoneInfo("America/Los_Angeles"))
    cal([("Lecture", "2026-08-20T10:00:00-04:00", "2026-08-20T11:00:00-04:00", 0)])

    # 10am Eastern is 7am Pacific: before the window, so it takes nothing.
    assert slots() == [("09:00", "21:00")]


def _call(**args):
    import asyncio

    from app import console_tools
    handler = [t for t in console_tools.TOOLS if t.name == "free_time"][0]
    return asyncio.run(handler.handler(args))["content"][0]["text"]


def test_the_tool_lists_a_days_gaps(cal):
    cal([("Lecture", "2026-08-20T10:00:00-04:00", "2026-08-20T11:30:00-04:00", 0)])

    said = _call(range=DAY)
    assert "09:00-10:00" in said and "11:30-21:00" in said


def test_the_tool_refuses_a_range_it_cannot_parse(cal):
    assert "unknown range" in _call(range="sometime next weekish")


def test_the_tool_refuses_a_clock_it_cannot_parse(cal):
    cal([])
    assert "HH:MM" in _call(range=DAY, earliest="9am")
