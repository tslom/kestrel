"""model_catalog: live model lists, per-account access, and alias/retired-id resolution.

Design: DESIGN.md → `app/model_catalog.py`: test-pinned behaviours.
"""

import asyncio

import claude_agent_sdk
import pytest
from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock

from app import agent, console, console_tools, db, model_catalog as mc
from app.delegates import registry

# The real readers, before conftest's empty_model_catalog blinds them.
REAL = {name: getattr(mc, name) for name in ("catalog", "access", "stored", "fetched")}

PRO = "86cf87a0-0000-4000-8000-000000000001"
MAX = "c30f91e5-0000-4000-8000-000000000002"

# /v1/models' answer, cut down: two Fables, two Opuses (one deprecated), a
# Sonnet and a Haiku.
MODELS = {"data": [
    {"id": "claude-fable-5-2", "display_name": "Claude Fable 5.2", "line": "fable",
     "created_at": "2026-11-09T18:30:00Z", "lifecycle": "active"},
    {"id": "claude-haiku-5-5", "display_name": "Claude Haiku 5.5", "line": "haiku",
     "created_at": "2026-10-07T18:00:00Z", "lifecycle": "active"},
    {"id": "claude-sonnet-5-5", "display_name": "Claude Sonnet 5.5", "line": "sonnet",
     "created_at": "2026-09-28T00:00:00Z", "lifecycle": "active"},
    {"id": "claude-opus-5-5", "display_name": "Claude Opus 5.5", "line": "opus",
     "created_at": "2026-09-01T00:00:00Z", "lifecycle": "active"},
    {"id": "claude-fable-5-1", "display_name": "Claude Fable 5.1", "line": "fable",
     "created_at": "2026-08-01T00:00:00Z", "lifecycle": "active"},
    {"id": "claude-opus-4-5-20251101", "display_name": "Claude Opus 4.5", "line": "opus",
     "created_at": "2025-11-01T00:00:00Z", "lifecycle": "deprecated"},
]}

# What the API says when a Pro account asks for Fable.
CREDITS = {"type": "error", "error": {
    "type": "rate_limit_error", "message": "Usage credits are required for this model.",
    "details": {"error_code": "credits_required", "disabled_reason": "org_level_disabled",
                "model": "claude-fable-5-2",
                "notice": {"title": "Fable requires usage credits"}}}}


@pytest.fixture
def live(dbfile, monkeypatch):
    """The real readers on a temp app.db holding MODELS and two accounts."""
    for name, fn in REAL.items():
        monkeypatch.setattr(mc, name, fn)
    mc.save_catalog("claude", mc.claude_rows(MODELS))
    with db.session() as con, con:
        for uuid, label, plan in ((PRO, "pro@example.com", "pro"), (MAX, "max@example.com", "max")):
            con.execute("INSERT INTO claude_accounts (uuid, label, email, plan, created_at)"
                        " VALUES (?, ?, ?, ?, '2026-11-23')", (uuid, label, label, plan))
    return mc


def pro_has_no_fable():
    mc.record(PRO, "fable", False, "Fable requires usage credits", "probe")
    mc.record(PRO, "opus", True, None, "probe")
    mc.record(MAX, "fable", True, None, "probe")


# --- alias resolution -------------------------------------------------------------------

def test_an_alias_is_the_newest_active_model_of_its_family(live):
    assert mc.resolve("opus").model == "claude-opus-5-5"
    assert mc.resolve("fable").model == "claude-fable-5-2"
    assert mc.resolve("Haiku").model == "claude-haiku-5-5"
    assert mc.resolve("fable[1m]").model == "claude-fable-5-2[1m]"   # a context suffix rides along


def test_a_tier_is_whatever_settings_says_and_its_builtin_otherwise(live):
    assert mc.resolve("grunt").model == "claude-haiku-5-5"
    assert mc.resolve("plan").model == "claude-fable-5-2"
    mc.set_default("tier:grunt", "sonnet")
    assert mc.resolve("grunt").model == "claude-sonnet-5-5"


