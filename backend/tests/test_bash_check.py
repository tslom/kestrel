""".claude/hooks/bash_check.py, replayed on the mistakes it stops.

Blocked commands come from transcripts (each failed); passes are near misses. Each rule's
premise is checked (the blocked sqlite3 form is a syntax error, the perl bound bounds), and
the hook is also run under macOS's python3 3.9.
"""

import importlib.util
import json
import shutil
import signal
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
HOOK = ROOT / ".claude" / "hooks" / "bash_check.py"
PYTHONS = [sys.executable] + [p for p in ("/usr/bin/python3",) if Path(p).exists()]
BARE_PATH = {"PATH": "/usr/bin:/bin"}   # no coreutils, whatever this machine has


@pytest.fixture
def bash_check(monkeypatch):
    spec = importlib.util.spec_from_file_location("bash_check", HOOK)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod.shutil, "which", lambda name: None)
    return mod


def run_hook(event, python=sys.executable, env=None):
    return subprocess.run([python, str(HOOK)], input=json.dumps(event),
                          capture_output=True, text=True, timeout=30, env=env)


def bash(command):
    return {"hook_event_name": "PreToolUse", "tool_name": "Bash",
            "tool_input": {"command": command}}


TIMEOUTS = [
    "cd /tmp/kv-verify && timeout 90 node run.mjs",
    "cd /Users/me/Projects/vault-dashboard/backend && timeout 600 .venv/bin/python -m pytest -q -x 2>&1 | tail -3",
    'cd backend && VAULT_PATH=/Users/me/Documents/main-vault timeout 60 .venv/bin/python -c "import time"',
    'cd /private/tmp && env -u ANTHROPIC_API_KEY timeout 90 /Users/me/.local/bin/claude -p --model opus "reply ok" 2>&1 | head -c 1500',
    'which gtimeout; gtimeout 60 .venv/bin/python -c "pass"',
    "npx esbuild /tmp/sbtest/t.jsx --bundle --platform=node --outfile=/tmp/sbtest/out.cjs && timeout 30 node /tmp/sbtest/out.cjs",
    "mkdir -p /tmp/kui/before && cd /tmp/kui && timeout 200 node shoot.mjs /tmp/kui/before && ls before",
    "cat > /tmp/kshot/shot.mjs <<'EOF'\nimport { chromium } from \"playwright-core\";\nEOF\ntimeout 60 node /tmp/kshot/shot.mjs base",
]
DOT_COMMANDS = [
    'sqlite3 data/app.db "PRAGMA busy_timeout=5000; .schema list_items" ; sqlite3 -header data/app.db "PRAGMA busy_timeout=5000; select li.* from list_items li"',
    'sqlite3 /Users/me/Projects/vault-dashboard/backend/data/app.db "PRAGMA busy_timeout=5000; .schema list_items" "SELECT l.id,l.name FROM lists l;"',
    "sqlite3 backend/data/app.db \"PRAGMA busy_timeout=5000; .tables\" | tr ' ' '\\n' | grep -i fail",
    "sqlite3 /Users/me/Projects/vault-dashboard/backend/data/app.db \"PRAGMA busy_timeout=5000; .backup '$S/data/app.db'\"",
    "sqlite3 -cmd 'PRAGMA busy_timeout=5000; .mode json' backend/data/app.db 'select 1'",
]
PASSES = [
    "sqlite3 -cmd '.timeout 5000' backend/data/app.db '.schema list_items' 'SELECT id FROM lists'",
    'sqlite3 backend/data/app.db "PRAGMA busy_timeout=5000; SELECT id, name FROM list_items WHERE list_id=2"',
    'sqlite3 backend/data/app.db "PRAGMA busy_timeout=5000;" ".schema list_items"',
    "sqlite3 backend/data/app.db \"select name from list_items where name like '%.json%'\"",
    "sqlite3 /tmp/x.db .tables",
    'grep -n "timeout" backend/app/console.py',
    "python -m pytest -q --timeout 30 tests/test_x.py",
    'echo "timeout 30 node x"',
    'git commit -qm "fix: timeout 30 on the probe"',
    "which timeout gtimeout; command -v timeout",
    "timeout=5; echo $timeout",
    "cat > /tmp/probe.py <<'EOF'\nimport subprocess\ntimeout = 30\nsubprocess.run(['true'], timeout=timeout)\nEOF\npython3 /tmp/probe.py",
    'node -e "setTimeout(() => process.exit(0), 10)"',
    'echo "unterminated',
]


