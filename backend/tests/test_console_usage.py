"""GET /api/console/usage — the percentages behind the console bar's chips.

The chips used to read from rate_limit events alone, which carry a status but
usually no utilization and only ever mention the window that is binding — so
the bar said "5h —" and never mentioned the week. The numbers come from our
own throttled fetch and from the cache the interactive CLI leaves in
~/.claude.json. Whichever looked at the API last wins.

What's worth pinning: the shapes that cache can be in when nothing has
refreshed it lately (absent, half-written, old), because those are the ones
that would otherwise put a confident wrong number on screen.
"""

import json
import os
import subprocess
import time
from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import claude_quota

# Trimmed to the fields the endpoint reads, from a real cachedUsageUtilization.
CACHE = {
    "cachedUsageUtilization": {
        "fetchedAtMs": 0,  # set per test
        "utilization": {
            "extra_usage": {
                "is_enabled": True, "monthly_limit": 4000, "used_credits": 2781,
                "utilization": 69.525, "currency": "USD", "decimal_places": 2,
                "spend_limit_reached": False,
            },
            "limits": [
                {"kind": "session", "group": "session", "percent": 61,
                 "severity": "normal", "resets_at": "2026-08-04T20:29:59.581813+00:00",
                 "scope": None, "is_active": False},
                {"kind": "weekly_all", "group": "weekly", "percent": 63,
                 "severity": "normal", "resets_at": "2026-08-06T02:59:59.581835+00:00",
                 "scope": None, "is_active": True},
            ],
        },
    }
}


def _iso_ago(seconds):
    return (datetime.fromtimestamp(time.time() - seconds, timezone.utc)
            .isoformat().replace("+00:00", "Z"))


