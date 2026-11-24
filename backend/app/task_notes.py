"""The CLI's `<task-notification>` messages (injected raw XML user messages when a
background agent or shell finishes, fails or is cut off), as a row a person can
read. `parse` splits them out for sessions.session_transcript and the live
stream; the rest of the message stays text. Format sample: DESIGN.md → `app/task_notes.py`.
"""

from __future__ import annotations

import html
import re

_BLOCK = re.compile(r"<task-notification>(.*?)</task-notification>", re.S)
_ENDED = "didn't finish before the previous session ended"
RESULT_CHARS = 4000


def _tags(body: str, tag: str) -> list[str]:
    return [html.unescape(m.strip())
            for m in re.findall(rf"<{tag}>(.*?)</{tag}>", body, re.S)]


def _one(body: str, tag: str) -> str | None:
    found = _tags(body, tag)
    return found[0] if found else None


def title(status: str | None, summary: str) -> str:
    """The row's one line: a cut-off run names what was cut off; the rest keep the CLI's summary."""
    if _ENDED in summary:
        names = re.findall(r'"([^"]+)"', summary.split(":", 1)[1]) if ":" in summary else []
        n = re.match(r"\s*(\d+)\s+background agents", summary)
        if n:
            k = int(n.group(1))
            what = f"{k} background agent{'s' if k != 1 else ''}"
        elif summary.lower().startswith("background shell"):
            what = "A background command"
        elif summary.lower().startswith("agent"):
            what = "A background agent"
        else:
            what = "A background task"
        return f"{what} stopped when the app quit" + (f": {', '.join(names)}" if names else "")
    return summary or f"A background task {status or 'finished'}"


def from_system(data: dict) -> dict:
    """The same row from the live stream's `task_notification` system message
    (task_id, status, summary, output_file, tool_use_id)."""
    status = str(data.get("status") or "").lower() or None
    summary = str(data.get("summary") or "")
    tid = data.get("task_id")
    return {
        "status": status, "title": title(status, summary), "summary": summary,
        "note": data.get("note"), "result": None,
        "task_ids": [tid] if tid else [],
        "tool_use_id": data.get("tool_use_id"),
        "output_file": data.get("output_file"),
    }


def text_of(content) -> str:
    """A message's text, from a string or a list of blocks (dicts or SDK
    TextBlocks)."""
    if isinstance(content, str):
        return content
    parts = []
    for b in content or []:
        t = b.get("text") if isinstance(b, dict) else getattr(b, "text", None)
        if isinstance(t, str):
            parts.append(t)
    return "\n".join(parts)


def parse(text: str) -> tuple[list[dict], str]:
    """(notes, the text with the notifications taken out)."""
    if "<task-notification>" not in text:
        return [], text
    notes = []
    for body in _BLOCK.findall(text):
        status = (_one(body, "status") or "").lower() or None
        summary = _one(body, "summary") or ""
        result = _one(body, "result")
        notes.append({
            "status": status,
            "title": title(status, summary),
            "summary": summary,
            "note": _one(body, "note"),
            "result": result[:RESULT_CHARS] if result else None,
            "task_ids": _tags(body, "task-id"),
            "tool_use_id": _one(body, "tool-use-id"),
            "output_file": _one(body, "output-file"),
        })
    return notes, _BLOCK.sub("", text).strip()
