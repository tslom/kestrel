"""Computer use for a normal tab: a real `claude` on a pseudo-terminal, as a job.

An SDK session has 0 computer-use tools (24 on a pty, measured), so `use_computer`
hands the task to a `claude` Kestrel runs itself; one job at a time (the CLI holds a
single computer-use lock). Nothing reads the screen: the job's transcript is the
answer. Only the CLI's own "allow this app?" prompt needs the user, on the live
terminal. Design: DESIGN.md → `app/console_terminal.py`.

Frames to the starting tab: computer_start {id}, computer_prompt {id, show}
(it needs you on the terminal, or not), computer_end {id}. /ws/console/computer?id=
is binary both ways plus JSON {"type": "resize", rows, cols}; it attaches, never starts.
"""
import asyncio
import fcntl
import json
import os
import pty
import re
import signal
import struct
import subprocess
import termios
import time
from pathlib import Path
from typing import Any, Awaitable, Callable
from uuid import uuid4

from claude_agent_sdk import ToolResultBlock
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from . import console, console_profiles, model_catalog, sessions
from .config import (COMPUTER_DIR, COMPUTER_JOB_LIMIT_S, COMPUTER_MODEL,
                     COMPUTER_SETUP_LIMIT_S, CONSOLE_CLI_PATH)

router = APIRouter()

TAIL_S = 0.4
QUIT_WAIT_S = 2
BACKLOG_BYTES = 256 * 1024      # what a terminal attaching late gets to catch up
QUIET_S = 7                     # transcript silent this long: bring up the terminal
ROWS, COLS = 40, 110
# Inherited, these turn transcript saving off in the child, and the transcript is all we read.
_DROP_ENV = {"ANTHROPIC_API_KEY", "CLAUDE_CODE_CHILD_SESSION", "CLAUDECODE"}
Send = Callable[[dict], Awaitable[None]]


class TranscriptEvents:
    """One session's transcript lines, as console frames.

    Streaming logs a message several times, so calls and results are deduped
    by id. A turn opens on what the user typed, and closes on `end_turn`.
    """

    def __init__(self) -> None:
        self.seen: set[str] = set()
        self.running = False
        self.last_text = ""

    def _begin(self, out: list[dict]) -> None:
        if not self.running:
            self.running = True
            out.append({"type": "turn_start"})

    def _end(self, out: list[dict], error: bool = False) -> None:
        if self.running:
            self.running = False
            out.append({"type": "turn_end", "error": error})

    def feed(self, line: str) -> list[dict]:
        try:
            entry = json.loads(line)
        except ValueError:
            return []
        if not isinstance(entry, dict) or entry.get("isSidechain") or entry.get("isMeta"):
            return []
        if entry.get("type") == "system" and entry.get("subtype") == "turn_duration":
            out: list[dict] = []
            self._end(out)
            return out
        message = entry.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        out: list[dict] = []
        if entry.get("type") == "user":
            self._user(content, out)
        elif entry.get("type") == "assistant" and isinstance(content, list):
            self._assistant(message, content, out)
        return out

    def _user(self, content: Any, out: list[dict]) -> None:
        blocks = content if isinstance(content, list) else [{"type": "text", "text": content}]
        for block in blocks:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_result":
                call = str(block.get("tool_use_id") or "")
                if f"r{call}" in self.seen:
                    continue
                self.seen.add(f"r{call}")
                result = ToolResultBlock(tool_use_id=call, content=block.get("content"),
                                         is_error=bool(block.get("is_error")))
                image = console.result_image(result)
                out.append({"type": "tool_result", "id": call, "is_error": bool(result.is_error),
                            **({"image": image} if image else {})})
            elif block.get("type") == "text":
                text = str(block.get("text") or "").strip()
                if text.startswith("[Request interrupted"):
                    self._end(out)
                elif text and not text.startswith("<"):      # <command-name> is a slash command
                    self._begin(out)

    def _assistant(self, message: dict, content: list, out: list[dict]) -> None:
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text" and str(block.get("text") or "").strip():
                self.last_text = str(block["text"]).strip()
            if block.get("type") != "tool_use":
                continue
            call = str(block.get("id") or "")
            if f"u{call}" in self.seen:
                continue
            self.seen.add(f"u{call}")
            self._begin(out)
            tool_input = block.get("input")
            out.append({"type": "tool_use", "id": call, "name": str(block.get("name") or ""),
                        "detail": sessions.tool_detail(tool_input),
                        "desc": sessions.tool_desc(tool_input), "raw": json.dumps(tool_input)})
        # Each logged block carries the final stop_reason, so a thinking block says end_turn
        # before the answer exists: end only on a text block.
        said = any(isinstance(b, dict) and b.get("type") == "text" for b in content)
        if said and message.get("stop_reason") == "end_turn":
            self._end(out)


