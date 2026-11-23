"""A dev tab's task list — the one Claude Code keeps with TaskCreate/TaskUpdate.

Read off the CLI's own files (`~/.claude/tasks/<session id>/<n>.json`, one
per task) rather than rebuilt from the tool calls: TaskUpdate carries only
what changed, so a replay would have to model every field the CLI merges,
and a resumed session's list was written by calls this socket never saw.
The files are the CLI's state, so this is read-only, like the transcripts.
"""

import json
import re
from pathlib import Path

TASKS_DIR = Path.home() / ".claude" / "tasks"

_SESSION = re.compile(r"[0-9a-fA-F-]{8,64}")
STATUSES = ("pending", "in_progress", "completed")


def _key(path: Path):
    return (0, int(path.stem)) if path.stem.isdigit() else (1, path.stem)


def read_tasks(session_id: str | None, root: Path | None = None) -> list[dict]:
    """The session's tasks in creation order; deleted ones left out."""
    if not session_id or not _SESSION.fullmatch(session_id):
        return []
    d = (root or TASKS_DIR) / session_id
    if not d.is_dir():
        return []
    out = []
    for f in sorted(d.glob("*.json"), key=_key):
        try:
            t = json.loads(f.read_text())
        except (OSError, ValueError):
            continue    # mid-write; the next change re-reads it
        if not isinstance(t, dict) or t.get("status") not in STATUSES:
            continue
        out.append({
            "id": str(t.get("id") or f.stem),
            "subject": str(t.get("subject") or ""),
            "active_form": str(t.get("activeForm") or ""),
            "status": t["status"],
            "blocked_by": [str(b) for b in t.get("blockedBy") or []],
        })
    return out
