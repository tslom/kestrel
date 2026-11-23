"""Calendar writes — the WRITE face of the calendar provider.

The provider only reads: Google Calendar is the truth and `events` in app.db is
a droppable cache of it. Creating, moving, cancelling and RSVPing are explicit
actions, and they live here rather than in the provider for the reason the
mailbox writes live in routes/email.py — a provider that mutates its own
upstream stops being a cache of it.

The claude.ai MCP connector tools can't stand in for these: that connector
goes needs-auth in every headless session, so the console could read the
calendar all week and not move one event. This path goes out through the backend's own Google token — the same
one the provider reads with — and works wherever the backend does.

A repeat is a property of an event, not a fifth operation: Google stores one
event carrying an RRULE and expands it, so "my Tuesday lecture until December"
is one create, not fifteen. `_rrule` builds that rule from words a person says
("weekly", "until 2026-12-12"), and the two ways it can go quietly wrong are
both handled there — an inclusive `until` becomes the last instant of that
local day (a bare date drops the last class of the semester), and its form has
to match the event's own (a DATE-TIME `UNTIL` on an all-day series is refused
by Google).

An existing event addressed by `match` is an *instance* — the cache stores the
expansion, not the master — so every write takes `scope`: "this" touches the
one occurrence, "all" redirects to the master behind it. "This and following"
is deliberately absent; it is a split, not an edit, and the honest recipe is to
end the series early (`repeat_until`) and create a new one.

Delete really is its own operation, not an update: Google has
DELETE /calendars/{cal}/events/{id}, and the alternatives are worse. Blanking
an event's fields leaves a ghost on the calendar; PATCHing status=cancelled is
the same delete with a spelling only organizers may use. RSVP is a third thing
again — declining an invitation records an answer, it does not remove anyone
else's event.

Scope: reading takes `calendar.readonly`, so a vault authorized before this
shipped has a token that cannot write. Every path here checks the stored scopes
first and says so, because Google's own answer is a 403 body about
"insufficientPermissions" that looks like a bug in the request.

Two habits carried over from the console's own history with this API:

  * The clock time in a request is in the zone the *user* is in on the event's
    date (tz.zone_on), never this machine's and never the zone the old event
    happened to be stored in. Same rule the `event_zone` console tool states.
  * An event is addressed by id or by a match against the cache, never by an id
    the caller retyped. A calendar id that lost its `@group.calendar.google.com`
    suffix once cost 18 consecutive 404s.

Routes (localhost-only, like everything else):
  POST   /api/calendar/events                     {summary, start, end?, repeat?,
                                                   repeat_until?, ...}
  PATCH  /api/calendar/events/{event_id}          {start?, end?, summary?,
                                                   scope?, repeat?, ...}
  DELETE /api/calendar/events/{event_id}          ?calendar_id=&scope=&notify=
  POST   /api/calendar/events/{event_id}/respond  {response: accepted|declined|
                                                             tentative}
"""

import urllib.parse
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from .. import db, tz
from ..config import GOOGLE_CALENDAR_ACCOUNT
from ..providers import calendar as calendar_provider
from ..providers import oauth

router = APIRouter(prefix="/api/calendar")

API_BASE = calendar_provider.API_BASE

# Either grants write on events. `calendar.events` is what config asks for;
# `calendar` is the older catch-all a hand-run authorize may have used.
WRITE_SCOPES = ("https://www.googleapis.com/auth/calendar.events",
                "https://www.googleapis.com/auth/calendar")

RECONNECT = (f"Reconnect the {GOOGLE_CALENDAR_ACCOUNT} Google account to grant "
             f"calendar writing: `python -m app.providers.oauth authorize "
             f"--account {GOOGLE_CALENDAR_ACCOUNT}` (it opens a browser), or "
             f"the dashboard's Connect button.")

# An event given a start and no end. An hour is what the Calendar UI itself
# assumes, so a wrong guess is the one the user already expects to fix.
DEFAULT_MINUTES = 60

RESPONSES = ("accepted", "declined", "tentative")

# How many occurrences of a series this will enumerate. A cap is needed because
# an open-ended rule has no last instance to page to; 500 covers a daily event
# for a year and a weekly one for a decade, which is past the point where
# counting them is the useful answer anyway.
MAX_OCCURRENCES = 500


# ---------- auth ----------

