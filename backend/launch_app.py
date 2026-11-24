"""Native desktop launcher: a pywebview window over the FastAPI backend.

Owns the lifecycle: rebuilds a stale frontend, runs uvicorn as a subprocess, and on close
kills everything it started, including the console's `claude` grandchildren. Backend edits
restart the server, held while a console turn runs (`Backend`); frontend edits need ⌘R.
Normally run via the generated Kestrel.app (scripts/make-app.sh).
Design: DESIGN.md → `backend/launch_app.py`: the native launcher.
"""
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

import webview

import desktop_shell
from app import merge_guard
from app.config import APP_DB, BACKEND_PORT, DATA_DIR, HELD_RELOAD_ENV, NOTIFY_FD_ENV

HOST = "127.0.0.1"
URL = f"http://{HOST}:{BACKEND_PORT}"

REPO = Path(__file__).resolve().parents[1]
BACKEND = REPO / "backend"
FRONTEND = REPO / "frontend"
DIST = FRONTEND / "dist"
ENV_FILE = BACKEND / ".env"
LOG_FILE = Path("/tmp/vault-dashboard-app.log")
# Per-machine state next to app.db; gitignored by name (DATA_DIR itself isn't).
FRAME_FILE = DATA_DIR / "window.json"
# Everything a build reads; node_modules is excluded (npm install touches thousands of files).
SOURCES = ["src", "index.html", "vite.config.js", "package.json"]


def _newest(path: Path) -> float:
    """Newest mtime at or under `path` (0.0 if it doesn't exist)."""
    if not path.exists():
        return 0.0
    if path.is_file():
        return path.stat().st_mtime
    return max((f.stat().st_mtime for f in path.rglob("*") if f.is_file()),
               default=0.0)


_build_lock = threading.Lock()


def ensure_frontend_built() -> None:
    """Rebuild frontend/dist if any source is newer than it; one build at a time."""
    with _build_lock:
        _ensure_frontend_built()


def _ensure_frontend_built() -> None:
    src = max(_newest(FRONTEND / s) for s in SOURCES)
    built = _newest(DIST)
    if built and built >= src:
        return

    why = "no build found" if not built else "sources changed since last build"
    print(f"[build] {why} — running npm run build …")
    try:
        r = subprocess.run(["npm", "run", "build"], cwd=FRONTEND,
                           capture_output=True, text=True, timeout=300)
    except (OSError, subprocess.TimeoutExpired) as e:
        print(f"[build] FAILED to run npm: {e}", file=sys.stderr)
        print("[build] serving the existing dist — it may be out of date.",
              file=sys.stderr)
        return
    if r.returncode != 0:
        # A stale dashboard beats a blank window.
        print(r.stdout + r.stderr, file=sys.stderr)
        print("[build] FAILED — serving the previous dist.", file=sys.stderr)
        return
    print(f"[build] ok — {DIST} is current.")


def _port_taken() -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.4)
        return s.connect_ex((HOST, BACKEND_PORT)) == 0


def _ps(pid: int) -> tuple[int, str] | None:
    """(parent pid, command line) of a process, or None if it's gone."""
    try:
        out = subprocess.run(["ps", "-o", "ppid=,command=", "-p", str(pid)],
                             capture_output=True, text=True, timeout=5).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return None
    ppid, _, cmd = out.partition(" ")
    return (int(ppid), cmd) if ppid.isdigit() else None


