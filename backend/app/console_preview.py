"""What an approval card shows instead of the tool's JSON input.

A card exists to be answered, and the only honest way to answer "may I delete
this?" is to see the thing. The raw input can't show it: `event_delete` is
addressed by `event_id` and `calendar_id`, two opaque strings, so the card that
asked to delete an event showed the calendar's 64-hex id and nothing a person
could recognise — not the title, not the time, not which calendar. The answer
was a guess dressed as consent.

So every card is built here instead: the ids are resolved against the same
caches the tool will write through, and the card shows what those ids name.

Three rules this file is written to:

- **Resolve the way the tool resolves.** The preview and the call must address
  the same event, or the card is worse than none — it would describe one event
  while a different one is deleted. `_event_for` mirrors `routes/calendar._
  address` exactly, including its ambiguity rule, and says "ambiguous" rather
  than picking a row the way that function refuses to.
- **Never raise, never block.** This runs inside `can_use_tool`, between the
  model asking and the card appearing. A lookup that fails costs the detail on
  one card; an exception would cost the approval path. Every resolver is
  wrapped, and every one of them reads the local cache only — no API call, so
  nothing here can hang the prompt on a network round trip.
- **Absence is a fact.** A field the cache doesn't have is left out rather than
  rendered empty. "no description" and "we didn't look" are different, and a
  blank row beside a delete button implies the first when it may be the second.
"""

import json
import os.path
import re
import sqlite3
from datetime import datetime
from typing import Any

from . import db
from . import tz as tzbase
from .console_tools import SERVER_NAME
from .routes.calendar import EVENT_COLS, event_candidates

# A card is read at a glance, so the long fields are cut here rather than in
# the browser: the note on an event can be a pasted agenda, and a card that
# scrolls is a card whose buttons moved.
MAX_NOTE = 400
MAX_VALUE = 200
# How many candidates an ambiguous `match` lists before it stops.
MAX_CANDIDATES = 4

# Keys never worth a row of their own: either the card already says it in a
# better form, or it is an id the rest of the card exists to translate.
_SKIP_KEYS = frozenset({
    "event_id", "eventId", "calendar_id", "calendarId", "match", "date",
    "message_id", "messageId", "thread_id", "threadId", "msg_id", "id",
})

# Input keys that hold something to be shown as a body rather than a value:
# wrapped, not truncated to a line.
_BODY_KEYS = ("body", "description", "note", "text", "content", "plan",
              "message")


# ---------- small helpers ----------

def _s(value: Any) -> str:
    """One-line string for a value of unknown shape."""
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, default=str)
    if isinstance(value, bool):
        return "yes" if value else "no"
    return str(value)


def _clip(text: str, limit: int) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[:limit].rstrip() + "…"


def _one_line(text: str, limit: int = MAX_VALUE) -> str:
    return _clip(" ".join(_s(text).split()), limit)


def _first(data: dict, *keys: str) -> str:
    for key in keys:
        value = data.get(key)
        if value not in (None, "", [], {}):
            return _s(value).strip()
    return ""


def _when(row: dict) -> str:
    """'Tue Sep 23 · 6:00–7:00 PM PT' for a cached event row.

    The zone label rides along because the console's whole calendar surface
    assumes it does: `start_at` strings carry mixed offsets, and a bare 6:00
    on a card is the one number a reader will act on without checking.
    """
    try:
        if row.get("all_day"):
            day = datetime.fromisoformat(str(row["start_at"])[:10])
            return f"{day:%a %b %-d} · all day"
        start = datetime.fromisoformat(str(row["start_at"]))
        day_key = str(row.get("day") or row["start_at"])[:10]
        zone = tzbase.zone_on(day_key)
        start = start.astimezone(zone)
        out = f"{start:%a %b %-d} · {start:%-I:%M %p}"
        end = row.get("end_at")
        if end:
            finish = datetime.fromisoformat(str(end)).astimezone(zone)
            out += f"–{finish:%-I:%M %p}"
        return f"{out} {tzbase.label(day_key)}"
    except (ValueError, TypeError, KeyError):
        return _one_line(row.get("start_at") or "")