def _token() -> str:
    row = oauth.status("google", GOOGLE_CALENDAR_ACCOUNT) or {}
    if row and not set((row.get("scopes") or "").split()) & set(WRITE_SCOPES):
        raise HTTPException(403, f"the Google token for {GOOGLE_CALENDAR_ACCOUNT}"
                                 f" can read the calendar but not write to it."
                                 f" {RECONNECT}")
    try:
        return oauth.get_token("google", GOOGLE_CALENDAR_ACCOUNT)
    except oauth.NotAuthorized as e:
        raise HTTPException(400, str(e))


def _api(url: str, token: str, method: str = "GET",
         body: dict | None = None) -> dict:
    try:
        return calendar_provider.api_request(url, token, method=method, body=body)
    except RuntimeError as e:
        msg = str(e)
        if "insufficientPermissions" in msg or "Insufficient Permission" in msg:
            raise HTTPException(403, f"Google refused the write: this token is"
                                     f" read-only. {RECONNECT}")
        if "calendar API 404" in msg:
            raise HTTPException(404, "no such event on that calendar — check the"
                                     " calendar id, or re-read the calendar for"
                                     " a current event id")
        if "calendar API 410" in msg:
            raise HTTPException(410, "that event is already gone")
        raise HTTPException(502, msg)


def _url(calendar_id: str, event_id: str | None = None, suffix: str = "",
         **params) -> str:
    path = f"{API_BASE}/calendars/{urllib.parse.quote(calendar_id, safe='')}/events"
    if event_id:
        path += f"/{urllib.parse.quote(event_id, safe='')}"
    path += suffix
    query = {k: v for k, v in params.items() if v is not None}
    return f"{path}?{urllib.parse.urlencode(query)}" if query else path


def _updates(notify: bool) -> str:
    """Whether Google emails the guests. Off unless asked: an approval card for
    "move the meeting" is not consent to mail six people about it."""
    return "all" if notify else "none"


# ---------- addressing an existing event ----------

def _cached(event_id: str) -> dict | None:
    """The cache row for an event, which is where its calendar id comes from."""
    with db.session() as con:
        row = con.execute(
            "SELECT event_id, calendar_id, calendar_name, summary, MIN(day) AS day"
            " FROM events WHERE event_id=? GROUP BY event_id", (event_id,)).fetchone()
    return dict(row) if row else None


EVENT_COLS = ("event_id, calendar_id, calendar_name, summary, start_at,"
              " end_at, all_day, location, description")


def event_candidates(con, match: str, day: str | None = None) -> list[dict]:
    """Every cached event whose title contains `match` (literally — `%` and
    `_` in it are not wildcards), optionally only on `day`; one row per
    event, earliest first, with `day`/`first_at` its first occurrence.

    The one query both find_event (which refuses on ambiguity) and the
    console's approval card (console_preview, which shows the ambiguity
    before the refusal) resolve a title with, so the two can't disagree
    about which event a call names."""
    sql = (f"SELECT {EVENT_COLS}, MIN(day) AS day, MIN(start_at) AS first_at"
           " FROM events WHERE summary LIKE ? ESCAPE '\\'")
    args: list = [db.like_contains(match)]
    if day:
        sql += " AND day=?"
        args.append(day)
    sql += " GROUP BY event_id ORDER BY day, start_at"
    return [dict(r) for r in con.execute(sql, args)]


def find_event(match: str, day: str | None = None) -> dict:
    """An event the user named ("the dentist thing") -> its ids, from the cache.

    Resolving here rather than making the caller carry an id around is what
    keeps ids out of model-typed arguments entirely. Ambiguity is an error
    with the candidates listed, never a guess: picking the wrong one of two
    events called "Standup" moves the wrong meeting.
    """
    match = (match or "").strip()
    if not match:
        raise HTTPException(400, "match is empty")
    with db.session() as con:
        rows = event_candidates(con, match, day)
    if not rows:
        where = f" on {day}" if day else ""
        raise HTTPException(404, f'no cached event matches "{match}"{where} —'
                                 f' read the calendar for that date first, and'
                                 f' note that only synced days are searchable')
    if len(rows) > 1:
        listed = "; ".join(f'{r["day"]} "{r["summary"]}" (event_id {r["event_id"]})'
                           for r in rows[:6])
        raise HTTPException(409, f'"{match}" matches {len(rows)} events — pass '
                                 f'event_id, or a date: {listed}')
    return rows[0]


