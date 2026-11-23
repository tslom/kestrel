"""Google Calendar provider (IDEAS §2) — first real provider on the §0
pattern. Google Calendar is truth; `events` + `calendar_days` in app.db are
the derived cache (safe to drop and re-pull). refresh() keeps a window around
today warm; get() reads the cache and pulls cold days on demand.

Auth: the shared oauth helper, token row (google, GOOGLE_CALENDAR_ACCOUNT).
Day mapping: an event belongs to every LOCAL calendar date it touches — plain
dates, not day_key rollover (a 1am lecture is on its own date; range anchoring
still uses day_key via ranges.py, like every provider).
"""

import urllib.parse
from datetime import date, datetime, time, timedelta

from .. import citations, db
from .. import tz as tzbase
from ..config import GOOGLE_CALENDAR_ACCOUNT, GOOGLE_CALENDAR_IDS
from . import http, metric_cache, oauth, offline
from .ranges import Range

NAME = "calendar"
METRICS = None          # no metric axis — an event list, not a time series
DEFAULT_METRIC = None
ALLOW_FUTURE = True     # next-* and future explicit dates are the point

API_BASE = "https://www.googleapis.com/calendar/v3"

# refresh() keeps [today-7 .. today+35] synced — covers the widget's "today",
# this/next-week and most of this/next-month; anything colder is fetched on
# demand by get() and backfills the cache.
REFRESH_PAST_DAYS = 7
REFRESH_FUTURE_DAYS = 35

# sync_days(): days this close together are re-pulled in one API window; a
# bigger gap gets its own call.
SYNC_CLUSTER_DAYS = 14


def _local_tz():
    """The machine's own zone. Only for the API fetch window, which is about
    where this process is running, not where the user will be — every mapping
    that a reader sees goes through `tz.zone_on(date)` instead."""
    return datetime.now().astimezone().tzinfo


def api_request(url: str, token: str, method: str = "GET",
             body: dict | None = None) -> dict:
    """One Calendar API call. `{}` for an empty response — DELETE answers 204."""
    return http.json_request(url, label="calendar API", token=token,
                             method=method, body=body, reauth=lambda:
                             oauth.refresh("google", GOOGLE_CALENDAR_ACCOUNT))


def normalize(ev: dict, cal_id: str, cal_name: str) -> dict:
    all_day = "date" in ev["start"]
    return {
        "event_id": ev["id"],
        "calendar_id": cal_id,
        "calendar_name": cal_name,
        "summary": ev.get("summary") or "(no title)",
        "start_at": ev["start"].get("dateTime") or ev["start"]["date"],
        "end_at": ev["end"].get("dateTime") or ev["end"]["date"],
        "all_day": all_day,
        "location": ev.get("location"),
        "description": ev.get("description"),
    }


def _calendars(token: str) -> list[tuple[str, str]]:
    """(id, display name) pairs to pull: the config override if set, else
    every calendar on the account's calendarList — including subscribed ones."""
    if GOOGLE_CALENDAR_IDS:
        return [(cid, cid) for cid in GOOGLE_CALENDAR_IDS]
    out, page_token = [], None
    while True:
        params = {"maxResults": "250"}
        if page_token:
            params["pageToken"] = page_token
        blob = api_request(f"{API_BASE}/users/me/calendarList"
                        f"?{urllib.parse.urlencode(params)}", token)
        out += [(c["id"], c.get("summaryOverride") or c.get("summary") or c["id"])
                for c in blob.get("items", [])]
        page_token = blob.get("nextPageToken")
        if not page_token:
            break
    return out