@pytest.fixture
def usage_client(tmp_path, monkeypatch):
    # Once per test, not once per call: a test may call through twice to watch
    # the live cache carry across, and may set its own token up front.
    # Unsetting CLAUDE_CODE_OAUTH_TOKEN is no longer enough to keep this suite
    # off the network — _oauth_token falls back to the login keychain, which on
    # a developer's own machine really does answer. Keep the env branch (tests
    # below set that var and expect it honoured) and sever only the keychain
    # one, so "no token" means no token. The keychain's own tests call the real
    # _oauth_token with a stubbed `security`.
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.setattr(claude_quota, "_oauth_token",
                        lambda: os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", "").strip())
    monkeypatch.setattr(claude_quota, "_live", claude_quota._LiveUsage())

    def write(payload, age_seconds=60.0):
        cfg = tmp_path / "claude.json"
        if payload is not None:
            if isinstance(payload, dict):
                if "cachedUsageUtilization" in payload:
                    payload["cachedUsageUtilization"]["fetchedAtMs"] = (
                        (time.time() - age_seconds) * 1000)
                cfg.write_text(json.dumps(payload))
            else:
                cfg.write_text(payload)   # raw text, for the half-written case
        monkeypatch.setattr(claude_quota, "CLAUDE_CONFIG", cfg)

        # Through console.router, as main.py mounts it: the route lives in
        # claude_quota but rides the console's router.
        from app import console
        app = FastAPI()
        app.include_router(console.router)
        return TestClient(app).get("/api/console/usage").json()
    return write


def test_windows_carry_percentages(usage_client):
    body = usage_client(json.loads(json.dumps(CACHE)))
    by_key = {w["key"]: w for w in body["windows"]}

    # The keys match the rate_limit_type the stream uses, which is what lets a
    # live event and a cached percentage merge onto one chip.
    assert by_key["five_hour"]["percent"] == 61
    assert by_key["five_hour"]["label"] == "5h"
    assert by_key["seven_day"]["percent"] == 63
    assert by_key["seven_day"]["status"] == "allowed"
    # ISO in the file, unix seconds out — the same units rate_limit events use.
    assert by_key["seven_day"]["resets_at"] == pytest.approx(1785985199.58, abs=1)
    assert not body["stale"]


def test_credits_convert_from_minor_units(usage_client):
    body = usage_client(json.loads(json.dumps(CACHE)))
    credits = next(w for w in body["windows"] if w["key"] == "overage")
    # 2781 is cents; the tooltip says $27.81, not $2781.
    assert credits["spend"] == {"used": 27.81, "limit": 40.0, "currency": "USD"}
    assert credits["percent"] == 69.5


def test_disabled_credits_get_no_chip(usage_client):
    payload = json.loads(json.dumps(CACHE))
    payload["cachedUsageUtilization"]["utilization"]["extra_usage"]["is_enabled"] = False
    body = usage_client(payload)
    assert [w["key"] for w in body["windows"]] == ["five_hour", "seven_day"]


def test_scoped_week_names_its_model(usage_client):
    payload = json.loads(json.dumps(CACHE))
    payload["cachedUsageUtilization"]["utilization"]["limits"].append(
        {"kind": "weekly_scoped", "percent": 12, "severity": "warning",
         "resets_at": None, "scope": {"model": {"display_name": "Opus"}},
         "is_active": False})
    body = usage_client(payload)
    opus = next(w for w in body["windows"] if w["key"] == "seven_day_opus")
    assert opus["label"] == "7d opus"
    assert opus["status"] == "allowed_warning"


def test_old_cache_is_marked_stale(usage_client):
    # Only the interactive CLI refreshes this file, so an hour-old number is
    # normal — it just must not be presented as current.
    body = usage_client(json.loads(json.dumps(CACHE)), age_seconds=3600)
    assert body["stale"]
    assert body["age_seconds"] == pytest.approx(3600, abs=5)
    assert body["windows"], "stale is a caveat on the numbers, not a reason to drop them"


def test_missing_file_answers_empty(usage_client):
    body = usage_client(None)
    assert body == {"windows": [], "age_seconds": None, "stale": True}


def test_half_written_file_answers_empty(usage_client):
    # The CLI rewrites ~/.claude.json wholesale; a poll can land mid-write.
    body = usage_client('{"cachedUsageUtilization": {"fetchedAtMs"')
    assert body["windows"] == []
    assert body["stale"]


def test_never_fetched_cache_answers_empty(usage_client):
    body = usage_client({"oauthAccount": {}})
    assert body == {"windows": [], "age_seconds": None, "stale": True}


# --- asking the API ourselves ------------------------------------------------
#
# The endpoint returns the same object the CLI caches under
# cachedUsageUtilization.utilization, so these reuse CACHE's inner payload.
LIVE = CACHE["cachedUsageUtilization"]["utilization"]


def test_live_fetch_outranks_the_cache(usage_client, monkeypatch):
    monkeypatch.setattr(claude_quota, "_fetch_usage",
                        lambda: (claude_quota._windows_from_utilization(LIVE), 0.0))
    body = usage_client(json.loads(json.dumps(CACHE)), age_seconds=17000)
    by_key = {w["key"]: w for w in body["windows"]}
    assert by_key["five_hour"]["percent"] == 61
    assert body["age_seconds"] == pytest.approx(0, abs=5)
    assert not body["stale"]


def test_no_token_never_touches_the_network(usage_client, monkeypatch):
    # The default state on a machine that has not run setup-token: the files
    # must still answer, and nothing may be sent.
    def explode(*a, **k):
        raise AssertionError("no token, so no request may be made")
    monkeypatch.setattr(claude_quota.urllib.request, "urlopen", explode)

    body = usage_client(json.loads(json.dumps(CACHE)))
    assert next(w for w in body["windows"] if w["key"] == "five_hour")["percent"] == 61


def test_token_is_sent_as_bearer_with_the_beta_header(usage_client, monkeypatch):
    seen = {}

    class Resp:
        def read(self): return json.dumps(LIVE).encode()
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def capture(req, timeout=None):
        seen["url"] = req.full_url
        seen["headers"] = {k.lower(): v for k, v in req.headers.items()}
        return Resp()

    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat-secret")
    monkeypatch.setattr(claude_quota.urllib.request, "urlopen", capture)
    body = usage_client(None)

    assert seen["url"] == "https://api.anthropic.com/api/oauth/usage"
    assert seen["headers"]["authorization"] == "Bearer sk-ant-oat-secret"
    assert seen["headers"]["anthropic-beta"] == "oauth-2025-04-20"
    assert next(w for w in body["windows"] if w["key"] == "five_hour")["percent"] == 61
    # The credential must never come back out.
    assert "secret" not in json.dumps(body)


def test_rejected_token_falls_back_and_backs_off(usage_client, monkeypatch):
    calls = []

    def unauthorized(req, timeout=None):
        calls.append(1)
        raise claude_quota.urllib.error.HTTPError(
            claude_quota.USAGE_API, 401, "Unauthorized", {}, None)

    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "expired")
    monkeypatch.setattr(claude_quota.urllib.request, "urlopen", unauthorized)

    body = usage_client(json.loads(json.dumps(CACHE)))
    assert next(w for w in body["windows"] if w["key"] == "five_hour")["percent"] == 61
    # Only a new setup-token fixes a 401, so the next poll must not retry.
    assert claude_quota._live.next_try - time.time() == pytest.approx(
        claude_quota.USAGE_AUTH_BACKOFF, abs=30)
    assert len(calls) == 1


