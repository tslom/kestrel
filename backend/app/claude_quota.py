"""GET /api/console/usage — Claude subscription quota per window (5h, the
week, paid credits) for the console bar's chips.

Moved out of console.py verbatim; it shares nothing with the session code.
console.py includes this router, so main.py wiring is unchanged.
"""

import json
import os
import subprocess
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter

router = APIRouter()


# --- usage windows -----------------------------------------------------------
#
# The rate_limit_event the CLI streams carries a status and a reset time but
# usually no utilization, and only for the window that is currently binding —
# so the console bar could say "5h —" forever and never mention the week. The
# percentages the interactive /usage screen shows come from GET
# /api/oauth/usage, which needs the OAuth token, and the CLI parks the answer
# in ~/.claude.json under cachedUsageUtilization. Reading that file is how we
# get real numbers without touching the token.
#
# The catch, and the reason age is part of the response: only the interactive
# UI refreshes that cache (throttled to five minutes). Print/SDK runs — this
# console included — never write it, so a number here can be an hour behind a
# heavy session. Better a percentage stamped with how old it is than a dash.
#
# Age is not the whole story either. Once a window's resets_at has passed, its
# percentage is not stale, it is void — the quota rolled over and the number
# describes a window that no longer exists. That is why the 5h chip was the
# wrong one so much more often than the 7d chip: five hours is short enough
# that an unrefreshed cache is usually describing a dead window, while the
# week's number is merely a little behind. The console drops the percentage
# for a window whose reset has passed (see mergeUsage), which is why resets_at
# travels with every window here even when we have no percent to pair it with.
CLAUDE_CONFIG = Path.home() / ".claude.json"

# Past this the percentage renders dimmed. It stays on screen either way — a
# number that admits its age beats a dash — but a quarter of an hour into a
# five-hour window it has drifted far enough that it should not look
# authoritative.
USAGE_STALE_SECONDS = 15 * 60

# limits[].kind -> the rate_limit_type the stream uses for the same window, so
# a live event and a cached percentage land on one chip instead of two.
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
        # A scoped week (Opus, Sonnet) names its model; anything else we have
        # never seen still renders under its own kind rather than vanishing.
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


# The API names these windows with the same strings the rate_limit stream
# uses, so its rows land on the console's existing chips without translation.
FLAT_WINDOW_LABELS = {"five_hour": "5h", "seven_day": "7d",
                      "seven_day_opus": "7d opus", "seven_day_sonnet": "7d sonnet"}


# The API's usage object, verbatim. The CLI stores exactly this under
# cachedUsageUtilization.utilization, so its cache and a live fetch parse
# through the same function rather than through two that drift apart.
def _windows_from_utilization(util: dict) -> list[dict]:
    windows = [w for w in (_usage_window(e) for e in (util.get("limits") or [])
                           if isinstance(e, dict)) if w]
    if not windows:
        # Older shape: the windows as flat keys instead of a limits[] list.
        # Same numbers, no severity or scope.
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

    # Paid overage rides in the same list under the key its rate_limit event
    # uses, so a live "rejected" and the cached percentage share one chip
    # instead of arguing next to each other.
    credits = util.get("extra_usage") or {}
    if credits.get("is_enabled") and isinstance(credits.get("utilization"), (int, float)):
        # Credits are counted in minor units (cents); everything else here is
        # already a percent.
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
        # The CLI rewrites this file wholesale, so a read can land mid-write.
        # It is a cache; the next poll gets it.
        return None

    cached = raw.get("cachedUsageUtilization") or {}
    windows = _windows_from_utilization(cached.get("utilization") or {})
    fetched = cached.get("fetchedAtMs")
    return windows, fetched / 1000 if isinstance(fetched, (int, float)) else None


# --- asking the API ourselves ------------------------------------------------
#
# The file above is somebody else's cache, and it goes quiet exactly when you
# want it most: the moment you stop using the interactive UI. A local proxy
# used to poll the endpoint on its own timer and give us a second, fresher
# reading, but that was removed on 2026-09-22 and the CLI's cache alone is
# routinely hours or days stale — so this fetch is now the only way the
# console sees a live number.
#
# Which credential to ask with. This used to be "the environment and nowhere
# else", on the principle that a dashboard has no business lifting the CLI's
# login out of the system keystore. That principle cost the feature outright,
# so it was overturned deliberately on 2026-09-22 — the reasoning, because the
# comment it replaces argued the opposite:
#
# A `claude setup-token` token does not work here and never did. It lacks the
# one scope the endpoint demands, which the endpoint states plainly:
#
#     403  OAuth token does not meet scope requirement user:profile
#          required_scopes: ["user:profile"]
#
# setup-token has no flag to ask for more (`--help` offers only -h), there is
# no `claude usage` subcommand, and `claude auth status` reports login but no
# usage. The CLI's own ~/.claude.json cache is not a fallback either: it froze
# on 2026-09-14 and does not resume — not from `-p`, not from `--bg`, and not
# from a real interactive TUI launched on a pty (all three tried). So the login
# keychain is not the convenient option, it is the only one.
#
# What makes it defensible: we read, never write. The CLI owns this credential
# and rotates it (~3h lifetime, its own refresh token); because we re-read on
# every fetch rather than caching the string, rotation is simply picked up on
# the next poll. We still never refresh it — that would rewrite a credential
# the CLI owns, which remains off limits.
#
# CLAUDE_CODE_OAUTH_TOKEN still wins when set, as an escape hatch for a token
# minted elsewhere with the right scopes. Do not point it at setup-token
# output: that silently reinstates the 403 this replaced.
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
USAGE_API = "https://api.anthropic.com/api/oauth/usage"
USAGE_API_BETA = "oauth-2025-04-20"

