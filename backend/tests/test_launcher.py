"""The launcher's staleness check — the thing that kept serving an old bundle.

The .app serves frontend/dist, so a build that never ran is invisible until
you notice the UI is a commit behind. These pin the rule that decides when to
rebuild; the window and the process-group reap aren't testable headless.
"""
import subprocess
from pathlib import Path

import launch_app

MAKE_APP = Path(__file__).resolve().parents[2] / "scripts" / "make-app.sh"


def _tree(root, when):
    """A fake frontend/ or dist/ with a known newest-mtime."""
    root.mkdir(parents=True, exist_ok=True)
    f = root / "index.js"
    f.write_text("x")
    import os
    os.utime(f, (when, when))
    return root


def test_rebuilds_when_a_source_is_newer(tmp_path, monkeypatch):
    fe = tmp_path / "frontend"
    _tree(fe / "src", when=2000)
    _tree(fe / "dist", when=1000)      # built before the source changed
    monkeypatch.setattr(launch_app, "FRONTEND", fe)
    monkeypatch.setattr(launch_app, "DIST", fe / "dist")

    calls = []
    monkeypatch.setattr(subprocess, "run",
                        lambda *a, **k: calls.append(a) or _ok())
    launch_app.ensure_frontend_built()
    assert calls, "a source newer than dist must trigger a build"


def test_skips_when_dist_is_current(tmp_path, monkeypatch):
    fe = tmp_path / "frontend"
    _tree(fe / "src", when=1000)
    _tree(fe / "dist", when=2000)      # built after the last edit
    monkeypatch.setattr(launch_app, "FRONTEND", fe)
    monkeypatch.setattr(launch_app, "DIST", fe / "dist")

    calls = []
    monkeypatch.setattr(subprocess, "run",
                        lambda *a, **k: calls.append(a) or _ok())
    launch_app.ensure_frontend_built()
    assert not calls, "a fresh dist must not pay for a build on every launch"


def test_builds_when_dist_is_missing(tmp_path, monkeypatch):
    fe = tmp_path / "frontend"
    _tree(fe / "src", when=1000)
    monkeypatch.setattr(launch_app, "FRONTEND", fe)
    monkeypatch.setattr(launch_app, "DIST", fe / "dist")

    calls = []
    monkeypatch.setattr(subprocess, "run",
                        lambda *a, **k: calls.append(a) or _ok())
    launch_app.ensure_frontend_built()
    assert calls, "no dist at all must build one"


def test_a_failed_build_still_serves_the_old_dist(tmp_path, monkeypatch):
    """A dashboard one commit behind beats a blank window."""
    fe = tmp_path / "frontend"
    _tree(fe / "src", when=2000)
    _tree(fe / "dist", when=1000)
    monkeypatch.setattr(launch_app, "FRONTEND", fe)
    monkeypatch.setattr(launch_app, "DIST", fe / "dist")
    monkeypatch.setattr(subprocess, "run",
                        lambda *a, **k: _fail("Unexpected token"))

    launch_app.ensure_frontend_built()          # must not raise
    assert (fe / "dist" / "index.js").exists()  # and must not delete anything


def test_missing_npm_does_not_take_the_app_down(tmp_path, monkeypatch):
    fe = tmp_path / "frontend"
    _tree(fe / "src", when=2000)
    _tree(fe / "dist", when=1000)
    monkeypatch.setattr(launch_app, "FRONTEND", fe)
    monkeypatch.setattr(launch_app, "DIST", fe / "dist")

    def boom(*a, **k):
        raise FileNotFoundError("npm")
    monkeypatch.setattr(subprocess, "run", boom)
    launch_app.ensure_frontend_built()          # must not raise


def test_node_modules_churn_does_not_trigger_a_build(tmp_path, monkeypatch):
    """npm install touches thousands of files and changes no output."""
    import os
    fe = tmp_path / "frontend"
    _tree(fe / "src", when=1000)
    _tree(fe / "dist", when=2000)
    nm = fe / "node_modules" / "left-pad"
    nm.mkdir(parents=True)
    (nm / "index.js").write_text("x")
    os.utime(nm / "index.js", (9000, 9000))     # far newer than dist
    monkeypatch.setattr(launch_app, "FRONTEND", fe)
    monkeypatch.setattr(launch_app, "DIST", fe / "dist")

    calls = []
    monkeypatch.setattr(subprocess, "run",
                        lambda *a, **k: calls.append(a) or _ok())
    launch_app.ensure_frontend_built()
    assert not calls


