"""Live model prices (model_prices.py): each list's parsing, the order they're
tried in, keeping the last good set, and pricing the delegates' runs."""

import json
import threading
from datetime import datetime

import pytest

from app import claude_quota, model_prices as mp
from app import token_usage as tu
from app.delegates import registry

REAL_GET = mp._get     # conftest swaps it for an offline stub per test

CLI_COSTS = {
    "claude-haiku-5-5": {
        "inputTokens": 0.1, "outputTokens": 0.5, "promptCacheWriteTokens": 0.125,
        "promptCacheWrite1hTokens": 0.2, "promptCacheReadTokens": 0.01,
        "longPrompt": {"abovePromptTokens": 100000, "inputTokens": 0.5, "outputTokens": 2.5,
                       "promptCacheWriteTokens": 0.625, "promptCacheWrite1hTokens": 1,
                       "promptCacheReadTokens": 0.05}},
    "claude-opus-5-5": {"inputTokens": 3, "outputTokens": 15, "promptCacheWriteTokens": 3.75,
                        "promptCacheWrite1hTokens": 6, "promptCacheReadTokens": 0.3,
                        "fast": {"inputTokens": 6, "outputTokens": 30}},
}
LITELLM = {
    "sample_spec": {"litellm_provider": "one of …", "input_cost_per_token": 0.0},
    "gpt-6.2-sol": {"litellm_provider": "openai", "mode": "chat",
                    "input_cost_per_token": 2e-06, "output_cost_per_token": 1e-05,
                    "cache_read_input_token_cost": 1e-07,
                    "input_cost_per_token_above_272k_tokens": 4e-06,
                    "output_cost_per_token_above_272k_tokens": 1.5e-05,
                    "cache_read_input_token_cost_above_272k_tokens": 2e-07,
                    "input_cost_per_token_batches": 1e-06},
    "gpt-6.2-sol-pro": {"litellm_provider": "openai", "mode": "chat",
                        "input_cost_per_token": 3e-05, "output_cost_per_token": 1.5e-04},
    "gemini/gemini-3.8-flash": {"litellm_provider": "gemini", "mode": "chat",
                                "input_cost_per_token": 9e-07, "output_cost_per_token": 4e-06},
    "gemini-3.8-flash": {"litellm_provider": "vertex_ai-language-models", "mode": "chat",
                         "input_cost_per_token": 7.5e-07, "output_cost_per_token": 3.75e-06,
                         "cache_read_input_token_cost": 7.5e-08},
    "gemini-9-pro": {"litellm_provider": "gemini", "mode": "chat",
                     "input_cost_per_token": 2e-06, "output_cost_per_token": 1.2e-05},
    "claude-opus-5-5": {"litellm_provider": "anthropic", "mode": "chat",
                        "input_cost_per_token": 4e-06, "output_cost_per_token": 2e-05},
    "claude-sonnet-5-5": {"litellm_provider": "anthropic", "mode": "chat",
                          "input_cost_per_token": 2e-06, "output_cost_per_token": 1e-05},
    "dall-e-9": {"litellm_provider": "openai", "mode": "image_generation",
                 "input_cost_per_token": 1e-06, "output_cost_per_token": 1e-06},
    "us.anthropic.claude-opus-5-5": {"litellm_provider": "bedrock", "mode": "chat",
                                     "input_cost_per_token": 9e-06, "output_cost_per_token": 9e-05},
    "free-thing": {"litellm_provider": "openai", "mode": "chat",
                   "input_cost_per_token": 0, "output_cost_per_token": 0},
}
OPENROUTER = {"data": [
    {"id": "openai/gpt-oss-120b", "pricing": {"prompt": "0.00000005", "completion": "0.00000025",
                                              "input_cache_read": "0.00000005"}},
    {"id": "openai/gpt-oss-120b:free", "pricing": {"prompt": "0", "completion": "0"}},
    {"id": "openai/gpt-6.2-sol", "pricing": {
        "prompt": "0.000002", "completion": "0.00001", "input_cache_read": "0.0000001",
        "overrides": [{"min_prompt_tokens": 272000, "prompt": "0.000004", "completion": "0.000015"}]}},
    {"id": "anthropic/claude-fable-9.1", "pricing": {"prompt": "0.00001", "completion": "0.00005"}},
    {"id": "openrouter/auto", "pricing": {"prompt": "-1", "completion": "-1"}},
]}


