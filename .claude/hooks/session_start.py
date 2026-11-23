"""SessionStart hook: a worktree gets the main checkout's gitignored setup.

Kestrel links a dev tab's venv, .env, data and node_modules when it makes the
tab's worktree (backend/app/console_worktree.py). A worktree made any other
way (`claude --worktree`, the overnight jobs, `git worktree add` by hand) got
none of it. On 11-18 an overnight session found no `.venv/bin/python`, and
the worktree guard refused all 7 of its tries to link one by hand. Without
the venv's ruff and node_modules' eslint, lint_on_edit.py also passes every
edit silently. This links the same list (.kestrel/worktree.json) when the
session starts. The main checkout and an already-linked worktree are left as
they are, and a failure never stops the session.
"""

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]   # the checkout (or worktree) this is in


def _console_worktree():
    # By file, not `from app import ...`: system python3 runs hooks, without
    # the venv, and the module is stdlib-only. No .pyc: this python isn't the
    # venv's, and the hook should leave nothing behind.
    sys.dont_write_bytecode = True
    path = ROOT / "backend" / "app" / "console_worktree.py"
    spec = importlib.util.spec_from_file_location("console_worktree", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main() -> int:
    try:
        cw = _console_worktree()
        repo = cw.main_checkout(ROOT)
        if repo.resolve() == ROOT:
            return 0
        made = cw.link(repo, ROOT)
    except Exception as e:  # anything: a setup hook must never block the session
        print(f"worktree setup skipped: {e}")
        return 0
    if made:
        print(f"Linked from the main checkout ({repo}): {', '.join(made)}. "
              "backend/data is the real app.db.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