def test_reap_refuses_when_we_are_not_the_group_leader(monkeypatch):
    """The safety interlock: signalling an inherited group kills the shell.

    pytest runs in the terminal's process group, so this is the real
    condition, not a simulated one.
    """
    import os
    assert os.getpgid(0) != os.getpid(), "test needs an inherited group"
    killed = []
    monkeypatch.setattr(os, "kill", lambda *a: killed.append(a))
    launch_app._reap_process_group()
    assert not killed


def test_reap_never_signals_itself(monkeypatch):
    """killpg has no way to spare the caller; the pid sweep must."""
    import os
    me = os.getpid()
    monkeypatch.setattr(os, "getpgid", lambda _: me)   # pretend we lead
    monkeypatch.setattr(launch_app, "_group_members",
                        lambda pgid: [me + 1, me + 2])
    killed = []
    monkeypatch.setattr(os, "kill", lambda pid, sig: killed.append(pid))
    monkeypatch.setattr(launch_app.time, "sleep", lambda _: None)
    launch_app._reap_process_group()
    assert me not in killed and killed


# -------------------------------------------------------- the backend server
#
# The .app used to run uvicorn on a thread, where --reload is impossible: the
# flag is read only by uvicorn.run(), and the reloader restarts its target
# process, which in-process is the window. These pin the subprocess shape that
# replaced it — silent regressions again, since a window with a stale server
# looks exactly like a window with a live one.

class _Proc:
    def __init__(self, rc=None):
        self.returncode, self.killed = rc, False

    def poll(self):
        return self.returncode

    def kill(self):
        self.killed = True


def _spawned(monkeypatch):
    """Capture the argv/kwargs _start_backend would hand to Popen."""
    seen = {}

    def fake_popen(cmd, **kw):
        seen["cmd"], seen["kw"] = cmd, kw
        return _Proc()

    monkeypatch.setattr(launch_app.subprocess, "Popen", fake_popen)
    launch_app._start_backend()
    return seen


def test_the_server_inherits_the_windows_notification_pipe(monkeypatch):
    """Banners go through the window process, which is the bundle (#80): the
    server gets the pipe's write end by fd and by name, and nothing else does."""
    seen = {}

    def fake_popen(cmd, **kw):
        seen.update(kw)
        return _Proc()

    monkeypatch.setattr(launch_app.subprocess, "Popen", fake_popen)
    launch_app._start_backend(7)
    assert seen["pass_fds"] == (7,)
    assert seen["env"][launch_app.NOTIFY_FD_ENV] == "7"
    launch_app._start_backend()
    assert seen["pass_fds"] == () and launch_app.NOTIFY_FD_ENV not in seen["env"]


def test_the_pipe_reader_posts_each_line_and_survives_a_bad_one():
    import os
    import time
    posted = []
    w = launch_app._notification_pipe(lambda *a: posted.append(a))
    os.write(w, b'{"title": "Kestrel finished", "body": "tab", "target": "t1"}\n'
                b"not json\n"
                b'{"title": "Amazon shipped"}\n')
    for _ in range(50):
        if len(posted) == 2:
            break
        time.sleep(0.02)
    os.close(w)
    assert posted == [("Kestrel finished", "tab", "t1"), ("Amazon shipped", "", None)]


def test_the_backend_starts_without_uvicorns_reloader(monkeypatch):
    """--reload restarts the instant a file changes — under a dev tab whose
    turn is running inside this server. `Backend` reloads instead."""
    seen = _spawned(monkeypatch)
    assert seen["cmd"][1:4] == ["-m", "uvicorn", "app.main:app"]
    assert "--reload" not in seen["cmd"]
    assert seen["kw"]["cwd"] == launch_app.BACKEND


def test_reload_watches_only_the_app_package():
    """Not backend/, which also holds data/ (app.db and its WAL) and .venv."""
    assert launch_app.WATCH_DIR == launch_app.BACKEND / "app"


class _Server(_Proc):
    def __init__(self):
        super().__init__()
        self.terminated = False

    def terminate(self):
        self.terminated = True

    def wait(self, timeout=None):
        return 0


def _backend(monkeypatch, turns):
    """A Backend over fake servers, with /api/console/busy answering `turns`
    in order and every wait instant."""
    started = []
    monkeypatch.setattr(launch_app, "_start_backend",
                        lambda fd=None: started.append(_Server()) or started[-1])
    answers = iter(turns)
    monkeypatch.setattr(launch_app, "_console_turns", lambda: next(answers))
    b = launch_app.Backend()
    monkeypatch.setattr(b._stopped, "wait", lambda t=None: b._stopped.is_set())
    return b, started


