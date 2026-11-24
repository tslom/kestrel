"""A handed-off tab's report fires at the real end of its work; runs frontend/tests/handoff.test.mjs under pytest (logic is lib/handoff.js)."""

import shutil
import subprocess
from pathlib import Path

FRONTEND = Path(__file__).resolve().parents[2] / "frontend"


def test_handoff_report_logic():
    node = shutil.which("node")
    assert node, "node isn't on PATH; the handoff tests can't run"
    r = subprocess.run([node, "--test", "tests/handoff.test.mjs"], cwd=FRONTEND,
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stdout[-4000:] + r.stderr[-2000:]
