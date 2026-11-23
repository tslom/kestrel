"""The lint checks, held by pytest so the commit bar ("the backend suite
passes", CLAUDE.md) covers them too. The lint-on-edit hook catches a file as
it's written; this catches whatever got past it (an edit made outside Claude
Code, or a rule that changed under old code).

- backend: `ruff check` with backend/ruff.toml (pyflakes, the date.today ban).
- frontend: scripts/lint-ratchet.mjs — no ESLint errors (raw fetch outside
  lib/api.js, raw setTimeout/clearTimeout outside lib/timers.js, the hooks
  and you-might-not-need-an-effect rules, a suppression without a reason),
  and no more useEffect calls than lint-baseline.json allows.
"""

import shutil
import subprocess
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
FRONTEND = BACKEND.parent / "frontend"


def test_ruff_is_clean():
    ruff = Path(sys.executable).with_name("ruff")
    r = subprocess.run([str(ruff), "check", "--output-format", "concise", "."],
                       cwd=BACKEND, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stdout + r.stderr


def test_frontend_lint_ratchet():
    node = shutil.which("node")
    assert node, "node isn't on PATH; the frontend lint can't run"
    assert (FRONTEND / "node_modules" / "eslint").is_dir(), "run `npm install` in frontend/"
    r = subprocess.run([node, "scripts/lint-ratchet.mjs"], cwd=FRONTEND,
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stdout + r.stderr