def test_default_and_empty_leave_it_to_the_cli(live):
    assert mc.resolve("default").model is None
    assert mc.resolve("").model is None


def test_with_no_list_yet_a_name_goes_through_as_given(dbfile, monkeypatch):
    for name, fn in REAL.items():
        monkeypatch.setattr(mc, name, fn)
    assert mc.resolve("opus").model == "opus"
    assert mc.resolve("grunt").model == "haiku"
    assert mc.resolve("opusplan").model == "opusplan"


# --- fallback -----------------------------------------------------------------------------

def test_a_listed_pinned_id_runs_as_is(live):
    assert mc.resolve("claude-fable-5-1") == mc.Pick("claude-fable-5-1", "claude-fable-5-1", [])


def test_a_retired_or_deprecated_id_runs_as_the_newest_of_its_family(live):
    gone = mc.resolve("claude-opus-4-8")
    assert gone.model == "claude-opus-5-5" and "no longer offered" in gone.notes[0]
    deprecated = mc.resolve("claude-opus-4-5-20251101")
    assert deprecated.model == "claude-opus-5-5" and deprecated.notes


def test_a_whole_family_gone_falls_down_the_ladder(live):
    mc.save_catalog("claude", [r for r in mc.claude_rows(MODELS) if r["family"] != "fable"])
    p = mc.resolve("fable")
    assert p.model == "claude-opus-5-5" and "no fable model" in p.notes[0]


def test_a_new_family_shows_up_with_no_code_change(live):
    rows = mc.claude_rows(MODELS) + [{"id": "claude-mythos-1", "family": "mythos",
                                      "label": "Mythos 1", "released": "2026-11-20T00:00:00Z"}]
    mc.save_catalog("claude", rows)
    assert mc.resolve("mythos").model == "claude-mythos-1"
    assert "mythos" in mc.claude_names()
    assert mc.choices()[0]["value"] == "mythos"


def test_a_failed_fetch_keeps_the_last_list(live):
    mc.save_catalog("claude", None, "/v1/models answered 500")
    assert mc.resolve("opus").model == "claude-opus-5-5"
    assert mc.fetched("claude")["ok"] == 0


# --- the access filter ----------------------------------------------------------------------

def test_pro_asking_for_fable_gets_opus_and_says_why(live):
    pro_has_no_fable()
    p = mc.pick("fable", PRO)
    assert p.model == "claude-opus-5-5"
    assert "Fable isn't available on pro@example.com (Pro):" in p.notes[0]
    assert "usage credits" in p.notes[0]
    assert mc.pick("plan", PRO).model == "claude-opus-5-5"        # the tier, too
    assert mc.pick("claude-fable-5-1", PRO).model == "claude-opus-5-5"


def test_max_keeps_fable_and_an_unprobed_family_is_not_held_back(live):
    pro_has_no_fable()
    assert mc.pick("fable", MAX) == mc.Pick("claude-fable-5-2", "fable", [])
    assert mc.pick("sonnet", PRO).model == "claude-sonnet-5-5"     # never probed: allowed


def test_the_ladder_skips_every_family_refused(live):
    pro_has_no_fable()
    mc.record(PRO, "opus", False, "nope", "probe")
    assert mc.pick("fable", PRO).model == "claude-sonnet-5-5"


def test_blocked_names_the_refused_families(live):
    pro_has_no_fable()
    assert mc.blocked(PRO) == {"fable": "Fable requires usage credits"}
    assert mc.blocked(MAX) == {}


def test_a_probe_reads_the_apis_answer():
    assert mc.verdict(200, {}) == (True, None)
    assert mc.verdict(429, CREDITS) == (False, "Fable requires usage credits")
    # A spent window says nothing about access.
    assert mc.verdict(429, {"error": {"type": "rate_limit_error", "message": "slow down"}}) == (None, None)
    assert mc.verdict(529, {}) == (None, None)
    assert mc.verdict(0, {}) == (None, None)
    assert mc.verdict(404, {"error": {"type": "not_found_error", "message": "model: x"}})[0] is False


