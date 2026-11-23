"""Writing to Google Calendar (app/routes/calendar.py + the console's
event_create / event_update / event_delete / event_rsvp).

The API call itself is one request; what's worth testing is everything decided
before it. Three of those decisions can silently corrupt a calendar rather than
fail: an all-day end is inclusive on the way in and exclusive on the way out,
a clock time belongs to the zone the user is standing in on the event's date
(never this machine's), and a PATCH that moves only one endpoint turns an hour
meeting into a day-long one. The fourth is that delete is a delete — it must
never reach the calendar as a blanked-out or rewritten event.
"""

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from fastapi import HTTPException

from app import db, tz
from app.providers import calendar as calendar_provider
from app.providers import oauth
from app.routes import calendar as calendar_routes

EVENT = {
    "id": "ev1",
    "summary": "Dentist",
    "start": {"dateTime": "2026-08-20T15:00:00-04:00",
              "timeZone": "America/New_York"},
    "end": {"dateTime": "2026-08-20T16:00:00-04:00",
            "timeZone": "America/New_York"},
    "htmlLink": "https://calendar.google.com/event?eid=ev1",
}

SCOPES = ("https://www.googleapis.com/auth/calendar.readonly"
          " https://www.googleapis.com/auth/calendar.events")


@pytest.fixture
def gcal(monkeypatch):
    """Stand in for Google: record calls, serve EVENT, echo writes back."""
    calls = []

    def fake_request(url, token, method="GET", body=None):
        calls.append({"url": url, "method": method, "body": body})
        if method == "DELETE":
            return {}
        if method in ("POST", "PATCH"):
            return {**EVENT, **(body or {})}
        return {**EVENT}

    monkeypatch.setattr(calendar_provider, "api_request", fake_request)
    monkeypatch.setattr(oauth, "status", lambda *a: {"scopes": SCOPES})
    monkeypatch.setattr(oauth, "get_token", lambda *a: "tok")
    monkeypatch.setattr(calendar_provider, "sync_days", lambda days: len(list(days)))
    monkeypatch.setattr(tz, "zone_on", lambda day: ZoneInfo("America/New_York"))
    return calls


def written(calls, method):
    return [c for c in calls if c["method"] == method]


def body_of(calls, method):
    return written(calls, method)[-1]["body"]


# ---------- creating ----------

def test_a_timed_event_without_an_end_runs_an_hour(gcal):
    calendar_routes.create_event(summary="Coffee", start="2026-08-20T09:00")

    sent = body_of(gcal, "POST")
    assert sent["start"]["dateTime"] == "2026-08-20T09:00:00"
    assert sent["end"]["dateTime"] == "2026-08-20T10:00:00"


def test_an_end_may_be_a_bare_clock_time(gcal):
    calendar_routes.create_event(summary="Coffee", start="2026-08-20T09:00",
                                 end="09:45")

    assert body_of(gcal, "POST")["end"]["dateTime"] == "2026-08-20T09:45:00"


def test_an_all_day_end_is_inclusive_going_in_and_exclusive_going_out(gcal):
    """"the 20th through the 22nd" is three days. Google stores the 23rd; get
    this backwards and the last day of every trip disappears."""
    calendar_routes.create_event(summary="Move-in", start="2026-08-20",
                                 end="2026-08-22")

    sent = body_of(gcal, "POST")
    assert sent["start"] == {"date": "2026-08-20"}
    assert sent["end"] == {"date": "2026-08-23"}


def test_a_one_day_all_day_event_ends_the_next_day(gcal):
    calendar_routes.create_event(summary="Holiday", start="2026-08-20")

    assert body_of(gcal, "POST")["end"] == {"date": "2026-08-21"}


def test_the_zone_is_where_the_user_is_that_day(gcal, monkeypatch):
    """The rule the console kept breaking: a bare clock time is in the user's
    zone on the event's date, not this machine's and not the zone the
    conversation has been discussing."""
    monkeypatch.setattr(tz, "zone_on", lambda day: ZoneInfo("America/Los_Angeles"))

    calendar_routes.create_event(summary="Lunch", start="2026-08-20T12:30")

    assert body_of(gcal, "POST")["start"]["timeZone"] == "America/Los_Angeles"


def test_a_named_zone_wins_over_the_lookup(gcal):
    calendar_routes.create_event(summary="Call", start="2026-08-20T12:30",
                                 time_zone="Europe/Berlin")

    assert body_of(gcal, "POST")["start"]["timeZone"] == "Europe/Berlin"


