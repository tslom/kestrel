"""get()'s output shape: which rows carry a `tz` label and what it says.

Guards the contract three renderers now depend on — the calendar card header,
the expansion's day headings, and brief_line().
"""

from datetime import datetime, timedelta, timezone

import pytest

from app import db, tz
from app.providers import calendar
from app.providers.ranges import parse_range

FAKE = [("2026-08-17", "America/Los_Angeles")]

ET = timezone(timedelta(hours=-4))
PT = timezone(timedelta(hours=-7))


@pytest.fixture
def cal(dbfile, monkeypatch):
    """A cache with one all-day row and two timed rows either side of the move,
    and no network: every day in range is marked synced so get() never fetches."""
    monkeypatch.setattr(tz, "TIMELINE", FAKE)
    monkeypatch.setattr(tz, "HOME_ZONE", "America/New_York")
    monkeypatch.setattr(calendar, "_sync",
                        lambda *a, **k: pytest.fail("get() tried to hit the API"))
    rows = [
        # Filed in Pacific but happening while the user is still in NC.
        ("2026-08-04", "e1", "USC orientation",
         "2026-08-04T09:00:00-07:00", "2026-08-04T10:00:00-07:00", 0),
        # Filed in Eastern, happening after the move.
        ("2026-09-04", "e2", "Zete call",
         "2026-09-04T21:00:00-04:00", "2026-09-04T22:00:00-04:00", 0),
        ("2026-08-19", "e3", "Move-In Day", "2026-08-19", "2026-08-20", 1),
    ]
    con = db.connect()
    with con:
        for day, eid, summary, start, end, all_day in rows:
            con.execute(
                "INSERT INTO events (day, event_id, calendar_id, calendar_name,"
                " summary, start_at, end_at, all_day, location)"
                " VALUES (?,?,'c','Events',?,?,?,?,NULL)",
                (day, eid, summary, start, end, all_day))
        for day in ("2026-08-04", "2026-08-19", "2026-09-04"):
            con.execute("INSERT INTO calendar_days (day, refreshed_at)"
                        " VALUES (?, '2026-08-03T00:00:00')", (day,))
    con.close()


def _one(day):
    return calendar.get(parse_range(f"{day}..{day}"))["data"][0]


def test_event_filed_in_pacific_reads_eastern_before_the_move(cal):
    """The instant is untouched; only the frame the reader gets."""
    ev = _one("2026-08-04")
    assert ev["tz"] == "ET"
    assert datetime.fromisoformat(ev["start"]) == datetime(2026, 8, 4, 9, tzinfo=PT)
    assert ev["start"].startswith("2026-08-04T12:00")


def test_event_filed_in_eastern_reads_pacific_after_the_move(cal):
    ev = _one("2026-09-04")
    assert ev["tz"] == "PT"
    assert datetime.fromisoformat(ev["end"]) == datetime(2026, 9, 4, 22, tzinfo=ET)
    assert ev["start"].startswith("2026-09-04T18:00")


def test_all_day_rows_carry_no_zone_label(cal):
    """No clock time, nothing to label — a lone 'ET' on an all-day card is
    noise, and the renderers key off the field's presence."""
    ev = _one("2026-08-19")
    assert ev["all_day"] is True
    assert ev["tz"] is None
    assert ev["start"] == "2026-08-19"
    assert calendar.brief_line(ev).startswith("2026-08-19 all-day ")
