"""The lint checks held by pytest so the commit bar covers what the lint-on-edit hook missed (edits outside Claude Code, changed rules).

- backend: `ruff check` with backend/ruff.toml.
- frontend: scripts/lint-ratchet.mjs (no ESLint errors; useEffect count within lint-baseline.json).
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
