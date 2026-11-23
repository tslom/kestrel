"""Model usage off the transcripts (token_usage.py): dedupe, pricing,
who a session was, and the grouping."""

import json
import time
from datetime import datetime, timezone

import pytest

from app import config, console_work
from app import token_usage as tu


def _line(**kw):
    return json.dumps(kw) + "\n"


def _assistant(mid, model, out, ts, cr=0, c1=0, inp=10):
    return _line(type="assistant", timestamp=ts, message={
        "id": mid, "model": model, "role": "assistant",
        "usage": {"input_tokens": inp, "output_tokens": out, "cache_read_input_tokens": cr,
                  "cache_creation": {"ephemeral_1h_input_tokens": c1, "ephemeral_5m_input_tokens": 0}}})


@pytest.fixture
def projects(tmp_path, monkeypatch):
    root = tmp_path / "projects"
    vault = tmp_path / "vault"
    monkeypatch.setattr(config, "VAULT_PATH", vault)
    monkeypatch.setattr(tu, "CLAUDE_PROJECTS", root)
    monkeypatch.setattr(tu, "_cache", tu._Cache())
    now = datetime.now(timezone.utc).isoformat()
    v = root / "-vault"
    v.mkdir(parents=True)
    (v / "s1.jsonl").write_text(
        _line(type="user", cwd=str(vault), entrypoint="sdk-py", timestamp=now,
              message={"role": "user", "content": "Sunday digest please"})
        # Streamed twice with the same cumulative usage: counted once.
        + _assistant("m1", "claude-opus-5-5", 100, now, cr=1000)
        + _assistant("m1", "claude-opus-5-5", 100, now, cr=1000)
        + _assistant("m2", "claude-haiku-5-5", 50, now))
    sub = v / "s1" / "subagents"
    sub.mkdir(parents=True)
    (sub / "agent-a.jsonl").write_text(
        _line(type="user", cwd=str(vault), entrypoint="sdk-py", timestamp=now,
              message={"role": "user", "content": "explore"})
        + _assistant("m3", "claude-haiku-5-5", 20, now))
    d = root / "-proj"
    d.mkdir()
    (d / "s2.jsonl").write_text(
        _line(type="user", cwd="/x/proj/.claude/worktrees/tab-abc", entrypoint="sdk-py",
              timestamp=now, message={"role": "user", "content": "build it"})
        + _assistant("m4", "claude-fable-5-2", 200, now, c1=500)
        + _assistant("m5", "<synthetic>", 9, now))
    return root


def test_parse_dedupes_and_reads_who(projects):
    info, calls = tu.parse_claude(projects / "-vault" / "s1.jsonl")
    assert info.first_prompt == "Sunday digest please" and info.entrypoint == "sdk-py"
    assert sorted(c.out for c in calls) == [50, 100]
    sinfo, scalls = tu.parse_claude(projects / "-vault" / "s1" / "subagents" / "agent-a.jsonl")
    assert sinfo.subagent and sinfo.session == "s1" and scalls[0].session == "s1"


def test_cost_and_cache_savings():
    c = tu.Call(ts=0, model="claude-opus-5-5", provider="anthropic", session="s",
                inp=0, out=1_000_000, cache_read=1_000_000, cache_5m=0, cache_1h=1_000_000)
    # 20 out + 0.20 read + 4*2 1h write
    assert tu.cost(c) == pytest.approx(28.2)
    assert tu.uncached_cost(c) == pytest.approx(28.0)
    assert tu.price_for("claude-haiku-4-5-20251001") == tu.PRICES["claude-haiku-4-5"]
    assert tu.cost(tu.Call(0, "gpt-9", "openai", "s", 1, 1, 0, 0, 0)) is None


def test_haiku_long_prompt_tier():
    short = tu.Call(0, "claude-haiku-5-5", "anthropic", "s", 100_000, 0, 0, 0, 0)
    long = tu.Call(0, "claude-haiku-5-5", "anthropic", "s", 100_001, 0, 0, 0, 0)
    assert tu.cost(long) / tu.cost(short) == pytest.approx(5.0, rel=1e-3)


