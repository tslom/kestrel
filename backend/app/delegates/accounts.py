"""Accounts for the outside providers: several sign-ins per CLI, usage for each, rules that pick one.

Each account is a private home under registry.ACCOUNT_HOMES; the CLI is pointed at it for
every run (Provider.home_env) and the sandbox opens only that home. app.db holds names,
rules and the home path, never a token. Adding one = signing in inside a new home (a
failed sign-in deletes it). `check` / `pick` are pure.
Design: DESIGN.md → Other models.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import secrets
import shutil
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from . import boundary, registry, store

TAGS = boundary.WORK_FOR
ALL_TAGS = ",".join(TAGS)
SCHOOL_TAGS = "personal,learning"
LOGIN_LIMIT_S = 10 * 60          # a browser sign-in left this long was abandoned
DEVICE_LIMIT_S = 16 * 60         # codex's device code lasts 15 minutes
READING_TTL_S = 5 * 60
USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
_URL = re.compile(r"https://[^\s\"'<>]+")
_DEVICE_CODE = re.compile(r"\b[A-Z0-9]{4}-[A-Z0-9]{4,6}\b")
_lock = threading.Lock()


class AccountError(RuntimeError):
    """Written to be shown."""


def home_for(account_id: str) -> Path:
    return registry.ACCOUNT_HOMES / account_id


def _is_ours(home: Path) -> bool:
    """A home Kestrel made, so it may delete it."""
    real = home.resolve()
    return registry.ACCOUNT_HOMES.resolve() in real.parents or real == registry.AGY_HOME.resolve()


def adopt_first_home() -> None:
    """The pre-accounts agy home becomes an account, the first time accounts are listed."""
    with _lock:
        if store.accounts("antigravity"):
            return
        ok, _ = registry.PROVIDERS["antigravity"].home_signed_in(registry.AGY_HOME)
        if ok:
            store.add_account("antigravity-main", "antigravity", "Google (first sign-in)",
                              str(registry.AGY_HOME), ALL_TAGS)
            # Every earlier agy run used that home.
            store.claim_runs("antigravity", "antigravity-main")
            registry.forget()       # the cached probe predates the account


# ---------- who an account is (codex) ----------

def _claims(jwt: str) -> dict:
    try:
        body = jwt.split(".")[1]
        return json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    except (IndexError, ValueError):
        return {}


def codex_auth(home: Path) -> dict:
    try:
        data = json.loads((home / "auth.json").read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def codex_identity(home: Path) -> dict:
    """Email and ChatGPT plan, from the id token codex saved."""
    tokens = codex_auth(home).get("tokens") or {}
    claims = _claims(str(tokens.get("id_token") or ""))
    auth = claims.get("https://api.openai.com/auth") or {}
    return {"email": claims.get("email"), "plan": auth.get("chatgpt_plan_type")}


def school_account(email: str | None, plan: str | None) -> bool:
    # ChatGPT says "education" for USC's (wham/usage and the id token).
    return bool((email or "").lower().endswith(".edu")
                or (plan or "").lower() in ("edu", "education"))


# ---------- usage ----------

@dataclass
class Reading:
    at: float
    plan: str | None = None
    windows: list[dict] = field(default_factory=list)
    credits: dict | None = None
    error: str | None = None


_readings: dict[str, Reading] = {}


def _window(key: str, w: dict | None, now: float) -> dict | None:
    if not isinstance(w, dict) or w.get("used_percent") is None:
        return None
    secs = int(w.get("limit_window_seconds") or 0)
    label = ("5h" if 4 * 3600 <= secs <= 6 * 3600 else "Week" if 6 * 86400 <= secs <= 8 * 86400
             else f"{round(secs / 3600)}h" if secs else key)
    reset = w.get("reset_at") or (now + int(w["reset_after_seconds"])
                                  if w.get("reset_after_seconds") is not None else None)
    return {"key": key, "label": label, "percent": float(w["used_percent"]),
            "resets_at": float(reset) if reset else None}


def parse_usage(data: dict, now: float) -> Reading:
    """The wham/usage payload: rate_limit.{primary,secondary}_window and credits (balance is a string)."""
    limit = data.get("rate_limit") if isinstance(data.get("rate_limit"), dict) else {}
    windows = [w for w in (_window("primary", limit.get("primary_window"), now),
                           _window("secondary", limit.get("secondary_window"), now)) if w]
    credits = data.get("credits") if isinstance(data.get("credits"), dict) else None
    if credits is not None:
        try:
            balance = float(credits.get("balance")) if credits.get("balance") is not None else None
        except (TypeError, ValueError):
            balance = None
        credits = {"balance": balance, "unlimited": bool(credits.get("unlimited")),
                   "has_credits": bool(credits.get("has_credits"))}
    return Reading(now, data.get("plan_type"), windows, credits)


def _fetch_codex(acct: dict) -> Reading:
    now = time.time()
    tokens = codex_auth(Path(acct["home"])).get("tokens") or {}
    if not tokens.get("access_token"):
        return Reading(now, error="not signed in")
    req = urllib.request.Request(USAGE_URL, headers={
        "Authorization": f"Bearer {tokens['access_token']}",
        "ChatGPT-Account-Id": str(tokens.get("account_id") or ""),
        "User-Agent": "codex_cli_rs", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            return parse_usage(json.loads(r.read()), now)
    except urllib.error.HTTPError as e:
        why = ("its token has lapsed; the next run refreshes it" if e.code in (401, 403)
               else f"ChatGPT said {e.code}")
        return Reading(now, error=why)
    except (OSError, ValueError) as e:
        return Reading(now, error=f"no reading: {str(e)[:80]}")


def reading(acct: dict, fetch: bool = True) -> Reading | None:
    """The last usage reading, refreshed past READING_TTL_S; only Codex has one."""
    if acct["provider"] != "codex":
        return None
    have = _readings.get(acct["id"])
    if fetch and (have is None or time.time() - have.at > READING_TTL_S):
        have = _readings[acct["id"]] = _fetch_codex(acct)
    return have


def month_start() -> str:
    return datetime.now().strftime("%Y-%m-01")


def credits_used(acct: dict, read: Reading | None) -> float | None:
    """Monthly allowance less ChatGPT's balance; None when either is missing."""
    bal = (read.credits or {}).get("balance") if read else None
    if bal is None or not acct.get("monthly_credits"):
        return None
    return max(0.0, float(acct["monthly_credits"]) - bal)