def _address(event_id: str | None, match: str | None, day: str | None,
             calendar_id: str | None) -> tuple[str, str]:
    """(calendar_id, event_id) for a write, from whichever the caller gave."""
    if event_id:
        if calendar_id:
            return calendar_id, event_id
        row = _cached(event_id)
        if not row:
            raise HTTPException(400, "that event isn't in the cached window, so"
                                     " its calendar is unknown — pass"
                                     " calendar_id (calendar_ids lists them)")
        return row["calendar_id"], event_id
    if not match:
        raise HTTPException(400, "say which event: pass event_id, or match")
    hit = find_event(match, day)
    return calendar_id or hit["calendar_id"], hit["event_id"]


# ---------- times ----------

def _zone(name: str | None, day: date) -> ZoneInfo:
    """The zone a bare clock time is in: the one the user is standing in on
    that date, unless the caller names one."""
    if not name:
        return tz.zone_on(day.isoformat())
    try:
        return ZoneInfo(name.strip())
    except (ZoneInfoNotFoundError, ValueError):
        raise HTTPException(400, f"unknown timezone {name!r} — pass an IANA name"
                                 f" like America/New_York")


def _parse(value: str, field: str) -> date | datetime:
    """"2026-08-20" -> date (all-day); anything with a clock -> datetime."""
    text = (value or "").strip().replace(" ", "T")
    try:
        if len(text) == 10:
            return date.fromisoformat(text)
        return datetime.fromisoformat(text)
    except ValueError:
        raise HTTPException(400, f"{field} must be YYYY-MM-DD for an all-day"
                                 f" event or YYYY-MM-DDTHH:MM for a timed one,"
                                 f" got {value!r}")


def _parse_end(value: str, start: datetime) -> datetime:
    """Same as `_parse`, plus the shorthand a caller reaches for after writing
    a full start: a bare "16:30" means that clock time on the start's date."""
    text = (value or "").strip()
    if len(text) in (4, 5) and ":" in text:
        text = f"{start.date().isoformat()}T{text}"
    end = _parse(text, "end")
    if isinstance(end, date) and not isinstance(end, datetime):
        raise HTTPException(400, "start and end must both be dates (all-day) or"
                                 " both carry a time")
    return end


def _google_time(when: date | datetime, zone: ZoneInfo) -> dict:
    if isinstance(when, datetime):
        return {"dateTime": when.isoformat(timespec="seconds"),
                "timeZone": zone.key}
    return {"date": when.isoformat()}


def _span(start: str, end: str | None, time_zone: str | None) -> tuple[dict, dict]:
    """The caller's start/end -> Google's two objects.

    All-day ends are inclusive on the way in and exclusive on the way out:
    people say "the 20th through the 22nd", Google stores end.date = the 23rd,
    and getting that backwards silently drops the last day of every trip.
    """
    begin = _parse(start, "start")
    if isinstance(begin, datetime):
        zone = _zone(time_zone, begin.date())
        finish = (_parse_end(end, begin) if end
                  else begin + timedelta(minutes=DEFAULT_MINUTES))
        if finish <= begin:
            raise HTTPException(400, f"the event would end ({finish}) before it"
                                     f" starts ({begin})")
        return _google_time(begin, zone), _google_time(finish, zone)

    last = _parse(end, "end") if end else begin
    if isinstance(last, datetime):
        raise HTTPException(400, "start and end must both be dates (all-day) or"
                                 " both carry a time")
    if last < begin:
        raise HTTPException(400, f"the event would end ({last}) before it starts"
                                 f" ({begin})")
    return {"date": begin.isoformat()}, {"date": (last + timedelta(days=1)).isoformat()}


def _existing(field: dict) -> date | datetime:
    """A Google start/end object back into a Python value."""
    if field.get("dateTime"):
        return datetime.fromisoformat(field["dateTime"])
    return date.fromisoformat(field["date"])


