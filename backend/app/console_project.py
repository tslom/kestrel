"""Per-project scripts, a port per worktree, and the PR watch's endpoint
(New Features #87 and #88).

A repo can say how it's run in `.kestrel/project.json` (T3 Code's `t3.json`
is the model), read from the tab's own folder, so a branch can change it:

    {
      "scripts": [
        {"id": "dev",  "name": "dev",  "icon": "run",  "command": "npm run dev -- --port $PORT"},
        {"id": "lint", "name": "lint", "icon": "lint", "command": "npm run lint"}
      ],
      "previewUrl": "http://localhost:$PORT",
      "autoOpenPreview": true,
      "runOnWorktreeCreate": ["npm install"]
    }

- **scripts** are the dev tab's bar buttons. Each runs in the tab's folder
  (its worktree), in a login zsh, output kept here for the view pane's Tasks.
  `icon`: run, test, lint or build. A second click on a running one stops it.
- **$PORT** is the tab's own port. Dev tabs run at once, each in its own
  worktree, and two `next dev`s on 3000 means the second tab previews the
  first one's code. So each tree gets a port of its own in 41000–41999,
  hashed from its path (stable across restarts), skipping one that's taken
  by another tree's server. It's in every script's env as PORT and
  KESTREL_PORT, and the start line tells the agent, so a server it starts
  itself lands there too.
- **previewUrl** (with $PORT, ${PORT} or {port} filled in) is where the
  preview pane points; **autoOpenPreview** opens the pane on it once a
  script's server starts listening.
- **runOnWorktreeCreate**: commands (or script ids) run, in order, as one
  "setup" run when a tab's worktree is made: the `npm install` a fresh tree
  of a site needs. In the background, so the tab opens at once, and shown in
  Tasks like any other run.

Runs belong to this backend process: they stop when their tree is released
(the tab closed with nothing to keep) and when the backend exits.
"""

from __future__ import annotations

import asyncio
import atexit
import json
import os
import re
import secrets
import signal
import socket
import subprocess
import threading
import time
import zlib
from pathlib import Path

from fastapi import APIRouter, HTTPException

from . import console_pr, console_profiles, console_worktree

CONFIG = Path(".kestrel") / "project.json"
ICONS = ("run", "test", "lint", "build")
PORT_BASE, PORT_SPAN = 41000, 1000
OUTPUT_BYTES = 64_000
KEEP_DONE = 8                  # finished runs kept per tree, for their output
ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07]*\x07|\r(?!\n)")
ID = re.compile(r"[\w.-]{1,40}")

router = APIRouter()
_lock = threading.Lock()
_ports: dict[str, int] = {}
_runs: dict[str, "Run"] = {}


class ProjectError(RuntimeError):
    pass


# ---------- the config ----------

def load(folder: Path) -> dict | None:
    """The folder's project.json, normalised. None: there isn't one."""
    path = folder / CONFIG
    try:
        raw = json.loads(path.read_text())
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as e:
        raise ProjectError(f"{CONFIG} doesn't parse: {e}")
    if not isinstance(raw, dict):
        raise ProjectError(f"{CONFIG} should be an object")
    scripts, seen = [], set()
    for i, s in enumerate(raw.get("scripts") or []):
        if not isinstance(s, dict) or not str(s.get("command") or "").strip():
            continue
        sid = str(s.get("id") or s.get("name") or f"script-{i}").strip()
        if not ID.fullmatch(sid) or sid in seen:
            raise ProjectError(f"script id {sid!r} must be unique, letters, digits, . _ -")
        seen.add(sid)
        icon = str(s.get("icon") or "run").lower()
        scripts.append({"id": sid, "name": str(s.get("name") or sid)[:24],
                        "command": str(s["command"]).strip(),
                        "icon": icon if icon in ICONS else "run"})
    setup = raw.get("runOnWorktreeCreate") or []
    if isinstance(setup, str):
        setup = [setup]
    return {"scripts": scripts,
            "previewUrl": str(raw.get("previewUrl") or "").strip() or None,
            "autoOpenPreview": bool(raw.get("autoOpenPreview")),
            "runOnWorktreeCreate": [str(c).strip() for c in setup if str(c).strip()]}