# ---------- the rules ----------

def tags(acct: dict) -> list[str]:
    return [t for t in (acct.get("allowed_for") or "").split(",") if t in TAGS]


def check(acct: dict, signed_in: bool, read: Reading | None, work_for: str | None) -> str | None:
    """Why this account can't take this errand, or None if it can."""
    if not acct.get("enabled", 1):
        return "turned off"
    if not signed_in:
        return "not signed in"
    if work_for and work_for not in tags(acct):
        return f"not allowed for {work_for} work (allowed: {', '.join(tags(acct)) or 'nothing'})"
    if read is not None:
        cap = acct.get("window_cap")
        for w in read.windows:
            live = w.get("resets_at") is None or w["resets_at"] > time.time()
            if live and cap is not None and w["percent"] >= cap:
                return f"{w['label']} window at {w['percent']:.0f}% (stops at {cap:.0f}%)"
            if live and w["percent"] >= 100:
                return f"{w['label']} limit reached"
        used = credits_used(acct, read)
        if used is not None and acct.get("credit_cap") is not None and used >= acct["credit_cap"]:
            return f"{used:,.0f} credits used this month (stops at {acct['credit_cap']:,.0f})"
        c = read.credits or {}
        if c and not c.get("unlimited") and not c.get("has_credits") and not read.windows:
            return "out of credits"
    return None


def pick(provider_id: str, work_for: str | None) -> tuple[dict | None, str]:
    """(first account in priority order inside its rules, why), or (None, line) on the API key.
    Raises AccountError when accounts exist, none fits and there's no key."""
    p = registry.PROVIDERS[provider_id]
    if p.home_signed_in is None:
        return None, ""
    if provider_id == "antigravity":
        adopt_first_home()
    accts = store.accounts(provider_id)
    ins = registry.account_sign_ins(provider_id)
    whys = {a["id"]: check(a, ins.get(a["id"], (False, ""))[0], reading(a), work_for)
            for a in accts}
    chosen = next((a for a in accts if whys[a["id"]] is None), None)
    if chosen:
        passed = [f"{a['label']} ({whys[a['id']]})" for a in accts[:accts.index(chosen)]]
        return chosen, f"on account {chosen['label']}" + (
            f"; skipped {', '.join(passed)}" if passed else "")
    key = next((k for k in p.key_env if os.environ.get(k)), None)
    if key:
        return None, f"on its API key ({key}): no account fits"
    if not accts:
        raise AccountError(f"{p.label} has no account yet: Settings → Providers → Add account")
    raise AccountError(f"no {p.label} account takes this: "
                       + "; ".join(f"{a['label']}: {whys[a['id']]}" for a in accts))


