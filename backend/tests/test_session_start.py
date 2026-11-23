""".claude/hooks/session_start.py: a worktree gets the main checkout's setup.

Against real git repos in tmp, with the hook and console_worktree copied in,
since the hook finds both relative to itself. Run under macOS's python3 too
when it's there: hooks run on PATH's python3, outside the venv.
"""

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PYTHONS = [sys.executable] + [p for p in ("/usr/bin/python3",) if Path(p).exists()]


def git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], check=True,
                          capture_output=True, text=True).stdout.strip()


@pytest.fixture
def main_and_tree(tmp_path):
    main = tmp_path / "main"
    for rel in (".claude/hooks/session_start.py", "backend/app/console_worktree.py"):
        (main / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(ROOT / rel, main / rel)
    (main / ".kestrel").mkdir()
    (main / ".kestrel" / "worktree.json").write_text(json.dumps(
        {"link": ["backend/.venv", "backend/data"], "setup": ["touch setup-ran"]}))
    (main / ".gitignore").write_text("backend/.venv/\nbackend/data/\n")
    git(tmp_path, "init", "-q", "-b", "main", str(main))
    git(main, "config", "user.email", "t@example.com")
    git(main, "config", "user.name", "t")
    git(main, "add", "-A")
    git(main, "commit", "-qm", "init")
    (main / "backend" / ".venv" / "bin").mkdir(parents=True)
    (main / "backend" / "data").mkdir()
    (main / "backend" / "data" / "app.db").write_text("real")
    tree = tmp_path / "tree"
    git(main, "worktree", "add", "-q", "-b", "t", str(tree))
    return main, tree


def start(where, python=sys.executable):
    return subprocess.run([python, str(where / ".claude" / "hooks" / "session_start.py")],
                          input="{}", capture_output=True, text=True, timeout=30)


@pytest.mark.parametrize("python", PYTHONS)
def test_a_worktree_made_outside_kestrel_gets_its_links(main_and_tree, python):
    main, tree = main_and_tree
    r = start(tree, python)
    assert r.returncode == 0 and "backend/.venv, backend/data" in r.stdout, r.stdout + r.stderr
    assert (tree / "backend" / "data").is_symlink()
    assert (tree / "backend" / "data" / "app.db").read_text() == "real"
    assert git(tree, "status", "--porcelain") == ""   # links never get committed; no .pyc left
    assert not (tree / "setup-ran").exists()          # links only: no setup commands
    again = start(tree, python)
    assert again.returncode == 0 and again.stdout == ""


def test_the_main_checkout_is_left_alone(main_and_tree):
    main, _ = main_and_tree
    r = start(main)
    assert r.returncode == 0 and r.stdout == ""
    assert not (main / "backend" / ".venv").is_symlink()


def test_a_broken_config_never_stops_the_session(main_and_tree):
    main, tree = main_and_tree
    (main / ".kestrel" / "worktree.json").write_text("{not json")
    r = start(tree)
    assert r.returncode == 0 and "skipped" in r.stdout


def test_settings_run_it_at_session_start():
    hooks = json.loads((ROOT / ".claude" / "settings.json").read_text())["hooks"]
    assert any("$CLAUDE_PROJECT_DIR/.claude/hooks/session_start.py" in h["command"]
               for entry in hooks.get("SessionStart", []) for h in entry["hooks"])
