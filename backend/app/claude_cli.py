"""The Claude Code CLI the console drives: which version, whether a newer one
is out, and the button that installs it.

The console runs `CONSOLE_CLI_PATH` (a native install: a symlink into
~/.local/share/claude/versions/<version>), and this machine has auto-updates
off, so nothing tells you a release landed short of opening a terminal.

- `GET /api/console/cli` — installed vs latest. The installed version is the
  symlink's target name, so asking costs no process. The latest is the
  channel file the native installer itself reads
  (`downloads.claude.ai/claude-code-releases/<channel>`, a bare version
  string), fetched lazily and held an hour: the bar polls, and a release
  check per poll would be traffic for nothing.
- `POST /api/console/cli/update` — runs `claude update`, the CLI's own
  updater, which repoints the symlink. A running session keeps the binary it
  started with; the next connect (a new tab, or a model/mode switch) gets the
  new one — the page reconnects its tab for that.
"""

import asyncio
import json
import re
import subprocess
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from fastapi import APIRouter, HTTPException

from .config import CONSOLE_CLI_PATH

router = APIRouter()

RELEASES_URL = "https://downloads.claude.ai/claude-code-releases/{channel}"
LATEST_TTL = 60 * 60          # a successful check holds an hour
LATEST_RETRY = 5 * 60         # a failed one is retried sooner
UPDATE_TIMEOUT = 300          # a download on a slow link, not a hang
SETTINGS = Path.home() / ".claude" / "settings.json"

_VERSION = re.compile(r"\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?")


def _vkey(v: str) -> tuple:
    """Sortable form of `2.1.312` — numeric, so .312 beats .99."""
    core = v.split("-", 1)[0]
    return tuple(int(p) for p in core.split("."))


def installed_version(cli: Path | None = None) -> str | None:
    """The version CONSOLE_CLI_PATH runs, or None when it isn't there."""
    cli = cli or CONSOLE_CLI_PATH
    try:
        target = cli.resolve(strict=True)
    except OSError:
        return None
    if _VERSION.fullmatch(target.name):
        return target.name
    # Not the native layout (a package-manager install, a wrapper): ask it.
    try:
        out = subprocess.run([str(cli), "--version"], capture_output=True,
                             text=True, timeout=15).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    m = _VERSION.search(out)
    return m.group(0) if m else None


def channel() -> str:
    """`autoUpdatesChannel` from user settings — "latest" unless set to stable."""
    try:
        value = json.loads(SETTINGS.read_text()).get("autoUpdatesChannel")
    except (OSError, ValueError, AttributeError):
        value = None
    return "stable" if value == "stable" else "latest"


class _Latest:
    lock = threading.Lock()
    version: str | None = None
    channel: str | None = None
    checked: float = 0.0
    next_try: float = 0.0


def _fetch_latest(chan: str) -> str | None:
    req = urllib.request.Request(RELEASES_URL.format(channel=chan),
                                 headers={"User-Agent": "vault-dashboard"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            body = r.read(64).decode("ascii", "replace").strip()
    except (urllib.error.URLError, OSError, ValueError):
        return None
    return body if _VERSION.fullmatch(body) else None


def latest_version(force: bool = False) -> tuple[str | None, float | None]:
    """(latest on this machine's channel, when it was checked)."""
    chan = channel()
    now = time.time()
    with _Latest.lock:
        fresh = (_Latest.channel == chan and now < _Latest.next_try)
        if fresh and not force:
            return _Latest.version, _Latest.checked or None
    found = _fetch_latest(chan)
    with _Latest.lock:
        if found:
            _Latest.version, _Latest.channel, _Latest.checked = found, chan, now
            _Latest.next_try = now + LATEST_TTL
        else:
            # Keep the last answer on offer (it is for this channel only).
            if _Latest.channel != chan:
                _Latest.version, _Latest.checked = None, 0.0
            _Latest.channel = chan
            _Latest.next_try = now + LATEST_RETRY
        return _Latest.version, _Latest.checked or None


def status(force: bool = False) -> dict:
    installed = installed_version()
    latest, checked = latest_version(force)
    return {
        "installed": installed,
        "latest": latest,
        "channel": channel(),
        "checked_at": checked,
        "update_available": bool(installed and latest
                                 and _vkey(latest) > _vkey(installed)),
    }


@router.get("/api/console/cli")
def console_cli(refresh: bool = False) -> dict:
    return status(force=refresh)


_update_lock = asyncio.Lock()


@router.post("/api/console/cli/update")
async def console_cli_update() -> dict:
    """Run the CLI's own updater. One at a time: two would race on the symlink."""
    if _update_lock.locked():
        raise HTTPException(409, "an update is already running")
    async with _update_lock:
        before = installed_version()
        if before is None:
            raise HTTPException(404, f"no claude CLI at {CONSOLE_CLI_PATH}")
        try:
            proc = await asyncio.create_subprocess_exec(
                str(CONSOLE_CLI_PATH), "update",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
            out, _ = await asyncio.wait_for(proc.communicate(), UPDATE_TIMEOUT)
        except OSError as e:
            raise HTTPException(500, f"couldn't run claude update: {e}")
        except asyncio.TimeoutError:
            proc.kill()
            raise HTTPException(504, f"claude update ran past {UPDATE_TIMEOUT}s")
        text = out.decode("utf-8", "replace").strip()
        after = installed_version()
        result = status()
        return {**result, "ok": proc.returncode == 0, "before": before,
                "updated": bool(after and after != before),
                "output": text[-2000:]}