# ---------- signing in ----------

# What a sign-in's output means, in plain words. First match wins.
PLAIN_ERRORS = (
    (re.compile(r"enable device code authentication|device code auth\w* (?:is )?(?:disabled|not enabled)", re.I),
     "This ChatGPT workspace has device-code sign-in turned off (USC's does). "
     "Use the browser sign-in instead."),
    (re.compile(r"not enabled for your workspace|not currently authorized to use Codex|"
                r"do not have access to Codex|codex_cli_workspace_disabled", re.I),
     "This ChatGPT account isn't allowed to use Codex in its workspace. Only the "
     "workspace's admin can turn that on."),
    (re.compile(r"address already in use|os error 48|operation not permitted \(os error 1\)", re.I),
     "Codex couldn't listen for the sign-in: ports 1455 and 1457 are both taken, "
     "probably by a sign-in left running. Wait a minute and start again."),
    (re.compile(r"sign-in was cancelled", re.I),
     "The Google sign-in was closed before it finished. Start it again when you're ready."),
    (re.compile(r"couldn't open a web browser", re.I),
     "Antigravity couldn't open your browser for the sign-in."),
)


def plain_error(text: str) -> str | None:
    return next((said for pattern, said in PLAIN_ERRORS if pattern.search(text or "")), None)


# agy's Agent Client Protocol: say hello, then ask for the Google sign-in.
ACP_INIT = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
    "protocolVersion": 1, "clientInfo": {"name": "kestrel", "version": "1"},
    "clientCapabilities": {"fs": {"readTextFile": False, "writeTextFile": False},
                           "terminal": False}}}
ACP_METHOD = "oauth-personal"
ACP_AUTH = {"jsonrpc": "2.0", "id": 2, "method": "authenticate",
            "params": {"methodId": ACP_METHOD}}
_LOGIN_PORT = re.compile(r"local login server on http://(?:localhost|127\.0\.0\.1):(\d+)")


@dataclass
class Login:
    provider: str
    account_id: str
    label: str | None
    allowed_for: str | None
    way: str = "browser"           # registry.LoginWay.id: browser | acp | device
    started: float = field(default_factory=time.time)
    state: str = "starting"        # starting | waiting | checking | done | failed
    url: str | None = None         # the page to sign in on (codex prints it)
    code: str | None = None        # codex's device code, shown to the user
    port: int | None = None        # where codex listens for the callback
    opened: bool = False           # the browser was sent to the page
    said: list[str] = field(default_factory=list)
    error: str | None = None
    proc: object = None
    task: object = None

    def public(self) -> dict:
        return {"provider": self.provider, "way": self.way, "state": self.state,
                "url": self.url, "code": self.code, "opened": self.opened,
                "error": self.error, "started": self.started, "account": self.account_id,
                "limit_s": LOGIN_LIMIT_S if self.way != "device" else DEVICE_LIMIT_S}


_logins: dict[str, Login] = {}


def login_state(provider_id: str) -> dict | None:
    lg = _logins.get(provider_id)
    return lg.public() if lg else None


def _prepare_home(p: registry.Provider, home: Path) -> None:
    home.mkdir(parents=True, exist_ok=True)
    home.chmod(0o700)
    (home / ".login").mkdir(exist_ok=True)
    if p.id == "codex":
        (home / "config.toml").write_text('cli_auth_credentials_store = "file"\n')
    if p.id == "antigravity":
        boundary.make_keychain(home)


def _remove_home(home: Path) -> None:
    if _is_ours(home) and home != registry.AGY_HOME:
        shutil.rmtree(home, ignore_errors=True)