def test_an_unknown_zone_is_refused_before_google_is_touched(gcal):
    with pytest.raises(HTTPException) as e:
        calendar_routes.create_event(summary="Call", start="2026-08-20T12:30",
                                     time_zone="Eastern")
    assert e.value.status_code == 400
    assert not gcal


def test_an_event_that_ends_before_it_starts_is_refused(gcal):
    with pytest.raises(HTTPException) as e:
        calendar_routes.create_event(summary="Call", start="2026-08-20T12:30",
                                     end="11:00")
    assert e.value.status_code == 400
    assert not gcal


def test_a_date_start_with_a_timed_end_is_refused(gcal):
    with pytest.raises(HTTPException) as e:
        calendar_routes.create_event(summary="Trip", start="2026-08-20",
                                     end="2026-08-22T17:00")
    assert e.value.status_code == 400
    assert not gcal


def test_the_calendar_id_goes_into_the_url_verbatim(gcal):
    cal = "abc123@group.calendar.google.com"
    calendar_routes.create_event(summary="Lecture", start="2026-08-20T09:00",
                                 calendar_id=cal)

    assert "abc123%40group.calendar.google.com/events" in gcal[-1]["url"]


def test_guests_are_not_emailed_unless_asked(gcal):
    calendar_routes.create_event(summary="Sync", start="2026-08-20T09:00",
                                 attendees=["a@b.co"])
    assert "sendUpdates=none" in gcal[-1]["url"]

    calendar_routes.create_event(summary="Sync", start="2026-08-20T09:00",
                                 attendees=["a@b.co"], notify=True)
    assert "sendUpdates=all" in gcal[-1]["url"]


# ---------- moving ----------

def test_moving_only_the_start_keeps_the_events_length(gcal):
    """"push the 3pm to 4" — the event was an hour and stays an hour."""
    calendar_routes.update_event(event_id="ev1", calendar_id="primary",
                                 start="2026-08-20T16:00")

    sent = body_of(gcal, "PATCH")
    assert sent["start"]["dateTime"] == "2026-08-20T16:00:00"
    assert sent["end"]["dateTime"] == "2026-08-20T17:00:00"


def test_a_move_always_sends_both_endpoints(gcal):
    """Google validates start against end; half a change is how an hour
    meeting becomes a 23-hour one."""
    calendar_routes.update_event(event_id="ev1", calendar_id="primary",
                                 end="17:30")

    sent = body_of(gcal, "PATCH")
    assert set(sent) == {"start", "end"}
    assert sent["start"]["dateTime"].startswith("2026-08-20T15:00")
    assert sent["end"]["dateTime"].startswith("2026-08-20T17:30:00")


def test_renaming_leaves_the_time_alone(gcal):
    calendar_routes.update_event(event_id="ev1", calendar_id="primary",
                                 summary="Dentist (rescheduled)")

    sent = body_of(gcal, "PATCH")
    assert sent == {"summary": "Dentist (rescheduled)"}


def test_a_location_can_be_cleared_but_an_absent_one_is_not_a_change(gcal):
    calendar_routes.update_event(event_id="ev1", calendar_id="primary",
                                 location="")
    assert body_of(gcal, "PATCH") == {"location": ""}


def test_an_update_that_changes_nothing_is_refused(gcal):
    with pytest.raises(HTTPException) as e:
        calendar_routes.update_event(event_id="ev1", calendar_id="primary")
    assert e.value.status_code == 400
    assert not written(gcal, "PATCH")


def test_switching_a_timed_event_to_all_day_needs_both_ends(gcal):
    with pytest.raises(HTTPException) as e:
        calendar_routes.update_event(event_id="ev1", calendar_id="primary",
                                     start="2026-08-21")
    assert e.value.status_code == 400
    assert not written(gcal, "PATCH")


def _google_patch(stored: dict, body: dict) -> dict:
    """PATCH as Google does it, which the gcal fixture's shallow echo is not:
    nested objects merge into what's stored, and a null deletes a field."""
    out = dict(stored)
    for k, v in body.items():
        if v is None:
            out.pop(k, None)
        elif isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _google_patch(out[k], v)
        else:
            out[k] = v
    return out


@pytest.fixture
def merging_gcal(gcal, monkeypatch):
    """gcal with Google's merge, and its refusal of an endpoint that is both
    a date and a dateTime. Seed `stored["event"]` before writing."""
    stored = {"event": dict(EVENT)}

    def fake_request(url, token, method="GET", body=None):
        gcal.append({"url": url, "method": method, "body": body})
        if method == "PATCH":
            merged = _google_patch(stored["event"], body or {})
            for end in ("start", "end"):
                if ("date" in merged[end]) == ("dateTime" in merged[end]):
                    raise RuntimeError('calendar API 400: "Invalid start time."')
            stored["event"] = merged
        return dict(stored["event"])

    monkeypatch.setattr(calendar_provider, "api_request", fake_request)
    return stored


