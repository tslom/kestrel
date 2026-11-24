"""Tabs at rest and pinned/reordered: runs the frontend node tests (frontend/tests/tabRest.test.mjs) under pytest."""

import shutil
import subprocess
from pathlib import Path

FRONTEND = Path(__file__).resolve().parents[2] / "frontend"


def test_tab_rest_logic():
    node = shutil.which("node")
    assert node, "node isn't on PATH; the tab rest tests can't run"
    r = subprocess.run([node, "--test", "tests/tabRest.test.mjs"], cwd=FRONTEND,
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stdout[-4000:] + r.stderr[-2000:]
