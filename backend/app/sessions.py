"""Claude Code sessions for this vault: JSONL under ~/.claude/projects/<munged-cwd>/.

The console resumes via the SDK's `resume` against the same store, so terminal and browser sessions
are one list. Read-only; the transcript is the source of truth.
"""

import json
import re
import time
from pathlib import Path

from . import task_notes
from .config import VAULT_PATH

CLAUDE_PROJECTS = Path.home() / ".claude" / "projects"

# The wrapper console.shell_prompt sends for a `!` command, read back into what the user ran.
_SHELL_TURN = re.compile(
    r"The user ran this themselves with a ! command in the console\.\n"
    r"<bash-input>(?P<command>.*?)</bash-input>\n"
    r"<bash-stdout>(?P<output>.*)</bash-stdout>\n"
    r"<bash-exit-code>(?P<code>-?\d+)</bash-exit-code>\s*", re.S)


def _shell_turn(text: str) -> dict | None:
    m = _SHELL_TURN.fullmatch(text)
    return m and {"command": m["command"], "output": m["output"], "code": int(m["code"])}


def project_dir(cwd: Path | None = None) -> Path:
    # Claude Code names the dir by replacing path separators and dots with '-'.
    munged = str(cwd or VAULT_PATH).replace("/", "-").replace(".", "-")
    return CLAUDE_PROJECTS / munged


def _session_dirs(cwd: Path | None = None) -> list[Path]:
    """A project's session dirs: its own, plus one per dev-tab worktree (sessions file under cwd)."""
    main = project_dir(cwd) if cwd else project_dir()
    dirs = [main] if main.exists() else []
    if cwd is not None:
        prefix = main.name + "--claude-worktrees-tab-"
        try:
            dirs += [d for d in CLAUDE_PROJECTS.iterdir()
                     if d.name.startswith(prefix) and d.is_dir()]
        except OSError:
            pass
    return dirs


def _session_file(session_id: str, cwd: Path | None = None) -> Path:
    for d in _session_dirs(cwd):
        if (d / f"{session_id}.jsonl").exists():
            return d / f"{session_id}.jsonl"
    return (project_dir(cwd) if cwd else project_dir()) / f"{session_id}.jsonl"


def list_sessions(limit: int | None = None, cwd: Path | None = None) -> list[dict]:
    """Newest first. `limit=None` lists every session (a few dozen a month; previews stop at the first user message)."""
    files = sorted(((p.stat().st_mtime, p) for d in _session_dirs(cwd)
                    for p in d.glob("*.jsonl")), reverse=True)
    if limit is not None:
        files = files[:limit]
    sessions = []
    for mtime, f in files:
        preview = _first_user_text(f)
        sessions.append({
            "id": f.stem,
            "mtime": mtime,
            "preview": preview[:120] if preview else "(no preview)",
        })
    return sessions


def text_blocks(content) -> str:
    """Concatenate the text blocks of a message's content (str or block list),
    ignoring tool_use/tool_result blocks."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            b.get("text", "") for b in content
            if isinstance(b, dict) and b.get("type") == "text").strip()
    return ""


def strip_context(text: str) -> str:
    """Drop the <attached_context>…</attached_context> preamble the console
    prepends to a user turn, leaving just what the person typed."""
    marker = "</attached_context>"
    idx = text.find(marker)
    return text[idx + len(marker):].strip() if idx != -1 else text


# One formatter for tool calls, shared by the live stream and this replay.
# Ordered by how much the argument says; the tail is the vault MCP tools' vocabulary
# (`title`, `summary`, `subject`), else the fallback branch prints `title=...`.
_DETAIL_KEYS = ("command", "script", "pattern", "query", "url", "file_path",
                "notebook_path", "path", "prompt", "description",
                "summary", "title", "subject", "name")
_PATH_KEYS = ("file_path", "notebook_path", "path")


def _short_path(text: str) -> str:
    """Drop the shared vault/home prefix and keep the last two segments."""
    for prefix in (f"{VAULT_PATH}/", f"{Path.home()}/"):
        if text.startswith(prefix):
            text = text[len(prefix):]
            break
    parts = [p for p in text.split("/") if p]
    return "/".join(parts[-2:]) if len(parts) > 2 else text


def result_text(content) -> str:
    """A tool result's text with its line breaks kept (str or block list)."""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        return "\n".join(b.get("text", "") for b in content
                         if isinstance(b, dict) and b.get("type") == "text").strip()
    return ""


def tool_desc(inp, width: int = 140) -> str:
    """The call's own one-line account of itself (Bash's `description`), or ""; the console leads with it."""
    if not isinstance(inp, dict) or not isinstance(inp.get("description"), str):
        return ""
    out = " ".join(inp["description"].split())
    return out[:width] + "…" if len(out) > width else out