def test_an_all_day_event_can_be_given_a_clock_time(merging_gcal):
    """An all-day event given a 17:00-23:59 time must not 400 ("Invalid start
    time": a PATCH that merges dateTime in beside the stored date), or the
    session deletes and re-creates the event instead."""
    merging_gcal["event"] = {**EVENT, "start": {"date": "2026-11-28"},
                             "end": {"date": "2026-11-29"}}
    out = calendar_routes.update_event(event_id="ev1", calendar_id="primary",
                                       start="2026-11-28T17:00", end="23:59")
    stored = merging_gcal["event"]
    assert stored["start"] == {"dateTime": "2026-11-28T17:00:00",
                               "timeZone": "America/New_York"}
    assert "date" not in stored["end"] and not out["all_day"]


def test_a_timed_event_can_become_all_day_with_both_ends(merging_gcal):
    calendar_routes.update_event(event_id="ev1", calendar_id="primary",
                                 start="2026-08-21", end="2026-08-22")
    stored = merging_gcal["event"]
    assert stored["start"] == {"date": "2026-08-21"}
    assert stored["end"] == {"date": "2026-08-23"}


def test_a_move_within_its_kind_sends_no_nulls(gcal):
    calendar_routes.update_event(event_id="ev1", calendar_id="primary",
                                 start="2026-08-20T16:00", end="17:30")
    sent = body_of(gcal, "PATCH")
    assert None not in sent["start"].values() and "date" not in sent["start"]


def test_an_update_reports_what_the_time_used_to_be(gcal):
    out = calendar_routes.update_event(event_id="ev1", calendar_id="primary",
                                       start="2026-08-20T16:00")
    assert out["was"]["start"] == "2026-08-20T15:00:00-04:00"


# ---------- deleting ----------

def test_deleting_issues_a_delete_and_never_rewrites_the_event(gcal):
    out = calendar_routes.delete_event(event_id="ev1", calendar_id="primary")

    assert [c["method"] for c in gcal] == ["GET", "DELETE"]
    assert not written(gcal, "PATCH") and not written(gcal, "POST")
    # Read first, so the answer can name what went.
    assert out["summary"] == "Dentist" and out["deleted"] is True


def test_deleting_does_not_cancel_on_the_guests_unless_asked(gcal):
    calendar_routes.delete_event(event_id="ev1", calendar_id="primary")
    assert "sendUpdates=none" in written(gcal, "DELETE")[-1]["url"]

    calendar_routes.delete_event(event_id="ev1", calendar_id="primary",
                                 notify=True)
    assert "sendUpdates=all" in written(gcal, "DELETE")[-1]["url"]


def test_an_already_deleted_event_says_so(gcal, monkeypatch):
    def gone(url, token, method="GET", body=None):
        raise RuntimeError("calendar API 410: deleted")

    monkeypatch.setattr(calendar_provider, "api_request", gone)
    with pytest.raises(HTTPException) as e:
        calendar_routes.delete_event(event_id="ev1", calendar_id="primary")
    assert e.value.status_code == 410


# ---------- RSVP ----------

INVITE = {**EVENT, "attendees": [
    {"email": "boss@work.com", "responseStatus": "accepted", "organizer": True},
    {"email": "me@school.edu", "responseStatus": "needsAction", "self": True},
]}


@pytest.fixture
def invite(gcal, monkeypatch):
    def fake_request(url, token, method="GET", body=None):
        gcal.append({"url": url, "method": method, "body": body})
        if method == "PATCH":
            return {**INVITE, **(body or {})}
        return {k: v for k, v in INVITE.items()}

    monkeypatch.setattr(calendar_provider, "api_request", fake_request)
    return gcal


def test_an_rsvp_answers_for_the_user_and_keeps_the_guest_list(invite):
    """`attendees` is replaced wholesale — sending only our own entry
    uninvites everyone else on the event."""
    out = calendar_routes.respond_event(event_id="ev1", calendar_id="primary",
                                        response="declined")

    sent = body_of(invite, "PATCH")["attendees"]
    assert [a["email"] for a in sent] == ["boss@work.com", "me@school.edu"]
    assert sent[1]["responseStatus"] == "declined"
    assert sent[0]["responseStatus"] == "accepted"
    assert out["response"] == "declined"


def test_an_rsvp_can_carry_a_note(invite):
    calendar_routes.respond_event(event_id="ev1", calendar_id="primary",
                                  response="tentative", comment="may be late")
    assert body_of(invite, "PATCH")["attendees"][1]["comment"] == "may be late"


