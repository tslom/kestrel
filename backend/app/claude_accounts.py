"""Several Claude accounts, one CLI login slot, and the rules for which one Kestrel uses.

Each saved account's tokens sit in our own Keychain item (STASH_SERVICE, per uuid); a switch
copies one into the CLI's slot. app.db holds names and rules, never a token. Trap: never
refresh the slot's token here (two refreshers on one refresh token log each other out).
`check` / `pick` are pure. Design: DESIGN.md → Claude accounts.
"""

import asyncio
import getpass
import json
import logging
import os
import re
import subprocess
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from . import claude_quota, db

log = logging.getLogger(__name__)
router = APIRouter()

SLOT_SERVICE = claude_quota.KEYCHAIN_SERVICE
STASH_SERVICE = "Kestrel Claude account"
# The CLI's own OAuth client and token endpoint, so a refresh is the CLI's own request.
TOKEN_URL = "https://platform.claude.com/v1/oauth/token"
CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
# Refresh a parked account's access token this long before it lapses.
REFRESH_MARGIN = 10 * 60
TICK_SECONDS = 30
FIVE_HOURS = 5 * 3600
# A window starting this soon after Kestrel took the account began with Kestrel's call.
IDLE_SLACK = 120
# `security -i` truncates long lines; past this, fall back to argv like the CLI.
STDIN_LINE_MAX = 4000
EVENTS_KEPT = 200

_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_lock = threading.RLock()


class KeychainError(RuntimeError):
    pass


# --- the Keychain ---------------------------------------------------------------

def _kc_read(service: str, account: str | None = None) -> dict | None:
    argv = ["security", "find-generic-password", "-s", service, "-w"]
    if account:
        argv[2:2] = ["-a", account]
    try:
        run = subprocess.run(argv, capture_output=True, text=True, timeout=5)
        if run.returncode != 0:
            return None
        raw = json.loads(run.stdout)
    except Exception:  # absent, locked, not macOS, garbage body
        return None
    return raw if isinstance(raw, dict) else None


def _kc_write(service: str, account: str, blob: dict) -> None:
    """Write as the CLI does: hex through `security -i`, so the secret never
    sits on an argv unless it's too long for stdin."""
    hexed = json.dumps(blob, separators=(",", ":")).encode().hex()
    line = f'add-generic-password -U -a "{account}" -s "{service}" -X "{hexed}"\n'
    if len(line) <= STDIN_LINE_MAX:
        run = subprocess.run(["security", "-i"], input=line, capture_output=True,
                             text=True, timeout=10)
    else:
        run = subprocess.run(["security", "add-generic-password", "-U", "-a", account,
                              "-s", service, "-X", hexed], capture_output=True,
                             text=True, timeout=10)
    if run.returncode != 0 or (run.stderr or "").strip():
        raise KeychainError(f"couldn't write the Keychain item {service!r}")
    if _kc_read(service, account) != blob:
        raise KeychainError(f"the Keychain item {service!r} didn't take the write")


def _kc_delete(service: str, account: str) -> None:
    subprocess.run(["security", "delete-generic-password", "-s", service, "-a", account],
                   capture_output=True, timeout=5)


def read_slot() -> dict | None:
    """The CLI's login: {"claudeAiOauth": {...}, ...}, or None."""
    return _kc_read(SLOT_SERVICE)


def slot_uuid(slot: dict | None) -> str | None:
    oauth = (slot or {}).get("claudeAiOauth") or {}
    uuid = str(oauth.get("accountUuid") or "")
    return uuid if _UUID.fullmatch(uuid) and oauth.get("refreshToken") else None


def _write_slot(oauth: dict, slot: dict | None) -> None:
    # Keep whatever else the CLI keeps in the item (MCP logins, say).
    _kc_write(SLOT_SERVICE, getpass.getuser(), {**(slot or {}), "claudeAiOauth": oauth})


def _stash(uuid: str) -> dict | None:
    return (_kc_read(STASH_SERVICE, uuid) or {}).get("claudeAiOauth")


def _put_stash(uuid: str, oauth: dict) -> None:
    if _stash(uuid) != oauth:
        _kc_write(STASH_SERVICE, uuid, {"claudeAiOauth": oauth})


