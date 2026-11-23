"""Subagents (and other CLI tasks) at work, as the console shows them.

The Agent tool now runs its subagent in the background by default: the call
comes back at once ("launched"), the parent's turn can end, and the subagent
keeps going. Its own tool calls don't ride the parent's stream, so the only
signs of it are the CLI's task lifecycle messages:

  task_started       {task_id, description, tool_use_id, task_type}
  task_progress      {task_id, description (= what it's doing now), last_tool_name,
                      usage{total_tokens, tool_uses, duration_ms}}
  task_notification  {task_id, status, summary, usage}   (finished)
  task_updated       {task_id, patch{status, ...}}       (sometimes the only
                                                         word that it finished)

Read off the raw `data` dict rather than the SDK's typed subclasses, so an
older or newer SDK that types them differently still feeds the strip.

`apply` folds one message into `agents` (task_id -> row) and says whether
anything changed; the console then sends the whole list (`rows`), like it does
for background `!` commands, so a reconnecting page gets it whole too.
"""

from __future__ import annotations

import time
from typing import Any

TERMINAL = frozenset({"completed", "failed", "stopped", "killed"})
# Finished rows kept for the view pane, newest last; running rows are never dropped.
KEEP_FINISHED = 6


def apply(agents: dict[str, dict], subtype: str, data: dict[str, Any]) -> bool:
    task_id = data.get("task_id")
    if not task_id or subtype not in ("task_started", "task_progress",
                                      "task_notification", "task_updated"):
        return False
    row = agents.get(task_id)
    if row is None:
        if subtype == "task_updated" and not (data.get("patch") or {}).get("status"):
            return False
        row = agents[task_id] = {
            "id": task_id, "description": "", "kind": None, "tool_use_id": None,
            "status": "running", "activity": None, "last_tool": None, "tokens": 0, "tool_uses": 0,
            "started": time.time(), "duration_ms": None, "summary": None,
        }
    before = dict(row)
    # task_started's description is what it was sent to do; task_progress
    # reuses the field for what it's doing right now ("Running echo hi").
    if data.get("description"):
        if subtype == "task_progress" and row["description"]:
            row["activity"] = str(data["description"])[:200]
        else:
            row["description"] = str(data["description"])[:200]
    if data.get("task_type"):
        row["kind"] = data["task_type"]
    if data.get("tool_use_id"):
        row["tool_use_id"] = data["tool_use_id"]
    usage = data.get("usage") or {}
    if usage:
        row["tokens"] = int(usage.get("total_tokens") or row["tokens"])
        row["tool_uses"] = int(usage.get("tool_uses") or row["tool_uses"])
        if usage.get("duration_ms") is not None:
            row["duration_ms"] = int(usage["duration_ms"])
    if data.get("last_tool_name"):
        row["last_tool"] = data["last_tool_name"]

    if subtype == "task_notification":
        row["status"] = data.get("status") or "completed"
        if data.get("summary"):
            row["summary"] = str(data["summary"])[:400]
    elif subtype == "task_updated":
        status = (data.get("patch") or {}).get("status")
        if status in TERMINAL or status == "running":
            row["status"] = status
    if row["status"] in TERMINAL and row["duration_ms"] is None:
        row["duration_ms"] = int((time.time() - row["started"]) * 1000)

    _trim(agents)
    return row != before


def _trim(agents: dict[str, dict]) -> None:
    done = [k for k, r in agents.items() if r["status"] in TERMINAL]
    for k in done[:-KEEP_FINISHED]:
        del agents[k]


def rows(agents: dict[str, dict]) -> list[dict]:
    return list(agents.values())


def running(agents: dict[str, dict]) -> int:
    return sum(1 for r in agents.values() if r["status"] not in TERMINAL)
