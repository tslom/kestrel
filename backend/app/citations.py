"""Citations: the short stable ref that gets a sentence's source back.

`sha1(kind + target)` truncated, behind a kind letter: derived, never
assigned. Resolution is the validation (an unhanded-out ref doesn't resolve).
`remember()` never overwrites. Refs point into provider caches; no bodies stored.
Design: DESIGN.md → Citations — what a sentence was read off.
"""
from __future__ import annotations

import hashlib
import re
from datetime import datetime
from typing import Any

from . import db

# Kind -> ref letter. Readable in a raw transcript; splits the hash space by kind.
KINDS = {"email": "e", "event": "v", "message": "m", "doc": "d"}

# Anchored to the kind letters: `[cite:x1234]` is a non-match, not an unresolvable ref.
REF_RE = re.compile(r"\b([evmd])([0-9a-f]{5,10})\b")
# Doc cites add `#p12 "quote"` after the ref (group 1); the frontend uses those (ui/DocPage.jsx).
CITE_RE = re.compile(r"\[cite:([evmd][0-9a-f]{5,10})(?:#p\d{1,4}(?:\s+\"[^\"\]\n]*\")?)?\]")

_HASH_MIN = 5      # digits in the usual ref
_HASH_MAX = 10     # how far a collision is allowed to push it


def _ref(kind: str, target: str, width: int) -> str:
    letter = KINDS[kind]
    digest = hashlib.sha1(f"{kind}\x00{target}".encode()).hexdigest()
    return f"{letter}{digest[:width]}"


def remember(kind: str, target: str, label: str, day: str | None,
             con: Any = None) -> str:
    """Give this row its ref, recording it the first time it is handed out.
    Call on the way *out* (row rendered into context), never lazily when the
    citation comes back: `resolve()` can't tell an unrecorded ref from an invented one."""
    if kind not in KINDS:
        raise ValueError(f"unknown citation kind: {kind}")
    target = str(target)

    def _work(c) -> str:
        for width in range(_HASH_MIN, _HASH_MAX + 1):
            ref = _ref(kind, target, width)
            held = c.execute(
                "SELECT kind, target FROM cite_refs WHERE ref = ?", (ref,)
            ).fetchone()
            if held is None:
                with c:
                    c.execute(
                        "INSERT OR IGNORE INTO cite_refs"
                        " (ref, kind, target, label, day, seen_at)"
                        " VALUES (?,?,?,?,?,?)",
                        (ref, kind, target, label[:200], day,
                         datetime.now().isoformat(timespec="seconds")))
                return ref
            if held["kind"] == kind and held["target"] == target:
                return ref          # already ours — the common path
            # Taken: lengthen, never steal (past transcripts keep their meaning).
        # Unreachable in practice; unrecorded beats raising inside a brief-line render.
        return _ref(kind, target, _HASH_MAX)

    if con is not None:
        return _work(con)
    with db.session() as c:
        return _work(c)


def _email(con, target: str) -> dict[str, Any] | None:
    r = con.execute(
        "SELECT msg_id, thread_id, day, received_at, from_name, from_addr,"
        " subject, snippet, unread FROM emails WHERE msg_id = ?",
        (target,)).fetchone()
    if r is None:
        return None
    return {
        "id": r["msg_id"], "thread_id": r["thread_id"], "day": r["day"],
        "at": r["received_at"],
        "who": r["from_name"] or r["from_addr"],
        "from_addr": r["from_addr"],
        "title": r["subject"] or "(no subject)",
        "detail": r["snippet"],
        "unread": bool(r["unread"]),
    }


def _event(con, target: str) -> dict[str, Any] | None:
    day, _, event_id = target.partition("/")
    r = con.execute(
        "SELECT day, event_id, summary, start_at, end_at, all_day, location,"
        " calendar_name FROM events WHERE day = ? AND event_id = ?",
        (day, event_id)).fetchone()
    if r is None:
        # Recurring events keep their id; a re-sync can drop the minted day. Nearest instance beats a dead pill.
        r = con.execute(
            "SELECT day, event_id, summary, start_at, end_at, all_day,"
            " location, calendar_name FROM events WHERE event_id = ?"
            " ORDER BY abs(julianday(day) - julianday(?)) LIMIT 1",
            (event_id, day)).fetchone()
    if r is None:
        return None
    return {
        "id": r["event_id"], "day": r["day"], "at": r["start_at"],
        "end": r["end_at"], "all_day": bool(r["all_day"]),
        "who": r["calendar_name"],
        "title": r["summary"] or "(untitled event)",
        "detail": r["location"],
    }


def _message(con, target: str) -> dict[str, Any] | None:
    r = con.execute(
        "SELECT guid, day, ts, chat_guid, handle, contact_name, is_from_me,"
        " text FROM messages WHERE guid = ?", (target,)).fetchone()
    if r is None:
        return None
    return {
        "id": r["guid"], "day": r["day"], "at": r["ts"],
        "handle": r["handle"],
        "who": ("you" if r["is_from_me"]
                else (r["contact_name"] or r["handle"] or "unknown")),
        "title": r["contact_name"] or r["handle"] or "unknown",
        "detail": r["text"],
        "from_me": bool(r["is_from_me"]),
    }


def _doc(con, target: str) -> dict[str, Any] | None:
    r = con.execute(
        "SELECT sha, filename, page_count, added_at FROM docs WHERE sha = ?",
        (target,)).fetchone()
    if r is None:
        return None
    return {
        "id": r["sha"], "at": r["added_at"],
        "title": r["filename"],
        "pages": r["page_count"],
        "detail": f"{r['page_count']} page{'s' if r['page_count'] != 1 else ''}",
    }


_READERS = {"email": _email, "event": _event, "message": _message, "doc": _doc}


def resolve(ref: str) -> dict[str, Any] | None:
    """One ref → what it points at, or None if it was never handed out.

    Handed out but row gone from the cache: `found: False` with the minted
    label, not None ("no longer cached" vs "the console made that up").
    """
    ref = (ref or "").strip().lower()
    m = REF_RE.fullmatch(ref)
    if not m:
        return None
    with db.session() as con:
        row = con.execute(
            "SELECT kind, target, label, day FROM cite_refs WHERE ref = ?",
            (ref,)).fetchone()
        if row is None:
            return None
        found = _READERS[row["kind"]](con, row["target"])
    out = {"ref": ref, "kind": row["kind"], "label": row["label"],
           "day": row["day"], "found": found is not None}
    if found:
        out.update(found)
    return out


def refs_in(text: str) -> list[str]:
    """Every `[cite:…]` in a block of text, in order, deduplicated."""
    seen: list[str] = []
    for m in CITE_RE.finditer(text or ""):
        if m.group(1) not in seen:
            seen.append(m.group(1))
    return seen