def test_refresh_access_probes_each_family_once_then_waits(live, monkeypatch):
    asked = []

    def probe(token, model_id):
        asked.append(model_id)
        return (False, "Fable requires usage credits") if "fable" in model_id else (True, None)
    monkeypatch.setattr(mc, "probe", probe)
    monkeypatch.setattr(mc.claude_accounts, "token_for", lambda uuid: "tok")
    mc.refresh_access(PRO)
    assert sorted(asked) == ["claude-fable-5-2", "claude-haiku-5-5", "claude-opus-5-5",
                             "claude-sonnet-5-5"]               # the newest of each
    assert mc.blocked(PRO) == {"fable": "Fable requires usage credits"}
    asked.clear()
    mc.refresh_access(PRO)
    assert asked == []                                          # fresh: nothing asked
    mc.refresh_access(PRO, force=True)                          # an account switch
    assert len(asked) == 4


def test_a_refused_turn_is_learnt_and_names_the_fallback(live):
    text = "Fable 5.2 requires usage credits. Switch to another model, or manage usage credits"
    assert mc.refusal_in(text)
    assert not mc.refusal_in("You've hit your session limit · resets 2:10pm")
    assert mc.learn_refusal("claude-fable-5-2", text, PRO) == "claude-opus-5-5"
    assert mc.access(PRO)["fable"]["source"] == "turn"


# --- every path asks here ---------------------------------------------------------------------

def test_the_console_starts_a_pro_tab_on_opus_with_a_note(live, monkeypatch):
    pro_has_no_fable()
    monkeypatch.setattr(mc, "_active", lambda: PRO)
    p = console.start_model("fable", "dev")
    assert p.model == "claude-opus-5-5" and p.notes
    assert console.start_model("default", "dev").model == "claude-opus-5-5"
    mc.set_default("mode:dev", "plan")
    assert console.start_model(None, "dev").model == "claude-opus-5-5"   # plan → fable → refused


class _State:
    models: list = []


def test_open_tab_refuses_a_model_the_account_cant_run(live, monkeypatch):
    pro_has_no_fable()
    monkeypatch.setattr(mc, "_active", lambda: PRO)
    model, effort, why = console_tools._tab_model(_State(), "dev", "fable", "")
    assert model is None and "isn't available" in why and "opus" in why and "fable," not in why
    assert console_tools._tab_model(_State(), "dev", "opus", "")[0] == "opus"
    monkeypatch.setattr(mc, "_active", lambda: MAX)
    assert console_tools._tab_model(_State(), "dev", "fable", "")[0] == "fable"


def _result(**kw):
    return ResultMessage(**{"subtype": "success", "duration_ms": 1, "duration_api_ms": 1,
                            "is_error": False, "num_turns": 1, "session_id": "s", **kw})


def test_agent_ask_swaps_a_refused_model_and_retries_without_tripping(live, monkeypatch):
    """An unknown refusal is retried on Opus and learnt; the usage breaker stays shut."""
    monkeypatch.setattr(mc, "_active", lambda: PRO)
    monkeypatch.setattr(agent, "_tripped", None)
    seen = []

    async def fake_query(*, prompt, options):
        seen.append(options.model)
        if "fable" in options.model:
            yield AssistantMessage(content=[TextBlock(
                text="Fable 5.2 requires usage credits. Switch to another model.")], model="<synthetic>")
            yield _result(is_error=True, api_error_status=429,
                          result="Fable 5.2 requires usage credits. Switch to another model.")
        else:
            yield AssistantMessage(content=[TextBlock(text="hello")], model=options.model)
            yield _result()
    monkeypatch.setattr(claude_agent_sdk, "query", fake_query)
    assert asyncio.run(agent.ask("hi", model="plan")) == "hello"
    assert seen == ["claude-fable-5-2", "claude-opus-5-5"]
    assert agent._tripped is None
    seen.clear()
    assert asyncio.run(agent.ask("hi", model="fable")) == "hello"
    assert seen == ["claude-opus-5-5"]                     # learnt: straight to Opus