def test_an_event_with_no_invitation_cannot_be_rsvped(gcal):
    with pytest.raises(HTTPException) as e:
        calendar_routes.respond_event(event_id="ev1", calendar_id="primary",
                                      response="accepted")
    assert e.value.status_code == 400
    assert not written(gcal, "PATCH")


def test_a_nonsense_response_is_refused(gcal):
    with pytest.raises(HTTPException) as e:
        calendar_routes.respond_event(event_id="ev1", calendar_id="primary",
                                      response="maybe?")
    assert e.value.status_code == 400
    assert not gcal


# ---------- addressing an event without an id ----------

def seed(rows):
    con = db.connect()
    with con:
        for r in rows:
            con.execute(
                "INSERT INTO events (day, event_id, calendar_id, calendar_name,"
                " summary, start_at, end_at, all_day, location)"
                " VALUES (?,?,?,?,?,?,?,0,NULL)", r)
    con.close()


def test_an_event_can_be_named_instead_of_id_typed(dbfile, gcal):
    seed([("2026-08-20", "ev1", "cal-a@group.calendar.google.com", "Personal",
           "Dentist", "2026-08-20T15:00:00-04:00", "2026-08-20T16:00:00-04:00")])

    calendar_routes.delete_event(match="dentist")

    # The calendar id came from the cache, not from a model retyping it.
    assert "cal-a%40group.calendar.google.com" in gcal[-1]["url"]


def test_an_ambiguous_match_is_refused_with_the_candidates(dbfile, gcal):
    seed([("2026-08-20", "ev1", "cal-a", "Personal", "Standup",
           "2026-08-20T09:00:00-04:00", "2026-08-20T09:15:00-04:00"),
          ("2026-08-21", "ev2", "cal-a", "Personal", "Standup",
           "2026-08-21T09:00:00-04:00", "2026-08-21T09:15:00-04:00")])

    with pytest.raises(HTTPException) as e:
        calendar_routes.delete_event(match="standup")
    assert e.value.status_code == 409
    assert "ev1" in e.value.detail and "ev2" in e.value.detail
    assert not gcal

    # ...and a date settles it.
    calendar_routes.delete_event(match="standup", day="2026-08-21")
    assert written(gcal, "DELETE")


def test_a_match_with_no_hit_is_a_404_not_a_guess(dbfile, gcal):
    seed([("2026-08-20", "ev1", "cal-a", "Personal", "Dentist",
           "2026-08-20T15:00:00-04:00", "2026-08-20T16:00:00-04:00")])

    with pytest.raises(HTTPException) as e:
        calendar_routes.update_event(match="orthodontist", start="09:00")
    assert e.value.status_code == 404
    assert not gcal


def test_an_uncached_id_without_a_calendar_says_which_argument_is_missing(
        dbfile, gcal):
    with pytest.raises(HTTPException) as e:
        calendar_routes.delete_event(event_id="unknown")
    assert e.value.status_code == 400
    assert "calendar_id" in e.value.detail
    assert not gcal


# ---------- the token ----------

def test_a_read_only_token_is_caught_before_the_write(monkeypatch, gcal):
    """Google's own answer is a 403 body about insufficientPermissions, which
    reads like a bug in the request rather than a consent that was never
    given."""
    monkeypatch.setattr(oauth, "status", lambda *a: {
        "scopes": "https://www.googleapis.com/auth/calendar.readonly"})

    with pytest.raises(HTTPException) as e:
        calendar_routes.create_event(summary="Coffee", start="2026-08-20T09:00")
    assert e.value.status_code == 403
    assert "authorize" in e.value.detail
    assert not gcal


def test_googles_own_permission_refusal_is_translated(monkeypatch, gcal):
    def refused(url, token, method="GET", body=None):
        raise RuntimeError('calendar API 403: {"reason": "insufficientPermissions"}')

    monkeypatch.setattr(calendar_provider, "api_request", refused)
    with pytest.raises(HTTPException) as e:
        calendar_routes.create_event(summary="Coffee", start="2026-08-20T09:00")
    assert e.value.status_code == 403
    assert "authorize" in e.value.detail


def test_a_missing_token_asks_for_a_connect(monkeypatch, gcal):
    monkeypatch.setattr(oauth, "status", lambda *a: {})

    def unauthorized(*a):
        raise oauth.NotAuthorized("no tokens for google/school")

    monkeypatch.setattr(oauth, "get_token", unauthorized)
    with pytest.raises(HTTPException) as e:
        calendar_routes.create_event(summary="Coffee", start="2026-08-20T09:00")
    assert e.value.status_code == 400
    assert not gcal


