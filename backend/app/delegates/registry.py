"""Which models Claude can send work to, and whether each is ready.

A provider is a CLI on this machine, driven headless: the same way T3 Code
does it, and the reason nothing here holds an API key. Ready means
installed, signed in and switched on; the first two are read off the machine
(cached briefly, since one of them runs the CLI), the switch is the user's
(store.switches).

The command lines were written against `gemini --help` 0.70.0 and
`codex exec --help` 0.171.0. What each prints when it finishes (`parse`) has
not been checked against a signed-in CLI: neither was installed when this was
written, so parsing falls back to the raw text when the shape isn't the one
expected.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from ..config import CONSOLE_CLI_PATH
from . import store

STATUS_TTL_S = 20
PROBE_TIMEOUT_S = 10
CODEX_REPLY = ".reply.txt"


@dataclass(frozen=True)
class Reply:
    text: str
    tokens_in: int | None = None
    tokens_out: int | None = None
    model: str | None = None


@dataclass(frozen=True)
class Provider:
    id: str
    label: str            # the product, as the user knows it
    vendor: str
    binary: str
    outside: bool         # not Claude: brief only, sandboxed (boundary.py)
    install: str = ""
    login: str = ""
    good_for: str = ""
    env_keys: tuple[str, ...] = ()
    config_dir: Path | None = None
    stdin_brief: bool = False
    argv: Callable[[str, Path, str | None], list[str]] | None = field(default=None, repr=False)
    parse: Callable[[str, Path], Reply] | None = field(default=None, repr=False)
    signed_in: Callable[[], tuple[bool, str]] | None = field(default=None, repr=False)


# ---------- gemini ----------

def _gemini_argv(brief: str, scratch: Path, model: str | None) -> list[str]:
    # yolo: it may write in its scratch directory without asking; the seatbelt
    # is what bounds it. The MCP name is one no server has, so whatever
    # servers the user's own settings list, none load.
    return ["gemini", f"--prompt={brief}", "--output-format", "json",
            "--approval-mode", "yolo", "--skip-trust",
            "--allowed-mcp-server-names", "kestrel-none",
            *(["--model", model] if model else [])]


def _gemini_parse(stdout: str, scratch: Path) -> Reply:
    start = stdout.find("{")
    try:
        data = json.loads(stdout[start:]) if start >= 0 else None
    except json.JSONDecodeError:
        data = None
    if not isinstance(data, dict) or not isinstance(data.get("response"), str):
        return Reply(stdout.strip())
    tin = tout = 0
    model = None
    models = (data.get("stats") or {}).get("models") or {}
    for name, row in models.items() if isinstance(models, dict) else []:
        tokens = (row or {}).get("tokens") or {}
        tin += int(tokens.get("prompt") or tokens.get("input") or 0)
        tout += int(tokens.get("candidates") or tokens.get("output") or 0)
        model = model or name
    return Reply(data["response"].strip(), tin or None, tout or None, model)


def _gemini_signed_in() -> tuple[bool, str]:
    for key in ("GEMINI_API_KEY", "GOOGLE_API_KEY"):
        if os.environ.get(key):
            return True, f"{key} is set"
    home = Path.home() / ".gemini"
    if (home / "oauth_creds.json").exists():
        return True, "signed in with Google"
    try:
        settings = json.loads((home / "settings.json").read_text())
        if ((settings.get("security") or {}).get("auth") or {}).get("selectedType") or settings.get("selectedAuthType"):
            return True, "an auth method is chosen in ~/.gemini/settings.json"
    except (OSError, ValueError):
        pass
    return False, "not signed in"


# ---------- codex ----------

def _codex_argv(brief: str, scratch: Path, model: str | None) -> list[str]:
    # Its own sandbox is off because seatbelt doesn't nest and ours is already
    # around it ("intended solely for … externally sandboxed" is this case).
    # --ignore-user-config: no MCP servers from ~/.codex/config.toml.
    # The brief goes in on stdin ("-").
    return ["codex", "exec", "--skip-git-repo-check", "--ephemeral", "--ignore-user-config",
            "--dangerously-bypass-approvals-and-sandbox", "--color", "never", "--json",
            "--cd", str(scratch), "--output-last-message", str(scratch / CODEX_REPLY),
            *(["--model", model] if model else []), "-"]


def _codex_parse(stdout: str, scratch: Path) -> Reply:
    tin = tout = 0
    last = ""
    for line in stdout.splitlines():
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(ev, dict):
            continue
        msg = ev.get("msg") if isinstance(ev.get("msg"), dict) else {}
        usage = ev.get("usage") or msg.get("usage")
        if isinstance(usage, dict):
            tin += int(usage.get("input_tokens") or 0)
            tout += int(usage.get("output_tokens") or 0)
        item = ev.get("item") if isinstance(ev.get("item"), dict) else {}
        if item.get("type") in ("agent_message", "assistant_message") and isinstance(item.get("text"), str):
            last = item["text"]
    reply = scratch / CODEX_REPLY
    try:
        text = reply.read_text().strip()
        reply.unlink()
    except OSError:
        text = last.strip() or stdout.strip()
    return Reply(text, tin or None, tout or None)


def _codex_signed_in() -> tuple[bool, str]:
    try:
        done = subprocess.run(["codex", "login", "status"], capture_output=True, text=True,
                              timeout=PROBE_TIMEOUT_S, stdin=subprocess.DEVNULL)
        said = (done.stdout + done.stderr).strip().splitlines()
        return done.returncode == 0, (said[-1][:120] if said else "")
    except (OSError, subprocess.SubprocessError):
        home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
        ok = (home / "auth.json").exists()
        return ok, "signed in" if ok else "not signed in"


PROVIDERS: dict[str, Provider] = {p.id: p for p in (
    Provider(
        id="claude", label="Claude", vendor="Anthropic", binary="claude", outside=False,
        good_for="orchestrates, judges, and anything that touches your own data",
    ),
    Provider(
        id="gemini", label="Gemini", vendor="Google", binary="gemini", outside=True,
        install="npm install -g @google/gemini-cli",
        login="run `gemini` once in a terminal and sign in with Google (or set GEMINI_API_KEY in backend/.env)",
        good_for="grunt work, long documents, a second take on a design",
        env_keys=("GEMINI_API_KEY", "GOOGLE_API_KEY", "GOOGLE_CLOUD_PROJECT",
                  "GOOGLE_GENAI_USE_VERTEXAI", "GOOGLE_CLOUD_LOCATION"),
        config_dir=Path.home() / ".gemini",
        argv=_gemini_argv, parse=_gemini_parse, signed_in=_gemini_signed_in,
    ),
    Provider(
        id="codex", label="ChatGPT (Codex)", vendor="OpenAI", binary="codex", outside=True,
        install="npm install -g @openai/codex",
        login="run `codex login` in a terminal and sign in with ChatGPT",
        good_for="grunt work, code, a second take on a design",
        env_keys=("OPENAI_API_KEY", "CODEX_HOME"),
        config_dir=Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex"),
        stdin_brief=True,
        argv=_codex_argv, parse=_codex_parse, signed_in=_codex_signed_in,
    ),
)}


def _version(binary: str) -> str | None:
    try:
        done = subprocess.run([binary, "--version"], capture_output=True, text=True,
                              timeout=PROBE_TIMEOUT_S, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return None
    lines = (done.stdout or done.stderr).strip().splitlines()
    return lines[-1].strip()[:60] if lines else None


def _probe(p: Provider) -> dict:
    path = shutil.which(p.binary)
    if p.id == "claude" and not path and CONSOLE_CLI_PATH.exists():
        path = str(CONSOLE_CLI_PATH)
    row = {"installed": bool(path), "path": path, "version": None,
           "signed_in": False, "auth_note": ""}
    if not path:
        return row
    row["version"] = _version(path)
    if p.signed_in is None:
        # Claude: the account this app already runs on (Settings → Account).
        row["signed_in"], row["auth_note"] = True, "the account Kestrel runs on"
    else:
        row["signed_in"], row["auth_note"] = p.signed_in()
    return row


_cache: tuple[float, dict[str, dict]] | None = None


def status(fresh: bool = False) -> list[dict]:
    """Every provider with what the page and the tools need. Blocking (it may
    run each CLI's --version): call it off the loop."""
    global _cache
    if fresh or _cache is None or time.monotonic() - _cache[0] > STATUS_TTL_S:
        _cache = (time.monotonic(), {p.id: _probe(p) for p in PROVIDERS.values()})
    on = store.switches()
    out = []
    for p in PROVIDERS.values():
        probe = _cache[1][p.id]
        enabled = True if not p.outside else on.get(p.id, True)
        ready = probe["installed"] and probe["signed_in"] and enabled
        need = (None if ready else
                f"install it: {p.install}" if not probe["installed"] else
                f"sign in: {p.login}" if not probe["signed_in"] else
                "switched off in Settings → Providers")
        out.append({"id": p.id, "label": p.label, "vendor": p.vendor, "outside": p.outside,
                    "good_for": p.good_for, "install": p.install, "login": p.login,
                    **probe, "enabled": enabled, "ready": bool(ready), "needs": need})
    return out


def ready_ids() -> list[str]:
    return [r["id"] for r in status() if r["ready"]]


def forget() -> None:
    global _cache
    _cache = None