def test_transient_failure_keeps_the_last_good_answer(usage_client, monkeypatch):
    monkeypatch.setattr(claude_quota, "_fetch_usage",
                        lambda: (claude_quota._windows_from_utilization(LIVE), 0.0))
    usage_client(None)
    live = claude_quota._live
    assert live.windows, "the first call should have populated the live cache"

    # Offline on the next attempt: the previous answer stays on offer rather
    # than the chips blanking out.
    monkeypatch.setattr(claude_quota, "_fetch_usage", lambda: (None, 0.0))
    live.next_try = 0.0
    body = usage_client(None)
    assert next(w for w in body["windows"] if w["key"] == "five_hour")["percent"] == 61


def test_fetch_is_throttled_between_polls(usage_client, monkeypatch):
    calls = []

    def once():
        calls.append(1)
        return claude_quota._windows_from_utilization(LIVE), 0.0

    # The console polls every 60s and two tabs double that; the API sees one
    # request per interval regardless.
    monkeypatch.setattr(claude_quota, "_fetch_usage", once)
    live = claude_quota._LiveUsage()
    monkeypatch.setattr(claude_quota, "_live", live)
    for _ in range(4):
        claude_quota._live_snapshot()
    assert len(calls) == 1
    assert live.next_try - time.time() == pytest.approx(
        claude_quota.USAGE_FETCH_INTERVAL, abs=30)


def test_stale_live_answer_loses_to_a_fresh_file(usage_client, monkeypatch):
    # Our fetch is not privileged, only usually freshest. An hour-old success
    # must not outrank a cache the CLI wrote a minute ago.
    monkeypatch.setattr(claude_quota, "_fetch_usage", lambda: (None, 0.0))
    live = claude_quota._LiveUsage()
    live.windows = claude_quota._windows_from_utilization(LIVE)
    live.fetched = time.time() - 3600
    live.next_try = time.time() + 300
    monkeypatch.setattr(claude_quota, "_live", live)

    body = usage_client(json.loads(json.dumps(CACHE)), age_seconds=60)
    assert next(w for w in body["windows"] if w["key"] == "five_hour")["percent"] == 61
    assert body["age_seconds"] == pytest.approx(60, abs=5)


def test_api_flat_window_shape_still_parses(usage_client, monkeypatch):
    # Same endpoint, older shape: windows as flat keys with no limits[] list.
    flat = {"five_hour": {"utilization": 42,
                          "resets_at": "2026-08-25T18:00:00+00:00"},
            "seven_day": {"utilization": 7,
                          "resets_at": "2026-08-27T03:00:00+00:00"}}
    monkeypatch.setattr(claude_quota, "_fetch_usage",
                        lambda: (claude_quota._windows_from_utilization(flat), 0.0))
    body = usage_client(None)
    by_key = {w["key"]: w for w in body["windows"]}
    assert by_key["five_hour"]["percent"] == 42
    assert by_key["five_hour"]["label"] == "5h"
    assert by_key["seven_day"]["percent"] == 7


# --- where the credential comes from ------------------------------------------
#
# A `claude setup-token` token cannot answer this endpoint at all: it has no
# user:profile scope, so the API 403s. The CLI's login keychain entry does have
# it, so that is what _oauth_token reaches for. These stub `security` rather
# than touching the real keychain — the point is the plumbing, not the secret.
KEYCHAIN_JSON = json.dumps({"claudeAiOauth": {
    "accessToken": "sk-ant-oat01-from-keychain",
    "scopes": ["user:inference", "user:profile"],
    "expiresAt": 1790125111920,
}})


def _stub_security(monkeypatch, *, stdout="", exc=None):
    """Pretend to be /usr/bin/security, and record how it was called."""
    seen = {}

    def run(cmd, **kw):
        seen["cmd"] = cmd
        if exc is not None:
            raise exc
        return subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr="")
    monkeypatch.setattr(claude_quota.subprocess, "run", run)
    return seen


def test_env_token_wins_over_the_keychain(monkeypatch):
    # The escape hatch stays an escape hatch: if someone exports a token, we
    # use it and never go near the keystore.
    seen = _stub_security(monkeypatch, stdout=KEYCHAIN_JSON)
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat01-from-env")
    assert claude_quota._oauth_token() == "sk-ant-oat01-from-env"
    assert "cmd" not in seen, "keychain must not be consulted when env is set"