def _payloads(monkeypatch, litellm=LITELLM, openrouter=OPENROUTER, cli=CLI_COSTS):
    def get(url):
        if isinstance(litellm, Exception) and url == mp.LITELLM_URL:
            raise litellm
        return {mp.LITELLM_URL: litellm, mp.OPENROUTER_URL: openrouter}[url]
    monkeypatch.setattr(mp, "_get", get)
    if cli is not None:
        claude_quota.CLAUDE_CONFIG.write_text(json.dumps({"additionalModelCostsCache": cli}))


def test_each_list_parses_to_per_million_rates():
    cli = {r["model"]: r for r in mp.parse_cli(CLI_COSTS)}
    assert cli["claude-haiku-5-5"]["long_above"] == 100_000
    assert cli["claude-haiku-5-5"]["long_input"] == 0.5
    assert cli["claude-opus-5-5"]["cache_write_1h"] == 6 and cli["claude-opus-5-5"]["long_above"] is None

    lite = {r["model"]: r for r in mp.parse_litellm(LITELLM)}
    # The vendors' chat rows, bare ids; the bare id beats gemini/x.
    assert set(lite) == {"gpt-6.2-sol", "gpt-6.2-sol-pro", "gemini-3.8-flash", "gemini-9-pro",
                         "claude-opus-5-5", "claude-sonnet-5-5"}
    sol = lite["gpt-6.2-sol"]
    assert (sol["input"], sol["output"], sol["cache_read"]) == pytest.approx((2.0, 10.0, 0.1))
    assert sol["long_above"] == 272_000 and sol["long_input"] == pytest.approx(4.0)
    # No cache write listed: OpenAI charges input for it.
    assert sol["cache_write_5m"] == pytest.approx(2.0)
    assert lite["gemini-3.8-flash"]["input"] == pytest.approx(0.75)

    orr = {r["model"]: r for r in mp.parse_openrouter(OPENROUTER)}
    assert set(orr) == {"gpt-oss-120b", "gpt-6.2-sol", "claude-fable-9-1"}
    assert orr["gpt-6.2-sol"]["long_above"] == 272_000
    assert orr["gpt-oss-120b"]["output"] == pytest.approx(0.25)


def test_lookup_order_and_matching(dbfile, monkeypatch):
    _payloads(monkeypatch)
    for s in (mp.CLI, mp.LITELLM, mp.OPENROUTER):
        assert mp.refresh(s)
    b = mp.book()
    # Claude: the CLI's list first, then LiteLLM, then OpenRouter, then the table.
    assert b.lookup("claude-opus-5-5").source == mp.CLI
    assert b.lookup("claude-opus-5-5[1m]").base.input == 3
    assert b.lookup("claude-sonnet-5-5").source == mp.LITELLM
    assert b.lookup("claude-fable-9-1").source == mp.OPENROUTER
    assert b.lookup("claude-fable-5-2").source == mp.BUILTIN
    # Everything else: LiteLLM, then OpenRouter; never the Claude table.
    assert b.lookup("gpt-6.2-sol").source == mp.LITELLM
    assert b.lookup("gpt-oss-120b-medium").source == mp.OPENROUTER   # agy's effort suffix
    assert b.lookup("gemini-3.8-flash-high").model == "gemini-3.8-flash"
    assert b.lookup("gpt-6.2-sol-pro").base.input == pytest.approx(30)
    assert b.lookup("gpt-9") is None and b.lookup(None) is None
    assert b.lookup("default model") is None
    # A shorter listed id never prices a newer version of it.
    assert mp.match({"claude-opus-5": 1}, "claude-opus-5-5") is None
    assert mp.match({"claude-haiku-4-5": 1}, "claude-haiku-4-5-20251001") == 1
    assert mp.match({"gpt-4o": 1}, "gpt-4o-2024-08-06") == 1