def tool_detail(inp, width: int = 110) -> str:
    """The one argument worth reading, plus where it applies (Grep's pattern
    and its directory), squeezed onto a single line."""
    if not isinstance(inp, dict):
        return ""
    bits: list[str] = []
    for key in _DETAIL_KEYS:
        if inp.get(key):
            raw = " ".join(str(inp[key]).split())
            bits.append(_short_path(raw) if key in _PATH_KEYS else raw)
            break
    for key in _PATH_KEYS:                 # Grep/Glob: the pattern, then where
        if inp.get(key):
            where = _short_path(" ".join(str(inp[key]).split()))
            if where not in bits:
                bits.append(where)
            break
    if not bits:                           # unknown tool — say something anyway
        bits = [f"{k}={' '.join(str(v).split())[:40]}"
                for k, v in list(inp.items())[:3]]
    out = " ".join(bits)
    return out[:width] + "…" if len(out) > width else out


def rewind_point(session_id: str, message_uuid: str,
                 cwd: Path | None = None) -> dict:
    """Where to resume so the conversation ends just before a message: its parent in the transcript.

    `--resume-session-at X` keeps everything up to and including X, so the cut is one record
    earlier. `at` None: the message was first (a new session). `found` False: not in this session."""
    if any(s in session_id for s in ("/", "\\", "..")):
        return {"found": False, "at": None}
    path = _session_file(session_id, cwd)
    try:
        with path.open(encoding="utf-8") as fh:
            for line in fh:
                if message_uuid not in line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get("uuid") == message_uuid:
                    return {"found": True, "at": rec.get("parentUuid")}
    except OSError:
        pass
    return {"found": False, "at": None}


def transcript_size(session_id: str | None, cwd: Path | None = None) -> int | None:
    """Bytes in a session's transcript now (console_link's cut point). 0 if not written yet."""
    if not session_id or "/" in session_id or ".." in session_id:
        return None
    try:
        return _session_file(session_id, cwd).stat().st_size
    except OSError:
        return 0


# The delegate tools (console_tools.DELEGATE_TOOLS), as a call's name ends.
DELEGATE_TOOLS = ("__delegate", "__fan_out", "__shortlist")


def session_transcript(session_id: str, limit: int | None = None,
                       cwd: Path | None = None, until: int | None = None) -> list[dict]:
    """Replay a past session's JSONL as console entries (the SDK's `resume` restores context, not the log).
    `limit=None` replays the whole session; a limit keeps the last N entries."""
    # session_id is a bare uuid stem; reject anything with path separators
    if "/" in session_id or "\\" in session_id or ".." in session_id:
        return []
    path = _session_file(session_id, cwd)
    if not path.exists():
        return []
    out: list[dict] = []
    scripts: dict[str, dict] = {}     # run_script entries awaiting their result
    try:
        with path.open(encoding="utf-8") as fh:
            # `until`: bytes (a file size), not characters.
            lines = (fh.buffer.read(until).decode("utf-8", "replace").splitlines()
                     if until is not None else fh)
            for line in lines:
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                # Only conversation records: an `api-request-blob` also carries a user-role `message`.
                if rec.get("type") not in (None, "user", "assistant"):
                    continue
                msg = rec.get("message") or {}
                role = msg.get("role") or rec.get("type")
                content = msg.get("content")
                if role == "user":
                    for b in content if isinstance(content, list) else []:
                        if (isinstance(b, dict) and b.get("type") == "tool_result"
                                and b.get("tool_use_id") in scripts):
                            scripts.pop(b["tool_use_id"])["result"] = (
                                result_text(b.get("content"))[:8000])
                    # A background task notification is the CLI talking: a compact row (task_notes).
                    notes, raw = task_notes.parse(text_blocks(content))
                    out.extend({"kind": "tasknote", **n} for n in notes)
                    text = strip_context(raw)
                    shell = _shell_turn(text) if text else None
                    if text:
                        # The uuid is a dev tab's rewind checkpoint.
                        out.append({"kind": "user",
                                    "text": f"!{shell['command']}" if shell else text,
                                    "checkpoint": rec.get("uuid")})
                    if shell:
                        out.append({"kind": "shell", "output": shell["output"],
                                    "code": shell["code"]})
                elif role == "assistant":
                    # Which model wrote it; the CLI's own error turns say "<synthetic>", which is no model.
                    model = msg.get("model")
                    by = ({"model": model} if isinstance(model, str) and model
                          and not model.startswith("<") else {})
                    if isinstance(content, str):
                        if content.strip():
                            out.append({"kind": "text", "text": content, **by})
                        continue
                    for b in content if isinstance(content, list) else []:
                        if not isinstance(b, dict):
                            continue
                        if b.get("type") == "text" and b.get("text", "").strip():
                            out.append({"kind": "text", "text": b["text"], **by})
                        elif b.get("type") == "tool_use":
                            # id: subagent_run finds an Agent call's subagent by it.
                            entry = {"kind": "tool", "name": b.get("name", "tool"),
                                     "detail": tool_detail(b.get("input")),
                                     "desc": tool_desc(b.get("input")),
                                     "id": b.get("id")}
                            # raw: an Agent call's card shows its input (prompt, model, type).
                            if entry["name"] in ("Agent", "Task"):
                                entry["raw"] = json.dumps(b.get("input"), default=str)[:2000]
                            # Scripts (console_code) and delegate rows are shown whole from raw.
                            if entry["name"].endswith(("__run_script", *DELEGATE_TOOLS)):
                                entry["raw"] = json.dumps(b.get("input"), default=str)[:25_000]
                                scripts[b.get("id")] = entry
                            out.append(entry)
    except OSError:
        return []
    return out if limit is None else out[-limit:]


