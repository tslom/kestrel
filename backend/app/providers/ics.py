"""The iCal reading two providers share (athletics, coursework).

Just enough RFC 5545 for a published feed: unfold continuation lines, split
VEVENTs into {NAME: (params, value)}, unescape text values, and turn a
DTSTART into a Pacific (day, clock) — or a day with no clock for a
VALUE=DATE entry, which is "no time set", never midnight. No RRULE: neither
Sidearm nor D2L publishes recurring events, each occurrence is its own VEVENT.
"""

import re
import urllib.request
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

PT = ZoneInfo("America/Los_Angeles")
_HEADERS = {"User-Agent": "Mozilla/5.0 (vault-dashboard; personal use)"}


def fetch(url: str, timeout: float = 20) -> str:
    req = urllib.request.Request(url, headers=_HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


def unescape(v: str) -> str:
    return (v.replace("\\n", "\n").replace("\\N", "\n").replace("\\,", ",")
            .replace("\\;", ";").replace("\\\\", "\\")).strip()


def events(ics: str) -> list[dict[str, tuple[str, str]]]:
    """VEVENTs as {NAME: (params, value)}. Lines are unfolded first (a CRLF
    followed by one space or tab continues the previous line)."""
    text = re.sub(r"\r?\n[ \t]", "", ics)
    out, cur = [], None
    for line in text.splitlines():
        if line == "BEGIN:VEVENT":
            cur = {}
        elif line == "END:VEVENT":
            if cur is not None:
                out.append(cur)
            cur = None
        elif cur is not None and ":" in line:
            head, value = line.split(":", 1)
            name, _, params = head.partition(";")
            cur[name.upper()] = (params, value)
    return out


def text(ev: dict, name: str) -> str:
    return unescape(ev.get(name, ("", ""))[1])


def when(params: str, value: str) -> tuple[str, str | None]:
    """(Pacific day, Pacific 'YYYY-MM-DDTHH:MM' or None for a date-only)."""
    if "VALUE=DATE" in params.upper() or len(value) == 8:
        return date(int(value[:4]), int(value[4:6]), int(value[6:8])).isoformat(), None
    t = datetime.strptime(value.rstrip("Z"), "%Y%m%dT%H%M%S")
    t = (t.replace(tzinfo=timezone.utc) if value.endswith("Z")
         else t.replace(tzinfo=PT)).astimezone(PT)
    return t.date().isoformat(), t.strftime("%Y-%m-%dT%H:%M")


def refresh_table(table: str, cols: tuple[str, ...], ttl: timedelta, fetch_fn,
                  row_fn, *, force: bool = False, label: str) -> dict:
    """Replace `table` with a fresh read of the feed if its newest fetched_at
    is past `ttl` (or `force`). The feeds are rolling windows, so a refetch
    is the whole truth and replaces the table rather than merging into it.
    An empty or garbled feed raises instead — "no rows" from a feed that had
    them an hour ago is a broken fetch, not an empty schedule."""
    from .. import db
    with db.session() as con:
        last = con.execute(f"SELECT MAX(fetched_at) AS t FROM {table}").fetchone()["t"]
        if (not force and last
                and datetime.now() - datetime.fromisoformat(last) < ttl):
            return {"skipped": "fresh", "refreshed_at": last}
        now = datetime.now().isoformat(timespec="seconds")
        rows = [r for ev in events(fetch_fn()) if (r := row_fn(ev, now))]
        if not rows:
            raise RuntimeError(f"{label} returned no events")
        with con:
            con.execute(f"DELETE FROM {table}")
            con.executemany(
                f"INSERT OR REPLACE INTO {table} ({', '.join(cols)})"
                f" VALUES ({', '.join('?' * len(cols))})",
                [tuple(r[c] for c in cols) for r in rows])
    return {"rows": len(rows), "refreshed_at": now}


def read_table(table: str, rng) -> tuple[list[dict], str | None]:
    """Rows whose `day` falls in the range, in time order (a date-only row
    after the timed ones that day), and when the cache was last filled."""
    from .. import db
    with db.session() as con:
        rows = [dict(r) for r in con.execute(
            f"SELECT * FROM {table} WHERE day BETWEEN ? AND ?"
            " ORDER BY day, start IS NULL, start",
            (rng.start_key, rng.end_key))]
        last = con.execute(f"SELECT MAX(fetched_at) AS t FROM {table}").fetchone()["t"]
    for r in rows:
        r.pop("fetched_at", None)
    return rows, last


def clock(start: str) -> str:
    """'2026-10-17T13:00' -> '1pm', '...T23:59' -> '11:59pm'."""
    t = datetime.strptime(start, "%Y-%m-%dT%H:%M")
    return t.strftime("%-I%p" if t.minute == 0 else "%-I:%M%p").lower()
