"""A past session, made small: a digest to attach as context, and a title.

Both read the transcript through `sessions.session_transcript`, the same
replay the console's back-scroll uses, so what's summarised is what you'd
see scrolling back.

The digest is extractive on purpose: what you asked, in
order, the files it changed and the last thing it said, cut to a budget. A
model-written summary would be a claim about the session, costs a call on
every attach, and is one more thing to be wrong; this is a record, and the
tab reading it can open the session by id if it needs the rest.

The title is the one place a model is asked (#102): a first message like
"ok so" names nothing, and only a reading of the whole session can do better.
One Haiku turn over the digest, asked for when you click, never in bulk.
"""

from __future__ import annotations

from pathlib import Path

from . import agent, sessions
from .config import TAB_TITLE_MODEL

DIGEST_CHARS = 6000
ASK_CHARS = 400
ASKS_KEPT = 14
LAST_REPLY_CHARS = 1500
FILES_KEPT = 30
WRITE_TOOLS = ("Edit", "Write", "MultiEdit", "NotebookEdit")
TITLE_WORDS = 7


def _clip(text: str, n: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= n else f"{text[:n - 1]}…"


def digest(session_id: str, cwd: Path | None = None) -> dict | None:
    """{id, title, asks, asked, files, last_reply, text}, or None when the
    session has no transcript (wrong id, wrong project, or never written)."""
    entries = sessions.session_transcript(session_id, cwd=cwd)
    asks = [e["text"] for e in entries if e["kind"] == "user" and e.get("text")]
    if not asks:
        return None
    files: list[str] = []
    for e in entries:
        if e["kind"] == "tool" and e.get("name") in WRITE_TOOLS and e.get("detail"):
            path = str(e["detail"]).split(" ")[0]
            if path not in files:
                files.append(path)
    replies = [e["text"] for e in entries if e["kind"] == "text"]
    last = _clip(replies[-1], LAST_REPLY_CHARS) if replies else ""
    # The first ask frames the session and the newest are where it ended;
    # past ASKS_KEPT the middle goes, and the digest says how many.
    kept = asks if len(asks) <= ASKS_KEPT else (
        asks[:4] + [None] + asks[-(ASKS_KEPT - 4):])
    lines = []
    for a in kept:
        lines.append(f"  … {len(asks) - ASKS_KEPT} more messages …" if a is None
                     else f"- {_clip(a, ASK_CHARS)}")
    title = _clip(asks[0], 80)
    parts = [f"Session {session_id} — \"{title}\" ({len(asks)} message"
             f"{'s' if len(asks) != 1 else ''} from the user)",
             "What the user asked, in order:", *lines]
    if files:
        shown = files[:FILES_KEPT]
        more = f" (+{len(files) - FILES_KEPT} more)" if len(files) > FILES_KEPT else ""
        parts += [f"Files it changed: {', '.join(shown)}{more}"]
    if last:
        parts += ["Its last reply:", last]
    text = "\n".join(parts)
    if len(text) > DIGEST_CHARS:
        text = f"{text[:DIGEST_CHARS - 40]}\n… (digest cut at {DIGEST_CHARS} characters)"
    return {"id": session_id, "title": title, "asked": len(asks), "files": files,
            "last_reply": last, "text": text}


TITLE_PROMPT = (
    "Below is a digest of a chat session between a user and a coding/life "
    f"assistant. Name it in at most {TITLE_WORDS} words, the way a tab or a "
    "browser history entry would be named: what the session is about, not how "
    "it started. No quotes, no trailing period, sentence case.\n\n<digest>\n{digest}\n</digest>")


def clean_title(raw: str) -> str:
    """The model's line, as a tab title: one line, unquoted, no period."""
    line = next((x for x in (raw or "").splitlines() if x.strip()), "")
    line = line.strip().strip("*#").strip()
    if line.lower().startswith("title:"):
        line = line[6:]
    line = line.strip().strip("\"'`*“”").strip().rstrip(".")
    words = line.split()
    return " ".join(words[:TITLE_WORDS + 2])[:80]


async def title(session_id: str, cwd: Path | None = None) -> str | None:
    d = digest(session_id, cwd)
    if d is None:
        return None
    with agent.interactive():
        raw = await agent.ask(TITLE_PROMPT.format(digest=d["text"]),
                              model=TAB_TITLE_MODEL,
                              thinking={"type": "disabled"})
    return clean_title(raw) or None
