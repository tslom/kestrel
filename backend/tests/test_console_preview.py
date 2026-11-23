"""What the approval card shows (app/console_preview.py).

The card this replaces rendered the tool's raw input, so "delete this event?"
was answered against a 64-hex calendar id and an opaque event id — nothing a
person could recognise, and no way to tell the 9am from the duplicate 9am. The
tests here are about that gap: the ids resolve to the thing they name, the
resolution matches what the tool will actually address, and a lookup that
fails says so instead of rendering a confident blank.
"""

from zoneinfo import ZoneInfo

import pytest

from app import console_preview as pv
from app import db
from app import tz as tzbase


@pytest.fixture(autouse=True)
def pinned_zone(monkeypatch):
    """Pin the zone the card renders in. Without it these assertions test the
    developer's own TIMELINE — the same reading that makes a bare clock time
    on a card worth distrusting in the first place."""
    monkeypatch.setattr(tzbase, "zone_on", lambda day: ZoneInfo("America/Los_Angeles"))
    monkeypatch.setattr(tzbase, "label", lambda day: "PT")


def _event(event_id="ev1", day="2026-09-22", summary="Lift",
           calendar_id="gym@g", calendar_name="Gym/Workouts",
           start="2026-09-22T17:00:00-07:00", end="2026-09-22T18:00:00-07:00",
           all_day=0, location="Lyon Center", description=None):
    with db.session() as con, con:
        con.execute(
            "INSERT INTO events (day, event_id, calendar_id, calendar_name,"
            " summary, start_at, end_at, all_day, location, description)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (day, event_id, calendar_id, calendar_name, summary, start, end,
             all_day, location, description))


def one(tool, **args):
    return pv.describe(tool, [args])[0]


# ---------- the event a delete is about ----------

def test_a_delete_card_names_the_event_not_the_id(dbfile):
    _event(description="bring the reader")
    card = one("mcp__vault__event_delete", event_id="ev1", calendar_id="gym@g")
    assert card["title"] == "Lift"
    assert "5:00 PM" in card["when"] and "Sep 22" in card["when"]
    assert ["calendar", "Gym/Workouts"] in card["rows"]
    assert ["where", "Lyon Center"] in card["rows"]
    assert card["note"] == "bring the reader"


def test_an_all_day_event_says_all_day_rather_than_midnight(dbfile):
    _event(all_day=1, start="2026-09-22", end="2026-09-23", summary="Fall Finals")
    card = one("mcp__vault__event_delete", event_id="ev1")
    assert card["when"] == "Tue Sep 22 · all day"


def test_the_whole_series_is_the_fact_that_changes_the_answer(dbfile):
    _event()
    card = one("mcp__vault__event_delete", event_id="ev1", scope="all")
    assert "EVERY occurrence" in card["warn"]
    assert one("mcp__vault__event_delete", event_id="ev1")["warn"] is None


def test_notifying_guests_is_on_the_card(dbfile):
    _event()
    card = one("mcp__vault__event_delete", event_id="ev1", notify=True)
    assert "guests are emailed" in card["warn"]


def test_a_match_resolves_the_same_way_the_tool_will(dbfile):
    _event(summary="Dentist")
    assert one("mcp__vault__event_delete", match="dent")["title"] == "Dentist"


def test_an_ambiguous_match_is_reported_not_guessed(dbfile):
    """routes/calendar.find_event refuses this call. A card that picked one of
    the two would describe an event nothing was about to happen to."""
    _event(event_id="a", summary="Standup", day="2026-09-22")
    _event(event_id="b", summary="Standup", day="2026-09-23",
           start="2026-09-23T17:00:00-07:00", end="2026-09-23T18:00:00-07:00")
    card = one("mcp__vault__event_delete", match="Standup")
    assert "matches 2 events" in card["unresolved"]
    assert card.get("when") is None


def test_a_wildcard_in_the_match_is_literal_on_card_and_route(dbfile):
    """`_` and `%` used to be LIKE wildcards in both title lookups: "Q_3" also
    matched "Q13 review", so the route refused an unambiguous call as a
    two-way match — and the card agreed, because it ran the same bad query."""
    from app.routes import calendar as calendar_routes
    _event(event_id="a", summary="Q_3 sync", day="2026-09-22")
    _event(event_id="b", summary="Q13 review", day="2026-09-23",
           start="2026-09-23T17:00:00-07:00", end="2026-09-23T18:00:00-07:00")
    assert one("mcp__vault__event_delete", match="Q_3")["title"] == "Q_3 sync"
    assert calendar_routes.find_event("Q_3")["event_id"] == "a"
    with pytest.raises(calendar_routes.HTTPException):
        calendar_routes.find_event("100%")


def test_a_date_settles_the_ambiguity(dbfile):
    _event(event_id="a", summary="Standup", day="2026-09-22")
    _event(event_id="b", summary="Standup", day="2026-09-23",
           start="2026-09-23T17:00:00-07:00", end="2026-09-23T18:00:00-07:00")
    card = one("mcp__vault__event_delete", match="Standup", date="2026-09-23")
    assert card["title"] == "Standup" and "Sep 23" in card["when"]


def test_an_id_outside_the_cache_says_so(dbfile):
    """The card is thin here for a reason, and the reason has to be on it —
    otherwise a delete with no detail looks like a delete of nothing."""
    card = one("mcp__vault__event_delete", event_id="nope", calendar_id="gym@g")
    assert "cached window" in card["unresolved"]


