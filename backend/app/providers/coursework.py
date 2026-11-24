"""Coursework provider: Brightspace due dates from the D2L calendar feed (BRIGHTSPACE_ICS_URL, a token URL in .env).

Kinds: " - Due", " - Available", and unsuffixed items. The feed has no submitted/graded
state, so "how many have I missed" is the user's to answer. Same cache shape as athletics
(providers/ics.py): droppable `coursework_items`. Design: DESIGN.md → `app/providers/athletics.py` and `coursework.py`: iCal feeds.

    python -m app.providers coursework --range next-week --format brief
    python -m app.providers coursework --range this-month --query WRIT --format brief
"""

import html
import re
from datetime import date, timedelta

from ..config import BRIGHTSPACE_ICS_URL
from . import ics, offline
from .ranges import Range

NAME = "coursework"
METRICS = None
DEFAULT_METRIC = None
ALLOW_FUTURE = True

TTL = timedelta(hours=1)
DETAILS_CHARS = 600
_COURSE = re.compile(r"^(\d+)_(\d+)\s+([A-Z]{2,5}-\d{3}[A-Z]?):\s*(.*)$")
_KIND = ((" - Due", "due"), (" - Available", "available"))


def _fetch() -> str:
    """GET the feed. Tests monkeypatch this name."""
    return ics.fetch(BRIGHTSPACE_ICS_URL)


def _plain(desc: str) -> str:
    """Description as plain text (tags out, entities decoded, 'View event - <url>' dropped)."""
    s = re.sub(r"<[^>]+>", " ", desc)
    s = html.unescape(s)
    s = re.sub(r"View event - \S+", "", s)
    return " ".join(s.split())


def _row(ev: dict, fetched_at: str) -> dict | None:
    if "DTSTART" not in ev or "SUMMARY" not in ev:
        return None
    summary = ics.text(ev, "SUMMARY")
    kind, title = "event", summary
    for suffix, k in _KIND:
        if summary.endswith(suffix):
            kind, title = k, summary[: -len(suffix)].strip()
    m = _COURSE.match(ics.text(ev, "LOCATION"))
    section, code, course = (m.group(2), m.group(3), m.group(4).strip()) if m \
        else (None, None, ics.text(ev, "LOCATION") or None)
    desc = ics.text(ev, "DESCRIPTION")
    # Prefer the assignment's own page over the calendar event view.
    links = re.findall(r"https://\S+", desc)
    url = next((u for u in links if "/dropbox/" in u or "/quizzing/" in u),
               next((u for u in links if "/calendar/" in u), None))
    details = _plain(desc)
    day, start = ics.when(*ev["DTSTART"])
    return {"uid": ics.text(ev, "UID") or f"{summary}|{day}", "day": day,
            "start": start, "kind": kind, "title": title, "course_code": code,
            "course": course, "section": section,
            "details": details[:DETAILS_CHARS] or None, "url": url,
            "fetched_at": fetched_at}


_COLS = ("uid", "day", "start", "kind", "title", "course_code", "course",
         "section", "details", "url", "fetched_at")


def refresh(force: bool = False) -> dict:
    if not BRIGHTSPACE_ICS_URL:
        return {"skipped": "BRIGHTSPACE_ICS_URL not set"}
    return ics.refresh_table("coursework_items", _COLS, TTL, _fetch, _row,
                             force=force, label="Brightspace feed")


def get(rng: Range, metric: str | None = None) -> dict:
    with offline.best_effort("Brightspace fetch"):
        refresh()
    rows, last = ics.read_table("coursework_items", rng)
    out = {"data": rows, "refreshed_at": last}
    if not BRIGHTSPACE_ICS_URL:
        out["note"] = ("BRIGHTSPACE_ICS_URL is not set — no Brightspace feed"
                       " is configured")
    return out


def brief_line(it: dict) -> str:
    """2026-10-14 Wed 11:59pm WRIT-150 DUE AWA-15: Research Worksheet"""
    d = date.fromisoformat(it["day"])
    when = ics.clock(it["start"]) if it.get("start") else "(no time)"
    tag = {"due": "DUE", "available": "opens"}.get(it["kind"], "event")
    return (f"{it['day']} {d.strftime('%a')} {when} "
            f"{it.get('course_code') or it.get('course') or '?'} {tag} "
            f"{it['title']}")