def days_covered(ev: dict) -> list[str]:
    """Dates an event touches, in the zone the user is in then. Google's end is
    exclusive (all-day ends the next date; timed events ending exactly at
    midnight don't spill over).

    Which date an event falls on is a zone question, and answering it with the
    machine clock files a 1am Eastern event on the wrong date for someone
    living in Pacific — the row would then render under a day whose label
    disagrees with the day it was filed under. The zone lookup needs a date to
    start from, which is the date we're computing; the event's own filing date
    seeds it, since no zone here is more than three hours off and the only way
    that seed picks the wrong side of a TIMELINE entry is an event on the
    transition date itself, i.e. while the user is on the plane."""
    if ev["all_day"]:
        start = date.fromisoformat(ev["start_at"])
        end = date.fromisoformat(ev["end_at"]) - timedelta(days=1)
    else:
        tz = tzbase.zone_on(ev["start_at"][:10])
        start = datetime.fromisoformat(ev["start_at"]).astimezone(tz).date()
        end_dt = datetime.fromisoformat(ev["end_at"]).astimezone(tz)
        end = (end_dt - timedelta(microseconds=1)).date()
    end = max(end, start)
    out, d = [], start
    while d <= end:
        out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def _fetch(start: date, end: date) -> tuple[list[dict], list[tuple[str, str]]]:
    """Pull every event overlapping [start, end] (inclusive) from the API.

    Returns the calendar list alongside the events because it was already
    fetched to walk it: `_sync` needs the (id, name) pairs to rewrite the
    `calendars` table, and asking calendarList a second time for rows we are
    holding would be a second round trip for the same answer.

    The window is padded a day each side. `days_covered` maps events into the
    user's zone for that date, which can be three hours off this machine's, so
    an unpadded window would miss an event that lands inside the range only
    after the shift. Overshooting costs nothing: `_sync` keeps only the days
    inside the range."""
    tz = _local_tz()
    time_min = datetime.combine(start - timedelta(days=1), time.min, tz).isoformat()
    time_max = datetime.combine(end + timedelta(days=2), time.min, tz).isoformat()
    token = oauth.get_token("google", GOOGLE_CALENDAR_ACCOUNT)
    events = []
    calendars = _calendars(token)
    for cal_id, cal_name in calendars:
        page_token = None
        while True:
            params = {
                "timeMin": time_min,
                "timeMax": time_max,
                "singleEvents": "true",   # expand recurring events
                "orderBy": "startTime",
                "maxResults": "250",
            }
            if page_token:
                params["pageToken"] = page_token
            url = (f"{API_BASE}/calendars/{urllib.parse.quote(cal_id)}/events"
                   f"?{urllib.parse.urlencode(params)}")
            blob = api_request(url, token)
            events += [normalize(ev, cal_id, cal_name)
                       for ev in blob.get("items", [])
                       if ev.get("status") != "cancelled"]
            page_token = blob.get("nextPageToken")
            if not page_token:
                break
    return events, calendars


def _save_calendars(con, calendars: list[tuple[str, str]], now: str) -> None:
    """Rewrite the `calendars` table from what calendarList just said.

    This is what makes a rename in Google heal itself: the name lives here,
    and the copy on each cached event row is dragged along to match, including
    on rows far outside the window this sync pulled. Without that second
    statement a rename would only reach the days being re-fetched, and
    `calendar_ids` would list the old and new names against the same id until
    the cold rows aged out — which, for an event six months out, is never.

    Calendars that have gone are deleted, but only when the whole list was
    pulled: with GOOGLE_CALENDAR_IDS pinned to a subset, everything not in
    that subset is absent from `calendars` rather than unsubscribed, and
    pruning would empty the table down to the pinned few on every sync.
    """
    with con:
        for cal_id, name in calendars:
            con.execute(
                "INSERT INTO calendars (calendar_id, name, refreshed_at)"
                " VALUES (?,?,?) ON CONFLICT(calendar_id) DO UPDATE SET"
                " name=excluded.name, refreshed_at=excluded.refreshed_at",
                (cal_id, name, now))
            con.execute(
                "UPDATE events SET calendar_name=? WHERE calendar_id=?"
                " AND (calendar_name IS NULL OR calendar_name<>?)",
                (name, cal_id, name))
        if GOOGLE_CALENDAR_IDS is None and calendars:
            ids = [c[0] for c in calendars]
            con.execute(
                "DELETE FROM calendars WHERE calendar_id NOT IN"
                f" ({','.join('?' * len(ids))})", ids)