def _port_holders() -> list[int] | None:
    """Dashboard backends on our port (a uvicorn serving app.main:app, plus the
    reloader parent of a --reload worker). None if anything else holds it."""
    try:
        out = subprocess.run(["lsof", "-nP", "-t", f"-iTCP:{BACKEND_PORT}", "-sTCP:LISTEN"],
                             capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    ours = lambda cmd: "uvicorn" in cmd and "app.main:app" in cmd
    pids: set[int] = set()
    for pid in {int(p) for p in out.split() if p.isdigit()}:
        me = _ps(pid)
        if me is None:
            continue
        parent = _ps(me[0])
        if parent and ours(parent[1]):
            pids |= {pid, me[0]}
        elif ours(me[1]):
            pids.add(pid)
        else:
            return None
    return sorted(pids)


def _take_over_port() -> bool:
    """Stop a dashboard backend this app didn't start. False if the port is held
    by something else, or won't let go."""
    pids = _port_holders()
    if not pids:
        return False
    print(f"[app] {URL} was held by another dashboard backend {pids} — stopping it.")
    for sig, wait in ((signal.SIGTERM, 10.0), (signal.SIGKILL, 3.0)):
        for pid in pids:
            try:
                os.kill(pid, sig)
            except OSError:
                pass
        end = time.monotonic() + wait
        while time.monotonic() < end:
            if not _port_taken():
                return True
            time.sleep(0.2)
    return False


def _wait_for_health(proc: subprocess.Popen | None = None,
                     timeout: float = 30.0) -> bool:
    """Poll /api/healthz until the backend answers; a dead `proc` ends the wait at once."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"{URL}/api/healthz", timeout=1) as r:
                if r.status == 200:
                    return True
        except OSError:
            if proc is not None and proc.poll() is not None:
                print(f"[app] backend exited with code {proc.returncode} "
                      "before answering — see the log above.", file=sys.stderr)
                return False
            time.sleep(0.25)
    return False


def _db_ok() -> bool:
    """/api/healthz?db=1: the server can open app.db, so its migrations ran."""
    try:
        with urllib.request.urlopen(f"{URL}/api/healthz?db=1", timeout=30) as r:
            return r.status == 200
    except OSError:
        return False


def _backend_python() -> str:
    """The venv's interpreter, not `sys.executable`: from the .app that is the
    bundled stub, and a server child would add a second Dock icon per restart."""
    venv = BACKEND / ".venv" / "bin" / "python"
    return str(venv) if venv.exists() else sys.executable


def _start_backend(notify_fd: int | None = None) -> subprocess.Popen:
    """The API server as a subprocess (so a restart never hits the window).
    No `--reload`: `Backend` restarts it between console turns."""
    cmd = [_backend_python(), "-m", "uvicorn", "app.main:app",
           "--host", HOST, "--port", str(BACKEND_PORT), "--log-level", "info"]
    print(f"[app] starting backend: {' '.join(cmd)}")
    # HELD_RELOAD_ENV tells console.py edits are held; GUARD_ENV says this
    # launcher runs the merge guard's check at restarts; notify_fd goes to this child only.
    env = {**os.environ, HELD_RELOAD_ENV: "1", merge_guard.GUARD_ENV: "1"}
    env.pop(NOTIFY_FD_ENV, None)   # never one inherited from whatever started us
    if notify_fd is not None:
        env[NOTIFY_FD_ENV] = str(notify_fd)
    return subprocess.Popen(cmd, cwd=BACKEND, env=env,
                            pass_fds=(notify_fd,) if notify_fd is not None else ())


# app/ only: backend/ also holds data/ (app.db and WAL, rewritten constantly) and .venv.
WATCH_DIR = BACKEND / "app"
# Quiet spell required after the last write before a restart.
SETTLE_S = 0.5
# Backstop only: /api/console/busy already drops a quiet turn, and a working turn must never reach it.
MAX_HOLD_S = 3 * 60 * 60


def _py_mtimes(root: Path) -> dict[str, float]:
    """Every *.py under `root` and its mtime."""
    out = {}
    for f in root.rglob("*.py"):
        try:
            out[str(f)] = f.stat().st_mtime
        except OSError:
            pass                  # deleted mid-walk; the next pass sees it gone
    return out


def _console_turns() -> int | None:
    """Console turns in flight (GET /api/console/busy), or None when the
    server can't say — down, or an older one without the route."""
    try:
        with urllib.request.urlopen(f"{URL}/api/console/busy", timeout=2) as r:
            return int(json.load(r).get("turns", 0))
    except (OSError, ValueError):
        return None


class Backend:
    """The server process, restarted when app/ changes but not mid-turn.

    A restart drops every console socket; mid-turn it cuts the reply. A dev tab's
    backend edits therefore apply when its turn ends, so a curl in the same turn
    sees old code.
    """

    def __init__(self, notify_fd: int | None = None):
        self.notify_fd = notify_fd
        self.proc = _start_backend(notify_fd)
        self._lock = threading.Lock()
        self._stopped = threading.Event()

    def watch(self) -> None:
        threading.Thread(target=self._watch, name="reloader", daemon=True).start()

    def _watch(self) -> None:
        seen = _py_mtimes(WATCH_DIR)
        while not self._stopped.wait(1.0):
            now = _py_mtimes(WATCH_DIR)
            if now == seen:
                continue
            # Let a burst of writes finish before deciding anything.
            while not self._stopped.wait(SETTLE_S):
                later = _py_mtimes(WATCH_DIR)
                if later == now:
                    break
                now = later
            changed = sorted(Path(p).name for p in set(now) ^ set(seen)
                             | {p for p in now if seen.get(p) != now[p]})
            seen = now
            self._hold_while_busy(changed)
            # Edits made while held are part of this restart, not the next.
            seen = _py_mtimes(WATCH_DIR)
            self.restart(f"{', '.join(changed[:3])}{' …' if len(changed) > 3 else ''}")

    def _hold_while_busy(self, changed: list[str]) -> None:
        deadline = time.time() + MAX_HOLD_S
        said = False
        while not self._stopped.is_set():
            turns = _console_turns()
            if not turns:         # 0, or a server that can't answer (crashed on
                return            # a bad edit) — nothing to protect either way
            if time.time() >= deadline:
                print(f"[reload] {turns} console turn(s) still running after "
                      f"{MAX_HOLD_S // 60} min — restarting anyway", flush=True)
                return
            if not said:
                print(f"[reload] {', '.join(changed[:3])} changed — holding the "
                      f"restart until {turns} console turn(s) finish", flush=True)
                said = True
            self._stopped.wait(2.0)

    def restart(self, why: str) -> None:
        with self._lock:
            if self._stopped.is_set():
                return
            print(f"[reload] {why} — restarting the backend", flush=True)
            self._end(self.proc)
            check = merge_guard.pending()
            if check is None:
                self.proc = _start_backend(self.notify_fd)
                return
            # A merge added a migration: snapshot while nothing writes, then
            # the new server must open app.db.
            check = merge_guard.refresh_snapshot(check)
            try:
                self.proc, res = merge_guard.verify_restart(
                    check, start=lambda: _start_backend(self.notify_fd), stop=self._end,
                    healthy=lambda p: _wait_for_health(p, merge_guard.HEALTH_S) and _db_ok(),
                    db_path=APP_DB, log=lambda s: print(s, flush=True))
            except Exception as e:   # never leave the window without a server
                print(f"[merge-guard] check failed to run: {e!r}", flush=True)
                if self.proc.poll() is not None:
                    self.proc = _start_backend(self.notify_fd)
                return
            self._banner("Merge rolled back" if not res["ok"] else "Merge checked",
                         res["detail"])

    def _banner(self, title: str, body: str) -> None:
        """Through the window's notification pipe, like the server's own."""
        if self.notify_fd is None:
            return
        try:
            os.write(self.notify_fd, (json.dumps({"title": title, "body": body[:240]}) + "\n").encode())
        except OSError:
            pass

    def stop(self) -> None:
        """For the exit path: no restart may start after this, and the one
        running finishes first (the lock), so `proc` is the live process."""
        self._stopped.set()
        with self._lock:
            pass

    @staticmethod
    def _end(proc: subprocess.Popen) -> None:
        # SIGTERM lets uvicorn drain the console sockets so the `claude` children end.
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


def _notification_pipe(post) -> int:
    """A pipe the server writes banner JSON lines into (app/notify.py `_post`),
    posted by this process as the bundle. Returns the write end, which every
    server inherits; we keep it open so a restart never reads as EOF."""
    r, w = os.pipe()

    def read() -> None:
        with os.fdopen(r, encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    msg = json.loads(line)
                    post(str(msg.get("title") or "Kestrel"),
                         str(msg.get("body") or ""), msg.get("target"))
                except Exception as e:                # keep reading
                    print(f"[notify] bad line from the server: {e!r}", flush=True)

    threading.Thread(target=read, name="notify-pipe", daemon=True).start()
    return w


def _group_members(pgid: int) -> list[int]:
    """PIDs in our process group, excluding this process."""
    try:
        out = subprocess.run(["pgrep", "-g", str(pgid)],
                             capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.TimeoutExpired):
        return []
    me = os.getpid()
    return [int(p) for p in out.split() if p.isdigit() and int(p) != me]


def _reap_process_group() -> None:
    """SIGTERM then SIGKILL everything in our group, one pid at a time: killpg
    can't spare the caller. Only safe because main() made us group leader."""
    try:
        pgid = os.getpgid(0)
    except OSError:
        return
    if pgid != os.getpid():       # setpgrp() failed — don't signal someone
        return                    # else's group

    for sig, grace in ((signal.SIGTERM, 1.5), (signal.SIGKILL, 0.0)):
        pids = _group_members(pgid)
        if not pids:
            return
        if sig is signal.SIGKILL:
            print(f"[app] force-killing {len(pids)} leftover process(es): {pids}")
        for pid in pids:
            try:
                os.kill(pid, sig)
            except OSError:
                pass              # already gone
        if grace:
            time.sleep(grace)


def main() -> None:
    # Own process group, so shutdown never reaches anything already running.
    try:
        os.setpgrp()
    except OSError as e:
        print(f"[app] could not claim a process group ({e}) — "
              "console subprocesses may outlive the window.", file=sys.stderr)

    ensure_frontend_built()

    # A backend already on the port is stopped, never attached to or raced for the bind.
    if _port_taken() and not _take_over_port():
        print(f"[app] {URL} is held by something that isn't a dashboard backend "
              "— not starting over it.", file=sys.stderr)
        sys.exit(1)
    # The pipe must exist before the server and the window. A click dispatches
    # `kestrel:notification` (PlanApp.jsx) via evaluate_js on its own thread.
    windows = []

    def on_notification_click(target: str | None) -> None:
        desktop_shell.bring_forward()
        if not windows:
            return
        detail = json.dumps({"tab": target})
        threading.Thread(target=lambda: windows[0].evaluate_js(
            f"window.dispatchEvent(new CustomEvent('kestrel:notification', {{detail: {detail}}}))"),
            daemon=True).start()

    notifier = desktop_shell.Notifier(on_notification_click)
    backend = Backend(_notification_pipe(notifier.post))

    if not _wait_for_health(backend.proc):
        print("Backend did not come up — aborting.", file=sys.stderr)
        if backend.proc.poll() is None:
            backend.proc.kill()
        sys.exit(1)
    backend.watch()

    # Before the window exists (the delegate class is patched once).
    desktop_shell.allow_media_capture()

    frame = desktop_shell.load_frame(FRAME_FILE)

    # text_select=True: otherwise pywebview injects user-select: none.
    window = webview.create_window(
        "Kestrel", URL,
        width=frame["width"], height=frame["height"],
        x=frame["x"], y=frame["y"],
        min_size=(desktop_shell.MIN_W, desktop_shell.MIN_H),
        text_select=True,
    )
    desktop_shell.track_frame(window, FRAME_FILE)
    windows.append(window)

    def quit_app():
        # Same path as the red button, so shutdown stays one code path.
        window.destroy()

    # evaluate_js deadlocks on the main thread (menu actions run there), so ⌘R
    # runs it on a worker, after a rebuild if sources moved on.
    def _rebuild_and_reload():
        ensure_frontend_built()
        window.evaluate_js("location.reload()")

    def reload():
        threading.Thread(target=_rebuild_and_reload, daemon=True).start()

    def menu():
        desktop_shell.install_menu({
            "reload": reload,
            "quit": quit_app,
            "settings": lambda: desktop_shell.open_in_editor(ENV_FILE),
            "logs": lambda: desktop_shell.open_in_editor(LOG_FILE),
        })
        desktop_shell.tab_to_every_control(window)

    # Blocks on the main thread until the window closes; `func` runs once AppKit
    # accepts a main menu. private_mode=False keeps localStorage across ⌘Q.
    webview.start(menu, private_mode=False)

    # Order matters: stop() so no restart starts behind us, SIGTERM the server
    # alone so it drains before the `claude` grandchildren die, then sweep the group.
    backend.stop()
    backend.proc.terminate()
    try:
        backend.proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        backend.proc.kill()
    _reap_process_group()


if __name__ == "__main__":
    main()
