"""Which zone a date renders in (app/tz.py), and that the calendar uses it.

The bug these guard against isn't a wrong instant — instants were always right.
It's a slice of calendar that changes frame halfway down, so 9:00 AM Pacific and
8:30 PM Eastern sit next to each other looking three hours apart.
"""

from datetime import date
from zoneinfo import ZoneInfo

import pytest

from app import tz
from app.providers import calendar

# Structural tests run against a fixture timeline, not the real one, so editing
# a flight date in tz.py doesn't break them.
FAKE = [("2026-08-17", "America/Los_Angeles"),
        ("2026-11-25", "America/New_York"),
        ("2026-11-29", "America/Los_Angeles")]


@pytest.fixture
def timeline(monkeypatch):
    monkeypatch.setattr(tz, "TIMELINE", FAKE)
    monkeypatch.setattr(tz, "HOME_ZONE", "America/New_York")


def _name(day):
    return str(tz.zone_on(day))


@pytest.mark.parametrize("day,zone", [
    ("2026-01-01", "America/New_York"),     # long before any transition
    ("2026-08-16", "America/New_York"),     # day before the move
    ("2026-08-17", "America/Los_Angeles"),  # the move itself — boundary is >=
    ("2026-08-18", "America/Los_Angeles"),
    ("2026-11-24", "America/Los_Angeles"),
    ("2026-11-25", "America/New_York"),     # home for Thanksgiving
    ("2026-11-28", "America/New_York"),
    ("2026-11-29", "America/Los_Angeles"),  # and back
    ("2027-06-01", "America/Los_Angeles"),  # past the end, last entry holds
])
def test_zone_on_each_side_of_every_transition(timeline, day, zone):
    assert _name(day) == zone


def test_short_trip_flips_the_zone_and_flips_back(timeline):
    """The Thanksgiving window is four days wide; a rule that only tracked
    'where do they live now' would render all of it Pacific."""
    week = [f"2026-11-{d}" for d in range(23, 31)]
    assert [tz.label(d) for d in week] == \
        ["PT", "PT", "ET", "ET", "ET", "ET", "PT", "PT"]


def test_zone_on_takes_a_date_object_too(timeline):
    assert tz.zone_on(date(2026, 8, 18)) == ZoneInfo("America/Los_Angeles")


def test_label_is_coast_not_dst(timeline):
    """August is EDT and January is EST; the reader needs neither."""
    assert tz.label("2026-08-01") == "ET"
    assert tz.label("2026-01-01") == "ET"
    assert tz.label("2026-09-01") == "PT"


def test_to_local_keeps_the_instant(timeline):
    """A USC block filed at 9am Pacific, read from North Carolina in August."""
    out = tz.to_local("2026-08-04T09:00:00-07:00", "2026-08-04")
    assert out == "2026-08-04T12:00:00-04:00"


def test_to_local_after_the_move_leaves_pacific_alone(timeline):
    out = tz.to_local("2026-09-04T09:00:00-07:00", "2026-09-04")
    assert out == "2026-09-04T09:00:00-07:00"


def test_to_local_passes_all_day_values_through(timeline):
    """A bare date has no instant to move — shifting it would invent one."""
    assert tz.to_local("2026-08-19", "2026-08-19") == "2026-08-19"


def test_real_timeline_is_sorted_and_uses_valid_zones():
    """The configured timeline is hand-edited; zone_on walks it in order and
    would silently skip an out-of-order entry."""
    keys = [k for k, _ in tz.TIMELINE]
    assert keys == sorted(keys)
    for key, zone in tz.TIMELINE:
        assert date.fromisoformat(key)
        assert ZoneInfo(zone)
    assert ZoneInfo(tz.HOME_ZONE)


def _row(start, end, all_day=False):
    return {"start_at": start, "end_at": end, "all_day": all_day}


def test_days_covered_uses_the_users_zone_not_the_machines(timeline):
    """A 1am Eastern event in September is a 10pm Pacific event the night
    before — the day it's filed under has to be the day the user lives in."""
    assert calendar.days_covered(
        _row("2026-09-02T01:00:00-04:00", "2026-09-02T02:00:00-04:00")) \
        == ["2026-09-01"]
    # The same wall-clock event in August, before the move, stays on its date.
    assert calendar.days_covered(
        _row("2026-08-02T01:00:00-04:00", "2026-08-02T02:00:00-04:00")) \
        == ["2026-08-02"]


def test_days_covered_spans_every_day_touched(timeline):
    days = calendar.days_covered(
        _row("2026-08-03T22:00:00-04:00", "2026-08-05T01:00:00-04:00"))
    assert days == ["2026-08-03", "2026-08-04", "2026-08-05"]


def test_brief_line_names_the_zone(timeline):
    """The label is the safeguard: a bare HH:MM can be re-read in any zone."""
    ev = {"day": "2026-08-04", "summary": "Club call", "location": None,
          "calendar": "Chess Club", "all_day": False, "tz": "ET",
          "start": "2026-08-04T21:30:00-04:00",
          "end": "2026-08-04T23:00:00-04:00"}
    assert calendar.brief_line(ev) == \
        "2026-08-04 21:30-23:00 ET Club call [Chess Club]"


def test_brief_line_falls_back_to_the_day_when_tz_is_absent(timeline):
    """Pins and cached payloads predate the `tz` field; they still get a
    label rather than an unmarked clock time."""
    ev = {"day": "2026-09-04", "summary": "Rush", "location": None,
          "calendar": "Chess Club", "all_day": False,
          "start": "2026-09-04T19:00:00-07:00",
          "end": "2026-09-04T20:00:00-07:00"}
    assert calendar.brief_line(ev).endswith("19:00-20:00 PT Rush [Chess Club]")


def test_console_prompt_states_todays_zone(timeline, monkeypatch):
    """The prompt told the console today's day-key but never which zone the
    user was standing in, so the write path had nothing to anchor to. Any
    placeholder left unfilled reaches the model as literal braces."""
    from app import console_prompt
    monkeypatch.setattr(console_prompt, "day_key", lambda dt=None: "2026-09-04")

    text = console_prompt.render_append()
    # Placeholders, not braces: the quiz/cards format is shown as literal JSON.
    import re
    assert not re.search(r"\{[a-z_]+\}", text)
    assert "2026-09-04" in text
    assert "Today the user is in America/Los_Angeles (PT)." in text