def _sync(con, start: date, end: date) -> int:
    """API -> cache for the window: replace event rows, mark days synced."""
    events, calendars = _fetch(start, end)
    now = datetime.now().isoformat(timespec="seconds")
    _save_calendars(con, calendars, now)
    start_key, end_key = start.isoformat(), end.isoformat()
    with con:
        con.execute("DELETE FROM events WHERE day BETWEEN ? AND ?",
                    (start_key, end_key))
        for ev in events:
            for day in days_covered(ev):
                if start_key <= day <= end_key:
                    con.execute(
                        "INSERT OR REPLACE INTO events (day, event_id, calendar_id,"
                        " calendar_name, summary, start_at, end_at, all_day,"
                        " location, description)"
                        " VALUES (?,?,?,?,?,?,?,?,?,?)",
                        (day, ev["event_id"], ev["calendar_id"], ev["calendar_name"],
                         ev["summary"], ev["start_at"], ev["end_at"],
                         int(ev["all_day"]), ev["location"], ev.get("description")))
        d = start
        while d <= end:
            con.execute(
                "INSERT INTO calendar_days (day, refreshed_at) VALUES (?,?)"
                " ON CONFLICT(day) DO UPDATE SET refreshed_at=excluded.refreshed_at",
                (d.isoformat(), now))
            d += timedelta(days=1)
    return len(events)


def refresh() -> dict:
    """Poller entrypoint: keep the window around today warm."""
    today = date.fromisoformat(db.day_key())
    start = today - timedelta(days=REFRESH_PAST_DAYS)
    end = today + timedelta(days=REFRESH_FUTURE_DAYS)
    with db.session() as con:
        n = _sync(con, start, end)
    return {"events": n, "window": f"{start}..{end}"}


def sync_days(days) -> int:
    """Re-pull the cache for the dates a write touched.

    A write knows exactly which days it changed, so refresh()'s six-week window
    is both too much work and — for an event three months out — the wrong
    window. Days far apart are synced as separate calls rather than one span:
    moving a lecture from September to December must not re-fetch the autumn.
    """
    ds = sorted({d for d in days if d})
    if not ds:
        return 0
    runs: list[list[str]] = [[ds[0]]]
    for d in ds[1:]:
        gap = (date.fromisoformat(d) - date.fromisoformat(runs[-1][-1])).days
        if gap > SYNC_CLUSTER_DAYS:
            runs.append([])
        runs[-1].append(d)
    n = 0
    with db.session() as con:
        for run in runs:
            n += _sync(con, date.fromisoformat(run[0]), date.fromisoformat(run[-1]))
    return n


def cite(ev: dict) -> str | None:
    """This event's citation ref.

    Keyed on `day/event_id`, not `event_id` alone: a recurring event carries
    one id across every instance, so a ref built from the id would make
    "your 9am Tuesday" and "your 9am next Tuesday" the same citation.
    """
    if not ev.get("id"):
        return None
    day = ev.get("day") or (ev.get("start") or "")[:10]
    return citations.remember(
        "event", f"{day}/{ev['id']}", ev.get("summary") or "(untitled event)",
        day)


def brief_line(ev: dict) -> str:
    """One-line event for --format brief."""
    if ev.get("span"):
        s = datetime.fromisoformat(ev["start"])
        e = datetime.fromisoformat(ev["end"])
        when = f"multi-day {s:%m-%d %H:%M} to {e:%m-%d %H:%M}"
    elif ev["all_day"]:
        when = "all-day"
    else:
        # get() already put these in the right zone; the label rides along so a
        # bare HH:MM can't be re-interpreted by whoever reads the line.
        s = datetime.fromisoformat(ev["start"])
        e = datetime.fromisoformat(ev["end"])
        when = f"{s:%H:%M}-{e:%H:%M} {ev.get('tz') or tzbase.label(ev['day'])}"
    loc = f" @ {ev['location']}" if ev["location"] else ""
    return f"{ev['day']} {when} {ev['summary']}{loc} [{ev['calendar']}]"


