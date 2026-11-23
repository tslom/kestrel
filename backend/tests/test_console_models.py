"""The model pickers' list comes from the CLI (console.session_models), and
the frontend caches it per profile (lib/consolePrefs.js, whose node tests
run here so pytest holds them)."""

import asyncio
import shutil
import subprocess
from pathlib import Path

from app import console

FRONTEND = Path(__file__).resolve().parents[2] / "frontend"


def test_session_models_keeps_alias_resolved_name_and_efforts():
    info = {"models": [
        {"value": "fable", "resolvedModel": "claude-fable-5-2", "displayName": "Fable 5.2",
         "supportsEffort": True, "supportedEffortLevels": ["low", "high", "max"]},
        {"value": "claude-haiku-4-5", "resolvedModel": "claude-haiku-4-5", "displayName": "Haiku 4.5",
         "supportedEffortLevels": ["low"]},   # no supportsEffort: no effort setting
        {"resolvedModel": "nameless"},
    ]}
    assert console.session_models(info) == [
        {"value": "fable", "resolved": "claude-fable-5-2", "label": "Fable 5.2",
         "efforts": ["low", "high", "max"]},
        {"value": "claude-haiku-4-5", "resolved": "claude-haiku-4-5", "label": "Haiku 4.5",
         "efforts": []},
    ]
    assert console.session_models({}) == []


class _WS:
    def __init__(self):
        self.sent = []

    async def send_json(self, m):
        self.sent.append(m)


def test_note_model_sends_only_changes_and_skips_synthetic():
    ws, state = _WS(), console.SessionState()
    for m in ["claude-opus-5-5", "claude-opus-5-5", "<synthetic>", None, "",
              "claude-fable-5-2"]:
        asyncio.run(console.note_model(ws, state, m))
    assert ws.sent == [{"type": "model", "model": "claude-opus-5-5"},
                       {"type": "model", "model": "claude-fable-5-2"}]
    assert state.model == "claude-fable-5-2"


def test_console_bar_node():
    """The model list's cache (consolePrefs), the status bar's fold (barHost)
    and when a tab the usage limit cut off picks itself back up (autoResume)."""
    node = shutil.which("node")
    assert node, "node isn't on PATH; the console bar's tests can't run"
    r = subprocess.run([node, "--test", "tests/consolePrefs.test.mjs", "tests/barHost.test.mjs",
                        "tests/autoResume.test.mjs"],
                       cwd=FRONTEND, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stdout[-4000:] + r.stderr[-2000:]
