"""Calendar writes (create, move, delete, RSVP), the write face of providers/calendar.py:
a provider that mutates its own upstream stops being a cache of it. Goes out through the
backend's own Google token, not the claude.ai connector. Every write takes `scope`
("this" or "all"); clock times are in `tz.zone_on(event date)`; guests are mailed only
if `notify`.
Design: DESIGN.md → §2 Calendar.
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

# Either grants write on events (`calendar` is the older catch-all).
WRITE_SCOPES = ("https://www.googleapis.com/auth/calendar.events",
                "https://www.googleapis.com/auth/calendar")

RECONNECT = (f"Reconnect the {GOOGLE_CALENDAR_ACCOUNT} Google account to grant "
             f"calendar writing: `python -m app.providers.oauth authorize "
             f"--account {GOOGLE_CALENDAR_ACCOUNT}` (it opens a browser), or "
             f"the dashboard's Connect button.")

# Length of an event given a start and no end (the Calendar UI's own default).
DEFAULT_MINUTES = 60

RESPONSES = ("accepted", "declined", "tentative")

# Cap on enumerated occurrences: an open-ended rule has no last instance; 500 = a daily event for a year.
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
    """Whether Google emails the guests: off unless asked."""
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
    """Cached events whose title contains `match` (literal, no wildcards), optionally on `day`;
    one row per event, earliest first, `day`/`first_at` = first occurrence. Shared by
    find_event and console_preview so they can't disagree on which event a call names."""
    sql = (f"SELECT {EVENT_COLS}, MIN(day) AS day, MIN(start_at) AS first_at"
           " FROM events WHERE summary LIKE ? ESCAPE '\\'")
    args: list = [db.like_contains(match)]
    if day:
        sql += " AND day=?"
        args.append(day)
    sql += " GROUP BY event_id ORDER BY day, start_at"
    return [dict(r) for r in con.execute(sql, args)]


def find_event(match: str, day: str | None = None) -> dict:
    """An event the user named -> its ids, from the cache (keeps ids out of model-typed
    arguments). Ambiguity is an error listing the candidates, never a guess."""
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
    """The zone a bare clock time is in: the user's on that date unless the caller names one."""
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
    """`_parse`, plus a bare "16:30" meaning that clock time on the start's date."""
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
    """The caller's start/end -> Google's two objects. All-day ends are inclusive in,
    exclusive out (Google stores the day after)."""
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
    """The start/end half of a PATCH, always sent as a pair (half a change turns 10-11am into
    23 hours). An unstated end keeps the event's existing duration."""
    was_start, was_end = _existing(event["start"]), _existing(event["end"])
    if start and len(start.strip()) in (4, 5) and ":" in start:
        # bare "15:00" is a time on the event's own date (a series' first occurrence may be months back)
        if not isinstance(was_start, datetime):
            raise HTTPException(400, "an all-day event has no clock time to "
                                     "move — give start as a date")
        start = f"{was_start.date().isoformat()}T{start.strip()}"
    if start and end:
        pair = dict(zip(("start", "end"), _span(start, end, time_zone)))
        if ("dateTime" in pair["start"]) == isinstance(was_start, datetime):
            return pair
        # Timed <-> all-day: a PATCH merges, so an event would hold both `date` and
        # `dateTime` ("Invalid start time"); a null clears a field.
        stale = ({"date": None} if "dateTime" in pair["start"]
                 else {"dateTime": None, "timeZone": None})
        return {k: {**v, **stale} for k, v in pair.items()}

    if end and not start:
        if isinstance(was_start, datetime):
            zone = _zone(time_zone, was_start.date())
            finish = _parse_end(end, was_start)
            if finish.tzinfo is None:
                # new end is wall time in the user's zone; was_start is offset-aware (comparing raw is a TypeError)
                finish = finish.replace(tzinfo=zone)
            if finish <= was_start:
                raise HTTPException(400, f"the event would end ({finish}) before"
                                         f" it starts ({was_start})")
            # start goes back as Google gave it (re-deriving would rewrite its zone)
            return {"start": event["start"], "end": _google_time(finish, zone)}
        return dict(zip(("start", "end"),
                        _span(was_start.isoformat(), end, time_zone)))

    begin = _parse(start or "", "start")
    if isinstance(begin, datetime) != isinstance(was_start, datetime):
        # one endpoint given: the other can't carry over, so the duration can't be kept
        raise HTTPException(400, "changing between a timed and an all-day event"
                                 " needs both start and end")
    if isinstance(begin, datetime):
        zone = _zone(time_zone, begin.date())
        finish = begin + (was_end - was_start)
        return {"start": _google_time(begin, zone), "end": _google_time(finish, zone)}
    return {"start": {"date": begin.isoformat()},
            "end": {"date": (begin + (was_end - was_start)).isoformat()}}


# ---------- recurrence ----------