# ---------- the cache ----------

def test_a_move_re_pulls_both_the_old_and_the_new_day(gcal, monkeypatch):
    pulled = []
    monkeypatch.setattr(calendar_provider, "sync_days",
                        lambda days: pulled.extend(days))

    calendar_routes.update_event(event_id="ev1", calendar_id="primary",
                                 start="2026-09-05T16:00")

    assert "2026-08-20" in pulled and "2026-09-05" in pulled


def test_a_failed_re_pull_does_not_fail_the_write(gcal, monkeypatch):
    """The event has already moved by then; raising here would report a real
    change as a failure and invite a second write."""
    def broken(days):
        raise RuntimeError("offline")

    monkeypatch.setattr(calendar_provider, "sync_days", broken)
    out = calendar_routes.create_event(summary="Coffee", start="2026-08-20T09:00")
    assert out["ok"] is True


def test_distant_days_are_re_pulled_as_separate_windows(dbfile, monkeypatch):
    """One span from August to December would re-fetch the whole autumn."""
    windows = []
    monkeypatch.setattr(calendar_provider, "_sync",
                        lambda con, start, end: windows.append((start, end)) or 0)

    calendar_provider.sync_days(["2026-08-20", "2026-08-21", "2026-12-01"])

    assert len(windows) == 2
    assert windows[0][0].isoformat() == "2026-08-20"
    assert windows[1][1].isoformat() == "2026-12-01"


# ---------- repeating events ----------

# A semester of one class: the master carries the rule, and every Tuesday the
# user can see is an expansion of it with an id of its own. `match` resolves to
# one of those expansions, which is why every write has to decide whether it
# means the occurrence or the series.

MASTER = {
    "id": "lecture",
    "summary": "PHYS 101",
    "start": {"dateTime": "2026-09-15T14:00:00-04:00",
              "timeZone": "America/New_York"},
    "end": {"dateTime": "2026-09-15T15:20:00-04:00",
            "timeZone": "America/New_York"},
    "recurrence": ["RRULE:FREQ=WEEKLY;BYDAY=TU;UNTIL=20261213T045959Z"],
    "htmlLink": "https://calendar.google.com/event?eid=lecture",
}

INSTANCE = {
    "id": "lecture_20260922T180000Z",
    "recurringEventId": "lecture",
    "summary": "PHYS 101",
    "start": {"dateTime": "2026-09-22T14:00:00-04:00",
              "timeZone": "America/New_York"},
    "end": {"dateTime": "2026-09-22T15:20:00-04:00",
            "timeZone": "America/New_York"},
}

OCCURRENCES = [
    {**INSTANCE, "id": f"lecture_2026{md}T180000Z",
     "start": {"dateTime": f"2026-{d}T14:00:00-04:00",
               "timeZone": "America/New_York"},
     "end": {"dateTime": f"2026-{d}T15:20:00-04:00",
             "timeZone": "America/New_York"}}
    for md, d in (("0915", "09-15"), ("0922", "09-22"), ("1208", "12-08"))
]


def addressed(url: str) -> dict:
    """Which of the two a request URL names. The instance id starts with the
    master's, so this compares paths rather than asking `in`."""
    path = url.split("?")[0].removesuffix("/instances")
    return MASTER if path.endswith("/events/lecture") else INSTANCE


@pytest.fixture
def series(dbfile, gcal, monkeypatch):
    """Google with a repeating event on it: master, expansions, /instances."""
    def fake_request(url, token, method="GET", body=None):
        gcal.append({"url": url, "method": method, "body": body})
        if url.split("?")[0].endswith("/instances"):
            return {"items": [dict(o) for o in OCCURRENCES]}
        if method == "DELETE":
            return {}
        if method == "POST":
            return {**MASTER, **(body or {})}
        if method == "PATCH":
            return {**addressed(url), **(body or {})}
        return {**addressed(url)}

    monkeypatch.setattr(calendar_provider, "api_request", fake_request)
    return gcal


def rule_of(calls):
    return body_of(calls, "POST")["recurrence"][0]


def test_a_repeating_event_is_one_write_not_fifteen(gcal):
    """The whole point: a semester of Tuesdays is an event with a rule on it,
    not fifteen events that happen to share a title."""
    calendar_routes.create_event(summary="PHYS 101",
                                 start="2026-09-15T14:00", end="15:20",
                                 repeat="weekly", repeat_until="2026-12-12")

    assert len(written(gcal, "POST")) == 1
    assert rule_of(gcal).startswith("RRULE:FREQ=WEEKLY;UNTIL=")