def fill(text: str, port: int) -> str:
    return re.sub(r"\$\{PORT\}|\$PORT\b|\{port\}", str(port), text)


# ---------- a port per tree ----------

def _listening(port: int) -> bool:
    for host in ("127.0.0.1", "::1"):
        try:
            with socket.create_connection((host, port), timeout=0.2):
                return True
        except OSError:
            continue
    return False


def port_for(tree: Path) -> int:
    """This tree's port: its hash's slot, or the next one that's free (or
    already served from this tree, after a backend restart) and not another
    tree's."""
    key = str(tree.resolve())
    with _lock:
        if key in _ports:
            return _ports[key]
        taken = set(_ports.values())
    start = zlib.crc32(key.encode()) % PORT_SPAN
    ours: set[int] | None = None
    port = PORT_BASE + start
    for step in range(PORT_SPAN):
        port = PORT_BASE + (start + step) % PORT_SPAN
        if port in taken:
            continue
        if not _listening(port):
            break
        if ours is None:
            ours = {s["port"] for s in console_profiles.dev_servers(tree)}
        if port in ours:
            break
    with _lock:
        return _ports.setdefault(key, port)


# ---------- runs ----------

class Run:
    def __init__(self, tree: Path, sid: str, name: str, command: str, icon: str, port: int):
        self.id = secrets.token_hex(4)
        self.tree, self.sid, self.name, self.command, self.icon = str(tree), sid, name, command, icon
        self.port = port
        self.started = time.time()
        self.code: int | None = None
        self.out = bytearray()
        env = {**os.environ, "PORT": str(port), "KESTREL_PORT": str(port),
               "BROWSER": "none", "FORCE_COLOR": "0", "NO_COLOR": "1"}
        # Kestrel's own key would shadow a script's `claude` login, as in the backend shell.
        env.pop("ANTHROPIC_API_KEY", None)
        self.proc = subprocess.Popen(["/bin/zsh", "-lc", command], cwd=tree, env=env,
                                     stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                     stderr=subprocess.STDOUT, start_new_session=True)
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self) -> None:
        assert self.proc.stdout is not None
        for chunk in iter(lambda: self.proc.stdout.read1(4096), b""):
            with _lock:
                self.out += chunk
                if len(self.out) > OUTPUT_BYTES:
                    del self.out[:len(self.out) - OUTPUT_BYTES]
        self.code = self.proc.wait()

    @property
    def running(self) -> bool:
        return self.code is None and self.proc.poll() is None

    def stop(self) -> None:
        if not self.running:
            return
        try:
            os.killpg(self.proc.pid, signal.SIGTERM)
            self.proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            os.killpg(self.proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    def row(self, output: bool = True) -> dict:
        with _lock:
            text = self.out.decode(errors="replace")
        row = {"id": self.id, "script": self.sid, "name": self.name, "icon": self.icon,
               "command": self.command, "running": self.running, "code": self.code,
               "started": self.started, "port": self.port}
        if output:
            row["output"] = ANSI.sub("", text)[-8000:]
        return row


def _prune(tree: str) -> None:
    done = sorted((r for r in _runs.values() if r.tree == tree and not r.running),
                  key=lambda r: r.started)
    for r in done[:-KEEP_DONE]:
        _runs.pop(r.id, None)


def runs(tree: Path) -> list[dict]:
    key = str(tree)
    with _lock:
        mine = [r for r in _runs.values() if r.tree == key]
    return [r.row() for r in sorted(mine, key=lambda r: r.started)]


def start(tree: Path, sid: str, name: str, command: str, icon: str = "run") -> dict:
    with _lock:
        live = next((r for r in _runs.values()
                     if r.tree == str(tree) and r.sid == sid and r.running), None)
    if live:
        return live.row()
    run = Run(tree, sid, name, command, icon, port_for(tree))
    with _lock:
        _runs[run.id] = run
        _prune(str(tree))
    return run.row()


def stop(run_id: str) -> bool:
    with _lock:
        run = _runs.get(run_id)
    if run is None:
        return False
    run.stop()
    return True


def stop_tree(tree: Path) -> None:
    """A released tree's runs go with it (console.py, on the tab's close)."""
    key = str(tree)
    with _lock:
        mine = [r for r in _runs.values() if r.tree == key]
        _ports.pop(key, None)
    for r in mine:
        r.stop()


@atexit.register
def _stop_all() -> None:
    for r in list(_runs.values()):
        r.stop()


def on_created(tree: Path) -> dict | None:
    """runOnWorktreeCreate, as one background run. Never raises: a tab whose
    install didn't start can still edit (console_worktree._setup's rule)."""
    try:
        cfg = load(tree)
    except ProjectError:
        return None
    if not cfg or not cfg["runOnWorktreeCreate"]:
        return None
    by_id = {s["id"]: s["command"] for s in cfg["scripts"]}
    steps = [by_id.get(c, c) for c in cfg["runOnWorktreeCreate"]]
    try:
        return start(tree, "setup", "setup", " && ".join(f"({c})" for c in steps), "build")
    except OSError:
        return None


def start_note(tree: Path) -> str:
    """For the tab's start line: its port, when the repo has a project.json."""
    try:
        cfg = load(tree)
    except ProjectError as e:
        return f"\n{e}"
    if cfg is None:
        return ""
    return (f"\nThis tree's dev port is {port_for(tree)} ($PORT in .kestrel/project.json's "
            "scripts). Other tabs' worktrees run their own servers at the same time, so "
            "start any dev server you run yourself on this port, not the framework default.")


# ---------- routes ----------

def folder(project: str | None, worktree: str | None) -> Path:
    """A dev tab's folder: its worktree when it names one of the project's."""
    try:
        repo = console_profiles.resolve_project(project)
    except console_profiles.ProfileError as e:
        raise HTTPException(404, str(e))
    if not worktree:
        return repo
    tree = console_worktree.resolve(repo, worktree)
    if tree is None:
        raise HTTPException(404, f"{worktree!r} isn't one of {repo.name}'s tab worktrees")
    return tree


def describe(where: Path) -> dict:
    try:
        cfg, error = load(where), None
    except ProjectError as e:
        cfg, error = None, str(e)
    port = port_for(where)
    url = fill(cfg["previewUrl"], port) if cfg and cfg["previewUrl"] else None
    return {"config": cfg and {**cfg, "scripts": [{k: s[k] for k in ("id", "name", "icon", "command")}
                                                    for s in cfg["scripts"]]},
            "error": error, "port": port, "preview_url": url,
            "listening": bool(url) and _listening(port), "runs": runs(where)}


@router.get("/api/console/project")
async def project_get(project: str, worktree: str | None = None) -> dict:
    return await asyncio.to_thread(describe, folder(project, worktree))


@router.post("/api/console/project/run")
async def project_run(body: dict) -> dict:
    where = folder(body.get("project"), body.get("worktree"))
    try:
        cfg = load(where)
    except ProjectError as e:
        raise HTTPException(400, str(e))
    script = next((s for s in (cfg or {}).get("scripts", []) if s["id"] == body.get("script")), None)
    if script is None:
        raise HTTPException(404, f"no script {body.get('script')!r} in {CONFIG}")
    return start(where, script["id"], script["name"], script["command"], script["icon"])


@router.post("/api/console/project/stop")
async def project_stop(body: dict) -> dict:
    if not await asyncio.to_thread(stop, str(body.get("id") or "")):
        raise HTTPException(404, "no such run")
    return {"ok": True}


@router.post("/api/console/pr")
async def pr_poll(body: dict) -> dict:
    """One PR-watch poll: the snapshot now and the notes since `last`."""
    where = folder(body.get("project"), body.get("worktree"))
    last = body.get("last") if isinstance(body.get("last"), dict) else None
    try:
        return await console_pr.poll_async(where, body.get("ref") or "", last)
    except console_pr.PrError as e:
        raise HTTPException(422, str(e))