def test_a_failed_fetch_keeps_the_last_good_set(dbfile, monkeypatch):
    _payloads(monkeypatch)
    assert mp.refresh(mp.LITELLM)
    good = mp.fetches()[mp.LITELLM]["good_at"]
    _payloads(monkeypatch, litellm=OSError("503"))
    assert not mp.refresh(mp.LITELLM)
    f = mp.fetches()[mp.LITELLM]
    assert not f["ok"] and "503" in f["note"] and f["good_at"] == good and f["models"] == 6
    assert mp.book().lookup("gpt-6.2-sol").source == mp.LITELLM
    # An empty list is a failure too, not a wipe.
    _payloads(monkeypatch, litellm={})
    assert not mp.refresh(mp.LITELLM)
    assert mp.book().lookup("gpt-6.2-sol") is not None
    # The CLI's list gone from its config: the last snapshot stands.
    claude_quota.CLAUDE_CONFIG.write_text(json.dumps({"additionalModelCostsCache": CLI_COSTS}))
    assert mp.refresh(mp.CLI)
    claude_quota.CLAUDE_CONFIG.write_text("{}")
    assert not mp.refresh(mp.CLI)
    assert mp.book().lookup("claude-opus-5-5").source == mp.CLI


def test_ensure_fresh_fetches_once_a_day(dbfile, monkeypatch):
    calls = []
    _payloads(monkeypatch)
    real = mp._get
    monkeypatch.setattr(mp, "_get", lambda url: calls.append(url) or real(url))
    mp.ensure_fresh()                      # never fetched: in line
    assert sorted(calls) == sorted(mp.WEB.values())
    calls.clear()
    mp.ensure_fresh()                      # fresh: nothing
    assert calls == []
    # A day on, with a good set in hand: in the background.
    started = []

    class Now(threading.Thread):
        def start(self):
            started.append(self.name)
            self.run()
    monkeypatch.setattr(mp.threading, "Thread", Now)
    later = mp.time.time() + 86400 + 1
    monkeypatch.setattr(mp.time, "time", lambda: later)
    mp.ensure_fresh()
    assert started == ["model-prices"] and len(calls) == 2


def test_fetch_is_an_anonymous_get(monkeypatch):
    seen = {}

    class Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b"{}"

    def urlopen(req, timeout):
        seen.update(headers=dict(req.header_items()), data=req.data, method=req.get_method())
        return Resp()
    monkeypatch.setattr(mp.urllib.request, "urlopen", urlopen)
    REAL_GET(mp.OPENROUTER_URL)
    assert seen == {"headers": {}, "data": None, "method": "GET"}


def _run(con, provider, model, tin, tout, cached=None):
    now = datetime.now().isoformat(timespec="seconds")
    con.execute(
        "INSERT INTO delegate_runs (batch, variant, kind, provider, model, status, tokens_in,"
        " tokens_out, tokens_cached, session_id, started_at, day)"
        " VALUES ('b', 1, 'delegate', ?, ?, 'ok', ?, ?, ?, 's1', ?, ?)",
        (provider, model, tin, tout, cached, now, now[:10]))