def _finish(lg: Login, home: Path) -> None:
    """The sign-in process ended: save the account if the home is signed in."""
    p = registry.PROVIDERS[lg.provider]
    ok, note = p.home_signed_in(home)
    if not ok:
        lg.state, lg.error = "failed", lg.error or _last_said(lg) or note or "it didn't sign in"
        _remove_home(home)
        return
    who = codex_identity(home) if p.id == "codex" else {}
    school = school_account(who.get("email"), who.get("plan"))
    allowed = lg.allowed_for or (SCHOOL_TAGS if school else ALL_TAGS)
    label = lg.label or who.get("email") or f"{p.label} {len(store.accounts(p.id)) + 1}"
    store.add_account(lg.account_id, p.id, label, str(home), allowed,
                      email=who.get("email"), plan=who.get("plan"))
    shutil.rmtree(home / ".login", ignore_errors=True)
    registry.forget()
    lg.state = "done"


def _last_said(lg: Login) -> str:
    lines = [s for s in lg.said if s and not _URL.search(s)]
    return lines[-1][:240] if lines else ""


def _read_line(lg: Login, line: str) -> None:
    """A line of a CLI sign-in's output (codex): its port, the page, a device
    code, and errors said plainly."""
    line = _ANSI.sub("", line).strip()
    if not line:
        return
    lg.said = (lg.said + [line])[-40:]
    if (m := _LOGIN_PORT.search(line)):
        lg.port = int(m.group(1))
    if lg.url is None and (m := _URL.search(line)):
        lg.url, lg.state = m.group(0), "waiting"
    if lg.way == "device" and lg.code is None and (m := _DEVICE_CODE.search(line)):
        lg.code = m.group(0)
    if (plain := plain_error(line)):
        lg.error = plain
    elif re.search(r"\berror\b|timed out|failed", line, re.I) and lg.state != "done":
        lg.error = line[:240]


def _acp_reply(lg: Login, line: str) -> dict | None:
    """One line from `agy acp`: the JSON-RPC reply to initialize (1) or
    authenticate (2), or None for anything else (its log lines)."""
    try:
        msg = json.loads(line)
    except ValueError:
        text = _ANSI.sub("", line).strip()
        if text:
            lg.said = (lg.said + [text])[-40:]
        return None
    return msg if isinstance(msg, dict) and msg.get("id") in (1, 2) else None


async def _acp(lg: Login) -> None:
    """agy's sign-in over ACP: initialize, then authenticate; the reply comes once Google's redirect lands."""
    proc = lg.proc

    async def send(msg: dict) -> None:
        proc.stdin.write((json.dumps(msg) + "\n").encode())
        await proc.stdin.drain()

    await send(ACP_INIT)
    while raw := await proc.stdout.readline():
        msg = _acp_reply(lg, raw.decode("utf-8", "replace"))
        if msg is None:
            continue
        if msg["id"] == 1:
            methods = [m.get("id") for m in (msg.get("result") or {}).get("authMethods") or []]
            if ACP_METHOD not in methods:
                lg.error = "this agy doesn't offer the Google sign-in over ACP; update it"
                return
            await send(ACP_AUTH)
            lg.state, lg.opened = "waiting", True
        else:
            if "error" in msg:
                said = str((msg["error"] or {}).get("message") or "the sign-in failed")
                lg.error = plain_error(said) or said[:240]
            else:
                lg.state = "checking"
            return


async def _watch(lg: Login, home: Path) -> None:
    proc = lg.proc

    async def cli() -> None:
        while raw := await proc.stdout.readline():
            _read_line(lg, raw.decode("utf-8", "replace"))
            if lg.way == "browser" and lg.url and not lg.opened:
                await _open(lg)
        await proc.wait()

    try:
        await asyncio.wait_for(_acp(lg) if lg.way == "acp" else cli(),
                               DEVICE_LIMIT_S if lg.way == "device" else LOGIN_LIMIT_S)
    except asyncio.TimeoutError:
        lg.error = (f"nothing came back from the browser in {LOGIN_LIMIT_S // 60} minutes; "
                    "start it again")
    except asyncio.CancelledError:
        lg.state, lg.error = "failed", "cancelled"
        await asyncio.to_thread(_remove_home, home)
        raise
    finally:
        boundary.stop_login(proc)
    if lg.state != "failed":
        await asyncio.to_thread(_finish, lg, home)