def test_a_restart_waits_for_the_console_turns_to_finish(monkeypatch):
    asked = []
    b, started = _backend(monkeypatch, [2, 1, 0])
    real = launch_app._console_turns
    monkeypatch.setattr(launch_app, "_console_turns", lambda: asked.append(1) or real())
    b._hold_while_busy(["email.py"])
    assert len(asked) == 3                     # held through 2 and 1, went at 0
    b.restart("email.py")
    assert started[0].terminated and len(started) == 2 and b.proc is started[1]


def test_a_server_that_cannot_answer_does_not_hold(monkeypatch):
    """A bad edit crashes the server; the next edit has to be able to fix it."""
    b, _ = _backend(monkeypatch, [None])
    b._hold_while_busy(["x.py"])               # returns: nothing left to protect


def test_a_wedged_turn_holds_only_so_long(monkeypatch):
    b, _ = _backend(monkeypatch, [1] * 1000)
    monkeypatch.setattr(launch_app, "MAX_HOLD_S", 0)
    b._hold_while_busy(["x.py"])               # returns instead of spinning


def test_no_restart_after_the_window_closes(monkeypatch):
    b, started = _backend(monkeypatch, [])
    b.stop()
    b.restart("x.py")
    assert len(started) == 1 and not started[0].terminated


def test_an_edit_under_app_restarts_the_server(tmp_path, monkeypatch):
    """The watch loop end to end, on a real directory and a real clock."""
    import os
    import time
    (tmp_path / "mod.py").write_text("x = 1")
    monkeypatch.setattr(launch_app, "WATCH_DIR", tmp_path)
    monkeypatch.setattr(launch_app, "SETTLE_S", 0.05)
    started = []
    monkeypatch.setattr(launch_app, "_start_backend",
                        lambda fd=None: started.append(_Server()) or started[-1])
    monkeypatch.setattr(launch_app, "_console_turns", lambda: 0)
    b = launch_app.Backend()
    b.watch()
    try:
        time.sleep(0.2)
        (tmp_path / "data.db").write_text("not python")   # filtered out
        os.utime(tmp_path / "mod.py", (time.time() + 5, time.time() + 5))
        deadline = time.time() + 5
        while len(started) < 2 and time.time() < deadline:
            time.sleep(0.1)
        assert len(started) == 2 and started[0].terminated
    finally:
        b.stop()


def test_the_server_runs_under_the_venv_not_the_bundle_stub(monkeypatch):
    """sys.executable inside the .app is the copied stub that gives this
    process its name and icon; the reloader spawns children with it."""
    stub = "/Applications/Kestrel.app/Contents/MacOS/Kestrel"
    monkeypatch.setattr(launch_app.sys, "executable", stub)
    assert launch_app._backend_python() != stub
    assert launch_app._backend_python().endswith(".venv/bin/python")


def test_a_missing_venv_falls_back_to_this_interpreter(tmp_path, monkeypatch):
    monkeypatch.setattr(launch_app, "BACKEND", tmp_path)
    assert launch_app._backend_python() == launch_app.sys.executable


def _unreachable(monkeypatch):
    """No backend on the port — including the real one, if it's running."""
    def refuse(*a, **k):
        raise OSError("connection refused")
    monkeypatch.setattr(launch_app.urllib.request, "urlopen", refuse)


def test_health_gives_up_as_soon_as_the_backend_dies(monkeypatch):
    """An import error in app/ exits uvicorn immediately — waiting out the
    full timeout on a socket that will never open just delays the message."""
    _unreachable(monkeypatch)
    slept = []
    monkeypatch.setattr(launch_app.time, "sleep", slept.append)
    assert launch_app._wait_for_health(_Proc(rc=1), timeout=30.0) is False
    assert not slept, "should not have waited on a dead process"


def test_health_keeps_polling_while_the_backend_lives(monkeypatch):
    _unreachable(monkeypatch)
    slept = []
    monkeypatch.setattr(launch_app.time, "sleep", slept.append)
    assert launch_app._wait_for_health(_Proc(rc=None), timeout=0.5) is False
    assert slept, "a live process should keep the poll going to the deadline"


# ------------------------------------------------- the generated app bundle
#
# The run script is written by make-app.sh into a gitignored bundle, so it has
# no other coverage — and both bugs below were silent: nothing failed, the app
# just quietly did the wrong thing. Asserting on the template is crude, but it
# is the only thing standing between a one-token edit and another silent
# regression.

def _run_script():
    """make-app.sh's run-script template, as it will be written.

    The heredoc escapes runtime expansions (`\\$STUB`) to keep them out of the
    generator's own substitution, so match on the resolved text rather than on
    the escaping, which is incidental.
    """
    return MAKE_APP.read_text().replace("\\$", "$")


