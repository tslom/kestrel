"""Native desktop launcher — wraps the dashboard in a pywebview window.

Owns the whole lifecycle: rebuilds the frontend if it's stale, starts the
FastAPI backend (which serves the built frontend, see main.py) as a uvicorn
subprocess, opens a native macOS window, and on close shuts down
everything it started — including the `claude` CLI subprocesses the console
spawns, which are grandchildren and don't die with uvicorn.

Because the server is its own process, editing anything under backend/app
restarts it under the open window — like a terminal's --reload, except that
it waits for any console turn in flight to finish first (see `Backend`).
Frontend edits still need the window reloaded (⌘R), which reruns the build
check before reloading.

No browser, no Vite dev server. The one thing deliberately *not* in scope is
the location listener: it's a `KeepAlive` launchd agent because location has
to keep recording whether or not this window is open.

Prereqs: `pip install pywebview` and node/npm on PATH. VAULT_PATH etc. come
from backend/.env via app.config.

Run from the backend/ dir with the venv active:  python launch_app.py
Normally you don't run this by hand — double-click the generated
"Kestrel.app" (see scripts/make-app.sh).
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
# Window geometry, not data — but it belongs next to app.db rather than in the
# repo: it's per-machine state. Gitignored by name (DATA_DIR itself isn't —
# only the *.db files in it are).
FRAME_FILE = DATA_DIR / "window.json"
# Everything a build reads. node_modules is excluded on purpose: npm install
# touches thousands of files and none of them change the output on their own.
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
    """Rebuild frontend/dist if any source is newer than it.

    Runs at launch and again on every ⌘R: a git pull, a branch switch and a
    hand edit all land here the same way, and a window left open across a
    commit is the case launch alone missed. A build is ~300ms; the check is
    a stat walk, so the common (fresh) case costs nothing.

    One at a time: a second ⌘R mid-build would run a second `vite build`
    over the same dist. Waiting on the lock instead, it finds the dist the
    first one just wrote and returns.
    """
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
        # Serve the stale build rather than showing nothing: a dashboard one
        # commit behind beats a blank window, and the log says which it is.
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
    """The dashboard backends listening on our port — a uvicorn serving
    app.main:app, plus the reloader parent of one started with --reload (its
    worker's own command line is a bare multiprocessing stub). None if
    anything else holds the port: that isn't ours to stop."""
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
    """Stop a dashboard backend this app didn't start, so the one it starts
    is the one serving. Attaching to it instead means ⌘Q leaves it running,
    and a stray terminal `uvicorn --reload` can sit under the window for
    weeks, restarting on every dev-tab edit. False if the port is held by something else, or won't let go."""
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
    """Poll /api/healthz until the backend answers or we give up.

    A dead `proc` ends the wait immediately: an import error in app/ exits
    uvicorn in well under a second, and there's nothing to gain from spending
    the other 29 asking a socket that will never open.
    """
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
    """The interpreter the server runs under — the venv's, deliberately not
    this one. Launched from the .app, sys.executable is the bundled stub, and
    the stub is what gives this process its name and Dock icon; the reloader
    spawns the server as a *child* process, so using it here would put a second
    one of those on screen every time the server restarted."""
    venv = BACKEND / ".venv" / "bin" / "python"
    return str(venv) if venv.exists() else sys.executable


def _start_backend(notify_fd: int | None = None) -> subprocess.Popen:
    """The API server, as a subprocess. `Backend` below restarts it on edits.

    In-process (`uvicorn.Server(...).run()` on a thread) is the obvious shape
    for an embedded server and it cannot reload: the reloader is a *process*
    supervisor, so it restarts its target, which in-process means this
    process, i.e. the window. As a subprocess the restart hits something that
    isn't the GUI. The cost is one more process to shut down, which the exit
    path already had to handle for the console's `claude` grandchildren.

    Not `--reload`: uvicorn's reloader restarts the moment a file changes, and
    the console runs *inside* this server. A dev tab editing backend/app
    restarts the process hosting its own turn — the socket drops mid-edit,
    the next approval card dies "Stream closed", and the tab is gone.
    `Backend` waits for the turn to end first.
    """
    cmd = [_backend_python(), "-m", "uvicorn", "app.main:app",
           "--host", HOST, "--port", str(BACKEND_PORT), "--log-level", "info"]
    print(f"[app] starting backend: {' '.join(cmd)}")
    # stdout/stderr inherited, so uvicorn's log lands in the same app log as
    # everything else here — including the "[reload]" lines, which are how
    # you tell a stale window from a stale server.
    # The marker console.py reads to tell a dev tab its edits are held. A
    # dev tab on a terminal `uvicorn --reload` drops on every save, and that
    # server is invisible until a tab says so.
    # notify_fd: the write end of the window's notification pipe (see
    # _notification_pipe), inherited by this one child and no further.
    # GUARD_ENV: this launcher runs the merge guard's check at restarts
    # (Backend.restart), so a merge may hold its push for it.
    env = {**os.environ, HELD_RELOAD_ENV: "1", merge_guard.GUARD_ENV: "1"}
    env.pop(NOTIFY_FD_ENV, None)   # never one inherited from whatever started us
    if notify_fd is not None:
        env[NOTIFY_FD_ENV] = str(notify_fd)
    return subprocess.Popen(cmd, cwd=BACKEND, env=env,
                            pass_fds=(notify_fd,) if notify_fd is not None else ())


# What the reloader watches: app/ rather than the whole of backend/, which
# also holds data/ (app.db and its WAL, rewritten constantly) and .venv.
WATCH_DIR = BACKEND / "app"
# A save is often several writes (an editor's temp-and-rename, a multi-file
# change); a restart waits for this long a quiet spell before it goes.
SETTLE_S = 0.5
# /api/console/busy stops counting a turn that has gone quiet, which is what
# frees a wedged one (console.TURN_QUIET_S). This is only the backstop for a
# server that keeps answering busy regardless; a working turn must not reach
# it, since a restart cuts the turn.
MAX_HOLD_S = 3 * 60 * 60


def _py_mtimes(root: Path) -> dict[str, float]:
    """Every *.py under `root` and its mtime — the same filter --reload used."""
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
    """The server process, restarted when app/ changes — but not mid-turn.

    The console's sessions live inside the server, so a restart drops every
    open console socket. Between turns that costs nothing: the page reconnects
    and resumes on its own. Mid-turn it cut the reply and orphaned whatever
    approval card was up. So a change waits until /api/console/busy says no
    turn is working (or MAX_HOLD_S passes), then restarts once for however
    many edits piled up meanwhile.

    The flip side, for a dev tab working on this repo: its backend edits take
    effect when its turn ends, so a curl at the live server inside the same
    turn sees the old code. pytest doesn't care.
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
            # A merge added a migration (app/merge_guard.py): snapshot now
            # that nothing writes, then the new server must open app.db.
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
        # SIGTERM lets uvicorn drain the console sockets, which is what gives
        #ClaudeSDKClient.disconnect() its chance to end the `claude` children.
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


def _notification_pipe(post) -> int:
    """A pipe the server writes banners into, one JSON line each
    (app/notify.py `_post`), read here and posted by the window process,
    which is the bundle — so they're Kestrel's notifications, not Script
    Editor's. Returns the write end, which every server this window starts
    inherits. This process keeps it open too, so a restart never reads as
    EOF and one reader serves every server for the life of the window."""
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
    """SIGTERM everything this launcher started, then SIGKILL the stragglers.

    uvicorn's shutdown cancels its own tasks, but the console's `claude` CLI
    sessions are spawned by the SDK as our *grandchildren* — nothing in the
    ASGI shutdown path reaches them, and they used to outlive the window.

    Signalled one pid at a time rather than with killpg, which has no way to
    spare the caller: the SIGKILL sweep would take this process down with the
    group and turn a clean exit into a crash.

    Only safe because main() put us in our own group — signalling the group we
    inherited from a terminal would take out the shell that launched us.
    """
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
    # Our own process group, so shutdown can signal the whole tree without
    # reaching anything that was already running when we started.
    try:
        os.setpgrp()
    except OSError as e:
        print(f"[app] could not claim a process group ({e}) — "
              "console subprocesses may outlive the window.", file=sys.stderr)

    ensure_frontend_built()

    # The app owns the backend: opening it starts one, ⌘Q stops it. One
    # already on the port (a terminal uvicorn, a window that crashed) is
    # stopped first rather than attached to — never raced for the bind, whose
    # loser exits while the health check passes against the winner.
    if _port_taken() and not _take_over_port():
        print(f"[app] {URL} is held by something that isn't a dashboard backend "
              "— not starting over it.", file=sys.stderr)
        sys.exit(1)
    # Before the server starts, so its first flush already has the pipe; and
    # before the window, since the center wants its delegate early. A click
    # brings Kestrel forward and tells the page which tab (PlanApp.jsx,
    # `kestrel:notification`). evaluate_js on its own thread: the delegate
    # may be called on the main thread, where evaluate_js deadlocks (see
    # reload() below).
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

    # Before the window exists: the delegate class is patched once, and the
    # window that gets built later picks it up. Without it the voice console's
    # getUserMedia never settles in the app (browser tabs are unaffected).
    desktop_shell.allow_media_capture()

    # Reopen where you left it, not dead centre at a fixed size every time.
    frame = desktop_shell.load_frame(FRAME_FILE)

    # text_select=True or pywebview injects `body { user-select: none }` and
    # nothing in the window can be selected or copied. (The Edit menu's items
    # target the responder chain, so they need something selectable to act on.)
    # target="_blank" links already hand off to the system browser via the
    # backend's OPEN_EXTERNAL_LINKS_IN_BROWSER default.
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
        # Same path as the red button: destroy the window, let start() return,
        # and let main() run the one shutdown sequence there is.
        window.destroy()

    # Menu actions run on the main thread, and evaluate_js blocks on a
    # semaphore that only the main thread can release — called from here it
    # deadlocks the window (⌘R freezes the app until it is force-quit, with
    # the main thread parked in that acquire). Off the main thread it returns
    # once the page answers.
    # ⌘R rebuilds first when sources moved on: a window left open across a
    # commit otherwise reloads into the launch-time dist forever. Same stat
    # walk as launch, so an up-to-date dist costs nothing.
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

    # webview.start() blocks on the main thread (required on macOS) until the
    # window is closed; then we ask uvicorn to shut down cleanly. `func` runs
    # once the GUI is up, which is the earliest AppKit will accept a main menu.
    # private_mode=False: pywebview's default (True) erases the web view's
    # data every time the window opens, so localStorage — the tab list, each
    # tab's session, every pref — would outlive ⌘R but never ⌘Q, and
    # reopening would come back to one empty tab.
    webview.start(menu, private_mode=False)

    # SIGTERM the server so it drains. Aimed at the one process rather than the
    # group because the ordering is the point: uvicorn drains the console
    # websockets, which is what gives ClaudeSDKClient.disconnect() a chance to
    # run, and a group signal would hit those `claude` grandchildren at the
    # same instant. Then take the rest. stop() first, so a held restart can't
    # start a fresh server behind us.
    backend.stop()
    backend.proc.terminate()
    try:
        backend.proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        backend.proc.kill()
    _reap_process_group()


if __name__ == "__main__":
    main()