async def _open(lg: Login) -> None:
    """Send the browser to codex's sign-in page (boundary.open_sign_in)."""
    try:
        await asyncio.to_thread(boundary.open_sign_in, lg.url)
        lg.opened = True
    except boundary.BoundaryError as e:
        lg.opened, lg.error = True, f"{e}; open the link below yourself"


async def start_login(provider_id: str, label: str | None = None,
                      allowed_for: list[str] | None = None, way: str | None = None) -> dict:
    p = registry.PROVIDERS.get(provider_id)
    ways = {w.id: w for w in p.logins} if p else {}
    if not ways:
        raise AccountError("that provider has no sign-in Kestrel can run")
    chosen = ways.get(way) if way else p.logins[0]
    if chosen is None:
        raise AccountError(f"{p.label} signs in by: {', '.join(ways)}")
    cancel_login(provider_id)       # a stale one would hold codex's port
    tags_in = [t for t in (allowed_for or []) if t in TAGS]
    lg = Login(provider_id, f"{provider_id}-{secrets.token_hex(3)}",
               (label or "").strip()[:60] or None, ",".join(tags_in) or None, chosen.id)
    home = home_for(lg.account_id)
    try:
        await asyncio.to_thread(_prepare_home, p, home)
        lg.proc = await boundary.start_login(list(chosen.argv), home,
                                             (*p.env_set, *p.home_env(home)),
                                             ports=chosen.ports, browser=chosen.browser)
    except (boundary.BoundaryError, OSError) as e:
        await asyncio.to_thread(_remove_home, home)
        raise AccountError(str(e)) from None
    _logins[provider_id] = lg
    lg.task = asyncio.ensure_future(_watch(lg, home))
    return lg.public()


async def reopen(provider_id: str) -> dict:
    """The browser tab was closed: send it to the same page again (codex's
    server is still waiting). agy's page can't be reopened; start again."""
    lg = _logins.get(provider_id)
    if lg is None or lg.state != "waiting" or lg.way != "browser" or not lg.url:
        raise AccountError("no sign-in page to open again; start the sign-in again")
    lg.opened = False
    await _open(lg)
    return lg.public()


def cancel_login(provider_id: str) -> None:
    lg = _logins.pop(provider_id, None)
    if lg and lg.task and not lg.task.done():
        lg.task.cancel()


def forget(account_id: str) -> None:
    acct = next((a for a in store.accounts() if a["id"] == account_id), None)
    if acct is None:
        raise AccountError("no such account")
    store.delete_account(account_id)
    _readings.pop(account_id, None)
    home = Path(acct["home"])
    if _is_ours(home):
        shutil.rmtree(home, ignore_errors=True)
    registry.forget()


# ---------- the page ----------

def listing(fetch: bool = True) -> dict[str, list[dict]]:
    """Every provider's accounts with sign-in, usage and a rule verdict per work tag."""
    adopt_first_home()
    month = store.account_usage(month_start())
    out: dict[str, list[dict]] = {}
    for a in store.accounts():
        ins = registry.account_sign_ins(a["provider"])
        ok, note = ins.get(a["id"], (False, "not checked yet"))
        read = reading(a, fetch=fetch)
        used = month.get(a["id"]) or {}
        out.setdefault(a["provider"], []).append({
            **{k: a.get(k) for k in ("id", "provider", "label", "email", "plan", "priority",
                                     "monthly_credits", "credit_cap", "window_cap")},
            "enabled": bool(a["enabled"]), "allowed_for": tags(a),
            "school": school_account(a.get("email"), a.get("plan")),
            "signed_in": ok, "auth_note": note,
            "windows": read.windows if read else [],
            "credits": read.credits if read else None,
            "reading_error": read.error if read else None,
            "credits_used": credits_used(a, read),
            "month": {"runs": used.get("runs", 0), "failed": used.get("failed") or 0,
                      "tokens": (used.get("tokens_in") or 0) + (used.get("tokens_out") or 0),
                      "tokens_in": used.get("tokens_in") or 0,
                      "tokens_out": used.get("tokens_out") or 0,
                      "tokens_cached": used.get("tokens_cached") or 0,
                      "models": [m for m in (used.get("models") or "").split(",") if m]},
            # Some plans (Edu) report no balance, so a credit ceiling can't apply.
            "credit_cap_inert": a.get("credit_cap") is not None and credits_used(a, read) is None,
            "why": check(a, ok, read, None),
        })
    return out