# --- defaults -----------------------------------------------------------------------------------

def test_a_default_is_checked_against_the_live_list(live):
    with pytest.raises(mc.DefaultError, match="no Claude model"):
        mc.set_default("mode:dev", "gpt-9")
    with pytest.raises(mc.DefaultError):
        mc.set_default("tier:grunt", "plan")                 # a tier can't name a tier
    with pytest.raises(mc.DefaultError, match="no setting"):
        mc.set_default("mode:kitchen", "opus")
    mc.set_default("mode:study", "claude-fable-5-1")
    assert mc.mode_default("study") == "claude-fable-5-1"
    mc.set_default("mode:study", "")
    assert mc.mode_default("study") == mc.builtin("mode:study")


def test_settings_says_when_a_default_fell_back(live, monkeypatch):
    pro_has_no_fable()
    monkeypatch.setattr(mc, "_active", lambda: PRO)
    mc.save_catalog("codex", [{"id": "gpt-6.2-sol", "rank": 1}, {"id": "gpt-6.1-sol", "rank": 4}])
    with db.session() as con, con:                           # set before gpt-6-sol was retired
        con.execute("INSERT INTO model_defaults (key, value) VALUES ('provider:codex', 'gpt-6-sol')")
    rows = {r["key"]: r for r in mc.get_models()["defaults"]}
    assert rows["tier:plan"]["runs"] == "claude-opus-5-5" and "Fable" in rows["tier:plan"]["notes"][0]
    assert rows["provider:codex"]["runs"] == "gpt-6.2-sol"
    assert "isn't offered any more" in rows["provider:codex"]["notes"][0]
    assert rows["tier:grunt"]["notes"] == []


# --- outside providers --------------------------------------------------------------------------

def test_an_outside_model_is_checked_against_its_list(live):
    mc.save_catalog("antigravity", [{"id": "gemini-3.8-flash-high", "rank": 0},
                                    {"id": "gemini-3.8-flash-low", "rank": 2}])
    assert mc.outside_pick("antigravity", "gemini-3.8-flash-low")[0].model == "gemini-3.8-flash-low"
    retired = mc.outside_pick("antigravity", "gemini-3.5-flash-high")[0]
    assert retired.model == "gemini-3.8-flash-high" and retired.notes
    none, why = mc.outside_pick("antigravity", "claude-opus-4-6")
    assert none is None and "gemini-3.8-flash-high" in why
    assert mc.outside_pick("antigravity", None)[0].model is None      # its own default


def test_the_provider_lists_parse(monkeypatch):
    class Done:
        def __init__(self, out):
            self.stdout, self.stderr, self.returncode = out, "", 0
    agy = ("Fetching available models...\ngemini-3.8-flash-high\tGemini 3.8 Flash (High)\n"
           "claude-sonnet-4-6\tClaude Sonnet 4.6 (Thinking)\n")
    codex = ('{"models": [{"slug": "gpt-6.2-sol", "display_name": "GPT-6.2-Sol", "priority": 1,'
             ' "visibility": "list"}, {"slug": "codex-auto-review", "priority": 43,'
             ' "visibility": "hide"}]}')
    monkeypatch.setattr(registry, "_probe_run",
                        lambda argv, env=None, timeout=0: Done(agy if argv[0] == "agy" else codex))
    assert [r["id"] for r in registry.PROVIDERS["antigravity"].models()] == [
        "gemini-3.8-flash-high", "claude-sonnet-4-6"]
    assert registry.PROVIDERS["codex"].models() == [
        {"id": "gpt-6.2-sol", "label": "GPT-6.2-Sol", "rank": 1}]


def test_an_outside_family_drops_its_version_numbers():
    assert mc.family_of("gpt-6.2-sol", "codex") == mc.family_of("gpt-5.6-sol", "codex") == "gpt-sol"
    assert mc.family_of("gemini-3.8-flash-high", "antigravity") == "gemini-flash-high"
    assert mc.family_of("claude-opus-5-5") == "opus"
