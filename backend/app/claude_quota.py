"""GET /api/console/usage: Claude subscription quota per window (5h, week, credits) for the console chips.

Two sources: our own throttled fetch of the OAuth usage API (token read, never written, from the
keychain) and the CLI's ~/.claude.json cache, which can be hours stale. Design: DESIGN.md → `app/claude_quota.py`.
"""

import json
import os
import subprocess
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter

router = APIRouter()


# --- usage windows -----------------------------------------------------------
# Age travels with the response, and so does resets_at: past it a percentage is void,
# not stale, and the console drops it (mergeUsage).
CLAUDE_CONFIG = Path.home() / ".claude.json"

# Past this the percentage renders dimmed (still shown: a number that admits its age beats a dash).
USAGE_STALE_SECONDS = 15 * 60

# limits[].kind -> the stream's rate_limit_type, so a live event and a cached percentage share a chip.
USAGE_KINDS = {"session": ("five_hour", "5h"), "weekly_all": ("seven_day", "7d")}
USAGE_SEVERITY = {"normal": "allowed", "warning": "allowed_warning",
                  "critical": "rejected", "exceeded": "rejected",
                  "rejected": "rejected"}


def _usage_reset_ts(value: object) -> float | None:
    """ISO reset stamp -> unix seconds, matching what rate_limit events send."""
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value).timestamp()
    except ValueError:
        return None


def _usage_status(pct: float) -> str:
    """Status for a source that reports a percentage and no severity of its own."""
    return "rejected" if pct >= 100 else "allowed"


def _usage_window(entry: dict) -> dict | None:
    pct = entry.get("percent")
    if not isinstance(pct, (int, float)):
        return None
    kind = str(entry.get("kind") or "")
    key, label = USAGE_KINDS.get(kind, ("", ""))
    if not key:
        # A scoped week names its model; an unknown kind renders under its own name.
        model = ((entry.get("scope") or {}).get("model") or {}).get("display_name")
        if kind == "weekly_scoped" and model:
            key, label = f"seven_day_{str(model).lower()}", f"7d {str(model).lower()}"
        else:
            key, label = kind, kind.replace("_", " ")
    status = USAGE_SEVERITY.get(str(entry.get("severity") or ""))
    if status is None:
        status = _usage_status(float(pct))
    return {"key": key, "label": label, "percent": round(float(pct), 1),
            "status": status, "resets_at": _usage_reset_ts(entry.get("resets_at")),
            "is_active": bool(entry.get("is_active"))}


# Same strings as the rate_limit stream, so rows land on the existing chips.
FLAT_WINDOW_LABELS = {"five_hour": "5h", "seven_day": "7d",
                      "seven_day_opus": "7d opus", "seven_day_sonnet": "7d sonnet"}


# Takes the API's usage object, which the CLI also stores as cachedUsageUtilization.utilization: one parser for both.
def _windows_from_utilization(util: dict) -> list[dict]:
    windows = [w for w in (_usage_window(e) for e in (util.get("limits") or [])
                           if isinstance(e, dict)) if w]
    if not windows:
        # Older shape: flat keys instead of limits[]; no severity or scope.
        for key, label in FLAT_WINDOW_LABELS.items():
            entry = util.get(key)
            if not isinstance(entry, dict):
                continue
            pct = entry.get("utilization")
            if not isinstance(pct, (int, float)):
                continue
            windows.append({"key": key, "label": label, "percent": round(float(pct), 1),
                            "status": _usage_status(float(pct)),
                            "resets_at": _usage_reset_ts(entry.get("resets_at")),
                            "is_active": False})

    # Paid overage uses its rate_limit event's key so live and cached share one chip.
    credits = util.get("extra_usage") or {}
    if credits.get("is_enabled") and isinstance(credits.get("utilization"), (int, float)):
        # Credits are in minor units (cents).
        scale = 10 ** (credits.get("decimal_places") or 0)
        windows.append({
            "key": "overage", "label": "credits",
            "percent": round(float(credits["utilization"]), 1),
            "status": "rejected" if credits.get("spend_limit_reached") else "allowed",
            "resets_at": None, "is_active": False,
            "spend": {"used": (credits.get("used_credits") or 0) / scale,
                      "limit": (credits.get("monthly_limit") or 0) / scale,
                      "currency": credits.get("currency") or "USD"},
        })
    return windows


