"""providers/athletics — USC's Sidearm iCal feed. _fetch is stubbed."""

import urllib.error

import pytest

from app import db
from app.providers import athletics, get as provider_get
from app.providers.ranges import parse_range

# Shapes copied from the real feed (2026-10-13), including a folded line.
FEED = "\r\n".join([
    "BEGIN:VCALENDAR",
    "BEGIN:VEVENT",
    "UID:vcal_1-admin.usctrojans.com",
    "DTSTART:20261017T200000Z",
    "DTEND:20261017T220000Z",
    "LOCATION:Los Angeles\\, Calif. \\, Uytengsu Aquatics Center",
    "SUMMARY:USC Trojans Men's Water Polo vs UCLA - Senior Day",
    "DESCRIPTION:USC Trojans Men's Water Polo vs UCLA - Senior Day\\nTV: Overn",
    " ght\\n",
    "URL:https://admin.usctrojans.com/calendar.aspx?game_id=1&amp;sport_id=9",
    "END:VEVENT",
    "BEGIN:VEVENT",
    "UID:vcal_2-admin.usctrojans.com",
    "DTSTART;VALUE=DATE:20261114",
    "LOCATION:Bloomington\\, Ind.",
    "SUMMARY:USC Trojans Football at Indiana",
    "DESCRIPTION:USC Trojans Football at Indiana\\nTV: TBD\\n",
    "END:VEVENT",
    "BEGIN:VEVENT",
    "UID:vcal_3-admin.usctrojans.com",
    "DTSTART:20260913T200000Z",
    "LOCATION:Los Angeles\\, Calif.\\, Rawlinson Stadium",
    "SUMMARY:[L] USC Trojans Women's Soccer vs Purdue  - Kids Day",
    "DESCRIPTION:[L] USC Trojans Women's Soccer vs Purdue\\nL 0-2\\nTV: B1G+\\n",
    "END:VEVENT",
    "BEGIN:VEVENT",
    "UID:vcal_4-admin.usctrojans.com",
    "DTSTART:20261017T154000Z",
    "LOCATION:Riverside\\, Calif.\\, UCR Ag Ops Course",
    "SUMMARY:USC Trojans Women's Cross Country vs Highlander Invitational",
    "END:VEVENT",
    "BEGIN:VEVENT",
    "UID:vcal_5-admin.usctrojans.com",
    "DTSTART;VALUE=DATE:20261016",
    "LOCATION:Stanford\\, Calif.",
    "SUMMARY:[N] USC Trojans Women's Golf  Stanford Intercollegiate",
    "END:VEVENT",
    "END:VCALENDAR", ""])


@pytest.fixture
def feed(dbfile, monkeypatch):
    calls = []
    monkeypatch.setattr(athletics, "_fetch", lambda: calls.append(1) or FEED)
    return calls


def _rows(token):
    return {r["uid"]: r for r in athletics.get(parse_range(token))["data"]}


def test_the_water_polo_game_the_console_could_not_find(feed):
    """10-12: 'I couldn't find the 2026 schedule online.' It was in the feed."""
    r = _rows("2026-10-17")["vcal_1-admin.usctrojans.com"]
    assert r["start"] == "2026-10-17T13:00" and not r["time_tba"]   # 20:00Z = 1pm PDT
    assert (r["sport"], r["opponent"], r["side"], r["note"]) == (
        "Men's Water Polo", "UCLA", "home", "Senior Day")
    assert r["location"] == "Los Angeles, Calif., Uytengsu Aquatics Center"
    assert r["tv"] == "Overnght"     # the folded line was rejoined (typo is theirs)
    assert "&amp;" not in r["url"]
    assert athletics.brief_line(r).startswith(
        "2026-10-17 Sat 1pm PT Men's Water Polo vs UCLA (home) @ Los Angeles")


def test_date_only_kickoff_is_tba_not_midnight(feed):
    r = _rows("2026-11-14")["vcal_2-admin.usctrojans.com"]
    assert r["start"] is None and r["time_tba"] and r["side"] == "away"
    assert "time TBA Football at Indiana (away)" in athletics.brief_line(r)


def test_played_game_carries_its_score(feed):
    r = _rows("2026-09-13")["vcal_3-admin.usctrojans.com"]
    assert r["result"] == "L 0-2" and r["opponent"] == "Purdue"


def test_vs_away_from_los_angeles_is_neutral_not_home(feed):
    rows = _rows("2026-10-16..2026-10-17")
    assert rows["vcal_4-admin.usctrojans.com"]["side"] == "neutral"
    golf = rows["vcal_5-admin.usctrojans.com"]
    assert (golf["sport"], golf["opponent"], golf["side"], golf["note"]) == (
        "Women's Golf", None, None, "Stanford Intercollegiate")


def test_cache_is_reused_within_ttl_and_replaced_after(feed, monkeypatch):
    athletics.get(parse_range("this-week"))
    athletics.get(parse_range("this-week"))
    assert len(feed) == 1
    with db.session() as con, con:
        con.execute("UPDATE athletics_events SET fetched_at='2000-01-01T00:00:00'")
    athletics.get(parse_range("this-week"))
    assert len(feed) == 2


def test_an_empty_feed_keeps_the_last_schedule(feed, monkeypatch):
    athletics.refresh()
    monkeypatch.setattr(athletics, "_fetch", lambda: "BEGIN:VCALENDAR\r\nEND:VCALENDAR")
    with pytest.raises(RuntimeError, match="no events"):
        athletics.refresh(force=True)
    assert len(_rows("2026-09-01..2026-12-01")) == 5


def test_offline_serves_the_cache_marked_stale(feed, monkeypatch):
    athletics.refresh()
    with db.session() as con, con:
        con.execute("UPDATE athletics_events SET fetched_at='2000-01-01T00:00:00'")

    def down():
        raise urllib.error.URLError("nodename nor servname provided")
    monkeypatch.setattr(athletics, "_fetch", down)
    out = provider_get("athletics", "2026-10-17", query="polo")
    assert out["stale"] and len(out["data"]) == 1
