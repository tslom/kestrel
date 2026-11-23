"""Renaming a calendar in Google, and the cache catching up by itself.

The name used to live only as a copy on each cached event row, so renaming
"Gym/Sports" to "Gym/Workouts" reached the rows inside refresh()'s
[today-7, today+35] window and nothing else. Every colder row kept the old
name — `calendar_ids` listed both against one id — and the only fix was an
UPDATE typed by hand. The `calendars` table is the name's home now, and each
sync drags the copies along with it.
"""

from datetime import date

import pytest

from app import db
from app.providers import calendar as cal
from app.providers import oauth

DAY = "2026-09-22"


def _events_page(*summaries):
    return {"items": [
        {"id": f"ev{i}", "summary": s, "status": "confirmed",
         "description": "bring the reader",
         "start": {"dateTime": f"{DAY}T17:00:00-07:00"},
         "end": {"dateTime": f"{DAY}T18:00:00-07:00"}}
        for i, s in enumerate(summaries)]}


@pytest.fixture
def google(monkeypatch):
    """A fake Calendar API whose calendar list the test can rename."""
    state = {"calendars": [("gym@g", "Gym/Sports"), ("fun@g", "Fun")],
             "events": {"gym@g": _events_page("Lift"), "fun@g": _events_page()}}

    def fake_request(url, token, method="GET", body=None):
        if "calendarList" in url:
            return {"items": [{"id": cid, "summary": name}
                              for cid, name in state["calendars"]]}
        for cid in state["events"]:
            if cid.replace("@", "%40") in url or cid in url:
                return state["events"][cid]
        return {"items": []}

    monkeypatch.setattr(cal, "api_request", fake_request)
    monkeypatch.setattr(oauth, "get_token", lambda *a: "tok")
    monkeypatch.setattr(cal, "GOOGLE_CALENDAR_IDS", None)
    return state


def _sync(day=DAY):
    con = db.connect()
    try:
        cal._sync(con, date.fromisoformat(day), date.fromisoformat(day))
    finally:
        con.close()


def _names():
    with db.session() as con:
        return {r["calendar_id"]: r["name"]
                for r in con.execute("SELECT calendar_id, name FROM calendars")}


def test_a_rename_reaches_rows_the_sync_never_pulled(dbfile, google):
    """The bug, in one test: an event six months out is not in any refresh
    window, so nothing would ever re-fetch the row that holds its calendar's
    name."""
    _sync()
    with db.session() as con, con:
        con.execute(
            "INSERT INTO events (day, event_id, calendar_id, calendar_name,"
            " summary, start_at, end_at, all_day) VALUES"
            " ('2027-03-04','cold','gym@g','Gym/Sports','Lift',"
            "  '2027-03-04T17:00:00-07:00','2027-03-04T18:00:00-07:00',0)")

    google["calendars"] = [("gym@g", "Gym/Workouts"), ("fun@g", "Fun")]
    _sync()

    assert _names()["gym@g"] == "Gym/Workouts"
    with db.session() as con:
        cold = con.execute("SELECT calendar_name FROM events WHERE event_id='cold'"
                           ).fetchone()
        distinct = [r[0] for r in con.execute(
            "SELECT DISTINCT calendar_name FROM events WHERE calendar_id='gym@g'")]
    assert cold["calendar_name"] == "Gym/Workouts"
    assert distinct == ["Gym/Workouts"], "one calendar must not have two names"


def test_the_calendar_list_survives_a_calendar_with_no_events(dbfile, google):
    """'Fun' has nothing cached, so a SELECT DISTINCT over event rows could
    not see it — and an empty calendar is exactly the one you need named
    before you can file anything onto it."""
    _sync()
    assert _names()["fun@g"] == "Fun"


def test_an_unsubscribed_calendar_stops_being_offered(dbfile, google):
    _sync()
    google["calendars"] = [("fun@g", "Fun")]
    _sync()
    assert "gym@g" not in _names()


def test_a_pinned_subset_never_prunes_the_rest(dbfile, google, monkeypatch):
    """With GOOGLE_CALENDAR_IDS set, the pull is a subset by configuration,
    not by unsubscription — pruning to it would empty the table down to the
    pinned few on every sync."""
    _sync()
    monkeypatch.setattr(cal, "GOOGLE_CALENDAR_IDS", ["fun@g"])
    google["calendars"] = [("fun@g", "fun@g")]
    _sync()
    assert "gym@g" in _names()


def test_the_sync_keeps_what_an_event_says(dbfile, google):
    """The description is what tells one 'Lift' from another on an approval
    card, and _normalize used to drop it on the floor."""
    _sync()
    with db.session() as con:
        row = con.execute("SELECT description FROM events WHERE event_id='ev0'"
                          ).fetchone()
    assert row["description"] == "bring the reader"