def test_summary_groups(projects, dbfile):
    by_mode = tu.summary("all", "mode")
    modes = {r["key"]: r for r in by_mode["rows"]}
    assert set(modes) == {"general/study", "dev"}
    assert modes["general/study"]["calls"] == 3      # m1 once, m2, the subagent's m3
    assert by_mode["total"]["calls"] == 4
    assert set(by_mode["if_all_on"]) == {"opus 5.5", "fable 5.2", "sonnet 5.5"}
    tabs = {r["key"]: r for r in tu.summary("all", "tab")["rows"]}
    assert tabs["s1"]["label"] == "Sunday digest please"
    assert tabs["s2"]["worktree"] == "tab-abc" and tabs["s2"]["project"] == "proj"
    # A session the console recorded: its mode and label win.
    console_work.remember_session("s1", tab="t1", profile="study", project=None, label="Exam prep")
    tabs = {r["key"]: r for r in tu.summary("all", "tab")["rows"]}
    assert tabs["s1"]["mode"] == "study" and tabs["s1"]["label"] == "Exam prep"
    models = {r["key"] for r in tu.summary("all", "model")["rows"]}
    assert models == {"claude:claude-opus-5-5", "claude:claude-haiku-5-5", "claude:claude-fable-5-2"}


def _run(con, provider, model, tin, tout, session, started, status="ok"):
    con.execute(
        "INSERT INTO delegate_runs (batch, variant, kind, provider, model, status,"
        " tokens_in, tokens_out, session_id, started_at, day)"
        " VALUES ('b', 1, 'delegate', ?, ?, ?, ?, ?, ?, ?, ?)",
        (provider, model, status, tin, tout, session, started, started[:10]))


def test_delegate_runs_are_their_own_provider(projects, con):
    now = datetime.now().isoformat(timespec="seconds")
    with con:
        _run(con, "gemini", "gemini-2.5-pro", 1000, 200, "s1", now)
        _run(con, "codex", None, 500, 50, "s2", now)
        _run(con, "claude", "claude-sonnet-5-5", 9, 9, "s1", now)    # a fan-out variant: in transcripts
        _run(con, "gemini", "gemini-2.5-pro", 7, 7, "s1", now, status="running")
        _run(con, "gemini", "gemini-2.5-pro", 7, 7, "s1", "2020-01-01T00:00:00")
    week = tu.summary("week", "provider")
    rows = {r["key"]: r for r in week["rows"]}
    assert set(rows) == {"claude", "gemini", "codex"}
    assert rows["gemini"]["calls"] == 1 and rows["gemini"]["input"] == 1000
    assert rows["gemini"]["cost"] == 0 and rows["gemini"]["unpriced"] == 1
    assert rows["codex"]["label"] == "ChatGPT (Codex)"
    assert [p["key"] for p in week["providers"]][0] == "claude"
    # Tiering compares Claude with Claude: errands change neither side.
    claude_only = tu.summary("week", "provider", sources=tu.SOURCES)
    assert week["claude_cost"] == claude_only["claude_cost"]
    assert week["if_all_on"] == claude_only["if_all_on"]
    # An errand counts to the tab that sent it.
    tabs = {r["key"]: r for r in tu.summary("week", "tab")["rows"]}
    assert "Gemini gemini-2.5-pro" in tabs["s1"]["models"]
    model_rows = {r["key"]: r for r in tu.summary("week", "model")["rows"]}
    assert model_rows["codex:codex"]["provider"] == "codex"


def test_a_broken_delegates_log_costs_only_its_rows(projects, dbfile, monkeypatch):
    from app.delegates import store

    def boom(*a):
        raise RuntimeError("no table")
    monkeypatch.setattr(store, "finished_runs", boom)
    assert {r["key"] for r in tu.summary("all", "provider")["rows"]} == {"claude"}


def test_rescan_reads_only_changed_files(projects, dbfile, monkeypatch):
    tu.scan()
    seen = []
    real = tu.parse_claude
    monkeypatch.setattr(tu, "SOURCES", [("anthropic", tu.claude_files,
                                         lambda p: seen.append(p.name) or real(p))])
    tu.scan()
    assert seen == []
    f = projects / "-proj" / "s2.jsonl"
    time.sleep(0.01)
    with open(f, "a") as fh:
        fh.write(_assistant("m6", "claude-opus-5-5", 5, datetime.now(timezone.utc).isoformat()))
    tu.scan()
    assert seen == ["s2.jsonl"]


def test_range_validation(dbfile):
    with pytest.raises(Exception):
        tu.range_start("forever")
    start, what = tu.range_start("5h")
    assert start is not None and "5" in what
