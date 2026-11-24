"""Saved Claude accounts (app/claude_accounts.py): the rules that pick one,
and the swap that puts it in the CLI's login slot.

check/pick are pure (a table of readings); tick() runs on conftest's fake_keychain,
usage keyed by access token.
"""

import json
import time

import pytest

from app import agent, claude_accounts as ca, claude_quota, db

# Taken at import, before conftest's autouse fixture blinds it for every test.
REAL_ACCOUNTS_REFUSAL = agent._accounts_refusal
NOW = 1_800_000_000.0
A, B, C = ("aaaaaaaa-0000-4000-8000-00000000000a", "bbbbbbbb-0000-4000-8000-00000000000b",
           "cccccccc-0000-4000-8000-00000000000c")


def win(five=None, week=None, five_reset=NOW + 3600, week_reset=NOW + 86400, status="allowed"):
    out = []
    if five is not None:
        out.append({"key": "five_hour", "label": "5h", "percent": five, "status": status,
                    "resets_at": five_reset})
    if week is not None:
        out.append({"key": "seven_day", "label": "7d", "percent": week, "status": "allowed",
                    "resets_at": week_reset})
    return out


def acct(uuid=A, **kw):
    return {"uuid": uuid, "label": uuid[0].upper(), "enabled": 1, "five_hour_cap": None,
            "weekly_cap": None, "idle_only": 0, "active_since": None, "claimed_idle": 0,
            "needs_login": None, **kw}


# --- check ----------------------------------------------------------------------------

def test_no_rules_is_eligible_even_unread():
    assert ca.check(acct(), None, NOW, active=False) is None


def test_caps_need_a_reading_except_on_the_account_in_use():
    assert ca.check(acct(weekly_cap=60), None, NOW, False) == "no usage reading yet"
    assert ca.check(acct(weekly_cap=60), None, NOW, True) is None


@pytest.mark.parametrize("five,week,caps,blocked", [
    (10, 59, {"weekly_cap": 60}, None),
    (10, 60, {"weekly_cap": 60}, "weekly at 60% (stops at 60%)"),
    (80, 10, {"five_hour_cap": 75}, "5h at 80% (stops at 75%)"),
    (74, 10, {"five_hour_cap": 75, "weekly_cap": 60}, None),
])
def test_ceilings(five, week, caps, blocked):
    assert ca.check(acct(**caps), win(five, week), NOW, False) == blocked


def test_a_window_past_its_reset_counts_as_empty():
    stale = win(five=95, five_reset=NOW - 1)
    assert ca.check(acct(five_hour_cap=50), stale, NOW, False) is None


def test_the_hard_limit_blocks_without_any_rule():
    assert ca.check(acct(), win(five=100, status="rejected"), NOW, False) == "5h limit reached"


def test_off_and_signed_out_accounts_are_skipped():
    assert ca.check(acct(enabled=0), win(0, 0), NOW, False) == "turned off"
    assert ca.check(acct(needs_login="x"), win(0, 0), NOW, False) == "needs signing in again"


# --- idle only ---------------------------------------------------------------------------

def test_idle_only_takes_an_unused_window():
    assert ca.check(acct(idle_only=1), win(five=0, week=20), NOW, False) is None
    assert ca.check(acct(idle_only=1), win(week=20), NOW, False) is None


def test_idle_only_refuses_a_window_someone_else_opened():
    assert ca.check(acct(idle_only=1), win(five=12), NOW, False) == "its 5h window is in use elsewhere"


def test_idle_only_keeps_a_window_kestrel_opened():
    reset = NOW + 4 * 3600                          # began an hour ago
    taken = acct(idle_only=1, claimed_idle=1, active_since=NOW - 3600 - 30)
    assert ca.check(taken, win(five=30, five_reset=reset), NOW, active=True) is None
    # Once Kestrel moves off, the window isn't its own any more.
    assert ca.check(taken, win(five=30, five_reset=reset), NOW, active=False) is not None


def test_idle_only_does_not_count_a_window_older_than_kestrels_claim():
    reset = NOW + 2 * 3600                          # began three hours ago
    late = acct(idle_only=1, claimed_idle=1, active_since=NOW - 3600)
    assert ca.check(late, win(five=30, five_reset=reset), NOW, True) is not None


def test_idle_only_needs_the_window_idle_when_taken():
    taken = acct(idle_only=1, claimed_idle=0, active_since=NOW - 3600 - 30)
    assert ca.check(taken, win(five=30, five_reset=NOW + 4 * 3600), NOW, True) is not None