SUBAGENT_CALLS = 300      # newest kept; a long research agent makes hundreds


def subagent_run(session_id: str, tool_use_id: str,
                 cwd: Path | None = None) -> dict | None:
    """What the subagent one Agent call started has done: its tool calls and its last text.

    Subagent calls never reach the parent's stream; the CLI writes `<session>/subagents/agent-<id>.jsonl`
    with a `.meta.json` naming the Agent call (`toolUseId`). None when not on disk yet (the meta lands
    a moment after the call; the page asks again)."""
    if any(c in session_id + tool_use_id for c in "/\\") or ".." in session_id:
        return None
    sub = _session_file(session_id, cwd).with_suffix("") / "subagents"
    try:
        metas = list(sub.glob("agent-*.meta.json"))
    except OSError:
        return None
    for meta_path in metas:
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if meta.get("toolUseId") == tool_use_id:
            break
    else:
        return None
    calls: list[dict] = []
    by_id: dict[str, dict] = {}
    last_text = ""
    path = sub / meta_path.name.replace(".meta.json", ".jsonl")
    try:
        # Time since last write: a subagent that died with its session is never marked finished.
        idle = max(0, int(time.time() - path.stat().st_mtime))
    except OSError:
        idle = None
    try:
        with path.open(encoding="utf-8") as fh:
            for line in fh:
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get("type") not in ("user", "assistant"):
                    continue
                content = (rec.get("message") or {}).get("content")
                for b in content if isinstance(content, list) else []:
                    if not isinstance(b, dict):
                        continue
                    if b.get("type") == "tool_use":
                        call = {"id": b.get("id"), "name": b.get("name", "tool"),
                                "detail": tool_detail(b.get("input")),
                                "desc": tool_desc(b.get("input")), "state": "live"}
                        calls.append(call)
                        by_id[call["id"]] = call
                    elif b.get("type") == "tool_result" and b.get("tool_use_id") in by_id:
                        by_id[b["tool_use_id"]]["state"] = (
                            "error" if b.get("is_error") else "done")
                    elif (b.get("type") == "text" and rec.get("type") == "assistant"
                          and b.get("text", "").strip()):
                        last_text = b["text"]
    except OSError:
        pass
    return {
        "agent_type": meta.get("agentType"), "model": meta.get("model"),
        "description": meta.get("description"),
        "finished": bool(meta.get("finished")), "idle_s": idle,
        "total": len(calls), "calls": calls[-SUBAGENT_CALLS:],
        "report": last_text[:4000] if meta.get("finished") else "",
        "said": "" if meta.get("finished") else last_text[:300],
    }


def _snippet(text: str, q: str, width: int = 90) -> str:
    """One line of context centred on the first hit, elided where cut."""
    flat = " ".join(text.split())
    idx = flat.lower().find(q)
    if idx == -1:
        return flat[:width]
    start = max(0, idx - width // 3)
    end = min(len(flat), start + width)
    return ("…" if start else "") + flat[start:end] + ("…" if end < len(flat) else "")


def search_sessions(query: str, limit: int = 50,
                    per_session: int = 3, cwd: Path | None = None) -> list[dict]:
    """Case-insensitive substring search across every session, newest first.

    A hit is a listing row plus matching snippets; tool calls are searched by name and detail.
    `limit` caps matching sessions; `per_session` caps snippets, `match_count` is the true total."""
    q = query.strip().lower()
    if not q:
        return []
    hits: list[dict] = []
    for row in list_sessions(cwd=cwd):
        matches = []
        for e in session_transcript(row["id"], cwd=cwd):
            hay = (f"{e.get('name', '')} {e.get('detail', '')}"
                   if e["kind"] == "tool" else e.get("text") or e.get("output") or "")
            if q in hay.lower():
                matches.append({"kind": e["kind"], "snippet": _snippet(hay, q)})
        if matches:
            hits.append({**row, "match_count": len(matches),
                         "matches": matches[:per_session]})
            if len(hits) >= limit:
                break
    return hits


def _first_user_text(path: Path) -> str | None:
    """The first thing the user actually typed (preview). Strips <attached_context> like the transcript does;
    a message that is only context is skipped for the next."""
    try:
        with path.open(encoding="utf-8") as fh:
            for line in fh:
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get("type") not in (None, "user", "assistant"):
                    continue
                msg = rec.get("message") or {}
                if rec.get("type") == "user" or msg.get("role") == "user":
                    text = strip_context(text_blocks(msg.get("content")))
                    if text:
                        shell = _shell_turn(text)
                        return " ".join((f"!{shell['command']}" if shell else text).split())
    except OSError:
        pass
    return None
