"""Child tabs (grouping, working-cap queue, done state): runs frontend/tests/children.test.mjs
(lib/children.js) under pytest, the commit bar."""

import shutil
import subprocess
from pathlib import Path

FRONTEND = Path(__file__).resolve().parents[2] / "frontend"


def test_child_tab_logic():
    node = shutil.which("node")
    assert node, "node isn't on PATH; the child-tab tests can't run"
    r = subprocess.run([node, "--test", "tests/children.test.mjs"], cwd=FRONTEND,
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stdout[-4000:] + r.stderr[-2000:]