# --- pick ----------------------------------------------------------------------------------

def test_pick_takes_the_first_inside_its_rules():
    accts = [acct(A, weekly_cap=60), acct(B, weekly_cap=60), acct(C)]
    reads = {A: win(10, 70), B: win(10, 20), C: win(0, 0)}
    target, whys = ca.pick(accts, reads, NOW, active=A)
    assert target == B
    assert whys[A].startswith("weekly at 70%") and whys[B] is None


def test_pick_none_and_the_soonest_reset():
    accts = [acct(A, weekly_cap=60), acct(B, five_hour_cap=50)]
    reads = {A: win(10, 70, week_reset=NOW + 9000), B: win(60, 10, five_reset=NOW + 1800)}
    target, whys = ca.pick(accts, reads, NOW, active=A)
    assert target is None
    text = ca.refusal_text(accts, whys, reads, NOW)
    assert "A: weekly at 70%" in text and "B: 5h at 60%" in text
    assert time.strftime("%-I:%M", time.localtime(NOW + 1800)) in text


# --- the swap and the tick -------------------------------------------------------------------

def oauth(uuid, token):
    return {"accessToken": token, "refreshToken": f"r-{token}", "expiresAt": (time.time() + 7200) * 1000,
            "scopes": ["user:inference", "user:profile"], "subscriptionType": "pro",
            "accountUuid": uuid}


@pytest.fixture
def world(dbfile, fake_keychain, monkeypatch):
    """Three accounts, A in the slot. usage[token] is what the API says."""
    usage = {}
    def slot():
        return fake_keychain.get((ca.SLOT_SERVICE, "me"))
    monkeypatch.setattr(ca.getpass, "getuser", lambda: "me")
    monkeypatch.setattr(claude_quota, "_oauth_token",
                        lambda: (slot() or {}).get("claudeAiOauth", {}).get("accessToken", ""))
    monkeypatch.setattr(claude_quota, "_fetch_with", lambda tok: (usage.get(tok), 0.0))
    monkeypatch.setattr(claude_quota, "USAGE_FETCH_INTERVAL", 0)
    claude_quota.CLAUDE_CONFIG.write_text(json.dumps(
        {"oauthAccount": {"accountUuid": A, "emailAddress": "a@example.com"}, "other": 1}))
    for uuid, tok in ((C, "tc"), (B, "tb"), (A, "ta")):     # each signs in; A ends in the slot
        fake_keychain[(ca.SLOT_SERVICE, "me")] = {"claudeAiOauth": oauth(uuid, tok), "mcpOAuth": {"x": 1}}
        ca.capture()
    ca.tick()
    return {"usage": usage, "slot": slot, "store": fake_keychain}


def uuids_in_order():
    return [a["uuid"] for a in ca.accounts()]


def test_capture_saves_each_login_once_in_sign_in_order(world):
    assert uuids_in_order() == [C, B, A]
    assert {k for k in world["store"] if k[0] == ca.STASH_SERVICE} == {
        (ca.STASH_SERVICE, u) for u in (A, B, C)}
    a = next(x for x in ca.accounts() if x["uuid"] == A)
    assert a["email"] == "a@example.com" and a["label"] == "a@example.com"
    assert "accessToken" not in json.dumps(ca.accounts())          # no token in app.db


def test_auto_moves_to_priority_then_off_a_capped_account(world):
    # Auto mode already put C, first in priority, in the slot on the first tick.
    assert world["slot"]()["claudeAiOauth"]["accountUuid"] == C
    assert world["slot"]()["mcpOAuth"] == {"x": 1}                 # the rest of the item kept
    ca.patch_account(C, ca.AccountIn(weekly_cap=60))
    world["usage"].update({"tc": win(10, 61, five_reset=time.time() + 600,
                                     week_reset=time.time() + 9000),
                           "tb": win(0, 5, five_reset=time.time() + 600,
                                     week_reset=time.time() + 9000)})
    ca.tick()
    assert world["slot"]()["claudeAiOauth"]["accountUuid"] == B
    assert claude_quota.active_key() == B
    assert ca.events()[0]["text"].startswith("Switched to") and "weekly at 61%" in ca.events()[0]["text"]
    assert ca.refusal() is None


