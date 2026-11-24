"""Reply drafter: candidate replies for a thread waiting on you. Not a provider; called
only from the People router, never the poller. **Never sends**: output is text to copy.
Design: DESIGN.md → People (`app/reply_draft.py`).
"""

import asyncio
import json
import re

from . import agent, db
from .config import REPLY_DRAFT_MODEL

SYSTEM = (
    "You draft candidate replies to personal messages, for the owner of the "
    "conversation to pick from and edit. You are given the recent turns of one "
    "thread, oldest first, each marked `me:` (the owner) or `them:`.\n"
    "Write replies the owner could plausibly have typed:\n"
    "- Match the register of the thread — if they text in lowercase fragments, "
    "so do you; if it's a formal email, write a formal line.\n"
    "- Keep them short. A text reply is usually under 15 words.\n"
    "- Answer what was actually asked. If they asked a question you cannot "
    "answer from the thread (a date, a preference, a decision only the owner "
    "knows), write a reply that asks or defers rather than inventing a fact.\n"
    "- Never invent plans, times, places, or opinions that aren't in the thread.\n"
    "- Offer genuinely different options, not three phrasings of one reply: "
    "e.g. a yes, a defer, and a question.\n"
    "Reply with ONLY a JSON array of strings, no prose, no keys."
)


def _parse(text: str) -> list[str]:
    """Model reply -> list of draft strings; tolerates ```json fences."""
    m = re.search(r"\[.*\]", text, flags=re.S)
    if not m:
        raise ValueError(f"no JSON array in model reply: {text[:200]!r}")
    out = []
    for item in json.loads(m.group(0)):
        if isinstance(item, str) and item.strip():
            out.append(item.strip()[:500])
        elif isinstance(item, dict):  # a stray {"reply": "..."} shape
            for v in item.values():
                if isinstance(v, str) and v.strip():
                    out.append(v.strip()[:500])
                    break
    return out


def message_thread(con, chat_guid: str, limit: int) -> list[dict]:
    """Recent turns of one iMessage thread, oldest first."""
    rows = con.execute(
        "SELECT ts, is_from_me, text FROM messages"
        " WHERE chat_guid = ? AND is_reaction = 0 AND TRIM(COALESCE(text,'')) != ''"
        " ORDER BY ts DESC LIMIT ?", (chat_guid, limit)).fetchall()
    return [{"who": "me" if r["is_from_me"] else "them", "when": r["ts"],
             "text": r["text"]} for r in reversed(rows)]


def email_thread(con, thread_id: str, limit: int) -> list[dict]:
    """Recent messages of one email thread, oldest first. Snippets only (bodies are never stored)."""
    rows = con.execute(
        "SELECT received_at, is_sent, subject, snippet FROM emails"
        " WHERE thread_id = ? ORDER BY received_at DESC LIMIT ?",
        (thread_id, limit)).fetchall()
    return [{"who": "me" if r["is_sent"] else "them", "when": r["received_at"],
             "text": f'{r["subject"] or ""} — {r["snippet"] or ""}'.strip(" —")}
            for r in reversed(rows)]


def drafts_for(*, kind: str, name: str | None, chat_guid: str | None = None,
               thread_id: str | None = None, limit: int, count: int) -> dict:
    """Candidate replies for one waiting thread -> `{"drafts": [...], "turns": N}`.
    An empty thread returns no drafts rather than inviting the model to invent one."""
    with db.session() as con:
        if kind == "message" and chat_guid:
            turns = message_thread(con, chat_guid, limit)
        elif kind == "email" and thread_id:
            turns = email_thread(con, thread_id, limit)
        else:
            turns = []

    if not turns:
        return {"drafts": [], "turns": 0}

    who = name or "them"
    transcript = "\n".join(f'{t["who"]}: {t["text"]}' for t in turns)
    prompt = (f"Thread with {who} ({kind}), oldest first:\n\n{transcript}\n\n"
              f"The last message is theirs and is waiting on a reply. "
              f"Give {count} candidate replies.")
    drafts = _parse(asyncio.run(agent.ask(prompt, model=REPLY_DRAFT_MODEL, system=SYSTEM)))
    return {"drafts": drafts[:count], "turns": len(turns)}