# A repeat as said out loud -> (FREQ, INTERVAL); "every other week" gets its own names.
REPEATS = {"daily": ("DAILY", 1),
           "weekly": ("WEEKLY", 1),
           "biweekly": ("WEEKLY", 2),
           "fortnightly": ("WEEKLY", 2),
           "monthly": ("MONTHLY", 1),
           "quarterly": ("MONTHLY", 3),
           "yearly": ("YEARLY", 1),
           "annually": ("YEARLY", 1)}

# Day sets for a weekly repeat, not frequencies.
REPEAT_SETS = {"weekdays": ("MO", "TU", "WE", "TH", "FR"),
               "weekends": ("SA", "SU")}

# Words that mean "stop repeating".
REPEAT_NEVER = ("none", "never", "no", "off", "once", "single")

WEEKDAY_NAMES = ("monday", "tuesday", "wednesday", "thursday", "friday",
                 "saturday", "sunday")
WEEKDAY_CODES = ("MO", "TU", "WE", "TH", "FR", "SA", "SU")


def _byday(name: str) -> str:
    """"Tuesday", "tues", "TU" -> "TU". One letter is refused: "T" and "S" each name two days."""
    text = (name or "").strip().rstrip(".").lower()
    if len(text) >= 2:
        for full, code in zip(WEEKDAY_NAMES, WEEKDAY_CODES):
            if text == code.lower() or full.startswith(text):
                return code
    raise HTTPException(400, f"unknown weekday {name!r} — use a name like"
                             f" Tuesday, or the two-letter TU")


def _until(value: str, begins: dict) -> str:
    """The last date the user named -> an RFC 5545 UNTIL. Two silent traps: it must match
    DTSTART's form (DATE all-day, UTC DATE-TIME timed), and it is an instant, so it becomes
    the end of that local day (a bare midnight drops the last occurrence)."""
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
    # the start's own zone; the lookup is a fallback for an existing event returned without one
    zone = _zone(begins.get("timeZone"), last)
    return (datetime.combine(last, time.max, zone)
            .astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ"))


def _rrule(repeat: str | None, begins: dict, days: list[str] | None = None,
           until: str | None = None, count: int | None = None) -> list[str]:
    """The `recurrence` field for a repeating event, or [] for a single one. `begins` is the
    event's Google start object (UNTIL must match its form and zone). A full RRULE passes
    through alone, carrying its own ending (merging two stop rules stops at neither)."""
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
        # dict.fromkeys keeps the order said
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
    """Every occurrence of a series, as Google expands it. Best-effort like `_recache`:
    it runs after the write, so failing to count them is not a failed write."""
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
    """(event_id, event) for the series behind an instance, or the event itself if it isn't
    one. `recurringEventId` exists only on the expansion, which is what `match` resolves to."""
    parent = event.get("recurringEventId")
    if not parent:
        return event.get("id") or "", event
    return parent, _api(_url(calendar_id, parent), token)


# ---------- cache ----------

def _days(event: dict, calendar_id: str) -> list[str]:
    """Local dates an event's row would occupy (the provider's own mapping)."""
    try:
        normalized = calendar_provider.normalize(event, calendar_id, calendar_id)
        return calendar_provider.days_covered(normalized)
    except Exception:
        return []


def _recache(*day_lists: list[str]) -> None:
    """Re-pull the cache for the days a write touched. Best-effort: the write already
    happened, so raising would report a real change as a failure."""
    try:
        calendar_provider.sync_days([d for days in day_lists for d in days])
    except Exception:
        pass


def _series_days(instances: list[dict], calendar_id: str) -> list[str]:
    """The days a series write made stale: only those `calendar_days` already holds
    (an unsynced day can't be stale; `get()` fetches it on demand)."""
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
    """Fold a series write's occurrence count and last date into its result. `open_ended` is
    read off the rule (no UNTIL/COUNT), not the count, so a failed `_instances` can't hide it."""
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
    # built before the POST: a bad rule must not leave a non-repeating event behind
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
    # a recurring create returns the master (first occurrence); the rest is in Google's expansion
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

    # repeat is a series property: always patch the master (Google refuses `recurrence` on an instance)
    changing_repeat = bool(repeat) or repeat_days or repeat_until or repeat_count
    if _series_scope(scope) or changing_repeat:
        event_id, before = _master(before, calendar_id, token)
    series = bool(before.get("recurrence"))
    if series and not time_zone:
        # a series keeps its creation zone (re-anchoring shifts every occurrence)
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

    # read before the write: after the PATCH the vacated days aren't derivable
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
    """Remove an event; not recoverable from here. On a series the default is the one
    occurrence named; `scope="all"` deletes the master and the whole series."""
    calendar_id, event_id = _address(event_id, match, day, calendar_id)
    token = _token()
    # read first so the answer can name what went
    doomed = _api(_url(calendar_id, event_id), token)
    if _series_scope(scope):
        event_id, doomed = _master(doomed, calendar_id, token)
    # enumerated before the delete: the master's days can't be asked afterwards
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
    """RSVP to an invitation: answers the organizer, leaves the event on the calendar."""
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

    # whole guest list goes back: `attendees` is replaced wholesale (one entry uninvites the rest)
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