def transcript_path(cwd: Path, session_id: str) -> Path:
    """Every non-alphanumeric becomes a dash, of the path with symlinks resolved
    (/tmp is /private/tmp here), or a folder with a space loses its transcript."""
    munged = re.sub(r"[^A-Za-z0-9]", "-", str(cwd.resolve()))
    return console_profiles.CLAUDE_PROJECTS / munged / f"{session_id}.jsonl"


def computer_enabled() -> bool:
    """Whether `/mcp` has computer-use on for the job folder. Only ever read:
    the CLI rewrites ~/.claude.json constantly and it holds the login."""
    try:
        config = json.loads((Path.home() / ".claude.json").read_text())
        project = config.get("projects", {}).get(str(COMPUTER_DIR.resolve()), {})
        return "computer-use" in project.get("enabledMcpServers", [])
    except (OSError, ValueError, AttributeError):
        return False


def command(session_id: str, task: str | None) -> list[str]:
    cli = str(CONSOLE_CLI_PATH) if CONSOLE_CLI_PATH.exists() else "claude"
    # --model, never /model (it writes the user's global default); swapped if the account can't run it.
    model = model_catalog.pick(COMPUTER_MODEL).model or COMPUTER_MODEL
    argv = [cli, "--session-id", session_id, "--model", model]
    return [*argv, "--", task] if task else argv


def set_size(fd: int, rows: int, cols: int) -> None:
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


def parse_size(value: Any, fallback: int) -> int:
    return value if isinstance(value, int) and 1 <= value <= 1000 else fallback


def spawn(argv: list[str], cwd: Path, rows: int, cols: int,
          env_extra: dict[str, str] | None = None) -> tuple[subprocess.Popen, int]:
    """`argv` on a new pty, as the session leader. Returns (process, master fd).
    `env_extra` overrides the terminal defaults (console.run_shell's `!`)."""
    master, slave = pty.openpty()
    set_size(slave, rows, cols)
    env = {k: v for k, v in os.environ.items() if k not in _DROP_ENV}
    env.update(TERM="xterm-256color", COLORTERM="truecolor")
    if env_extra:
        env.update(env_extra)
    try:
        proc = subprocess.Popen(
            argv, cwd=cwd, env=env, stdin=slave, stdout=slave, stderr=slave,
            start_new_session=True,
            # Without a controlling terminal, ⌃C and a window resize reach nothing.
            preexec_fn=lambda: fcntl.ioctl(0, termios.TIOCSCTTY, 0))
    except BaseException:
        os.close(master)
        raise
    finally:
        os.close(slave)
    return proc, master


class Job:
    """A `claude` on a pty, with whoever's watching. Output is kept back a way,
    so a terminal that attaches when a prompt comes up sees the prompt."""

    def __init__(self, owner: Any) -> None:
        self.id = str(uuid4())
        self.owner = owner
        self.stopped = False
        self.backlog = bytearray()
        self.viewers: set[WebSocket] = set()
        self.proc: subprocess.Popen | None = None
        self.master = -1
        self.pump: asyncio.Task | None = None

    def start(self, task: str | None) -> None:
        loop = asyncio.get_running_loop()
        output: asyncio.Queue[bytes | None] = asyncio.Queue()

        def readable() -> None:
            try:
                data = os.read(self.master, 65536)
            except OSError:                 # EIO is how a pty says the child left
                data = b""
            if not data:
                loop.remove_reader(self.master)
            output.put_nowait(data or None)

        async def pump() -> None:
            while (data := await output.get()) is not None:
                self.backlog += data
                del self.backlog[:-BACKLOG_BYTES]
                for viewer in list(self.viewers):
                    try:
                        await viewer.send_bytes(data)
                    except Exception:
                        self.viewers.discard(viewer)

        self.proc, self.master = spawn(command(self.id, task), COMPUTER_DIR, ROWS, COLS)
        loop.add_reader(self.master, readable)
        self.pump = asyncio.create_task(pump())

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None and not self.stopped

    async def end(self) -> None:
        proc, master = self.proc, self.master
        if proc is None:
            return
        self.proc = None
        asyncio.get_running_loop().remove_reader(master)
        if self.pump:
            self.pump.cancel()
        await asyncio.to_thread(_quit, proc, master)


