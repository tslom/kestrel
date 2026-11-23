"""Athletics provider — USC's game schedule for every sport, from Sidearm's
public iCal feed (ATHLETICS_ICS_URL). No key.

Why it exists: across Sep–Oct the console spent ~15 web searches on kickoff
times (Fresno State, Oregon, Washington, the season schedule) and on 10-12
gave up on a water polo game the feed had all along (Oct 17, 1pm, Uytengsu).
Search results for a schedule are a week stale or a different season; the
athletics department's own calendar is the source those pages copy.

The feed is truth; `athletics_events` is a derived cache, safe to drop. A read
refetches when the cache is older than the feed's own X-PUBLISHED-TTL (2h) —
lazily, on the read, inside offline.best_effort so a dead network serves the
last copy marked stale. The feed is a rolling window (roughly a month back
through the end of the academic year), so a full refetch replaces the table.

Times: Sidearm writes DTSTART in UTC; a game is shown on its Pacific date and
clock, because that's where the user watches it from. A game with a date but
no time yet (football kickoffs are set ~12 days out) arrives as
DTSTART;VALUE=DATE and is stored with start = null — "TBA", never midnight.

    python -m app.providers athletics --range this-week --query polo --format brief
"""

import re
from datetime import date, timedelta

from ..config import ATHLETICS_ICS_URL
from . import ics, offline
from .ranges import Range

NAME = "athletics"
METRICS = None
DEFAULT_METRIC = None
ALLOW_FUTURE = True

TTL = timedelta(hours=2)
_RESULT = re.compile(r"^\[([WLT])\]\s*")
_PREFIX = re.compile(r"^\[[A-Z]\]\s*")
_SCORE = re.compile(r"^([WLT])\s+(\d+-\d+.*)$")


def _fetch() -> str:
    """GET the feed. Tests monkeypatch this name."""
    return ics.fetch(ATHLETICS_ICS_URL)


def parse_summary(summary: str, location: str | None = None) -> dict:
    """'[W] USC Trojans Men's Water Polo vs UCLA - Senior Day' ->
    sport, opponent, side, note. Sidearm writes "at" for an away game and
    "vs" for both home and neutral-site ones — the first live read had a cross
    country meet in Riverside and a swim meet in Illinois down as "home" — so
    a "vs" is home only when it's in Los Angeles, else neutral. A tournament
    has neither."""
    s = _PREFIX.sub("", summary).strip()
    s = re.sub(r"^USC Trojans\s+", "", s)
    vs_side = ("home" if location and "los angeles" in location.lower()
               else "neutral")
    for sep, side in ((" vs ", vs_side), (" at ", "away")):
        if sep in s:
            sport, rest = s.split(sep, 1)
            opp, _, note = rest.partition(" - ")
            return {"sport": sport.strip(), "opponent": opp.strip() or None,
                    "side": side, "note": note.strip() or None}
    # A tournament: "Men's Golf  Vuori Invitational" — Sidearm leaves a double
    # space where the vs/at and opponent would go.
    sport, _, event = s.partition("  ")
    return {"sport": sport.strip(), "opponent": None, "side": None,
            "note": event.strip() or None}


def _row(ev: dict, fetched_at: str) -> dict | None:
    if "DTSTART" not in ev or "SUMMARY" not in ev:
        return None
    summary = ics.text(ev, "SUMMARY")
    day, start = ics.when(*ev["DTSTART"])
    desc = ics.text(ev, "DESCRIPTION").splitlines()
    result = next((f"{m.group(1)} {m.group(2).strip()}" for line in desc
                   if (m := _SCORE.match(line.strip()))), None)
    if result is None and (m := _RESULT.match(summary)):
        result = m.group(1)
    tv = next((line.split(":", 1)[1].strip() for line in desc
               if line.startswith("TV:")), None)
    location = re.sub(r"\s+,", ",", ics.text(ev, "LOCATION")) or None
    return {"uid": ev.get("UID", ("", summary + day))[1], "day": day,
            "start": start, **parse_summary(summary, location),
            "location": location,
            "result": result, "tv": tv,
            "url": ics.text(ev, "URL").replace("&amp;", "&") or None,
            "summary": summary, "fetched_at": fetched_at}


_COLS = ("uid", "day", "start", "sport", "opponent", "side", "note",
         "location", "result", "tv", "url", "summary", "fetched_at")


def refresh(force: bool = False) -> dict:
    """Refetch the whole feed if the cache is past TTL (or `force`)."""
    return ics.refresh_table("athletics_events", _COLS, TTL, _fetch, _row,
                             force=force, label="athletics feed")


def get(rng: Range, metric: str | None = None) -> dict:
    with offline.best_effort("athletics fetch"):
        refresh()
    rows, last = ics.read_table("athletics_events", rng)
    for r in rows:
        r["time_tba"] = r["start"] is None
    return {"data": rows, "refreshed_at": last}


def brief_line(it: dict) -> str:
    """2026-10-17 Sat 1pm PT Men's Water Polo vs UCLA (home) @ Uytengsu
    Aquatics Center — Senior Day [W 12-10] TV: ESPN"""
    d = date.fromisoformat(it["day"])
    when = f"{ics.clock(it['start'])} PT" if it.get("start") else "time TBA"
    head = f"{it['day']} {d.strftime('%a')} {when} {it['sport']}"
    if it.get("opponent"):
        head += (f" {'at' if it['side'] == 'away' else 'vs'} {it['opponent']}"
                 f" ({it['side']})")
    if it.get("location"):
        head += f" @ {it['location']}"
    if it.get("note"):
        head += f" — {it['note']}"
    if it.get("result"):
        head += f" [{it['result']}]"
    if it.get("tv"):
        head += f" TV: {it['tv']}"
    return head