def test_none_eligible_stops_the_guard_and_hold_overrides(world, monkeypatch):
    soon = time.time() + 600
    for a in (A, B, C):
        ca.patch_account(a, ca.AccountIn(five_hour_cap=50))
    world["usage"].update({t: win(70, 10, five_reset=soon, week_reset=soon + 9000)
                           for t in ("ta", "tb", "tc")})
    ca.tick()
    assert ca.refusal() and "no Claude account is inside its rules" in ca.refusal()
    monkeypatch.setattr(agent, "_accounts_refusal", REAL_ACCOUNTS_REFUSAL)
    assert agent.over_budget(fetch=False).startswith("automatic model calls paused — no Claude account")
    ca.post_use(A)
    assert ca.mode() == "hold" and ca.refusal() is None
    assert world["slot"]()["claudeAiOauth"]["accountUuid"] == A
    ca.post_auto()
    assert ca.mode() == "auto"


def test_a_slot_that_drifted_is_stashed_and_put_back(world):
    chosen = claude_quota.active_key()
    other = A if chosen != A else B
    newer = oauth(other, "rotated")
    world["store"][(ca.SLOT_SERVICE, "me")] = {"claudeAiOauth": newer}
    ca.tick()
    assert world["slot"]()["claudeAiOauth"]["accountUuid"] == chosen
    assert ca._stash(other) == newer                      # its rotated tokens kept
    assert "put the chosen one back" in ca.events()[0]["text"]


def test_a_new_sign_in_wins(world):
    D = "dddddddd-0000-4000-8000-00000000000d"
    ca.before_login()
    world["store"][(ca.SLOT_SERVICE, "me")] = {"claudeAiOauth": oauth(D, "td")}
    ca.after_login()
    assert uuids_in_order()[-1] == D
    # Auto still applies its priority: D is last, so the first eligible takes over.
    assert ca._get_state("login_at") is None


def test_parked_accounts_refresh_and_report(world, monkeypatch):
    stale = {**oauth(A, "ta"), "expiresAt": (time.time() + 60) * 1000}
    world["store"][(ca.STASH_SERVICE, A)] = {"claudeAiOauth": stale}
    sent = {}

    class Resp:
        def __init__(self, body): self.body = body
        def read(self): return self.body
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def urlopen(req, timeout):
        sent.update(json.loads(req.data))
        return Resp(json.dumps({"access_token": "ta2", "refresh_token": "r2", "expires_in": 3600}).encode())
    monkeypatch.setattr(ca.urllib.request, "urlopen", urlopen)
    world["usage"]["ta2"] = win(5, 7)
    assert claude_quota.active_key() != A
    ca.tick()
    assert sent["grant_type"] == "refresh_token" and sent["refresh_token"] == "r-ta"
    assert sent["client_id"] == ca.CLIENT_ID
    assert ca._stash(A)["accessToken"] == "ta2" and ca._stash(A)["refreshToken"] == "r2"
    row = next(a for a in ca.get_accounts()["accounts"] if a["uuid"] == A)
    assert [w["percent"] for w in row["windows"]] == [5, 7]


def test_a_refused_refresh_marks_the_account(world, monkeypatch):
    import urllib.error
    world["store"][(ca.STASH_SERVICE, A)] = {"claudeAiOauth": {**oauth(A, "ta"), "expiresAt": 0}}

    def urlopen(req, timeout):
        raise urllib.error.HTTPError(req.full_url, 400, "invalid_grant", {}, None)
    monkeypatch.setattr(ca.urllib.request, "urlopen", urlopen)
    ca.tick()
    a = next(x for x in ca.accounts() if x["uuid"] == A)
    assert a["needs_login"]
    assert ca.check(a, None, time.time(), False) == "needs signing in again"


def test_patch_validates(world):
    from fastapi import HTTPException
    with pytest.raises(HTTPException):
        ca.patch_account(A, ca.AccountIn(weekly_cap=0))
    with pytest.raises(HTTPException):
        ca.post_order(ca.OrderIn(uuids=[A, B]))
    ca.post_order(ca.OrderIn(uuids=[A, B, C]))
    assert uuids_in_order() == [A, B, C]


def test_no_accounts_means_no_stop(dbfile):
    ca.tick()
    assert ca.refusal() is None and ca.accounts() == []


def test_the_switch_keeps_the_cli_profile_in_step(world):
    with db.session() as con, con:
        con.execute("UPDATE claude_accounts SET profile = ? WHERE uuid = ?",
                    (json.dumps({"accountUuid": B, "emailAddress": "b@example.com"}), B))
    ca.post_use(B)
    raw = json.loads(claude_quota.CLAUDE_CONFIG.read_text())
    assert raw["oauthAccount"]["emailAddress"] == "b@example.com" and raw["other"] == 1