def _within_day_order(ev: dict):
    """day, all-day first, then timed events by instant — `start_at` strings
    carry mixed UTC offsets, so comparing them raw mis-sorts within a day."""
    if ev["all_day"]:
        return (ev["day"], 0, 0.0)
    return (ev["day"], 1, datetime.fromisoformat(ev["start"]).timestamp())


def _dedupe(data: list[dict]) -> list[dict]:
    """Drop events that are the same event twice.

    The cache can't prevent these: its PK is (day, event_id), which correctly
    collapses re-syncs of one event, but Google itself hands out two distinct
    ids for the same thing when an invite arrives by two routes (a subscribed
    calendar plus a copy on your own). Nothing downstream can tell those apart
    from real data, so the widget showed the same class registration twice.

    Identical summary + start + end on a day is never worth rendering twice,
    so the first wins — `data` is already in display order, which puts the
    primary calendar's copy ahead of the subscribed one. Two genuinely
    distinct events that share a title AND both endpoints are indistinguishable
    to a reader anyway; the surviving row keeps its own `calendar` label, and
    `duplicates` records what was folded in so the expansion can say so."""
    out: list[dict] = []
    seen: dict[tuple, dict] = {}
    for ev in data:
        key = (ev["day"], ev["summary"], ev["start"], ev["end"])
        if (first := seen.get(key)) is not None:
            first.setdefault("duplicates", []).append(ev["calendar"])
            continue
        seen[key] = ev
        out.append(ev)
    return out


# A timed event longer than a day (a retreat, a trip, a hold, or a mis-dated
# end: a 1h appointment filed as ending a week later) frames the days it
# touches rather than filling them. get() hands it out as all-day (with
# `span`), so free slots, the brief, the Load chart and the cards treat it as
# a banner instead of 24h of busy on every day; SQL readers of `events` use
# SPAN_SQL for the same rule. 11-14: one such appointment read as 696h booked
# for the week.
SPAN_SQL = "(julianday(end_at) - julianday(start_at) > 1)"


def spans_days(start_iso: str | None, end_iso: str | None) -> bool:
    try:
        return (datetime.fromisoformat(end_iso)
                - datetime.fromisoformat(start_iso)) > timedelta(days=1)
    except (TypeError, ValueError):
        return False


