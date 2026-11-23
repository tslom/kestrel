"""The diff pane's frontend logic: syntax colour (lib/syntax.js) and line
comments (lib/review.js). Its node tests (frontend/tests/review.test.mjs)
run under pytest, which is the commit bar — the backend half of the review
is test_console_review.py."""

import shutil
import subprocess
from pathlib import Path

FRONTEND = Path(__file__).resolve().parents[2] / "frontend"


def test_review_pane_logic():
    node = shutil.which("node")
    assert node, "node isn't on PATH; the review tests can't run"
    r = subprocess.run([node, "--test", "tests/review.test.mjs"], cwd=FRONTEND,
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stdout[-4000:] + r.stderr[-2000:]