def test_an_update_shows_the_event_and_what_it_becomes(dbfile):
    _event()
    card = one("mcp__vault__event_update", event_id="ev1", start="18:30")
    assert card["title"] == "Lift"
    assert ["new time", "18:30"] in card["rows"]


def test_a_create_card_reads_its_calendar_by_name(dbfile):
    with db.session() as con, con:
        con.execute("INSERT INTO calendars (calendar_id, name, refreshed_at)"
                    " VALUES ('gym@g','Gym/Workouts','now')")
    card = one("mcp__vault__event_create", summary="Lift",
               start="2026-09-23T18:00", end="19:30", calendar_id="gym@g")
    assert ["calendar", "Gym/Workouts"] in card["rows"]
    assert card["when"] == "Wed Sep 23 · 6:00 PM–7:30 PM"


def test_an_all_day_span_is_two_dates_not_a_midnight(dbfile):
    card = one("mcp__vault__event_create", summary="Trip",
               start="2026-09-23", end="2026-09-27")
    assert card["when"] == "Wed Sep 23 – Sun Sep 27"


def test_the_connector_tools_resolve_through_the_same_cache(dbfile):
    """The claude.ai Calendar connector names the same events by the same
    ids, and its delete is just as final."""
    _event()
    card = one("mcp__claude_ai_Google_Calendar__delete_event", event_id="ev1")
    assert card["title"] == "Lift"


# ---------- mail ----------

def _email(msg_id="m1", thread_id="t1", subject="Rent due", day="2026-09-20"):
    with db.session() as con, con:
        con.execute(
            "INSERT INTO emails (msg_id, thread_id, day, received_at, from_name,"
            " from_addr, subject, refreshed_at) VALUES (?,?,?,?,?,?,?,?)",
            (msg_id, thread_id, day, f"{day}T09:00:00", "Landlord",
             "l@x.com", subject, "now"))


def test_trashing_a_thread_shows_the_subject(dbfile):
    _email()
    card = one("mcp__claude_ai_Gmail__trash_thread", thread_id="t1")
    assert card["title"] == "Rent due"
    assert ["from", "Landlord"] in card["rows"]


def test_a_thread_says_how_many_messages_go_with_it(dbfile):
    _email(msg_id="m1")
    _email(msg_id="m2", day="2026-09-21")
    card = one("mcp__claude_ai_Gmail__trash_thread", thread_id="t1")
    assert ["messages", "2 in the thread"] in card["rows"]


def test_sending_shows_the_recipient_and_that_it_leaves(dbfile):
    card = one("mcp__claude_ai_Gmail__send_message", to="a@b.com",
               subject="Re: Rent", body="Paid on the 1st.")
    assert card["title"] == "Re: Rent"
    assert ["to", "a@b.com"] in card["rows"]
    assert card["note"] == "Paid on the 1st."
    assert "leaves for the recipient" in card["warn"]


# ---------- shell and everything else ----------

def test_a_command_is_shown_whole(dbfile):
    card = one("Bash", command="rm -rf /Users/x/vault/06-Generated/note.md")
    assert card["mono"] and "06-Generated/note.md" in card["title"]
    assert ["deletes", "note.md"] in card["rows"]


def test_a_command_leads_with_its_description(dbfile):
    long = "\n".join(f"echo {i}" for i in range(200))
    card = one("Bash", command=long, description="Print two hundred lines")
    assert card["lead"] == "Print two hundred lines"
    # the card folds a long command now, so it isn't cut at 600 chars
    assert card["title"] == long
    assert one("Bash", command="ls")["lead"] is None


def test_tool_desc_is_the_calls_own_sentence():
    from app import sessions
    assert sessions.tool_desc({"command": "ls", "description": "  List\n files "}) == "List files"
    assert sessions.tool_desc({"command": "ls"}) == ""
    assert sessions.tool_desc("nope") == ""
    assert sessions.tool_desc({"description": "x" * 300}).endswith("…")


def test_an_unknown_tool_still_reads_as_rows(dbfile):
    card = one("mcp__some_server__do_thing", who="Ada", count=3, event_id="x")
    assert ["who", "Ada"] in card["rows"]
    assert ["count", "3"] in card["rows"]
    # The id is what the rest of the card exists to translate; untranslated,
    # it is noise on a line of its own.
    assert not any(r[0] == "event id" for r in card["rows"])


def test_a_long_body_becomes_the_note_rather_than_a_line(dbfile):
    card = one("mcp__some_server__do_thing", body="x" * 900)
    assert card["note"] and len(card["note"]) <= pv.MAX_NOTE + 1
    assert card["rows"] == []


def test_a_resolver_that_trips_costs_the_card_its_detail_and_no_more(dbfile,
                                                                     monkeypatch):
    """This runs inside can_use_tool, between the model asking and the card
    appearing. An exception here would take the approval path with it."""
    def boom(*a, **k):
        raise RuntimeError("cache on fire")

    monkeypatch.setattr(pv, "_event_for", boom)
    card = one("mcp__vault__event_delete", event_id="ev1")
    assert card["rows"] == [] or isinstance(card["rows"], list)


def test_every_call_in_a_burst_gets_its_own_preview(dbfile):
    _event(event_id="a", summary="Lift")
    _event(event_id="b", summary="Swim", day="2026-09-23",
           start="2026-09-23T17:00:00-07:00", end="2026-09-23T18:00:00-07:00")
    cards = pv.describe("mcp__vault__event_delete",
                        [{"event_id": "a"}, {"event_id": "b"}])
    assert [c["title"] for c in cards] == ["Lift", "Swim"]
