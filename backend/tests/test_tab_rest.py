"""Tabs at rest (#91) and pinned/reordered (#102): snoozed until a time,
settled onto a list, settled by themselves when merged or a week idle. The
logic is frontend (lib/tabRest.js), so this runs its node tests
(frontend/tests/tabRest.test.mjs) under pytest, which is the commit bar."""

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