def _rows(*pairs: tuple[str, Any]) -> list[list[str]]:
    """Label/value rows, dropping the ones with nothing to say."""
    out = []
    for label, value in pairs:
        text = _one_line(value)
        if text:
            out.append([label, text])
    return out


# ---------- calendar ----------

def _event_for(data: dict) -> tuple[dict | None, str | None]:
    """(the event this call addresses, why we couldn't say) from the cache.

    Mirrors `routes/calendar._address`: an explicit id wins, otherwise the
    title is matched by the same `event_candidates` query. Where that function raises on an ambiguous match, this one reports
    the ambiguity — the call is about to be refused for it, and a card that
    said nothing would make the refusal look like it came from nowhere.
    """
    event_id = _first(data, "event_id", "eventId")
    match = _first(data, "match")
    day = _first(data, "date", "day")
    try:
        with db.session() as con:
            if event_id:
                row = con.execute(
                    f"SELECT {EVENT_COLS}, MIN(day) AS day FROM events"
                    " WHERE event_id=? GROUP BY event_id", (event_id,)).fetchone()
                if row:
                    return dict(row), None
                return None, "not in the cached window — the card can't show it"
            if not match:
                return None, None
            rows = event_candidates(con, match, day)
    except sqlite3.Error:
        return None, None
    if not rows:
        return None, f'nothing cached matches "{_one_line(match, 60)}"'
    if len(rows) > 1:
        listed = "; ".join(f'{r["day"]} {r["summary"]}'
                           for r in rows[:MAX_CANDIDATES])
        return None, (f'"{_one_line(match, 40)}" matches {len(rows)} events '
                      f'({listed}) — the call will be refused for it')
    return rows[0], None


def _calendar_name(calendar_id: str) -> str:
    """A calendar id as the user knows it. Falls back to the id, which is at
    least true, rather than to a blank row."""
    cid = (calendar_id or "").strip()
    if not cid or cid == "primary":
        return cid
    try:
        with db.session() as con:
            row = con.execute("SELECT name FROM calendars WHERE calendar_id=?",
                              (cid,)).fetchone()
            if row:
                return row["name"]
            row = con.execute(
                "SELECT calendar_name FROM events WHERE calendar_id=?"
                " AND calendar_name IS NOT NULL LIMIT 1", (cid,)).fetchone()
    except sqlite3.Error:
        return cid
    return (row["calendar_name"] if row else None) or cid


def _series_warn(data: dict, verb: str) -> str | None:
    """The one fact about a calendar write that a reader cannot recover from
    getting wrong: whether it lands on one occurrence or on all of them."""
    scope = _first(data, "scope").lower()
    if scope == "all":
        return f"{verb} EVERY occurrence of this repeating event, past included"
    return None


def _existing_event(data: dict, verb: str) -> dict:
    row, problem = _event_for(data)
    if row is None:
        return {"title": _first(data, "match") or "(event not resolved)",
                "unresolved": problem or "no event named in the call",
                "rows": _rows(("event id", _first(data, "event_id", "eventId")),
                              ("calendar", _calendar_name(_first(data, "calendar_id"))))}
    warns = [w for w in (_series_warn(data, verb),) if w]
    if data.get("notify"):
        warns.append("guests are emailed about it")
    return {
        "title": row.get("summary") or "(no title)",
        "when": _when(row),
        "rows": _rows(("calendar", row.get("calendar_name")
                                   or _calendar_name(row.get("calendar_id") or "")),
                      ("where", row.get("location"))),
        "note": _clip(" ".join(_s(row.get("description")).split()), MAX_NOTE) or None,
        "warn": " · ".join(warns) or None,
    }


def _changes_to(data: dict) -> list[list[str]]:
    """What an update is setting, so the card is a diff and not just a target."""
    return _rows(("new time", _first(data, "start")),
                 ("new end", _first(data, "end")),
                 ("new title", _first(data, "summary")),
                 ("new place", _first(data, "location")),
                 ("new notes", _first(data, "description")),
                 ("repeat", _first(data, "repeat")),
                 ("until", _first(data, "repeat_until")))


