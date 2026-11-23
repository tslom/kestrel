"""File links in console replies: which spans are asked about is frontend
(lib/paths.js), so this runs its node tests (frontend/tests/paths.test.mjs)
under pytest, which is the commit bar. The backend half is test_files.py."""

import shutil
import subprocess
from pathlib import Path

FRONTEND = Path(__file__).resolve().parents[2] / "frontend"


def test_path_detection():
    node = shutil.which("node")
    assert node, "node isn't on PATH; the path tests can't run"
    r = subprocess.run([node, "--test", "tests/paths.test.mjs"], cwd=FRONTEND,
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stdout[-4000:] + r.stderr[-2000:]