# --- the CLI's profile cache -------------------------------------------------------

def _cli_profile() -> dict:
    """~/.claude.json's oauthAccount: who the CLI thinks it is (names, no secrets)."""
    try:
        raw = json.loads(claude_quota.CLAUDE_CONFIG.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    acct = raw.get("oauthAccount")
    return acct if isinstance(acct, dict) else {}


def _set_cli_profile(profile: dict | None) -> None:
    """Point ~/.claude.json's oauthAccount at the slot's account. Best effort: the CLI rewrites it."""
    if not profile:
        return
    path = claude_quota.CLAUDE_CONFIG
    try:
        raw = json.loads(path.read_text())
        raw["oauthAccount"] = profile
        tmp = path.with_name(f".{path.name}.kestrel-{os.getpid()}")
        tmp.write_text(json.dumps(raw, indent=2))
        os.chmod(tmp, path.stat().st_mode & 0o777)
        os.replace(tmp, path)
    except (OSError, json.JSONDecodeError, ValueError) as e:
        log.warning("couldn't update the CLI's profile cache: %s", e)


# --- saved accounts (app.db) --------------------------------------------------------

def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def accounts() -> list[dict]:
    with db.session() as con:
        rows = con.execute("SELECT * FROM claude_accounts ORDER BY priority, created_at").fetchall()
    return [dict(r) for r in rows]


def _get_state(key: str, default: str | None = None) -> str | None:
    with db.session() as con:
        row = con.execute("SELECT value FROM claude_account_state WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def _set_state(key: str, value: str | None) -> None:
    with db.session() as con, con:
        if value is None:
            con.execute("DELETE FROM claude_account_state WHERE key = ?", (key,))
        else:
            con.execute("INSERT INTO claude_account_state (key, value) VALUES (?, ?) "
                        "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))


def mode() -> str:
    """"auto" (the rules choose) or "hold" (a manual pick stays put)."""
    return _get_state("mode", "auto") or "auto"


def note(text: str) -> None:
    """One line in the switch log (Settings → Claude accounts) and the backend log."""
    log.warning("[accounts] %s", text)
    with db.session() as con, con:
        con.execute("INSERT INTO claude_account_events (at, text) VALUES (?, ?)", (_now_iso(), text))
        con.execute("DELETE FROM claude_account_events WHERE id NOT IN "
                    "(SELECT id FROM claude_account_events ORDER BY id DESC LIMIT ?)", (EVENTS_KEPT,))


def events(limit: int = 12) -> list[dict]:
    with db.session() as con:
        rows = con.execute("SELECT at, text FROM claude_account_events ORDER BY id DESC LIMIT ?",
                           (limit,)).fetchall()
    return [dict(r) for r in rows]


def _adopt(uuid: str, oauth: dict) -> None:
    """A login we haven't seen: save it, last in priority."""
    prof = _cli_profile()
    mine = prof if prof.get("accountUuid") == uuid else {}
    email = mine.get("emailAddress")
    with db.session() as con, con:
        n = con.execute("SELECT COUNT(*), COALESCE(MAX(priority), -1) FROM claude_accounts").fetchone()
        con.execute(
            "INSERT OR IGNORE INTO claude_accounts (uuid, label, email, plan, profile, priority, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (uuid, email or f"Account {n[0] + 1}", email, oauth.get("subscriptionType"),
             json.dumps(mine) if mine else None, n[1] + 1, _now_iso()))
    note(f"Saved a new account: {email or uuid[:8]}")


def _update(uuid: str, **fields) -> None:
    if not fields:
        return
    cols = ", ".join(f"{k} = ?" for k in fields)
    with db.session() as con, con:
        con.execute(f"UPDATE claude_accounts SET {cols} WHERE uuid = ?", (*fields.values(), uuid))


def _label(acct: dict | None, uuid: str | None = None) -> str:
    return (acct or {}).get("label") or (uuid or "?")[:8]


# --- keeping the slot and the stashes in step -----------------------------------------

def capture() -> str | None:
    """Stash the slot under its account (saving a new one) and keep its CLI profile; returns its uuid."""
    with _lock:
        slot = read_slot()
        uuid = slot_uuid(slot)
        if uuid is None:
            return None
        oauth = slot["claudeAiOauth"]
        known = {a["uuid"]: a for a in accounts()}
        if uuid not in known:
            _adopt(uuid, oauth)
        _put_stash(uuid, oauth)
        prof = _cli_profile()
        fields = {"plan": oauth.get("subscriptionType")}
        if known.get(uuid, {}).get("needs_login"):
            fields["needs_login"] = None
        if prof.get("accountUuid") == uuid:
            fields["profile"] = json.dumps(prof)
            fields["email"] = prof.get("emailAddress") or known.get(uuid, {}).get("email")
            row = known.get(uuid) or {}
            if fields["email"] and not row.get("email") and re.fullmatch(r"Account \d+", row.get("label") or ""):
                fields["label"] = fields["email"]   # named before the CLI said who it was
        _update(uuid, **fields)
        claude_quota.set_active(uuid)
        return uuid


def switch(uuid: str, why: str) -> None:
    """Put a saved account in the CLI's slot. New CLI processes start on it;
    a turn already running finishes on the old one."""
    with _lock:
        current = capture()
        if current == uuid:
            return
        acct = next((a for a in accounts() if a["uuid"] == uuid), None)
        if acct is None:
            raise KeyError(uuid)
        oauth = _stash(uuid)
        if not oauth:
            _update(uuid, needs_login="its saved login is missing from the Keychain")
            raise KeychainError(f"No saved login for {_label(acct)}: sign in to it again.")
        _write_slot(oauth, read_slot())
        _set_cli_profile(json.loads(acct["profile"]) if acct.get("profile") else None)
        claude_quota.set_active(uuid)
        now = time.time()
        reading = claude_quota.account_windows(uuid)
        five = _live_windows(reading, now).get("five_hour")
        if current:
            _update(current, active_since=None, claimed_idle=0)
        _update(uuid, active_since=now, claimed_idle=int(_window_idle(five)))
        _set_state("desired", uuid)
        old = next((a for a in accounts() if a["uuid"] == current), None)
        note(f"Switched to {_label(acct)}" + (f" from {_label(old, current)}" if current else "")
             + f": {why}")
    for fn in list(_switch_listeners):
        try:
            fn(uuid)
        except Exception:   # a listener's failure isn't the switch's
            log.exception("switch listener failed")


# Called with the new account's uuid after every switch (model_catalog
# rechecks what it may run). Plain callables, run on the switching thread.
_switch_listeners: list = []


def on_switch(fn) -> None:
    _switch_listeners.append(fn)


# --- the rules ---------------------------------------------------------------------------

def _live_windows(windows: list[dict] | None, now: float) -> dict[str, dict]:
    """Windows by key, minus any whose reset has passed (void, not full)."""
    return {w["key"]: w for w in windows or []
            if w.get("resets_at") is None or w["resets_at"] > now}


def _window_idle(five: dict | None) -> bool:
    return five is None or not (five.get("percent") or 0) > 0


def check(acct: dict, windows: list[dict] | None, now: float, active: bool) -> str | None:
    """Why this account can't take Kestrel's work right now, or None if it can.

    `windows` is its last usage reading (None: never read); a stale one errs low, as in agent.over_budget."""
    if not acct.get("enabled", 1):
        return "turned off"
    if acct.get("needs_login"):
        return "needs signing in again"
    five_cap, week_cap = acct.get("five_hour_cap"), acct.get("weekly_cap")
    idle_only = bool(acct.get("idle_only"))
    if windows is None:
        # Unread isn't over: the account in use stays until a reading says
        # otherwise (right after a restart, every account is unread).
        ruled = five_cap is not None or week_cap is not None or idle_only
        return "no usage reading yet" if ruled and not active else None
    live = _live_windows(windows, now)
    for w in live.values():
        if w.get("status") == "rejected" and w["key"] != "overage":
            return f"{w.get('label') or w['key']} limit reached"
    for key, cap, name in (("five_hour", five_cap, "5h"), ("seven_day", week_cap, "weekly")):
        pct = (live.get(key) or {}).get("percent") or 0
        if cap is not None and pct >= cap:
            return f"{name} at {pct:.0f}% (stops at {cap:.0f}%)"
    if idle_only:
        five = live.get("five_hour")
        if not _window_idle(five) and not _kestrels_window(acct, five, active):
            return "its 5h window is in use elsewhere"
    return None


def _kestrels_window(acct: dict, five: dict, active: bool) -> bool:
    """Whether Kestrel opened this 5h window: took the account idle and held it since. Moving off ends that."""
    since, at = acct.get("active_since"), five.get("resets_at")
    if not (active and acct.get("claimed_idle") and since is not None and at is not None):
        return False
    return since <= at - FIVE_HOURS + IDLE_SLACK


def pick(accts: list[dict], readings: dict[str, list[dict] | None], now: float,
         active: str | None) -> tuple[str | None, dict[str, str | None]]:
    """The first account in priority order inside its rules, and why each of
    them is or isn't. `accts` comes in priority order."""
    whys = {a["uuid"]: check(a, readings.get(a["uuid"]), now, a["uuid"] == active)
            for a in accts}
    return next((a["uuid"] for a in accts if whys[a["uuid"]] is None), None), whys


def _soonest_reset(accts: list[dict], readings: dict, now: float) -> float | None:
    """When the earliest blocking window among the enabled accounts reopens."""
    at = [w["resets_at"] for a in accts if a.get("enabled", 1) and not a.get("needs_login")
          for w in _live_windows(readings.get(a["uuid"]), now).values()
          if w["key"] in ("five_hour", "seven_day") and w.get("resets_at")]
    return min(at) if at else None


def refusal_text(accts: list[dict], whys: dict, readings: dict, now: float) -> str:
    parts = "; ".join(f"{_label(a)}: {whys[a['uuid']]}" for a in accts)
    at = _soonest_reset(accts, readings, now)
    when = f" — a window reopens {datetime.fromtimestamp(at).strftime('%a %-I:%M %p')}" if at else ""
    return f"no Claude account is inside its rules ({parts}){when}"


class _Verdict:
    """The last tick's answer, so the guard and the console never wait on I/O."""
    refusal: str | None = None
    whys: dict[str, str | None] = {}


def start_note(uuid: str | None) -> str:
    """" · account Name" for the console's session line, once there's a
    choice of accounts to name."""
    accts = accounts()
    if not uuid or len(accts) < 2:
        return ""
    return f" · account {_label(next((a for a in accts if a['uuid'] == uuid), None), uuid)}"


def refusal() -> str | None:
    """Why Kestrel shouldn't spend on any account right now, or None. Read by
    agent.over_budget and the console's turn start."""
    return _Verdict.refusal


# --- usage for every account ----------------------------------------------------------------

def _refresh(uuid: str, oauth: dict) -> dict | None:
    """A fresh access token for a parked account, saved to its stash. None if
    the login is gone (marked needs_login)."""
    body = json.dumps({"grant_type": "refresh_token", "refresh_token": oauth["refreshToken"],
                       "client_id": CLIENT_ID,
                       "scope": " ".join(oauth.get("scopes") or [])}).encode()
    req = urllib.request.Request(TOKEN_URL, data=body, method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=claude_quota.USAGE_TIMEOUT) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        if e.code in (400, 401, 403):
            # The slot may hold newer tokens for it (a CLI still running on
            # it refreshed first); capture() takes those and clears this.
            capture()
            if _stash(uuid) == oauth:
                _update(uuid, needs_login=f"its saved login was refused ({e.code}); sign in to it again")
                note(f"{uuid[:8]}: saved login refused on refresh ({e.code})")
        return None
    except Exception:  # offline, DNS, timeout, garbage
        return None
    now_ms = time.time() * 1000
    fresh = {**oauth, "accessToken": data["access_token"],
             "refreshToken": data.get("refresh_token") or oauth["refreshToken"],
             "expiresAt": int(now_ms + float(data.get("expires_in") or 3600) * 1000)}
    if data.get("refresh_token_expires_in"):
        fresh["refreshTokenExpiresAt"] = int(now_ms + float(data["refresh_token_expires_in"]) * 1000)
    if data.get("scope"):
        fresh["scopes"] = str(data["scope"]).split()
    _put_stash(uuid, fresh)
    return fresh


def _parked_token(uuid: str) -> str:
    """Access token for an account that isn't in the slot, refreshed if due."""
    with _lock:
        if claude_quota.active_key() == uuid:
            return ""      # the slot's: claude_quota asks with the CLI's own
        oauth = _stash(uuid)
        if not oauth or not oauth.get("refreshToken"):
            return ""
        if (oauth.get("expiresAt") or 0) / 1000 - time.time() < REFRESH_MARGIN:
            oauth = _refresh(uuid, oauth)
        return str((oauth or {}).get("accessToken") or "")


def token_for(uuid: str) -> str:
    """An access token for any saved account: the slot's for the active one,
    the stash's (refreshed if due) for a parked one. Blocking."""
    if uuid == (claude_quota.active_key() or slot_uuid(read_slot())):
        return claude_quota.oauth_token()
    return _parked_token(uuid)


def readings(accts: list[dict], active: str | None, fetch: bool = True) -> dict[str, list[dict] | None]:
    out = {}
    for a in accts:
        uuid = a["uuid"]
        if uuid == active:
            snap = claude_quota.usage_snapshot(fetch=fetch)
            out[uuid] = snap["windows"] if snap["windows"] else None
        else:
            if fetch and a.get("enabled", 1) and not a.get("needs_login"):
                claude_quota.poll_account(uuid, lambda u=uuid: _parked_token(u))
            out[uuid] = claude_quota.account_windows(uuid)
    return out


# --- the tick ---------------------------------------------------------------------------------

def tick(fetch: bool = True) -> None:
    """Stash the slot, put the chosen account back if something moved it,
    read every account's usage, and apply the rules."""
    with _lock:
        active = capture()
        desired = _get_state("desired")
        accts = accounts()
        known = {a["uuid"] for a in accts}
        if desired not in known:
            desired = active
            _set_state("desired", active)
        elif active and desired != active:
            if _get_state("login_at"):
                # A sign-in just landed: that account is the one wanted.
                desired = active
                _set_state("desired", active)
            else:
                try:
                    switch(desired, "the CLI's login had moved (a running session refreshed the "
                                    "old account); put the chosen one back")
                    active = desired
                except (KeychainError, KeyError) as e:
                    note(f"Couldn't put {desired[:8]} back: {e}")
                    _set_state("desired", active)
        _set_state("login_at", None)

    reads = readings(accts, active, fetch)
    now = time.time()
    if not accts or mode() == "hold":
        _Verdict.refusal, _Verdict.whys = None, {}
        return
    target, whys = pick(accts, reads, now, active)
    _Verdict.whys = whys
    if target is None:
        why = refusal_text(accts, whys, reads, now)
        if why != _Verdict.refusal:
            note(f"Stopped: {why}")
        _Verdict.refusal = why
        return
    if _Verdict.refusal:
        note("Resumed: an account is inside its rules again")
    _Verdict.refusal = None
    if target != active:
        why = whys.get(active) if active else None
        try:
            switch(target, f"{_label(next(a for a in accts if a['uuid'] == active), active)} — {why}"
                   if why else "it's first in priority and inside its rules")
        except (KeychainError, KeyError) as e:
            note(f"Couldn't switch to {target[:8]}: {e}")


async def loop() -> None:
    while True:
        try:
            await asyncio.to_thread(tick)
        except Exception:   # the loop must outlive any one bad tick
            log.exception("account tick failed")
        await asyncio.sleep(TICK_SECONDS)


# --- routes -------------------------------------------------------------------------------------

def _public(a: dict, active: str | None, reads: dict, now: float) -> dict:
    w = reads.get(a["uuid"])
    return {"uuid": a["uuid"], "label": a["label"], "email": a.get("email"), "plan": a.get("plan"),
            "priority": a["priority"], "enabled": bool(a["enabled"]),
            "five_hour_cap": a.get("five_hour_cap"), "weekly_cap": a.get("weekly_cap"),
            "idle_only": bool(a.get("idle_only")), "needs_login": a.get("needs_login"),
            "active": a["uuid"] == active,
            "why": _Verdict.whys.get(a["uuid"]) if mode() == "auto" else None,
            "windows": [x for x in (w or []) if x["key"] in ("five_hour", "seven_day")],
            "read_age": claude_quota.account_age(a["uuid"]) if a["uuid"] != active else None}


@router.get("/api/console/accounts")
def get_accounts() -> dict:
    accts = accounts()
    active = claude_quota.active_key() or None
    reads = readings(accts, active, fetch=False)
    now = time.time()
    act = next((a for a in accts if a["uuid"] == active), None)
    return {"mode": mode(), "refusal": refusal(),
            "active": {"uuid": active, "label": _label(act, active)} if active else None,
            "accounts": [_public(a, active, reads, now) for a in accts],
            "events": events()}


@router.post("/api/console/accounts/{uuid}/use")
def post_use(uuid: str) -> dict:
    if uuid not in {a["uuid"] for a in accounts()}:
        raise HTTPException(404, "no such account")
    try:
        switch(uuid, "picked in Settings; rules off until auto is back on")
    except KeychainError as e:
        raise HTTPException(409, str(e))
    _set_state("mode", "hold")
    _Verdict.refusal = None
    return get_accounts()


@router.post("/api/console/accounts/auto")
def post_auto() -> dict:
    _set_state("mode", "auto")
    note("Auto: the rules pick the account again")
    tick(fetch=False)
    return get_accounts()


class AccountIn(BaseModel):
    label: str | None = None
    enabled: bool | None = None
    five_hour_cap: float | None = None
    weekly_cap: float | None = None
    idle_only: bool | None = None


@router.patch("/api/console/accounts/{uuid}")
def patch_account(uuid: str, body: AccountIn) -> dict:
    if uuid not in {a["uuid"] for a in accounts()}:
        raise HTTPException(404, "no such account")
    fields = body.model_dump(exclude_unset=True)
    for cap in ("five_hour_cap", "weekly_cap"):
        if fields.get(cap) is not None and not 1 <= fields[cap] <= 100:
            raise HTTPException(400, "A ceiling is a percentage from 1 to 100.")
    if "label" in fields:
        fields["label"] = (fields["label"] or "").strip()[:60] or None
        if fields["label"] is None:
            raise HTTPException(400, "A name can't be empty.")
    for flag in ("enabled", "idle_only"):
        if flag in fields:
            fields[flag] = int(bool(fields[flag]))
    _update(uuid, **fields)
    tick(fetch=False)
    return get_accounts()


class OrderIn(BaseModel):
    uuids: list[str]


@router.post("/api/console/accounts/order")
def post_order(body: OrderIn) -> dict:
    if sorted(body.uuids) != sorted(a["uuid"] for a in accounts()):
        raise HTTPException(400, "The order has to name every saved account once.")
    with db.session() as con, con:
        for i, uuid in enumerate(body.uuids):
            con.execute("UPDATE claude_accounts SET priority = ? WHERE uuid = ?", (i, uuid))
    tick(fetch=False)
    return get_accounts()


@router.delete("/api/console/accounts/{uuid}")
def delete_account(uuid: str) -> dict:
    if uuid == claude_quota.active_key():
        raise HTTPException(409, "That's the account in use; switch to another first.")
    acct = next((a for a in accounts() if a["uuid"] == uuid), None)
    if acct is None:
        raise HTTPException(404, "no such account")
    _kc_delete(STASH_SERVICE, uuid)
    with db.session() as con, con:
        con.execute("DELETE FROM claude_accounts WHERE uuid = ?", (uuid,))
    claude_quota.forget_account(uuid)
    note(f"Forgot {_label(acct)}")
    return get_accounts()


def before_login() -> None:
    """claude_account's sign-in is about to overwrite the slot: save what's in it."""
    capture()


def after_login() -> None:
    """A sign-in landed: save the new login and make it the wanted one."""
    _set_state("login_at", str(time.time()))
    tick(fetch=False)