def _reschedule(event: dict, start: str | None, end: str | None,
                time_zone: str | None) -> dict:
    """The start/end half of a PATCH, sent as a pair.

    Always both, even when only one moved: Google validates the two against
    each other, and half a change is how a 10am–11am meeting becomes a
    23-hour one. An unstated end keeps the length the event already had, which
    is what "move it to 4" means.
    """
    was_start, was_end = _existing(event["start"]), _existing(event["end"])
    if start and len(start.strip()) in (4, 5) and ":" in start:
        # The same shorthand `end` has, for the same reason: "move it to 15:00"
        # is a time, not a date, and on a series the date it would have to be
        # written against is the first occurrence — which may be months back
        # and outside anything the caller can see.
        if not isinstance(was_start, datetime):
            raise HTTPException(400, "an all-day event has no clock time to "
                                     "move — give start as a date")
        start = f"{was_start.date().isoformat()}T{start.strip()}"
    if start and end:
        pair = dict(zip(("start", "end"), _span(start, end, time_zone)))
        if ("dateTime" in pair["start"]) == isinstance(was_start, datetime):
            return pair
        # Timed <-> all-day. A PATCH merges into the stored start and end
        # rather than replacing them, so an all-day event handed a clock time
        # would hold both `date` and `dateTime`, which Google refuses as
        # "Invalid start time" — and the console then works around it by
        # deleting the event and creating it again. A null is
        # how a PATCH clears a field.
        stale = ({"date": None} if "dateTime" in pair["start"]
                 else {"dateTime": None, "timeZone": None})
        return {k: {**v, **stale} for k, v in pair.items()}

    if end and not start:
        if isinstance(was_start, datetime):
            zone = _zone(time_zone, was_start.date())
            finish = _parse_end(end, was_start)
            if finish.tzinfo is None:
                # A new end is wall time in the user's zone; the old start
                # came back from Google carrying an offset, and comparing the
                # two raw is a TypeError rather than a wrong answer only by
                # luck.
                finish = finish.replace(tzinfo=zone)
            if finish <= was_start:
                raise HTTPException(400, f"the event would end ({finish}) before"
                                         f" it starts ({was_start})")
            # The start goes back exactly as Google gave it — re-deriving it
            # would rewrite its stored zone for no reason.
            return {"start": event["start"], "end": _google_time(finish, zone)}
        return dict(zip(("start", "end"),
                        _span(was_start.isoformat(), end, time_zone)))

    begin = _parse(start or "", "start")
    if isinstance(begin, datetime) != isinstance(was_start, datetime):
        # Timed <-> all-day is a real change, but only if it was asked for:
        # here the caller gave one endpoint, so the other has no meaning to
        # carry over and the duration cannot be preserved.
        raise HTTPException(400, "changing between a timed and an all-day event"
                                 " needs both start and end")
    if isinstance(begin, datetime):
        zone = _zone(time_zone, begin.date())
        finish = begin + (was_end - was_start)
        return {"start": _google_time(begin, zone), "end": _google_time(finish, zone)}
    return {"start": {"date": begin.isoformat()},
            "end": {"date": (begin + (was_end - was_start)).isoformat()}}


# ---------- recurrence ----------

# A repeat the way it gets said out loud -> (FREQ, INTERVAL). "Every other
# week" is the one people phrase as a frequency rather than an interval, so it
# gets names of its own instead of an `interval` argument nobody would pass.
REPEATS = {"daily": ("DAILY", 1),
           "weekly": ("WEEKLY", 1),
           "biweekly": ("WEEKLY", 2),
           "fortnightly": ("WEEKLY", 2),
           "monthly": ("MONTHLY", 1),
           "quarterly": ("MONTHLY", 3),
           "yearly": ("YEARLY", 1),
           "annually": ("YEARLY", 1)}

# Not frequencies: day sets that pick which days a weekly repeat lands on.
REPEAT_SETS = {"weekdays": ("MO", "TU", "WE", "TH", "FR"),
               "weekends": ("SA", "SU")}

# Words that mean "stop repeating" — the way back out of a series that should
# have been one event.
REPEAT_NEVER = ("none", "never", "no", "off", "once", "single")

WEEKDAY_NAMES = ("monday", "tuesday", "wednesday", "thursday", "friday",
                 "saturday", "sunday")
WEEKDAY_CODES = ("MO", "TU", "WE", "TH", "FR", "SA", "SU")


def _byday(name: str) -> str:
    """"Tuesday", "tues", "TU" -> "TU". Anything shorter than two letters is
    refused rather than guessed: "T" and "S" each name two days."""
    text = (name or "").strip().rstrip(".").lower()
    if len(text) >= 2:
        for full, code in zip(WEEKDAY_NAMES, WEEKDAY_CODES):
            if text == code.lower() or full.startswith(text):
                return code
    raise HTTPException(400, f"unknown weekday {name!r} — use a name like"
                             f" Tuesday, or the two-letter TU")