def _quit(proc: subprocess.Popen, master: int) -> None:
    try:
        if proc.poll() is None:
            os.killpg(proc.pid, signal.SIGHUP)
            try:
                proc.wait(QUIT_WAIT_S)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait(QUIT_WAIT_S)
    except (OSError, subprocess.SubprocessError):
        pass
    os.close(master)


_job: Job | None = None


def stop(owner: Any) -> None:
    """A Stop, or the tab going away: the job it started ends with it."""
    if _job is not None and _job.owner is owner:
        _job.stopped = True


def _read(path: Path, at: int) -> bytes:
    try:
        with path.open("rb") as f:
            f.seek(at)
            return f.read()
    except OSError:
        return b""


async def _follow(job: Job, send: Send, key: str) -> tuple[bool, str]:
    """Relay a running job's transcript until its turn ends."""
    path = transcript_path(COMPUTER_DIR, job.id)
    events = TranscriptEvents()
    began = moved = time.monotonic()
    showing = False
    offset, partial = 0, b""
    while True:
        if job.stopped:
            return False, "Stopped by the user."
        if not job.alive():
            return False, "Claude exited before finishing." + (
                f" It had said: {events.last_text}" if events.last_text else "")
        now = time.monotonic()
        if now - began > COMPUTER_JOB_LIMIT_S:
            return False, f"Stopped after {COMPUTER_JOB_LIMIT_S // 60} minutes without finishing."
        data = await asyncio.to_thread(_read, path, offset)
        if data:
            offset += len(data)
            moved = now
            *lines, partial = (partial + data).split(b"\n")
            for line in lines:
                for frame in events.feed(line.decode("utf-8", "replace")):
                    if frame["type"] == "turn_end":
                        return True, events.last_text or "Done, with nothing to report."
                    if frame["type"] != "turn_start":
                        await send(frame)
            console.terminal_active[key] = now
        want = now - moved > QUIET_S
        if want != showing:
            showing = want
            await send({"type": "computer_prompt", "id": job.id, "show": want})
        await asyncio.sleep(TAIL_S)


async def _setup(job: Job, send: Send) -> bool:
    """First use: computer-use is off for the job folder, and only `/mcp` turns
    it on. Open a bare `claude` on screen, and wait until the user has."""
    job.start(None)
    await send({"type": "computer_start", "id": job.id})
    await send({"type": "computer_prompt", "id": job.id, "show": True})
    deadline = time.monotonic() + COMPUTER_SETUP_LIMIT_S
    while job.alive() and time.monotonic() < deadline:
        if computer_enabled():
            return True
        await asyncio.sleep(1)
    return computer_enabled()


async def run_computer(task: str, send: Send, owner: Any) -> tuple[bool, str]:
    """Do `task` with computer use, on a job of its own. (ok, what to tell the model)."""
    global _job
    if _job is not None:
        return False, "Another computer-use job is running. Only one can at a time."
    COMPUTER_DIR.mkdir(parents=True, exist_ok=True)
    key = uuid4().hex
    try:
        if not computer_enabled():
            _job = setup = Job(owner)
            try:
                ready = await _setup(setup, send)
            finally:
                await setup.end()
                await send({"type": "computer_end", "id": setup.id})
            if not ready:
                return False, ("Computer use isn't turned on yet. It needs `/mcp`, then "
                               "computer-use, then Enable, on the terminal in Claude's view.")
        _job = job = Job(owner)
        job.start(task)
        await send({"type": "computer_start", "id": job.id})
        try:
            return await _follow(job, send, key)
        finally:
            await job.end()
            await send({"type": "computer_end", "id": job.id})
    finally:
        _job = None
        console.terminal_active.pop(key, None)


@router.websocket("/ws/console/computer")
async def computer_ws(ws: WebSocket, id: str = "") -> None:
    """The running job's terminal, to watch or to answer its prompts."""
    job = _job
    if not console.origin_ok(ws) or job is None or job.id != id or job.proc is None:
        await ws.close(code=1008)
        return
    await ws.accept()
    job.viewers.add(ws)
    try:
        await ws.send_bytes(bytes(job.backlog))
        while True:
            msg = await ws.receive()
            if msg["type"] == "websocket.disconnect" or job.proc is None:
                return
            if msg.get("bytes"):
                await asyncio.to_thread(os.write, job.master, msg["bytes"])
            elif msg.get("text"):
                try:
                    frame = json.loads(msg["text"])
                except ValueError:
                    continue
                if isinstance(frame, dict) and frame.get("type") == "resize":
                    set_size(job.master, parse_size(frame.get("rows"), ROWS), parse_size(frame.get("cols"), COLS))
    except WebSocketDisconnect:
        pass                        # the pane closed, or the job ended under it
    finally:
        job.viewers.discard(ws)
