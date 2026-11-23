"""Citations: the handle that gets a sentence's source back.

The console reads an address out of an email and tells you where the meeting
is. The address survives the retelling; the email does not. This module gives
every citable row a short stable ref, hands it to the model alongside the row,
and resolves it back to something clickable when the model quotes it.

Three rules shape the whole thing:

**The ref is derived, never assigned.** `sha1(kind + target)` truncated to
five hex digits, behind a one-letter kind. No counter, so nothing needs to
know which session is asking, and the same email is `e7a3f1` today and in a
transcript replayed next March. A per-session counter looked cheaper until you
replay a transcript: its `e3` means whatever the *replaying* session numbered
third.

**Resolution is the validation.** There is no separate check that the turn
really read what it cites. A ref the model invented is five random hex digits
that were never handed out, so it does not resolve, and the frontend renders
it dead rather than linking to the wrong email with a straight face. The one
failure that would be silent is a collision — two targets, one ref — so
`remember()` is INSERT OR IGNORE and never overwrites: whoever got the ref
first keeps it, `resolve()` returns that row, and the loser gets a ref with a
longer hash instead (see `_ref`).

**Targets are ids the provider caches already hold.** Nothing here stores an
email body or a message text; a ref is a pointer into `emails` / `events` /
`messages`, and `resolve()` reads the live row. So a re-synced subject shows
its new self, and a purged cache resolves to nothing — which is the truth.
"""
from __future__ import annotations

import hashlib
import re
from datetime import datetime
from typing import Any

from . import db

# Kind ↔ the letter its refs start with. The letter is not decoration: it is
# what makes `[cite:e7a3f1]` readable in a raw transcript, and it splits the
# hash space by kind so a collision has to be within one kind.
KINDS = {"email": "e", "event": "v", "message": "m", "doc": "d"}

# What the model writes and the frontend matches. Anchored to the letters
# above so `[cite:x1234]` is a non-match rather than an unresolvable ref —
# the distinction matters when deciding whether to blame the model or the DB.
REF_RE = re.compile(r"\b([evmd])([0-9a-f]{5,10})\b")
# A document citation carries where in the document, after the ref:
# `[cite:d3f9a1#p12 "the quoted words"]`. The ref is still group 1; the page
# and quote are the frontend's to use (ui/DocPage.jsx), not something to look up.
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

    The recording is the whole point: `resolve()` is a lookup on this table,
    so a ref that was never written here is indistinguishable from one the
    model invented. That is deliberate — see the module docstring — but it
    means this must be called on the way *out*, as the row is rendered into
    the model's context, never lazily when the citation comes back.
    """
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
            # Taken by someone else. Lengthen rather than steal: the ref
            # already in a past transcript keeps pointing where it pointed.
        # Ten hex digits deep and still colliding is not a thing that happens;
        # returning the long form unrecorded is better than raising inside a
        # brief-line render.
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
        # A recurring event keeps its id across days, and a cache re-sync can
        # drop the exact day a past ref was minted on. Any surviving instance
        # answers "what was this" better than a dead pill does.
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

    A ref that *was* handed out but whose row has since left the cache comes
    back `found: False` with the label it was minted with, rather than None.
    The two cases read differently on screen and should: one is "the console
    made that up", the other is "that email is no longer in the cache".
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