def _cli_snapshot() -> tuple[list[dict], float | None] | None:
    """Windows the interactive CLI cached, and when it fetched them."""
    try:
        raw = json.loads(CLAUDE_CONFIG.read_text())
    except (OSError, json.JSONDecodeError):
        # The CLI rewrites this file wholesale; a mid-write read is retried next poll.
        return None

    cached = raw.get("cachedUsageUtilization") or {}
    windows = _windows_from_utilization(cached.get("utilization") or {})
    fetched = cached.get("fetchedAtMs")
    return windows, fetched / 1000 if isinstance(fetched, (int, float)) else None


# --- asking the API ourselves ------------------------------------------------
# The CLI cache alone is routinely hours stale, so this fetch is the only live number.
# The credential is the CLI's keychain login, read on every fetch and never written or
# refreshed. CLAUDE_CODE_OAUTH_TOKEN wins when set, but never a `setup-token` token: it
# lacks the `user:profile` scope (403).
KEYCHAIN_SERVICE = "Claude Code-credentials"


def _oauth_token() -> str:
    """The subscription credential to ask with, or "" if there is none."""
    token = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", "").strip()
    if token:
        return token
    try:
        raw = subprocess.run(
            ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-w"],
            capture_output=True, text=True, timeout=5, check=True).stdout
        oauth = (json.loads(raw) or {}).get("claudeAiOauth") or {}
        return str(oauth.get("accessToken") or "").strip()
    except Exception:  # absent, locked, not macOS, garbage body
        return ""


def oauth_token() -> str:
    """The slot's access token, for another module asking as the CLI does."""
    return _oauth_token()


USAGE_API = "https://api.anthropic.com/api/oauth/usage"
USAGE_API_BETA = "oauth-2025-04-20"

# One request per minute however many tabs poll; a 429 backs off separately.
USAGE_FETCH_INTERVAL = 60
# fresh=1 (a turn just ended) may refetch this soon after the last attempt.
USAGE_FRESH_INTERVAL = 15
# An unusable token won't fix itself: back off hard.
USAGE_AUTH_BACKOFF = 3600
# On 429: the server's Retry-After, else five minutes (the CLI's own cadence).
USAGE_RATE_BACKOFF = 300
USAGE_TIMEOUT = 10


def _retry_after(exc: urllib.error.HTTPError) -> float:
    """Seconds to hold off after a 429: its Retry-After, else the default."""
    try:
        wait = float((exc.headers or {}).get("Retry-After") or 0)
    except (TypeError, ValueError):
        wait = 0
    return max(wait, USAGE_FETCH_INTERVAL) if wait else USAGE_RATE_BACKOFF


def _fetch_usage() -> tuple[list[dict] | None, float]:
    """(windows, extra seconds to wait before trying again), for the account
    in the CLI's slot."""
    return _fetch_with(_oauth_token())


def _fetch_with(token: str) -> tuple[list[dict] | None, float]:
    if not token:
        return None, USAGE_AUTH_BACKOFF

    req = urllib.request.Request(USAGE_API, headers={
        "Authorization": f"Bearer {token}",
        "anthropic-beta": USAGE_API_BETA,
    })
    try:
        with urllib.request.urlopen(req, timeout=USAGE_TIMEOUT) as resp:
            payload = json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        # 401/403: bad token; 404: no subscription windows. Asking again won't help.
        if exc.code in (401, 403, 404):
            return None, USAGE_AUTH_BACKOFF
        return None, _retry_after(exc) if exc.code == 429 else 0.0
    except Exception:  # offline, DNS, timeout, garbage body
        return None, 0.0
    if not isinstance(payload, dict):
        return None, 0.0
    return _windows_from_utilization(payload) or None, 0.0