def test_an_until_is_inclusive_so_the_last_class_still_happens(gcal):
    """"Until December 12" names a day the event happens on. Written as that
    day's midnight it would be the first day it doesn't, and the semester
    would quietly end a week early."""
    calendar_routes.create_event(summary="PHYS 101",
                                 start="2026-09-15T14:00", end="15:20",
                                 repeat="weekly", repeat_until="2026-12-12")

    stamp = rule_of(gcal).split("UNTIL=")[1]
    until = datetime.strptime(stamp, "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
    # 2pm Eastern on the last day of class is 19:00Z — the rule has to outlast
    # it, and by less than a day.
    last_class = datetime(2026, 12, 12, 19, 0, tzinfo=UTC)
    assert last_class < until < last_class + timedelta(days=1)


def test_an_all_day_series_ends_on_a_date_not_an_instant(gcal):
    """Google refuses a DATE-TIME UNTIL on a DATE start outright."""
    calendar_routes.create_event(summary="Trash day", start="2026-09-16",
                                 repeat="weekly", repeat_until="2026-12-16")

    assert rule_of(gcal).endswith("UNTIL=20261216")


def test_the_days_a_class_meets_are_named_not_counted(gcal):
    calendar_routes.create_event(summary="PHYS 101",
                                 start="2026-09-15T14:00", end="15:20",
                                 repeat="weekly",
                                 repeat_days=["Tuesday", "thu"],
                                 repeat_count=28)

    assert rule_of(gcal) == "RRULE:FREQ=WEEKLY;BYDAY=TU,TH;COUNT=28"


def test_every_other_week_is_an_interval(gcal):
    calendar_routes.create_event(summary="1:1", start="2026-09-15T14:00",
                                 repeat="biweekly", repeat_count=6)

    assert rule_of(gcal) == "RRULE:FREQ=WEEKLY;INTERVAL=2;COUNT=6"


def test_weekdays_is_a_day_set_rather_than_a_frequency(gcal):
    calendar_routes.create_event(summary="Standup", start="2026-09-15T09:00",
                                 repeat="weekdays", repeat_count=20)

    assert rule_of(gcal) == "RRULE:FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR;COUNT=20"


def test_a_rule_words_cannot_reach_can_be_written_out(gcal):
    calendar_routes.create_event(summary="Rent", start="2026-10-01",
                                 repeat="FREQ=MONTHLY;BYMONTHDAY=1;COUNT=12")

    assert rule_of(gcal) == "RRULE:FREQ=MONTHLY;BYMONTHDAY=1;COUNT=12"


def test_a_written_out_rule_must_carry_its_own_ending(gcal):
    """Merging two statements of when a series stops is how you get one that
    stops at neither."""
    with pytest.raises(HTTPException) as e:
        calendar_routes.create_event(summary="Rent", start="2026-10-01",
                                     repeat="FREQ=MONTHLY",
                                     repeat_until="2027-06-01")
    assert e.value.status_code == 400
    assert not gcal


def test_two_endings_are_refused(gcal):
    with pytest.raises(HTTPException) as e:
        calendar_routes.create_event(summary="PHYS 101",
                                     start="2026-09-15T14:00", repeat="weekly",
                                     repeat_until="2026-12-12", repeat_count=14)
    assert e.value.status_code == 400
    assert not gcal


def test_a_series_that_stops_before_it_starts_is_refused(gcal):
    with pytest.raises(HTTPException) as e:
        calendar_routes.create_event(summary="PHYS 101",
                                     start="2026-09-15T14:00", repeat="weekly",
                                     repeat_until="2026-08-01")
    assert e.value.status_code == 400
    assert not gcal


def test_a_repeat_nobody_can_spell_never_reaches_google(gcal):
    """A word this doesn't know is a rule Google would reject with a 400 about
    RRULE syntax — after putting a non-repeating event on the calendar."""
    with pytest.raises(HTTPException) as e:
        calendar_routes.create_event(summary="PHYS 101",
                                     start="2026-09-15T14:00",
                                     repeat="every third tuesday")
    assert e.value.status_code == 400
    assert "weekly" in e.value.detail
    assert not gcal


def test_an_end_date_without_a_repeat_is_refused(gcal):
    with pytest.raises(HTTPException) as e:
        calendar_routes.create_event(summary="PHYS 101",
                                     start="2026-09-15T14:00",
                                     repeat_until="2026-12-12")
    assert e.value.status_code == 400
    assert not gcal


def test_days_within_a_week_dont_apply_to_a_monthly_repeat(gcal):
    with pytest.raises(HTTPException) as e:
        calendar_routes.create_event(summary="Rent", start="2026-10-01",
                                     repeat="monthly", repeat_days=["Monday"],
                                     repeat_count=12)
    assert e.value.status_code == 400
    assert not gcal


def test_an_unknown_weekday_is_refused_rather_than_guessed(gcal):
    with pytest.raises(HTTPException) as e:
        calendar_routes.create_event(summary="PHYS 101",
                                     start="2026-09-15T14:00", repeat="weekly",
                                     repeat_days=["T"], repeat_count=4)
    assert e.value.status_code == 400
    assert not gcal


def test_a_series_re_pulls_only_the_days_the_cache_already_had(series,
                                                               monkeypatch):
    """A semester of Tuesdays spans four months. Re-pulling all of it would
    fetch an autumn the cache never had — and a day that was never synced
    cannot be stale, because get() fetches it on demand."""
    pulled = []
    monkeypatch.setattr(calendar_provider, "sync_days",
                        lambda days: pulled.extend(days))
    con = db.connect()
    with con:
        con.execute("INSERT INTO calendar_days (day, refreshed_at)"
                    " VALUES ('2026-09-22', 'now')")
    con.close()

    calendar_routes.create_event(summary="PHYS 101",
                                 start="2026-09-15T14:00", end="15:20",
                                 repeat="weekly", repeat_until="2026-12-12")

    # The first occurrence always (it is the day the write landed on), plus the
    # one later day the cache claims to know. December was never synced.
    assert set(pulled) == {"2026-09-15", "2026-09-22"}


def test_a_created_series_reports_what_it_actually_produced(series):
    out = calendar_routes.create_event(summary="PHYS 101",
                                       start="2026-09-15T14:00", end="15:20",
                                       repeat="weekly",
                                       repeat_until="2026-12-12")

    assert out["occurrences"] == 3
    assert out["last"].startswith("2026-12-08")
    assert out["open_ended"] is False


def test_deleting_an_occurrence_leaves_the_rest_of_the_series(series):
    """'Cancel Tuesday's lecture' is one week. The default has to be the
    occurrence, because the id `match` resolves to is an occurrence."""
    calendar_routes.delete_event(event_id=INSTANCE["id"], calendar_id="primary")

    assert "lecture_20260922T180000Z" in written(series, "DELETE")[-1]["url"]


def test_deleting_the_series_deletes_the_master_behind_it(series):
    out = calendar_routes.delete_event(event_id=INSTANCE["id"],
                                       calendar_id="primary", scope="all")

    assert written(series, "DELETE")[-1]["url"].split("?")[0].endswith("/lecture")
    assert out["series"] is True
    assert out["occurrences"] == 3


def test_this_and_following_is_refused_with_the_recipe(series):
    """Splitting a series is two writes, not a scope. Saying so is better than
    a scope that silently means something else."""
    with pytest.raises(HTTPException) as e:
        calendar_routes.delete_event(event_id=INSTANCE["id"],
                                     calendar_id="primary", scope="following")
    assert e.value.status_code == 400
    assert "repeat_until" in e.value.detail
    assert not written(series, "DELETE")


def test_moving_the_whole_series_patches_the_master(series):
    out = calendar_routes.update_event(event_id=INSTANCE["id"],
                                       calendar_id="primary", scope="all",
                                       start="15:00")

    patch = written(series, "PATCH")[-1]
    assert patch["url"].split("?")[0].endswith("/lecture")
    # A bare clock time moves the time and leaves the anchor date alone —
    # rewriting that date would re-anchor every occurrence.
    assert patch["body"]["start"]["dateTime"] == "2026-09-15T15:00:00"
    # ...and the class is still 80 minutes long.
    assert patch["body"]["end"]["dateTime"] == "2026-09-15T16:20:00"
    assert out["series"] is True


def test_a_series_keeps_the_zone_it_was_created_in(series, monkeypatch):
    """The user is in California this week; the lecture is not. Re-deriving the
    zone from where they are standing would move every Tuesday by three
    hours."""
    monkeypatch.setattr(tz, "zone_on", lambda day: ZoneInfo("America/Los_Angeles"))

    calendar_routes.update_event(event_id=INSTANCE["id"], calendar_id="primary",
                                 scope="all", start="15:00")

    sent = body_of(series, "PATCH")["start"]
    assert sent["timeZone"] == "America/New_York"


def test_moving_one_occurrence_leaves_the_master_alone(series):
    calendar_routes.update_event(event_id=INSTANCE["id"], calendar_id="primary",
                                 start="2026-09-22T16:00")

    patch = written(series, "PATCH")[-1]
    assert "lecture_20260922T180000Z" in patch["url"]


def test_making_an_existing_event_repeat(gcal):
    calendar_routes.update_event(event_id="ev1", calendar_id="primary",
                                 repeat="weekly", repeat_count=4)

    assert body_of(gcal, "PATCH")["recurrence"] == ["RRULE:FREQ=WEEKLY;COUNT=4"]


def test_a_repeat_change_addresses_the_series_whatever_the_scope_says(series):
    """One expanded Tuesday cannot repeat differently from its own series;
    Google refuses the PATCH, and the refusal reads as a bug in the request."""
    calendar_routes.update_event(event_id=INSTANCE["id"], calendar_id="primary",
                                 repeat="biweekly", repeat_count=6)

    assert written(series, "PATCH")[-1]["url"].split("?")[0].endswith("/lecture")


def test_a_series_can_be_told_to_stop_repeating(series):
    """The way back out: the first occurrence stays as an ordinary event."""
    calendar_routes.update_event(event_id=INSTANCE["id"], calendar_id="primary",
                                 repeat="none")

    assert body_of(series, "PATCH")["recurrence"] == []


def test_a_series_re_pull_covers_the_days_it_left_behind(series, monkeypatch):
    """Moving Tuesdays to Thursdays leaves every Tuesday stale, and after the
    write there is nothing left to ask which days those were."""
    con = db.connect()
    with con:
        for day in ("2026-09-15", "2026-09-22"):
            con.execute("INSERT INTO calendar_days (day, refreshed_at)"
                        " VALUES (?, 'now')", (day,))
    con.close()
    pulled = []
    monkeypatch.setattr(calendar_provider, "sync_days",
                        lambda days: pulled.extend(days))

    calendar_routes.update_event(event_id=INSTANCE["id"], calendar_id="primary",
                                 scope="all", repeat="weekly",
                                 repeat_days=["Thursday"],
                                 repeat_until="2026-12-12")

    assert {"2026-09-15", "2026-09-22"} <= set(pulled)


# ---------- the console tools ----------

def _call(name, **args):
    import asyncio

    from app import console_tools
    handler = [t for t in console_tools.TOOLS if t.name == name][0]
    return asyncio.run(handler.handler(args))["content"][0]["text"]


def test_the_create_tool_reports_the_calendar_it_wrote_to(gcal):
    said = _call("event_create", summary="Coffee", start="2026-08-20T09:00",
                 calendar_id="cal-a", end="", location="", time_zone="")

    assert "cal-a" in said and "ev1" in said


def test_the_console_tools_report_refusals_instead_of_raising(gcal):
    assert "before it starts" in _call("event_create", summary="Coffee",
                                       start="2026-08-20T12:30", end="11:00")
    assert "one of accepted" in _call("event_rsvp", response="maybe?",
                                      event_id="ev1", calendar_id="primary")


def test_the_update_tool_can_clear_a_location_but_drops_blank_ids(gcal):
    """Every other blank means "not given" — for location and description it
    has to mean "clear it", or the console can never remove a wrong address."""
    _call("event_update", event_id="ev1", calendar_id="primary", location="",
          match="", start="", summary="")

    assert body_of(gcal, "PATCH") == {"location": ""}


def test_the_create_tool_says_how_many_times_it_will_happen(series):
    """"Created it" is equally true of a rule that fired once and one that
    filled the autumn, so the answer has to say which."""
    said = _call("event_create", summary="PHYS 101", start="2026-09-15T14:00",
                 end="15:20", calendar_id="cal-a", repeat="weekly",
                 repeat_until="2026-12-12")

    assert "3 occurrences" in said
    assert "2026-12-08" in said


def test_the_create_tool_says_when_a_series_never_ends(gcal):
    said = _call("event_create", summary="Standup", start="2026-09-15T09:00",
                 repeat="weekly")

    assert "no end" in said


def test_the_create_tool_takes_a_count_as_a_string(gcal):
    """The schema says integer; a model that has just written two dates as
    strings reaches for "6" often enough that refusing costs a turn."""
    _call("event_create", summary="1:1", start="2026-09-15T14:00",
          repeat="weekly", repeat_count="6")

    assert rule_of(gcal).endswith("COUNT=6")


def test_the_delete_tool_distinguishes_one_week_from_the_series(series):
    one = _call("event_delete", event_id=INSTANCE["id"], calendar_id="primary")
    assert "series" not in one

    whole = _call("event_delete", event_id=INSTANCE["id"],
                  calendar_id="primary", scope="all")
    assert "all 3 occurrences" in whole and "series is gone" in whole


def test_a_repeat_the_tool_cannot_spell_comes_back_as_words(gcal):
    said = _call("event_create", summary="PHYS 101", start="2026-09-15T14:00",
                 repeat="every third tuesday")

    assert "don't know how to repeat" in said
    assert not gcal