def get(rng: Range, metric: str | None = None) -> dict:
    """Read the slice from cache; sync any never-fetched days first (one API
    window spanning them — a few warm days re-synced beats N calls)."""
    with db.session() as con:
        missing = metric_cache.missing_days(con, "calendar_days", rng)
        if missing:
            with offline.best_effort("calendar backfill"), oauth.as_value_error():
                _sync(con, date.fromisoformat(missing[0]),
                      date.fromisoformat(missing[-1]))
        rows = con.execute(
            "SELECT * FROM events WHERE day BETWEEN ? AND ?",
            (rng.start_key, rng.end_key)).fetchall()
        refreshed = metric_cache.oldest_refresh(con, "calendar_days", rng)

    def _continued(r) -> bool:
        """True when this row is a later day of a multi-day event, i.e. the
        event started before this day locally. An 10pm-1am event is real on
        both days, but on day two it's a spill-over, not a 10pm start — the
        flag lets a renderer say "runs until 1am" instead of leading the day
        with yesterday's clock time.

        Same zone as the day mapping and the rendered clock time — asking this
        in the machine's zone would flag a row as a spill-over on the very day
        it starts."""
        if r["all_day"]:
            return r["start_at"][:10] < r["day"]
        return datetime.fromisoformat(r["start_at"]) \
            .astimezone(tzbase.zone_on(r["day"])).date().isoformat() < r["day"]

    # start/end are re-expressed in the zone the user is in on that date, and
    # `tz` names it. Google hands back each event in whatever zone it was filed
    # in, so passing those through means one slice can carry three frames with
    # nothing to distinguish them — which is how a console came to render a
    # 9:00 AM Pacific block next to an 8:30 PM Eastern one as if they were the
    # same clock. The instant never changes; only how it reads.
    def _row(r) -> dict:
        span = not r["all_day"] and spans_days(r["start_at"], r["end_at"])
        out = {"day": r["day"], "id": r["event_id"], "summary": r["summary"],
               "start": tzbase.to_local(r["start_at"], r["day"]),
               "end": tzbase.to_local(r["end_at"], r["day"]),
               # Only on timed rows: `tz` names the zone the clock times are
               # in, and an all-day event has none. Labelling one anyway put a
               # bare "ET" on a card whose every row said "all-day".
               "tz": None if r["all_day"] or span else tzbase.label(r["day"]),
               "all_day": bool(r["all_day"]) or span, "location": r["location"],
               "continued": _continued(r),
               "calendar": r["calendar_name"] or r["calendar_id"]}
        if span:
            out["span"] = True      # timed, but over a day: see SPAN_SQL
        return out

    data = [_row(r) for r in rows]
    data.sort(key=_within_day_order)
    data = _dedupe(data)
    # refreshed_at = the STALEST synced day in the slice — honest for the
    # "refreshed Nm ago" affordance (§1).
    return {"data": data, "refreshed_at": refreshed}


# The waking hours a "when am I free" answer is allowed to offer. Wide enough
# to cover a student's real day, narrow enough that nothing proposes 3am.
FREE_EARLIEST = time(9, 0)
FREE_LATEST = time(21, 0)
FREE_MIN_MINUTES = 30


def free_slots(rng: Range, *, earliest: time = FREE_EARLIEST,
               latest: time = FREE_LATEST,
               min_minutes: int = FREE_MIN_MINUTES) -> dict:
    """Open stretches per day — what's left of the waking window once the
    events on it are subtracted.

    Reads through get(), so this inherits the cache backfill, the per-day zone
    conversion and the duplicate collapsing rather than re-deriving any of
    them. All-day events are reported beside the slots instead of blocking
    them: a birthday or a "Move-in week" banner would otherwise mark whole
    days unavailable, which is the one answer that's never useful.
    """
    slice_ = get(rng)
    by_day: dict[str, list[dict]] = {}
    for ev in slice_["data"]:
        by_day.setdefault(ev["day"], []).append(ev)

    out = []
    for day in rng.days():
        zone = tzbase.zone_on(day)
        d = date.fromisoformat(day)
        window_start = datetime.combine(d, earliest, zone)
        window_end = datetime.combine(d, latest, zone)
        busy, banners = [], []
        for ev in by_day.get(day, []):
            if ev["all_day"]:
                banners.append(ev["summary"])
                continue
            # Clamp: an event that starts the night before (`continued`) or
            # runs past bedtime blocks only the part inside the window.
            s = max(datetime.fromisoformat(ev["start"]), window_start)
            e = min(datetime.fromisoformat(ev["end"]), window_end)
            if e > s:
                busy.append((s, e))
        busy.sort()

        slots, cursor = [], window_start
        for s, e in busy:
            if (s - cursor).total_seconds() >= min_minutes * 60:
                slots.append((cursor, s))
            cursor = max(cursor, e)
        if (window_end - cursor).total_seconds() >= min_minutes * 60:
            slots.append((cursor, window_end))

        out.append({"day": day, "tz": tzbase.label(day),
                    "all_day": banners,
                    "free": [{"start": s.strftime("%H:%M"),
                              "end": e.strftime("%H:%M"),
                              "minutes": int((e - s).total_seconds() // 60)}
                             for s, e in slots]})
    return {"days": out, "window": f"{earliest:%H:%M}-{latest:%H:%M}",
            "refreshed_at": slice_["refreshed_at"]}