class _LiveUsage:
    """The last API answer and when to ask again. Process-local on purpose (not app.db)."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.windows: list[dict] | None = None
        self.fetched = 0.0      # when that answer arrived
        self.next_try = 0.0     # do not ask again before this
        self.tried = 0.0        # when we last asked
        self.backoff = False    # next_try was set by a 401/429, not the interval


_live = _LiveUsage()

# Each saved account has its own _LiveUsage and throttle. `_live` is the slot account's;
# set_active trades it with the parked one so chips switch at once.
_parked: dict[str, _LiveUsage] = {}
_active = ""      # account uuid in the slot, as claude_accounts last saw it
_swap = threading.Lock()


def active_key() -> str:
    return _active


def set_active(uuid: str) -> None:
    global _live, _active
    with _swap:
        if uuid == _active:
            return
        if _active:
            _parked[_active] = _live
        elif _live.windows:
            # Read before anyone knew whose it was: the slot's, which is this.
            _parked.setdefault(uuid, _live)
        _live = _parked.pop(uuid, None) or _LiveUsage()
        _active = uuid


def _cache_of(uuid: str) -> _LiveUsage | None:
    with _swap:
        return _live if uuid == _active else _parked.get(uuid)


def poll_account(uuid: str, token: Callable[[], str]) -> None:
    """Refresh a parked account's reading if its throttle allows; `token` is called only when due."""
    with _swap:
        if uuid == _active:
            return
        live = _parked.setdefault(uuid, _LiveUsage())
    _snapshot_of(live, lambda: _fetch_with(token()), True, False)


def account_windows(uuid: str) -> list[dict] | None:
    live = _cache_of(uuid)
    if live is None:
        return None
    with live.lock:
        return list(live.windows) if live.windows else None


def account_age(uuid: str) -> float | None:
    live = _cache_of(uuid)
    if live is None or not live.windows:
        return None
    return max(0.0, time.time() - live.fetched)


def forget_account(uuid: str) -> None:
    with _swap:
        _parked.pop(uuid, None)


def _live_snapshot(fetch: bool = True, fresh: bool = False) -> tuple[list[dict], float] | None:
    """The slot account's API windows, refetched per USAGE_FETCH_INTERVAL (USAGE_FRESH_INTERVAL when
    `fresh`) unless backing off. fetch=False returns the last answer."""
    return _snapshot_of(_live, _fetch_usage, fetch, fresh)


def _snapshot_of(live: _LiveUsage, fetcher: Callable[[], tuple[list[dict] | None, float]],
                 fetch: bool, fresh: bool) -> tuple[list[dict], float] | None:
    now = time.time()
    with live.lock:
        due = now >= live.next_try or (
            fresh and not live.backoff and now - live.tried >= USAGE_FRESH_INTERVAL)
        mine = fetch and due
        if mine:
            # Claim the slot before letting go of the lock, so two open tabs
            # cannot put two requests in flight at once.
            live.next_try = now + USAGE_FETCH_INTERVAL
            live.tried = now
            live.backoff = False

    if mine:
        windows, backoff = fetcher()
        with live.lock:
            if windows:
                live.windows, live.fetched = windows, time.time()
            if backoff:
                live.next_try = max(live.next_try, time.time() + backoff)
                live.backoff = True

    with live.lock:
        # A previous answer stays on offer after a failed refetch; console_usage
        # weighs it against the files by age like any other source.
        if not live.windows:
            return None
        return list(live.windows), live.fetched


@router.get("/api/console/usage")
def console_usage(fresh: bool = False) -> dict:
    """Subscription usage per window (5h, the week, paid credits), as of
    whichever source looked at the API most recently.

    Two of them: our own throttled fetch and the cache the interactive CLI
    leaves behind. Empty windows is a normal answer, not an error — it means
    nothing has successfully asked the API under this login. `fresh` is the
    console saying a turn just ended: a tighter throttle applies."""
    return usage_snapshot(fresh=fresh)


def usage_snapshot(fetch: bool = True, fresh: bool = False) -> dict:
    """console_usage's answer. fetch=False never touches the network (for event-loop callers)."""
    snaps = [s for s in (_live_snapshot(fetch, fresh), _cli_snapshot()) if s]
    if not snaps:
        return {"windows": [], "age_seconds": None, "stale": True}

    # No fetch time = unknown age: any dated snapshot beats it.
    windows, fetched = max(
        snaps, key=lambda snap: snap[1] if snap[1] is not None else float("-inf"))
    age = None if fetched is None else max(0.0, datetime.now().timestamp() - fetched)
    return {"windows": windows, "age_seconds": age,
            "stale": age is None or age > USAGE_STALE_SECONDS}
