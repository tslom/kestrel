"""Google Calendar provider. Google is truth; `events` + `calendar_days` in app.db
are derived cache. An event belongs to every local calendar date it touches
(plain dates, not day_key rollover); that date comes from `tz.zone_on`, never
the machine's zone.
Design: DESIGN.md → §2 Calendar.
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

# refresh() keeps [today-7 .. today+35] synced; colder days are fetched on demand by get().
REFRESH_PAST_DAYS = 7
REFRESH_FUTURE_DAYS = 35

# sync_days(): days within this gap share one API window; a bigger gap gets its own call.
SYNC_CLUSTER_DAYS = 14


def _local_tz():
    """The machine's own zone, for the API fetch window only; readers use `tz.zone_on(date)`."""
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
    exclusive (all-day ends the next date; timed events ending at midnight don't spill).
    The zone lookup is seeded with the event's own start date (wrong only on a transition day)."""
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
    """Pull every event overlapping [start, end] (inclusive) -> (events, (id, name) calendars).
    The window is padded a day each side (the user's zone can be 3h off the machine's);
    `_sync` keeps only days inside the range."""
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
    """Rewrite `calendars` from calendarList; renames propagate to every cached event row.
    Gone calendars are pruned only when the whole list was pulled (not with pinned IDs)."""
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
    """Re-pull the cache for the dates a write touched, clustered by SYNC_CLUSTER_DAYS
    so a September-to-December move doesn't re-fetch the autumn."""
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
    """This event's citation ref, keyed `day/event_id`: recurring instances share one id."""
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
        # the zone label rides along so a bare HH:MM can't be re-read in another zone
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
    """Fold events with identical day + summary + start + end (Google issues two ids when an
    invite arrives by two routes). First in display order wins; `duplicates` lists the folded calendars."""
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


# A timed event over a day long is a banner, not busy time: get() hands it out as all-day
# with `span`; SQL readers of `events` use SPAN_SQL for the same rule.
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
        """True when the event started before this day locally (a spill-over, rendered
        "runs until 1am"). Uses the day's zone, not the machine's."""
        if r["all_day"]:
            return r["start_at"][:10] < r["day"]
        return datetime.fromisoformat(r["start_at"]) \
            .astimezone(tzbase.zone_on(r["day"])).date().isoformat() < r["day"]

    # start/end are re-expressed in the user's zone on that date and `tz` names it;
    # Google returns each event in whatever zone it was filed in.
    def _row(r) -> dict:
        span = not r["all_day"] and spans_days(r["start_at"], r["end_at"])
        out = {"day": r["day"], "id": r["event_id"], "summary": r["summary"],
               "start": tzbase.to_local(r["start_at"], r["day"]),
               "end": tzbase.to_local(r["end_at"], r["day"]),
               # timed rows only: an all-day event has no zone
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
    # refreshed_at = the stalest synced day in the slice
    return {"data": data, "refreshed_at": refreshed}


# The waking hours a "when am I free" answer may offer.
FREE_EARLIEST = time(9, 0)
FREE_LATEST = time(21, 0)
FREE_MIN_MINUTES = 30


def free_slots(rng: Range, *, earliest: time = FREE_EARLIEST,
               latest: time = FREE_LATEST,
               min_minutes: int = FREE_MIN_MINUTES) -> dict:
    """Open stretches per day: the waking window minus timed events, read through get().
    All-day events are listed beside the slots, never blocking them."""
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
            # clamp: spill-over events block only the part inside the window
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