def test_delegate_runs_are_priced(con, monkeypatch, tmp_path):
    monkeypatch.setattr(tu, "CLAUDE_PROJECTS", tmp_path / "none")
    monkeypatch.setattr(tu, "_cache", tu._Cache())
    _payloads(monkeypatch)
    with con:
        # 400k in (300k cached), far past the 272k tier, but summed over a run.
        _run(con, "codex", "gpt-6.2-sol", 400_000, 10_000, 300_000)
        _run(con, "antigravity", "gemini-3.8-flash-high", 1_000_000, 0)
        _run(con, "antigravity", None, 500, 5)
    out = tu.summary("week", "model")
    rows = {r["key"]: r for r in out["rows"]}
    sol = rows["codex:gpt-6.2-sol"]
    # 100k fresh at $2, 300k cached at $0.10, 10k out at $10: base rates.
    assert sol["cost"] == pytest.approx((100_000 * 2 + 300_000 * 0.1 + 10_000 * 10) / 1e6)
    assert sol["cache_read"] == 300_000 and sol["input"] == 100_000
    assert sol["cache_saved"] == pytest.approx(300_000 * (2 - 0.1) / 1e6)
    assert sol["price"]["source"] == mp.LITELLM and sol["price"]["listed_as"] == "gpt-6.2-sol"
    assert rows["antigravity:gemini-3.8-flash-high"]["cost"] == pytest.approx(0.75)
    unnamed = rows["antigravity:default model"]
    assert unnamed["price"] is None and unnamed["unpriced"] == 1 and unnamed["cost"] == 0
    # The comparison now covers the priced errands, on both sides.
    assert out["mix_cost"] == pytest.approx(sol["cost"] + 0.75, abs=1e-4)
    assert "opus 5.5" in out["if_all_on"]
    srcs = {s["id"]: s for s in out["price_sources"]}
    assert srcs[mp.LITELLM]["models"] == 6 and srcs[mp.LITELLM]["fetched_at"]
    by_prov = {r["key"]: r for r in tu.summary("week", "provider")["rows"]}
    assert by_prov["codex"]["cost"] == pytest.approx(sol["cost"])
    assert by_prov["antigravity"]["unpriced"] == 1


def test_a_long_prompt_bills_at_the_long_tier(dbfile, monkeypatch):
    _payloads(monkeypatch)
    mp.refresh(mp.LITELLM)
    mp.refresh(mp.CLI)
    one = tu.Call(0, "gpt-6.2-sol", "codex", "s", 300_000, 0, 0, 0, 0)
    assert tu.cost(one) == pytest.approx(300_000 * 4 / 1e6)
    summed = tu.Call(0, "gpt-6.2-sol", "codex", "s", 300_000, 0, 0, 0, 0, summed=True)
    assert tu.cost(summed) == pytest.approx(300_000 * 2 / 1e6)
    haiku = tu.Call(0, "claude-haiku-5-5", "claude", "s", 100_001, 0, 0, 0, 0)
    assert tu.cost(haiku) == pytest.approx(100_001 * 0.5 / 1e6)


def test_delegate_clis_report_their_cached_tokens(tmp_path, con):
    got = registry.PROVIDERS["codex"].parse(
        '{"type":"turn.completed","usage":{"input_tokens":900,"cached_input_tokens":600,'
        '"output_tokens":40}}', tmp_path)
    assert (got.tokens_in, got.tokens_cached, got.tokens_out) == (900, 600, 40)
    # agy names its model only in its log, which goes in the scratch dir.
    argv = registry.PROVIDERS["antigravity"].argv("hi", tmp_path, None)
    assert f"--log-file={tmp_path.resolve() / registry.AGY_LOG}" in argv
    (tmp_path / registry.AGY_LOG).write_text(
        'I1123 model_config_manager.go:375] Propagating selected model override to backend: '
        'label="Gemini 3.8 Flash (High)"\n')
    with con:
        con.execute("INSERT INTO model_catalog (provider, id, family, label, rank) VALUES"
                    " ('antigravity', 'gemini-3.8-flash-high', 'gemini-flash-high',"
                    " 'Gemini 3.8 Flash (High)', 0)")
    agy = registry.PROVIDERS["antigravity"].parse(
        '{"event":"result","result":{"response":"hi","usage":{"input_tokens":5,"output_tokens":2}}}',
        tmp_path)
    assert (agy.model, agy.tokens_in, agy.tokens_cached) == ("gemini-3.8-flash-high", 5, None)