@pytest.mark.parametrize("command", TIMEOUTS)
def test_a_timeout_that_cannot_run_is_blocked_with_the_fix(bash_check, command):
    msg = bash_check.check(command)
    assert msg and "process.exit(0)" in msg and "perl -e 'alarm shift; exec @ARGV'" in msg


@pytest.mark.parametrize("command", DOT_COMMANDS)
def test_a_dot_command_after_sql_is_blocked_with_the_fix(bash_check, command):
    msg = bash_check.check(command)
    assert msg and "-cmd '.timeout 5000'" in msg


@pytest.mark.parametrize("command", PASSES)
def test_near_misses_pass(bash_check, command):
    assert bash_check.check(command) is None


def test_timeout_is_allowed_once_one_exists(tmp_path):
    fake = tmp_path / "timeout"
    fake.write_text("#!/bin/sh\nshift; exec \"$@\"\n")
    fake.chmod(0o755)
    r = run_hook(bash("timeout 30 node x.mjs"), env={"PATH": f"{tmp_path}:/usr/bin:/bin"})
    assert r.returncode == 0, r.stderr


@pytest.mark.parametrize("python", PYTHONS)
def test_the_hook_blocks_with_exit_2_and_passes_with_0(python):
    blocked = run_hook(bash(TIMEOUTS[0]), python, BARE_PATH)
    assert blocked.returncode == 2 and "macOS has no `timeout`" in blocked.stderr
    ok = run_hook(bash(PASSES[0]), python, BARE_PATH)
    assert ok.returncode == 0 and ok.stderr == ""
    other = run_hook({"tool_name": "Read", "tool_input": {}}, python, BARE_PATH)
    assert other.returncode == 0


def test_the_blocked_sqlite3_form_is_a_syntax_error_and_the_fix_works(tmp_path):
    if not shutil.which("sqlite3"):
        pytest.skip("no sqlite3 CLI")
    db = tmp_path / "app.db"
    con = sqlite3.connect(db)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("CREATE TABLE list_items (id INTEGER PRIMARY KEY, name TEXT)")
    con.commit()
    con.close()
    bad = subprocess.run(["sqlite3", str(db), "PRAGMA busy_timeout=5000; .schema list_items"],
                         capture_output=True, text=True, timeout=30)
    assert bad.returncode != 0 and "syntax error" in bad.stderr
    good = subprocess.run(["sqlite3", "-cmd", ".timeout 5000", str(db), ".schema list_items",
                           "select count(*) from list_items"],
                          capture_output=True, text=True, timeout=30)
    assert good.returncode == 0, good.stderr
    assert "CREATE TABLE list_items" in good.stdout and good.stdout.strip().endswith("0")
    assert "5000" not in good.stdout     # the PRAGMA form prints it into every result


def test_the_recommended_perl_bound_bounds():
    if not shutil.which("perl"):
        pytest.fail("no perl: CLAUDE.md and bash_check.py recommend a bound that no longer works")
    t = time.monotonic()
    r = subprocess.run(["perl", "-e", "alarm shift; exec @ARGV", "1", "sleep", "5"], timeout=30)
    assert r.returncode == -signal.SIGALRM and time.monotonic() - t < 4


def test_settings_run_it_before_every_bash_call():
    hooks = json.loads((ROOT / ".claude" / "settings.json").read_text())["hooks"]
    assert any("$CLAUDE_PROJECT_DIR/.claude/hooks/bash_check.py" in h["command"]
               for entry in hooks.get("PreToolUse", []) if entry.get("matcher") == "Bash"
               for h in entry["hooks"])
