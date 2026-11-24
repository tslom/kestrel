"""Which models Claude can send work to, and whether each is ready.

A provider is a CLI on this machine, driven headless; nothing here holds an API key.
Ready = installed, signed in (probed, cached briefly) and switched on (store.switches).
Checked against `agy` 1.5.15 and `codex` 0.171.0; parsing falls back to raw text.
Probes run under boundary.clean_env. Design: DESIGN.md → Other models.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from ..config import CONSOLE_CLI_PATH
from . import boundary, store

STATUS_TTL_S = 20
PROBE_TIMEOUT_S = 10
CODEX_REPLY = ".reply.txt"
# agy's home for sandboxed runs (scripts/antigravity_home.py): its own .gemini and keychain.
AGY_HOME = Path.home() / ".kestrel" / "agents" / "antigravity"
AGY_KEYCHAIN_ITEM = ("gemini", "antigravity")      # go-keyring's service, account
AGY_TOKEN_FILE = AGY_HOME / ".gemini" / "antigravity-cli" / "antigravity-oauth-token"
AGY_SETUP = "backend/.venv/bin/python backend/scripts/antigravity_home.py"
# One home per account (accounts.py): siblings, never nested, so no sandbox reads another's token.
ACCOUNT_HOMES = Path.home() / ".kestrel" / "agents" / "accounts"
# codex keeps its login in a file in CODEX_HOME rather than the OS keyring:
# the keyring is the login keychain, which no account may touch.
CODEX_FILE_STORE = 'cli_auth_credentials_store="file"'
CODEX_LOGIN_PORTS = (1455, 1457)


@dataclass(frozen=True)
class Reply:
    text: str
    tokens_in: int | None = None
    tokens_out: int | None = None
    model: str | None = None
    tokens_cached: int | None = None   # the part of tokens_in read from cache


@dataclass(frozen=True)
class LoginWay:
    """One way to sign an account in, run by accounts.py via boundary.start_login.

    browser: codex listens on `ports` and prints the URL; Kestrel opens it.
    acp: agy's `authenticate` opens the page itself and binds any loopback port.
    device: link plus one-time code, for workspaces that allow it."""
    id: str
    argv: tuple[str, ...]
    label: str
    ports: tuple[int, ...] | None = ()      # None: any loopback port; (): none
    browser: bool = False


@dataclass(frozen=True)
class Provider:
    id: str
    label: str            # the product, as the user knows it
    vendor: str
    binary: str
    outside: bool         # not Claude: brief only, sandboxed (boundary.py)
    install: str = ""
    login: tuple[str, ...] = ()   # ways to sign in, any one will do; `code` in backticks
    good_for: str = ""
    best_at: tuple[str, ...] = ()   # what it's plainly better at than Claude: prefer it there
    env_keys: tuple[str, ...] = ()            # passed through from the backend's env
    env_set: tuple[tuple[str, str], ...] = ()  # set for it, whatever the backend has
    config_dir: Path | None = None
    stdin_brief: bool = False
    argv: Callable[[str, Path, str | None], list[str]] | None = field(default=None, repr=False)
    parse: Callable[[str, Path], Reply] | None = field(default=None, repr=False)
    signed_in: Callable[[], tuple[bool, str]] | None = field(default=None, repr=False)
    # Turn cap: ends_turn marks a stdout line closing a turn; None means only the clock caps it.
    ends_turn: Callable[[str], bool] | None = field(default=None, repr=False)
    max_turns: int = 0
    # Live model list [{id, label, rank}] (stored by model_catalog); None = can't list, see `no_list`.
    models: Callable[[], list[dict] | None] | None = field(default=None, repr=False)
    no_list: str = ""
    # A failed run's (stdout, stderr) as one line naming the fix; None = last stderr line.
    explain: Callable[[str, str], str | None] | None = field(default=None, repr=False)
    # Accounts (accounts.py): home env, sign-in commands, per-home signed-in probe.
    home_env: Callable[[Path], tuple[tuple[str, str], ...]] | None = field(default=None, repr=False)
    logins: tuple["LoginWay", ...] = ()     # the first is what "Add account" runs
    home_signed_in: Callable[[Path], tuple[bool, str]] | None = field(default=None, repr=False)
    # Model run in an account's home when none is named (codex's output doesn't say).
    home_model: Callable[[Path], str | None] | None = field(default=None, repr=False)
    key_env: tuple[str, ...] = ()     # the API-key variables that sign it in without an account


def _probe_run(argv: list[str], env: dict[str, str] | None = None,
               timeout: float = PROBE_TIMEOUT_S) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout,
                          stdin=subprocess.DEVNULL, env=env or boundary.clean_env())


MODELS_TIMEOUT_S = 30


def _agy_models() -> list[dict] | None:
    """`agy models`: id<TAB>name lines in agy's order, run in the first enabled account's home."""
    p = PROVIDERS["antigravity"]
    homes = [Path(a["home"]) for a in store.accounts("antigravity") if a["enabled"]]
    env = boundary.clean_env(p.env_keys, fixed=(*p.env_set, *p.home_env(homes[0])) if homes
                             else p.env_set)
    try:
        done = _probe_run(["agy", "models"], env, MODELS_TIMEOUT_S)
    except (OSError, subprocess.SubprocessError):
        return None
    rows = []
    for line in done.stdout.splitlines():
        mid, _, label = line.partition("\t")
        if mid.strip() and label.strip():
            rows.append({"id": mid.strip(), "label": label.strip(), "rank": len(rows)})
    return rows if done.returncode == 0 and rows else None


def _codex_models() -> list[dict] | None:
    """`codex debug models`: only `visibility: list` rows; `hide` are internal."""
    try:
        done = _probe_run(["codex", "debug", "models"], boundary.clean_env(
            PROVIDERS["codex"].env_keys), MODELS_TIMEOUT_S)
        data = json.loads(done.stdout[done.stdout.find("{"):])
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    rows = [{"id": m["slug"], "label": m.get("display_name") or m["slug"],
             "rank": int(m.get("priority") or 0)}
            for m in (data.get("models") if isinstance(data, dict) else None) or []
            if isinstance(m, dict) and m.get("slug") and m.get("visibility", "list") == "list"]
    return rows or None


# ---------- antigravity ----------

def _agy_argv(brief: str, scratch: Path, model: str | None) -> list[str]:
    # Permissions skipped (nobody to answer in print mode; our seatbelt bounds it, agy's own
    # --sandbox doesn't nest). The log names the model the stream doesn't (_agy_model); it
    # needs the resolved path, since the sandbox refuses a symlinked data/.
    return ["agy", f"--print={brief}", "--output-format", "stream-json",
            "--dangerously-skip-permissions", "--disable-slash-commands",
            f"--log-file={scratch.resolve() / AGY_LOG}",
            *(["--model", model] if model else [])]


AGY_LOG = ".agy.log"
_AGY_PICKED = re.compile(r'selected model override to backend: label="([^"]+)"')


def _agy_model(scratch: Path) -> str | None:
    """The model agy ran, off its log: its label mapped to an id via `agy models`, else the label."""
    try:
        said = _AGY_PICKED.findall((scratch / AGY_LOG).read_text(errors="replace"))
    except OSError:
        return None
    if not said:
        return None
    label = said[-1]
    try:
        return store.model_id("antigravity", label) or label
    except Exception:  # the label still names it
        return label


AGY_MAX_TURNS = 30


def json_lines(stdout: str):
    for line in stdout.splitlines():
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(ev, dict):
            yield ev


def _agy_step(ev: dict) -> dict:
    step = ev.get("step_update") if ev.get("event") == "step_update" else None
    return step if isinstance(step, dict) else {}


def _agy_ends_turn(line: str) -> bool:
    """One model response finished: a step_update for an agent_response, DONE."""
    if '"agent_response"' not in line:
        return False
    step = next((_agy_step(ev) for ev in json_lines(line)), {})
    return step.get("step_type") == "agent_response" and step.get("state") == "DONE"


def _agy_parse(stdout: str, scratch: Path) -> Reply:
    """The `result` event, else (cut off) the last response and usage summed over ended steps.
    output_tokens already includes thinking_tokens; agy reports no cached count."""
    events = list(json_lines(stdout))
    model = _agy_model(scratch)
    result = next((e["result"] for e in reversed(events)
                   if e.get("event") == "result" and isinstance(e.get("result"), dict)), None)
    if result is not None and isinstance(result.get("response"), str):
        usage = result.get("usage") if isinstance(result.get("usage"), dict) else {}
        tin, tout = int(usage.get("input_tokens") or 0), int(usage.get("output_tokens") or 0)
        return Reply(result["response"].strip(), tin or None, tout or None, model)
    if not events:
        return Reply(stdout.strip(), model=model)
    tin = tout = 0
    texts: dict[int, str] = {}
    for ev in events:
        step = _agy_step(ev)
        if step.get("step_type") != "agent_response":
            continue
        n = int(step.get("step_index") or 0)
        texts[n] = texts.get(n, "") + str(step.get("text_delta") or "")
        if step.get("state") == "DONE" and isinstance(step.get("usage"), dict):
            tin += int(step["usage"].get("input_tokens") or 0)
            tout += int(step["usage"].get("output_tokens") or 0)
    last = next((t for _, t in sorted(texts.items(), reverse=True) if t.strip()), "")
    return Reply(last.strip(), tin or None, tout or None, model)


def _agy_explain(stdout: str, stderr: str) -> str | None:
    """Its result error or stderr as what to do; a bad --model prints the list after the error."""
    result = next((e["result"] for e in reversed(list(json_lines(stdout)))
                   if e.get("event") == "result" and isinstance(e.get("result"), dict)), {})
    said = str(result.get("error") or "") or stderr
    low = said.lower()
    if "invalid model selection" in low:
        known = [ln.strip() for ln in said.splitlines() if ln.startswith("  ") and ln.strip()]
        return ("Antigravity has no such model. Leave model out for its default, or use an id "
                "from `agy models` (e.g. gemini-3.8-flash-high). It offers: " + ", ".join(known))
    if "authentication" in low or "not logged in" in low or "accounts.google.com" in low:
        return ("Antigravity isn't signed in, or its sign-in lapsed: sign it in again from "
                f"Settings → Providers → Add account (or run `{AGY_SETUP}` in a terminal)")
    first = next((ln.strip() for ln in said.splitlines() if ln.strip()), "")
    return first.removeprefix("Error: ").removeprefix("error: ")[:300] or None


# Where a home's token lands once agy leaves the keychain.
AGY_TOKEN_PATH = Path(".gemini") / "antigravity-cli" / "antigravity-oauth-token"


def _agy_home_signed_in(home: Path) -> tuple[bool, str]:
    """The keychain item, or the file agy falls back to when a keychain write fails (`security` exit 45)."""
    token = home / AGY_TOKEN_PATH
    if token.is_file() and token.stat().st_size:
        return True, "signed in with Google, in a folder of its own"
    if not (home / "Library" / "Keychains" / boundary.KEYCHAIN).exists():
        return False, "no sign-in of its own yet"
    env = boundary.clean_env()
    env["HOME"] = str(home)
    try:   # attributes only, no -w: whether the item is there, not the secret
        found = _probe_run(["/usr/bin/security", "find-generic-password",
                            "-s", "gemini", "-a", "antigravity"], env)
    except (OSError, subprocess.SubprocessError):
        return False, "couldn't read its keychain"
    return (True, "signed in with Google, in a keychain of its own") if found.returncode == 0 \
        else (False, "its keychain is there but holds no sign-in")


def _agy_signed_in() -> tuple[bool, str]:
    if os.environ.get("GEMINI_API_KEY"):
        return True, "GEMINI_API_KEY is set"
    return _agy_home_signed_in(AGY_HOME)


def _codex_argv(brief: str, scratch: Path, model: str | None) -> list[str]:
    # Its sandbox is off (seatbelt doesn't nest; ours surrounds it). --ignore-user-config drops
    # config.toml's MCP servers, so the file credential store is passed via -c. Brief on stdin.
    return ["codex", "exec", "--skip-git-repo-check", "--ephemeral", "--ignore-user-config",
            "-c", CODEX_FILE_STORE,
            "--dangerously-bypass-approvals-and-sandbox", "--color", "never", "--json",
            "--cd", str(scratch), "--output-last-message", str(scratch / CODEX_REPLY),
            *(["--model", model] if model else []), "-"]


def _codex_parse(stdout: str, scratch: Path) -> Reply:
    """`codex exec --json` (0.171): thread.started, turn.started, item.* and
    turn.completed, whose `usage` has input_tokens (cached ones included),
    cached_input_tokens and output_tokens (reasoning included). The reply is
    the last agent_message item, or the --output-last-message file."""
    tin = tout = cached = 0
    last = ""
    for ev in json_lines(stdout):
        usage = ev.get("usage") if ev.get("type") == "turn.completed" else None
        if isinstance(usage, dict):
            tin += int(usage.get("input_tokens") or 0)
            tout += int(usage.get("output_tokens") or 0)
            cached += int(usage.get("cached_input_tokens") or 0)
        item = ev.get("item") if isinstance(ev.get("item"), dict) else {}
        if ev.get("type") == "item.completed" and item.get("type") == "agent_message" \
                and isinstance(item.get("text"), str):
            last = item["text"]
    reply = scratch / CODEX_REPLY
    try:
        text = reply.read_text().strip()
        reply.unlink()
    except OSError:
        text = last.strip() or stdout.strip()
    return Reply(text, tin or None, tout or None, tokens_cached=cached if tin else None)


CODEX_MAX_STEPS = 80


def _codex_ends_turn(line: str) -> bool:
    """One item.completed (message, command, file change): steps, not model calls, so the cap is higher."""
    return '"item.completed"' in line and any(
        ev.get("type") == "item.completed" for ev in json_lines(line))


def _codex_signed_in() -> tuple[bool, str]:
    # `codex exec` reads CODEX_API_KEY; OPENAI_API_KEY only seeds its interactive login.
    if os.environ.get("CODEX_API_KEY"):
        return True, "an OpenAI API key (CODEX_API_KEY in backend/.env)"
    try:
        done = _probe_run(["codex", "login", "status"])
        said = (done.stdout + done.stderr).strip().splitlines()
        return done.returncode == 0, (said[-1][:120] if said else "")
    except (OSError, subprocess.SubprocessError):
        home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
        ok = (home / "auth.json").exists()
        return ok, "signed in" if ok else "not signed in"


def _codex_home_env(home: Path) -> tuple[tuple[str, str], ...]:
    return (("CODEX_HOME", str(home)), ("HOME", str(home)))


def _codex_home_signed_in(home: Path) -> tuple[bool, str]:
    env = boundary.clean_env(fixed=_codex_home_env(home))
    try:
        done = _probe_run(["codex", "login", "status", "-c", 'cli_auth_credentials_store="file"'], env)
    except (OSError, subprocess.SubprocessError):
        return False, "couldn't ask codex"
    said = [ln.strip() for ln in (done.stdout + done.stderr).splitlines() if ln.strip()]
    note = said[-1][:120] if said else ""
    return done.returncode == 0 and not note.lower().startswith("not logged"), note


def _codex_home_model(home: Path) -> str | None:
    """The lowest-`priority` listed model in the account's CODEX_HOME/models_cache.json (plan-dependent).
    `codex exec --json` names no model, so this one is passed explicitly and recorded."""
    try:
        models = json.loads((home / "models_cache.json").read_text()).get("models") or []
    except (OSError, ValueError, AttributeError):
        return None
    listed = [m for m in models if isinstance(m, dict) and m.get("slug")
              and m.get("visibility", "list") == "list"]
    return min(listed, key=lambda m: int(m.get("priority") or 0))["slug"] if listed else None


def _agy_home_env(home: Path) -> tuple[tuple[str, str], ...]:
    return (("HOME", str(home)),)


PROVIDERS: dict[str, Provider] = {p.id: p for p in (
    Provider(
        id="claude", label="Claude", vendor="Anthropic", binary="claude", outside=False,
        good_for="orchestrates, judges, and anything that touches your own data",
    ),
    Provider(
        id="antigravity", label="Antigravity", vendor="Google", binary="agy", outside=True,
        install="brew install --cask antigravity-cli",
        login=("Add account below: Kestrel gives it a home and keychain of its own and "
               "opens Google's sign-in in your browser; pick the account and allow",
               f"or copy an existing agy sign-in from a terminal: `{AGY_SETUP}`",
               "use a Gemini API key: put `GEMINI_API_KEY=…` in backend/.env and restart "
               "Kestrel (billed to that key, not your Google plan)"),
        good_for="grunt work: boilerplate, reformatting, test scaffolds, summaries of public docs",
        best_at=("images: it has image generation built in (artifact x.png)",
                 "very long public documents: a large context window"),
        env_keys=("GEMINI_API_KEY",),
        env_set=(("HOME", str(AGY_HOME)), ("AGY_CLI_DISABLE_AUTO_UPDATE", "true")),
        config_dir=AGY_HOME,
        argv=_agy_argv, parse=_agy_parse, signed_in=_agy_signed_in,
        ends_turn=_agy_ends_turn, max_turns=AGY_MAX_TURNS, models=_agy_models,
        explain=_agy_explain,
        home_env=_agy_home_env, home_signed_in=_agy_home_signed_in, key_env=("GEMINI_API_KEY",),
        # agy has no login command and print mode's link needs a pasted code; ACP `authenticate` catches the callback.
        logins=(LoginWay("acp", ("agy", "acp"), "Sign in with Google in your browser",
                         ports=None, browser=True),),
    ),
    Provider(
        id="codex", label="ChatGPT (Codex)", vendor="OpenAI", binary="codex", outside=True,
        install="npm install -g @openai/codex",
        login=("Add account below: Kestrel runs Codex's own sign-in in a home of that "
               "account's own and opens ChatGPT's sign-in in your browser. A work or school workspace "
               "with the Codex CLI turned off refuses with `codex_cli_workspace_disabled`, "
               "and only its admin can change that",
               "use an OpenAI API key: put `CODEX_API_KEY=sk-…` in backend/.env and "
               "restart Kestrel (billed per token to that key; only codex is given it)"),
        good_for="grunt work in code: boilerplate, mechanical refactors, test scaffolds",
        best_at=("mechanical code changes across many files, run and checked in its own folder",),
        env_keys=("CODEX_API_KEY", "CODEX_HOME"),
        config_dir=Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex"),
        stdin_brief=True,
        argv=_codex_argv, parse=_codex_parse, signed_in=_codex_signed_in,
        ends_turn=_codex_ends_turn, max_turns=CODEX_MAX_STEPS, models=_codex_models,
        home_env=_codex_home_env, home_signed_in=_codex_home_signed_in, key_env=("CODEX_API_KEY",),
        home_model=_codex_home_model,
        # Browser sign-in uses 1455, else 1457; device code is off in some workspaces, so fallback only.
        logins=(LoginWay("browser", ("codex", "login", "-c", CODEX_FILE_STORE),
                         "Sign in with ChatGPT in your browser", ports=CODEX_LOGIN_PORTS),
                LoginWay("device", ("codex", "login", "--device-auth", "-c", CODEX_FILE_STORE),
                         "Use a device code instead (only if your workspace allows it)")),
    ),
)}


def _version(binary: str) -> str | None:
    try:
        done = _probe_run([binary, "--version"])
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
    elif p.home_signed_in is not None:
        # Signed in = any of its accounts is, or its API key is set.
        row["accounts"] = {a["id"]: p.home_signed_in(Path(a["home"]))
                           for a in store.accounts(p.id)}
        key = next((k for k in p.key_env if os.environ.get(k)), None)
        ins = sum(ok for ok, _ in row["accounts"].values())
        row["signed_in"] = bool(ins or key)
        row["auth_note"] = (f"{ins} of {len(row['accounts'])} account{'s' if len(row['accounts']) != 1 else ''} signed in"
                            if row["accounts"] else "no account yet")
        if key:
            row["auth_note"] += f"; {key} is set"
    else:
        row["signed_in"], row["auth_note"] = p.signed_in()
    return row


def account_sign_ins(provider_id: str) -> dict[str, tuple[bool, str]]:
    """Each account's (signed in, what the CLI said), from the last probe."""
    status()
    return dict((_cache[1].get(provider_id) or {}).get("accounts") or {})


_cache: tuple[float, dict[str, dict]] | None = None


def status(fresh: bool = False) -> list[dict]:
    """Every provider's status. Blocking (runs each CLI's --version): call off the loop."""
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
                "sign in: " + "; or ".join(p.login).replace("`", "")
                if not probe["signed_in"] else
                "switched off in Settings → Providers")
        out.append({"id": p.id, "label": p.label, "vendor": p.vendor, "outside": p.outside,
                    "good_for": p.good_for, "best_at": list(p.best_at),
                    "install": p.install, "login": p.login,
                    "has_accounts": p.home_signed_in is not None,
                    "sign_in_ways": [{"id": w.id, "label": w.label} for w in p.logins],
                    **{k: v for k, v in probe.items() if k != "accounts"},
                    "enabled": enabled, "ready": bool(ready), "needs": need})
    return out


def ready_ids() -> list[str]:
    return [r["id"] for r in status() if r["ready"]]


def forget() -> None:
    global _cache
    _cache = None