def _new_when(start: str, end: str) -> str:
    """The time a create/update is asking for, read back as a date.

    Deliberately not put through `_when`: these strings are what the model
    typed, and the zone they land in is decided by the route, not here.
    Formatting them as a weekday and a clock time is the point — "2026-09-23T18:00"
    on a card is a string the reader has to decode before they can tell it's
    the wrong day.
    """
    try:
        at = datetime.fromisoformat(start)
        out = f"{at:%a %b %-d}" if len(start) <= 10 else f"{at:%a %b %-d} · {at:%-I:%M %p}"
    except (ValueError, TypeError):
        return f"{start}–{end}" if end else start
    if not end:
        return out
    # The three shapes `end` comes in are three different things to print: a
    # bare HH:MM is a time on the start's day, a bare date is the last day of
    # an all-day span, and a full datetime is both. Reading a date as a time
    # is what turned a four-day trip into "Wed Sep 23–12:00 AM".
    try:
        if len(end) <= 5:
            return f"{out}–{datetime.fromisoformat(f'{start[:10]}T{end}'):%-I:%M %p}"
        finish = datetime.fromisoformat(end)
        if len(end) <= 10:
            return f"{out} – {finish:%a %b %-d}"
        same_day = end[:10] == start[:10]
        return f"{out}–{finish:%-I:%M %p}" if same_day \
            else f"{out} – {finish:%a %b %-d} · {finish:%-I:%M %p}"
    except (ValueError, TypeError):
        return f"{out}–{end}"


def _event_create(data: dict) -> dict:
    cal = _first(data, "calendar_id", "calendarId") or "primary"
    when = _first(data, "start")
    end = _first(data, "end")
    return {
        "title": _first(data, "summary") or "(no title)",
        "when": _new_when(when, end) if when else "",
        "rows": _rows(("calendar", _calendar_name(cal)),
                      ("where", _first(data, "location")),
                      ("repeats", _first(data, "repeat")),
                      ("until", _first(data, "repeat_until")),
                      ("guests", ", ".join(data.get("attendees") or []))),
        "note": _clip(_s(data.get("description")), MAX_NOTE) or None,
        "warn": ("guests are emailed about it" if data.get("notify") else None),
    }


# ---------- mail ----------

def _email_for(data: dict) -> dict | None:
    """The cached message or thread a Gmail call names."""
    msg_id = _first(data, "message_id", "messageId", "msg_id")
    thread_id = _first(data, "thread_id", "threadId")
    try:
        with db.session() as con:
            if msg_id:
                row = con.execute(
                    "SELECT subject, from_name, from_addr, received_at, 1 AS n"
                    " FROM emails WHERE msg_id=?", (msg_id,)).fetchone()
                if row:
                    return dict(row)
            if thread_id:
                row = con.execute(
                    "SELECT subject, from_name, from_addr, MAX(received_at)"
                    " AS received_at, COUNT(*) AS n FROM emails"
                    " WHERE thread_id=? GROUP BY thread_id", (thread_id,)).fetchone()
                if row:
                    return dict(row)
    except sqlite3.Error:
        return None
    return None


def _mail(data: dict, bare: str) -> dict:
    """A mail call: the message it acts on, or the one it is about to send."""
    to = _first(data, "to", "recipient", "recipients")
    if to or bare in ("send_message", "create_draft", "reply", "forward"):
        row = _email_for(data)
        subject = _first(data, "subject") or (row or {}).get("subject") or ""
        body = _first(data, "body", "message", "text")
        return {
            "title": _one_line(subject) or "(no subject)",
            "when": None,
            "rows": _rows(("to", to), ("cc", _first(data, "cc")),
                          ("in reply to", (row or {}).get("subject")
                                          if not _first(data, "subject") else None)),
            "note": _clip(" ".join(body.split()), MAX_NOTE) or None,
            "warn": ("it leaves for the recipient immediately"
                     if bare in ("send_message", "reply", "forward") else None),
        }
    row = _email_for(data)
    if row is None:
        return {"title": "(message not in the cache)",
                "unresolved": "nothing cached under that id — the card can't "
                              "show what it is",
                "rows": _rows(("message id", _first(data, "message_id", "messageId")),
                              ("thread id", _first(data, "thread_id", "threadId")))}
    sender = row.get("from_name") or row.get("from_addr") or ""
    count = int(row.get("n") or 1)
    return {
        "title": _one_line(row.get("subject") or "(no subject)"),
        "when": _one_line(row.get("received_at") or "")[:16].replace("T", " "),
        "rows": _rows(("from", sender),
                      ("messages", f"{count} in the thread" if count > 1 else None)),
    }