def _until(value: str, begins: dict) -> str:
    """The last date the user named -> an RFC 5545 UNTIL for this event.

    Two traps, both silent. An `UNTIL` must match DTSTART's form — a DATE for
    an all-day series, a UTC DATE-TIME for a timed one — and it is an instant,
    not a day, so "until December 12" has to become the *end* of December 12
    where the event happens. A bare midnight there drops the last class of the
    semester, which is exactly the occurrence the date was naming.
    """
    last = _parse(value, "repeat_until")
    if isinstance(last, datetime):
        last = last.date()
    first = _existing(begins)
    first_day = first.date() if isinstance(first, datetime) else first
    if last < first_day:
        raise HTTPException(400, f"the series would stop repeating ({last})"
                                 f" before its first event ({first_day})")
    if not isinstance(first, datetime):
        return last.strftime("%Y%m%d")
    # `_google_time` wrote the zone alongside the start, so this is the same
    # zone the event was just filed under; the lookup is only a fallback for an
    # existing event Google handed back without one.
    zone = _zone(begins.get("timeZone"), last)
    return (datetime.combine(last, time.max, zone)
            .astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ"))


def _rrule(repeat: str | None, begins: dict, days: list[str] | None = None,
           until: str | None = None, count: int | None = None) -> list[str]:
    """The `recurrence` field for a repeating event, or [] for a single one.

    `begins` is the event's own Google start object because `_until` needs it:
    the rule's end has to be written in the same form, and the same zone, as
    the start it repeats from.

    A full RRULE passes through for the shapes words don't reach ("the first
    Monday of each month" is FREQ=MONTHLY;BYDAY=1MO); it must then carry its
    own ending, because merging two statements of when a series stops is how
    you get a series that stops at neither.
    """
    text = (repeat or "").strip()
    if not text:
        if days or until or count:
            raise HTTPException(400, "say how the event repeats (`repeat`), not"
                                     " only when it stops")
        return []
    if text.lower() in REPEAT_NEVER:
        return []

    upper = text.upper()
    if upper.startswith(("RRULE:", "FREQ=")):
        if days or until or count:
            raise HTTPException(400, "a full RRULE already says how and how long"
                                     " it repeats — pass the rule alone, or use"
                                     " a plain word like 'weekly'")
        return [upper if upper.startswith("RRULE:") else f"RRULE:{upper}"]

    word = text.lower()
    byday = [_byday(d) for d in (days or [])]
    if word in REPEAT_SETS:
        freq, interval = "WEEKLY", 1
        byday = byday or list(REPEAT_SETS[word])
    elif word in REPEATS:
        freq, interval = REPEATS[word]
    else:
        raise HTTPException(400, f"don't know how to repeat {repeat!r} — use one"
                                 f" of {', '.join(sorted(REPEATS))},"
                                 f" {', '.join(sorted(REPEAT_SETS))}, or a full"
                                 f" RRULE")
    if byday and freq != "WEEKLY":
        raise HTTPException(400, f"repeat_days picks days within a week, so it"
                                 f" doesn't apply to a {word} repeat")

    parts = [f"FREQ={freq}"]
    if interval > 1:
        parts.append(f"INTERVAL={interval}")
    if byday:
        # dict.fromkeys and not set(): "Tuesday, Thursday" should stay in the
        # order it was said.
        parts.append("BYDAY=" + ",".join(dict.fromkeys(byday)))
    if until and count:
        raise HTTPException(400, "end the series on a date or after a number of"
                                 " occurrences, not both")
    if count is not None:
        if not 2 <= count <= MAX_OCCURRENCES:
            raise HTTPException(400, f"repeat_count is how many times the event"
                                     f" happens in total, 2 to"
                                     f" {MAX_OCCURRENCES}; got {count}")
        parts.append(f"COUNT={count}")
    elif until:
        parts.append(f"UNTIL={_until(until, begins)}")
    return ["RRULE:" + ";".join(parts)]


def _instances(calendar_id: str, event_id: str, token: str) -> list[dict]:
    """Every occurrence of a series, as Google itself expands it.

    Best-effort by construction, like `_recache`: this runs after a write has
    already happened, and it exists to say what the write did ("13 Tuesdays,
    last one December 8") and to find the cached days it changed. Failing to
    count them is not a failed write.
    """
    items: list[dict] = []
    page = None
    try:
        while len(items) < MAX_OCCURRENCES:
            blob = _api(_url(calendar_id, event_id, suffix="/instances",
                             maxResults=250, pageToken=page), token)
            items += [i for i in blob.get("items", [])
                      if i.get("status") != "cancelled"]
            page = blob.get("nextPageToken")
            if not page:
                break
    except HTTPException:
        pass
    return items


def _series_scope(scope: str | None) -> bool:
    """scope -> "does this write address the whole series?"."""
    text = (scope or "").strip().lower() or "this"
    if text in ("this", "instance", "single", "occurrence"):
        return False
    if text in ("all", "series", "every"):
        return True
    raise HTTPException(400, f"scope is 'this' (the one occurrence) or 'all'"
                             f" (the whole repeating series), got {scope!r}."
                             f" There is no 'this and following': to stop a"
                             f" series early, update it with repeat_until set"
                             f" to the last date to keep, then create a new one")


def _master(event: dict, calendar_id: str, token: str) -> tuple[str, dict]:
    """(event_id, event) for the series behind an instance — or the event as
    given, when it isn't one. `recurringEventId` is only on the expansion,
    which is what the cache holds and therefore what `match` resolves to."""
    parent = event.get("recurringEventId")
    if not parent:
        return event.get("id") or "", event
    return parent, _api(_url(calendar_id, parent), token)


# ---------- cache ----------

def _days(event: dict, calendar_id: str) -> list[str]:
    """Local dates an event's row would occupy — the provider's own mapping,
    so a 10pm–1am event refreshes both of its days here too."""
    try:
        normalized = calendar_provider.normalize(event, calendar_id, calendar_id)
        return calendar_provider.days_covered(normalized)
    except Exception:
        return []


def _recache(*day_lists: list[str]) -> None:
    """Re-pull the cache for the days a write touched. Best-effort by
    construction: the write already happened, and a stale cache is what the
    next poll is for — raising here would report a real change as a failure."""
    try:
        calendar_provider.sync_days([d for days in day_lists for d in days])
    except Exception:
        pass


def _series_days(instances: list[dict], calendar_id: str) -> list[str]:
    """The days a series write went stale on — only the ones already cached.

    A single write re-pulls the days it touched, and a series touches every
    Tuesday until December. Re-pulling all of those would fetch an autumn the
    cache never had, so this keeps the days `calendar_days` already claims:
    a day that was never synced cannot be stale, because `get()` fetches it on
    demand and will see the series then.
    """
    days = sorted({d for inst in instances for d in _days(inst, calendar_id)})
    if not days:
        return []
    with db.session() as con:
        rows = con.execute(
            f"SELECT day FROM calendar_days WHERE day IN"
            f" ({','.join('?' * len(days))})", days).fetchall()
    return [r["day"] for r in rows]


def _result(event: dict, calendar_id: str) -> dict:
    start, end = event.get("start", {}), event.get("end", {})
    return {"ok": True,
            "event_id": event.get("id"),
            "calendar_id": calendar_id,
            "summary": event.get("summary") or "(no title)",
            "start": start.get("dateTime") or start.get("date"),
            "end": end.get("dateTime") or end.get("date"),
            "all_day": "date" in start,
            "location": event.get("location"),
            "recurrence": (event.get("recurrence") or [None])[0],
            "url": event.get("htmlLink")}


def _occurrences(out: dict, instances: list[dict]) -> dict:
    """What a series write actually produced, folded into its result: how many
    occurrences there are and when the last one is.

    Whether it ever ends is read off the rule, not off the count — a rule with
    neither UNTIL nor COUNT never stops, and that is true whether or not the
    enumeration came back. Inferring it from `len(instances)` would report the
    one series worth warning about as an ordinary bounded one every time
    `_instances` failed.
    """
    rule = out.get("recurrence") or ""
    out["occurrences"] = len(instances)
    out["open_ended"] = ("UNTIL=" not in rule and "COUNT=" not in rule
                         or len(instances) >= MAX_OCCURRENCES)
    if instances and not out["open_ended"]:
        last = instances[-1].get("start", {})
        out["last"] = last.get("dateTime") or last.get("date")
    return out


# ---------- writes ----------

def create_event(*, summary: str, start: str, end: str | None = None,
                 calendar_id: str = "primary", location: str | None = None,
                 description: str | None = None,
                 attendees: list[str] | None = None,
                 time_zone: str | None = None, repeat: str | None = None,
                 repeat_days: list[str] | None = None,
                 repeat_until: str | None = None,
                 repeat_count: int | None = None,
                 notify: bool = False) -> dict:
    """Put a new event on a calendar, once or every week until December."""
    summary = (summary or "").strip()
    if not summary:
        raise HTTPException(400, "an event needs a summary")
    begins, ends = _span(start, end, time_zone)
    body: dict = {"summary": summary, "start": begins, "end": ends}
    if location:
        body["location"] = location
    if description:
        body["description"] = description
    if attendees:
        body["attendees"] = [{"email": a.strip()} for a in attendees if a.strip()]
    # Built before the POST so a rule that can't be written costs nothing: the
    # alternative is an event on the calendar that doesn't repeat, which reads
    # as success to everyone except the person who misses the other fourteen.
    rule = _rrule(repeat, begins, days=repeat_days, until=repeat_until,
                  count=repeat_count)
    if rule:
        body["recurrence"] = rule

    token = _token()
    created = _api(_url(calendar_id, sendUpdates=_updates(notify)),
                   token, method="POST", body=body)
    out = _result(created, calendar_id)
    if not rule:
        _recache(_days(created, calendar_id))
        return out
    # A recurring create returns the master, whose start is the first
    # occurrence; the rest of the series exists only in Google's expansion.
    series = _instances(calendar_id, created.get("id") or "", token)
    _recache(_days(created, calendar_id), _series_days(series, calendar_id))
    return _occurrences(out, series)


def update_event(*, event_id: str | None = None, match: str | None = None,
                 day: str | None = None, calendar_id: str | None = None,
                 summary: str | None = None, start: str | None = None,
                 end: str | None = None, location: str | None = None,
                 description: str | None = None, time_zone: str | None = None,
                 scope: str | None = None, repeat: str | None = None,
                 repeat_days: list[str] | None = None,
                 repeat_until: str | None = None,
                 repeat_count: int | None = None,
                 notify: bool = False) -> dict:
    """Change an event that already exists — time, title, place, notes, or how
    it repeats."""
    calendar_id, event_id = _address(event_id, match, day, calendar_id)
    token = _token()
    before = _api(_url(calendar_id, event_id), token)

    # How an event repeats is a property of the series, so changing it always
    # addresses the master — patching `recurrence` onto one expanded Tuesday
    # is a thing Google refuses, and rightly.
    changing_repeat = bool(repeat) or repeat_days or repeat_until or repeat_count
    if _series_scope(scope) or changing_repeat:
        event_id, before = _master(before, calendar_id, token)
    series = bool(before.get("recurrence"))
    if series and not time_zone:
        # A series keeps the zone it was created in. Re-anchoring it to
        # wherever the user is standing today would shift every occurrence,
        # including the ones already behind them.
        time_zone = (before.get("start") or {}).get("timeZone")

    patch: dict = {}
    if summary and summary.strip():
        patch["summary"] = summary.strip()
    if location is not None:
        patch["location"] = location
    if description is not None:
        patch["description"] = description
    if start or end:
        patch.update(_reschedule(before, start, end, time_zone))
    if changing_repeat:
        patch["recurrence"] = _rrule(repeat, patch.get("start") or before["start"],
                                     days=repeat_days, until=repeat_until,
                                     count=repeat_count)
    if not patch:
        raise HTTPException(400, "nothing to change — pass a new start, end, "
                                 "summary, location, description or repeat")

    # Where the series lands *now*, read before the write: moving it to
    # Thursdays leaves every Tuesday it used to occupy stale, and after the
    # PATCH those days are no longer derivable from anything.
    vacated = _series_days(_instances(calendar_id, event_id, token),
                           calendar_id) if series else []

    after = _api(_url(calendar_id, event_id, sendUpdates=_updates(notify)),
                 token, method="PATCH", body=patch)
    out = _result(after, calendar_id)
    was = before.get("start", {})
    out["was"] = {"summary": before.get("summary"),
                  "start": was.get("dateTime") or was.get("date")}
    if not (series or after.get("recurrence")):
        _recache(_days(before, calendar_id), _days(after, calendar_id))
        return out

    out["series"] = bool(after.get("recurrence"))
    occupied = _instances(calendar_id, event_id, token) if out["series"] else []
    _recache(_days(before, calendar_id), _days(after, calendar_id),
             vacated, _series_days(occupied, calendar_id))
    return _occurrences(out, occupied) if out["series"] else out


def delete_event(*, event_id: str | None = None, match: str | None = None,
                 day: str | None = None, calendar_id: str | None = None,
                 scope: str | None = None, notify: bool = False) -> dict:
    """Remove an event from the calendar. Not recoverable from here — Google's
    trash for events is the UI's own undo, which this cannot reach.

    On a repeating event the default is the one occurrence the caller named;
    `scope="all"` deletes the master, which takes the whole series with it.
    """
    calendar_id, event_id = _address(event_id, match, day, calendar_id)
    token = _token()
    # Read first so the answer can name what went: "deleted" with nothing but
    # an id is unverifiable by the person who asked for it.
    doomed = _api(_url(calendar_id, event_id), token)
    if _series_scope(scope):
        event_id, doomed = _master(doomed, calendar_id, token)
    # Enumerated before the delete for the same reason as an update's: once the
    # master is gone there is nothing left to ask which days it occupied.
    series = (_instances(calendar_id, event_id, token)
              if doomed.get("recurrence") else [])

    _api(_url(calendar_id, event_id, sendUpdates=_updates(notify)),
         token, method="DELETE")
    _recache(_days(doomed, calendar_id), _series_days(series, calendar_id))
    gone = _result(doomed, calendar_id)
    gone["deleted"] = True
    gone["series"] = bool(series)
    return _occurrences(gone, series) if series else gone


def respond_event(*, response: str, event_id: str | None = None,
                  match: str | None = None, day: str | None = None,
                  calendar_id: str | None = None, comment: str | None = None,
                  notify: bool = False) -> dict:
    """RSVP to an invitation. Distinct from deleting it: this answers the
    organizer and leaves the event on the calendar."""
    response = (response or "").strip().lower()
    if response not in RESPONSES:
        raise HTTPException(400, f"response must be one of {', '.join(RESPONSES)},"
                                 f" got {response!r}")
    calendar_id, event_id = _address(event_id, match, day, calendar_id)
    token = _token()
    event = _api(_url(calendar_id, event_id), token)

    attendees = event.get("attendees") or []
    mine = [a for a in attendees if a.get("self")]
    if not mine:
        raise HTTPException(400, "there is no invitation to answer here — the "
                                 "user isn't on this event's guest list")
    for a in mine:
        a["responseStatus"] = response
        if comment:
            a["comment"] = comment

    # The whole guest list goes back, not just the one entry: `attendees` is
    # replaced wholesale, so sending one attendee uninvites everyone else.
    after = _api(_url(calendar_id, event_id, sendUpdates=_updates(notify)),
                 token, method="PATCH", body={"attendees": attendees})
    out = _result(after, calendar_id)
    out["response"] = response
    return out


# ---------- routes ----------

class EventIn(BaseModel):
    summary: str
    start: str
    end: str | None = None
    calendar_id: str = "primary"
    location: str | None = None
    description: str | None = None
    attendees: list[str] | None = None
    time_zone: str | None = None
    repeat: str | None = None
    repeat_days: list[str] | None = None
    repeat_until: str | None = None
    repeat_count: int | None = None
    notify: bool = False


class EventPatch(BaseModel):
    calendar_id: str | None = None
    summary: str | None = None
    start: str | None = None
    end: str | None = None
    location: str | None = None
    description: str | None = None
    time_zone: str | None = None
    scope: str | None = None
    repeat: str | None = None
    repeat_days: list[str] | None = None
    repeat_until: str | None = None
    repeat_count: int | None = None
    notify: bool = False


class RespondIn(BaseModel):
    response: str
    calendar_id: str | None = None
    comment: str | None = None
    notify: bool = False


@router.post("/events")
def post_event(payload: EventIn):
    return create_event(**payload.model_dump())


@router.patch("/events/{event_id}")
def patch_event(event_id: str, payload: EventPatch):
    return update_event(event_id=event_id, **payload.model_dump())


@router.delete("/events/{event_id}")
def del_event(event_id: str, calendar_id: str | None = None,
              scope: str | None = None, notify: bool = False):
    return delete_event(event_id=event_id, calendar_id=calendar_id, scope=scope,
                        notify=notify)


@router.post("/events/{event_id}/respond")
def post_respond(event_id: str, payload: RespondIn):
    return respond_event(event_id=event_id, **payload.model_dump())