def test_keychain_answers_when_no_env_token(monkeypatch):
    seen = _stub_security(monkeypatch, stdout=KEYCHAIN_JSON)
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    assert claude_quota._oauth_token() == "sk-ant-oat01-from-keychain"
    assert seen["cmd"][:2] == ["security", "find-generic-password"]
    assert claude_quota.KEYCHAIN_SERVICE in seen["cmd"]
    assert "-w" in seen["cmd"], "must ask for the password only"


def test_token_is_reread_each_time_so_rotation_is_picked_up(monkeypatch):
    # The CLI rotates this credential roughly every few hours. We cache the
    # windows, never the token, so the next poll simply sees the new one.
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    _stub_security(monkeypatch, stdout=KEYCHAIN_JSON)
    assert claude_quota._oauth_token() == "sk-ant-oat01-from-keychain"

    rotated = json.dumps({"claudeAiOauth": {"accessToken": "sk-ant-oat01-rotated"}})
    _stub_security(monkeypatch, stdout=rotated)
    assert claude_quota._oauth_token() == "sk-ant-oat01-rotated"


@pytest.mark.parametrize("kind, kwargs", [
    ("missing binary", {"exc": FileNotFoundError("security")}),
    ("locked or absent", {"exc": subprocess.CalledProcessError(44, "security")}),
    ("timed out", {"exc": subprocess.TimeoutExpired("security", 5)}),
    ("not json", {"stdout": "keychain: item not found"}),
    ("json, wrong shape", {"stdout": json.dumps({"somethingElse": {}})}),
    ("empty", {"stdout": ""}),
])
def test_unreadable_keychain_is_just_no_token(monkeypatch, kind, kwargs):
    # Every one of these is a normal state on some machine — a Linux box, a
    # locked keychain, a logged-out CLI. None may raise into the request.
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    _stub_security(monkeypatch, **kwargs)
    assert claude_quota._oauth_token() == "", kind


def test_no_credential_anywhere_backs_off_without_asking(monkeypatch):
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    _stub_security(monkeypatch, exc=FileNotFoundError("security"))

    def explode(*a, **k):
        raise AssertionError("no credential, so no request may be made")
    monkeypatch.setattr(claude_quota.urllib.request, "urlopen", explode)

    windows, backoff = claude_quota._fetch_usage()
    assert windows is None
    assert backoff == claude_quota.USAGE_AUTH_BACKOFF


def test_turn_end_gets_a_fresh_reading_sooner(monkeypatch):
    # A plain poll waits the full interval; the console's "a turn just ended"
    # read (fresh=1) may ask again once USAGE_FRESH_INTERVAL has passed.
    calls = []

    def once():
        calls.append(1)
        return claude_quota._windows_from_utilization(LIVE), 0.0

    monkeypatch.setattr(claude_quota, "_fetch_usage", once)
    live = claude_quota._LiveUsage()
    monkeypatch.setattr(claude_quota, "_live", live)
    claude_quota._live_snapshot()
    claude_quota._live_snapshot(fresh=True)        # too soon even for fresh
    assert len(calls) == 1
    live.tried -= claude_quota.USAGE_FRESH_INTERVAL + 1
    claude_quota._live_snapshot()                  # a poll still waits
    assert len(calls) == 1
    claude_quota._live_snapshot(fresh=True)
    assert len(calls) == 2


def test_rate_limited_backs_off_even_for_fresh(monkeypatch):
    calls = []

    def too_many(req, timeout=None):
        calls.append(1)
        raise claude_quota.urllib.error.HTTPError(
            claude_quota.USAGE_API, 429, "Too Many Requests", {"Retry-After": "120"}, None)

    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "tok")
    monkeypatch.setattr(claude_quota.urllib.request, "urlopen", too_many)
    live = claude_quota._LiveUsage()
    monkeypatch.setattr(claude_quota, "_live", live)
    claude_quota._live_snapshot()
    assert live.next_try - time.time() == pytest.approx(120, abs=5)
    live.tried -= claude_quota.USAGE_FRESH_INTERVAL + 1
    claude_quota._live_snapshot(fresh=True)         # told to wait: no fresh skip
    assert len(calls) == 1


def test_rate_limited_without_retry_after_uses_the_default():
    exc = claude_quota.urllib.error.HTTPError(claude_quota.USAGE_API, 429, "x", {}, None)
    assert claude_quota._retry_after(exc) == claude_quota.USAGE_RATE_BACKOFF