def test_the_generated_launch_is_unbuffered():
    """Python block-buffers stdout through the log redirect, so a crash
    flushes nothing — the log read empty exactly when it was needed."""
    script = _run_script()
    assert script.count("-u launch_app.py") == 2, (
        "both the bundled-interpreter and the venv fallback paths must pass "
        "-u, or the log is empty for whichever one ran")


def test_the_bundle_carries_its_own_interpreter():
    """Identity comes from the running executable, not from Info.plist.

    CoreFoundation resolves the venv's python symlink to the real framework
    binary and walks up from there, landing in Python.framework's own
    Python.app — which is why the app was called "Python", showed the generic
    rocket, and registered as org.python.python.
    """
    script = _run_script()
    assert 'cp "$STUB_SRC" "$MACOS/Kestrel"' in script
    assert 'exec "$STUB" -u launch_app.py' in script, (
        "the interpreter that actually runs has to live inside the bundle")
    assert "local.vault-dashboard" in script


def test_a_broken_interpreter_copy_falls_back():
    """The stub is linked against an exact Cellar path, so `brew upgrade
    python@3.12` leaves a copy that cannot load. Launching as "Python" beats
    not launching at all."""
    script = _run_script()
    assert '"$STUB" -c "" 2>/dev/null' in script, "the copy must be probed"
    assert "source .venv/bin/activate" in script, "…with the venv as fallback"


class _R:
    def __init__(self, rc, out=""):
        self.returncode, self.stdout, self.stderr = rc, out, ""


def _ok():
    return _R(0)


def _fail(msg):
    return _R(1, msg)


def test_two_reloads_mid_build_build_once(tmp_path, monkeypatch):
    """⌘R reruns the build check (a window left open across a commit served
    the launch-time dist forever). A second ⌘R while the first build runs
    waits for it, then finds the dist current, instead of building again."""
    import threading
    import time
    fe = tmp_path / "frontend"
    _tree(fe / "src", when=2000)
    _tree(fe / "dist", when=1000)
    monkeypatch.setattr(launch_app, "FRONTEND", fe)
    monkeypatch.setattr(launch_app, "DIST", fe / "dist")

    calls = []

    def build(*a, **k):
        calls.append(a)
        time.sleep(0.2)
        _tree(fe / "dist", when=3000)      # the build writes a fresh dist
        return _ok()
    monkeypatch.setattr(subprocess, "run", build)
    threads = [threading.Thread(target=launch_app.ensure_frontend_built) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(calls) == 1


# ---------- opening the app takes the port over ----------

def _ports(monkeypatch, listeners, procs):
    """lsof answers `listeners`; ps answers from `procs` {pid: (ppid, cmd)}."""
    def run(cmd, **kw):
        out = ""
        if cmd[0] == "lsof":
            out = "\n".join(map(str, listeners))
        elif cmd[0] == "ps":
            pid = int(cmd[-1])
            if pid in procs:
                out = f"{procs[pid][0]} {procs[pid][1]}"
        return subprocess.CompletedProcess(cmd, 0, out, "")
    monkeypatch.setattr(launch_app.subprocess, "run", run)


UVI = "/opt/python -m uvicorn app.main:app --host 127.0.0.1 --port 8788"


def test_a_terminal_reload_server_is_ours_to_stop_parent_included(monkeypatch):
    # --reload: the parent and its spawn-stub worker both hold the socket.
    _ports(monkeypatch, [4857, 83205], {
        83205: (1, UVI + " --reload --reload-dir app"),
        4857: (83205, "/opt/python -c from multiprocessing.spawn import spawn_main"),
        1: (0, "/sbin/launchd")})
    assert launch_app._port_holders() == [4857, 83205]


def test_something_else_on_the_port_is_left_alone(monkeypatch):
    _ports(monkeypatch, [700], {700: (1, "/usr/bin/python3 -m http.server 8788"),
                                1: (0, "/sbin/launchd")})
    assert launch_app._port_holders() is None
    killed = []
    monkeypatch.setattr(launch_app.os, "kill", lambda *a: killed.append(a))
    assert launch_app._take_over_port() is False and killed == []


def test_take_over_stops_the_holders_until_the_port_frees(monkeypatch):
    _ports(monkeypatch, [900], {900: (1, UVI), 1: (0, "/sbin/launchd")})
    killed = []
    monkeypatch.setattr(launch_app.os, "kill", lambda pid, sig: killed.append((pid, sig)))
    monkeypatch.setattr(launch_app, "_port_taken", lambda: not killed)
    assert launch_app._take_over_port() is True
    assert killed == [(900, launch_app.signal.SIGTERM)]