# How often a plain poll may ask. This was five minutes, copying the CLI's own
# throttle, and with the console polling every minute the chips ran up to six
# minutes behind a turn (New Features, 11-10). A minute is still one request
# per minute however many tabs poll, and a 429 below backs it off on its own.
USAGE_FETCH_INTERVAL = 60
# A turn just finished, which is the moment the number moved: the console asks
# with fresh=1 and may get a new reading this soon after the last attempt.
USAGE_FRESH_INTERVAL = 15
# A token we cannot use will not start working on its own — only a new
# setup-token does — so back off hard rather than asking every five minutes.
USAGE_AUTH_BACKOFF = 3600
# Too many requests: wait what the server says, or five minutes — the CLI's
# own cadence, which it has never been told off for.
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
    """(windows, extra seconds to wait before trying again)."""
    token = _oauth_token()
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
        # 401/403 is a token only a new setup-token can replace; 404 is an
        # account with no subscription windows to report. Neither improves by
        # being asked again in five minutes.
        if exc.code in (401, 403, 404):
            return None, USAGE_AUTH_BACKOFF
        return None, _retry_after(exc) if exc.code == 429 else 0.0
    except Exception:  # offline, DNS, timeout, garbage body
        return None, 0.0
    if not isinstance(payload, dict):
        return None, 0.0
    return _windows_from_utilization(payload) or None, 0.0


class _LiveUsage:
    """The last answer the API gave us, and when to bother it again.

    Process-local on purpose: it is a cache of a cache, worth nothing across a
    restart, and app.db is for data the vault owns."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.windows: list[dict] | None = None
        self.fetched = 0.0      # when that answer arrived
        self.next_try = 0.0     # do not ask again before this
        self.tried = 0.0        # when we last asked
        self.backoff = False    # next_try was set by a 401/429, not the interval


_live = _LiveUsage()


def _live_snapshot(fetch: bool = True, fresh: bool = False) -> tuple[list[dict], float] | None:
    """Windows from the API, refetched at most once per USAGE_FETCH_INTERVAL —
    or once per USAGE_FRESH_INTERVAL when `fresh` (a turn just ended), unless
    the server told us to back off. fetch=False hands back the last answer
    without asking."""
    now = time.time()
    with _live.lock:
        due = now >= _live.next_try or (
            fresh and not _live.backoff and now - _live.tried >= USAGE_FRESH_INTERVAL)
        mine = fetch and due
        if mine:
            # Claim the slot before letting go of the lock, so two open tabs
            # cannot put two requests in flight at once.
            _live.next_try = now + USAGE_FETCH_INTERVAL
            _live.tried = now
            _live.backoff = False

    if mine:
        windows, backoff = _fetch_usage()
        with _live.lock:
            if windows:
                _live.windows, _live.fetched = windows, time.time()
            if backoff:
                _live.next_try = max(_live.next_try, time.time() + backoff)
                _live.backoff = True

    with _live.lock:
        # A previous answer stays on offer after a failed refetch; console_usage
        # weighs it against the files by age like any other source.
        if not _live.windows:
            return None
        return list(_live.windows), _live.fetched


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
    """console_usage's answer. fetch=False never touches the network — for
    callers on the event loop, where a 10s urlopen would freeze the backend."""
    snaps = [s for s in (_live_snapshot(fetch, fresh), _cli_snapshot()) if s]
    if not snaps:
        return {"windows": [], "age_seconds": None, "stale": True}

    # A snapshot with no fetch time is of unknown age, so it only wins by
    # default — any dated snapshot beats it.
    windows, fetched = max(
        snaps, key=lambda snap: snap[1] if snap[1] is not None else float("-inf"))
    age = None if fetched is None else max(0.0, datetime.now().timestamp() - fetched)
    return {"windows": windows, "age_seconds": age,
            "stale": age is None or age > USAGE_STALE_SECONDS}
