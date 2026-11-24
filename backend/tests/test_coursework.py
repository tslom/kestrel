"""providers/coursework — the Brightspace (D2L) calendar feed. _fetch stubbed."""

import pytest

from app.providers import coursework
from app.providers.ranges import parse_range

# Shapes copied from the real feed.
FEED = "\r\n".join([
    "BEGIN:VCALENDAR",
    "BEGIN:VEVENT",
    "SUMMARY:WP2: Empowerment or Disempowerment? - Due",
    "LOCATION:20263_64775 WRIT-150: Writing and Critical Reasoning--Thematic",
    "  Approaches",
    "DESCRIPTION:Summary of what to submit:\\n\\nPDF of finished paper\\n\\nAssi",
    "\tgnments:\\nWP2 - https://brightspace.usc.edu/d2l/lms/dropbox/user/folder",
    "\t_submit_files.d2l?ou=300399&db=636918\\n\\nView event - https://brightspa",
    "\tce.usc.edu/d2l/le/calendar/300399/event/990366/detailsview",
    "DTSTART:20261007T210000Z",
    "UID:1234-567890@brightspace.usc.edu",
    "END:VEVENT",
    "BEGIN:VEVENT",
    "SUMMARY:Grand Challenge Team: Project Pitches",
    "LOCATION:20263_28561 ENGR-102: Viterbi First-Year Academy",
    "DESCRIPTION:<p>Submit a 3-5 minute video&nbsp;on your pitch.</p>",
    "DTSTART:20261015T065959Z",
    "UID:1-2@brightspace.usc.edu",
    "END:VEVENT",
    "BEGIN:VEVENT",
    "SUMMARY:170HW5sol - Available",
    "LOCATION:20263_29953 CSCI-170: Discrete Methods in Computer Science",
    "DTSTART:20261015T130000Z",
    "UID:3-4@brightspace.usc.edu",
    "END:VEVENT",
    "END:VCALENDAR", ""])


@pytest.fixture
def feed(dbfile, monkeypatch):
    monkeypatch.setattr(coursework, "BRIGHTSPACE_ICS_URL", "https://x/feed.ics")
    monkeypatch.setattr(coursework, "_fetch", lambda: FEED)


def _rows(token):
    return {r["uid"]: r for r in coursework.get(parse_range(token))["data"]}


def test_a_due_item_carries_course_deadline_and_its_dropbox(feed):
    r = _rows("2026-10-07")["1234-567890@brightspace.usc.edu"]
    assert (r["kind"], r["title"]) == ("due", "WP2: Empowerment or Disempowerment?")
    assert (r["course_code"], r["section"]) == ("WRIT-150", "64775")
    assert r["course"].endswith("Thematic Approaches")       # folded line rejoined
    assert r["start"] == "2026-10-07T14:00"                   # 21:00Z = 2pm PDT
    assert "/dropbox/" in r["url"] and "View event" not in r["details"]
    assert coursework.brief_line(r) == (
        "2026-10-07 Wed 2pm WRIT-150 DUE WP2: Empowerment or Disempowerment?")


def test_a_late_night_utc_deadline_lands_on_its_pacific_day(feed):
    """06:59Z on the 15th is 11:59pm on the 14th (a day early otherwise)."""
    r = _rows("2026-10-14")["1-2@brightspace.usc.edu"]
    assert r["start"] == "2026-10-14T23:59" and r["kind"] == "event"
    assert r["details"] == "Submit a 3-5 minute video on your pitch."


def test_release_dates_are_not_deadlines(feed):
    r = _rows("2026-10-15")["3-4@brightspace.usc.edu"]
    assert (r["kind"], r["title"]) == ("available", "170HW5sol")
    assert " opens 170HW5sol" in coursework.brief_line(r)


def test_unset_feed_answers_empty_with_a_note(dbfile, monkeypatch):
    monkeypatch.setattr(coursework, "BRIGHTSPACE_ICS_URL", "")
    monkeypatch.setattr(coursework, "_fetch",
                        lambda: pytest.fail("fetched with no URL"))
    out = coursework.get(parse_range("this-week"))
    assert out["data"] == [] and "not set" in out["note"]