# ---------- shell ----------

_RM_FLAGS = re.compile(r"^-")


def _bash(data: dict) -> dict:
    """The command, whole. `tool_detail` already squeezes it onto one line for
    the log; the card is the place it gets read before it runs, so it is shown
    in full and the paths an `rm` would take are pulled out on their own."""
    command = _s(data.get("command"))
    targets = []
    for word in command.split():
        if word in ("rm", "shred", "srm") or _RM_FLAGS.match(word):
            continue
        if "/" in word and not word.startswith("-"):
            targets.append(os.path.basename(word.rstrip(";&|")) or word)
    rows = []
    if command.startswith(("rm ", "rm\t")) and targets:
        rows = _rows(("deletes", ", ".join(targets[:6])))
    # `lead` is Claude's description, which the card reads first: the command
    # under it is the evidence, highlighted and folded when long. Clipped at
    # 4000 rather than 600 now that a long one folds instead of filling the
    # screen — a clipped command is one you approve without having read.
    return {"title": _clip(command, 4000), "mono": True, "rows": rows,
            "lead": _one_line(data.get("description") or "", MAX_NOTE) or None}


# ---------- generic ----------

def _generic(data: dict) -> dict:
    """Anything with no resolver: the same fields, minus the JSON braces.

    Still worth doing. A connector tool we've never seen arrives as a flat
    object of strings, and the difference between reading that as labelled
    rows and reading it as pretty-printed JSON is the difference between
    scanning a card and parsing one.
    """
    rows, note = [], None
    for key, value in list(data.items())[:12]:
        if key in _SKIP_KEYS or value in (None, "", [], {}):
            continue
        if key in _BODY_KEYS and isinstance(value, str) and len(value) > MAX_VALUE:
            note = _clip(value, MAX_NOTE)
            continue
        rows.append([key.replace("_", " "), _one_line(value)])
    return {"title": "", "rows": rows, "note": note}


# ---------- entry point ----------

_VAULT_PREFIX = f"mcp__{SERVER_NAME}__"


def _one(tool: str, data: dict) -> dict:
    bare = tool.rsplit("__", 1)[-1]
    if tool == "Bash":
        return _bash(data)
    if bare in ("event_delete", "delete_event"):
        return _existing_event(data, "deletes")
    if bare in ("event_update", "update_event"):
        out = _existing_event(data, "changes")
        out["rows"] = (out.get("rows") or []) + _changes_to(data)
        return out
    if bare in ("event_rsvp", "respond_to_event"):
        out = _existing_event(data, "answers")
        out["rows"] = (out.get("rows") or []) + _rows(
            ("answer", _first(data, "response")))
        return out
    if bare in ("event_create", "create_event"):
        return _event_create(data)
    if tool.startswith("mcp__claude_ai_Gmail__"):
        return _mail(data, bare)
    return _generic(data)


def describe(tool: str, inputs: list[dict]) -> list[dict]:
    """One preview per call on the card. Never raises: a resolver that trips
    costs that card its detail, and the raw input is on the card anyway."""
    out = []
    for data in inputs:
        if not isinstance(data, dict):
            data = {"input": data}
        try:
            out.append(_one(tool, data))
        except Exception:
            out.append(_generic(data if isinstance(data, dict) else {}))
    return out
