"""Delegates: the registry's availability report, a fan-out batch, the tools
and the routes. No real CLI runs here: outside providers are stand-ins."""

import asyncio
import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import agent, console_tools, sessions
from app.delegates import accounts, api, boundary, registry, runs, store

# Work that may go: grunt work, for the user's own purposes.
OK = runs.Send("variants", "personal")
OK_ARGS = {"purpose": "variants", "work_for": "personal"}


def codex_json(text: str, tin: int = 120, tout: int = 40, cached: int = 20) -> str:
    """`codex exec --json` as 0.171 prints it, trimmed."""
    return "\n".join(json.dumps(e) for e in (
        {"type": "thread.started", "thread_id": "t1"}, {"type": "turn.started"},
        {"type": "item.completed", "item": {"id": "i0", "type": "agent_message", "text": text}},
        {"type": "turn.completed", "usage": {"input_tokens": tin, "cached_input_tokens": cached,
                                             "output_tokens": tout}}))


@pytest.fixture
def rig(dbfile, tmp_path, monkeypatch):
    """codex signed in on an API key, antigravity absent; Claude stubbed; outside launches recorded, answering in codex's --json shape. No real home is read."""
    monkeypatch.setattr(runs, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(boundary, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(registry, "AGY_HOME", tmp_path / "agy-first")
    monkeypatch.setattr(registry, "ACCOUNT_HOMES", tmp_path / "homes")
    monkeypatch.setattr(registry, "account_sign_ins", lambda pid: {
        a["id"]: (True, "ok") for a in store.accounts(pid)})
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setenv("CODEX_API_KEY", "sk-test")
    registry.forget()

    def probe(p):
        if p.id == "antigravity":
            return {"installed": False, "path": None, "version": None,
                    "signed_in": False, "auth_note": ""}
        return {"installed": True, "path": f"/x/{p.binary}", "version": "1.0",
                "signed_in": True, "auth_note": "ok"}
    monkeypatch.setattr(registry, "_probe", probe)

    seen = {"briefs": [], "claude": [], "argv": []}

    async def launch(argv_for, brief, scratch, **kw):
        assert isinstance(brief, boundary.Brief)
        seen["briefs"].append(brief.text)
        argv = argv_for(brief.text)
        seen["argv"].append(argv)
        if "fail" in brief.text:
            return boundary.Finished(1, "", "quota exceeded for today")
        return boundary.Finished(0, codex_json(f"```html\n<h1>from {argv[0]}</h1>\n```"), "")
    monkeypatch.setattr(boundary, "launch", launch)

    async def ask(prompt, *, model, **kw):
        seen["claude"].append((model, prompt))
        return "<h1>from claude</h1>"
    monkeypatch.setattr(agent, "ask", ask)
    yield seen
    registry.forget()


def test_status_says_what_each_provider_needs(rig):
    rows = {r["id"]: r for r in registry.status(fresh=True)}
    assert rows["claude"]["ready"] and not rows["claude"]["outside"]
    assert set(rows) == {"claude", "antigravity", "codex"}     # no Gemini CLI any more
    assert rows["codex"]["ready"] and rows["codex"]["needs"] is None
    assert not rows["antigravity"]["ready"]
    assert "brew install --cask antigravity-cli" in rows["antigravity"]["needs"]
    store.set_enabled("codex", False)
    rows = {r["id"]: r for r in registry.status()}
    assert not rows["codex"]["ready"] and "switched off" in rows["codex"]["needs"]


def test_signed_out_names_the_login(rig, monkeypatch):
    monkeypatch.setattr(registry, "_probe", lambda p: {
        "installed": True, "path": "/x", "version": "1", "signed_in": p.id == "claude",
        "auth_note": ""})
    rows = {r["id"]: r for r in registry.status(fresh=True)}
    assert "Add account" in rows["codex"]["needs"]


def test_mix_takes_turns_claude_first():
    assert runs.plan_mix(["codex", "claude"], 5, None) == [
        "claude", "codex", "claude", "codex", "claude"]
    assert runs.plan_mix(["claude"], None, None) == ["claude"] * 3
    assert runs.plan_mix(["claude"], 2, ["Codex", "claude"]) == ["codex", "claude"]


def test_fan_out_runs_the_mix_and_logs_it(rig):
    out = asyncio.run(runs.fan_out(
        "Hero section for Acme Dental; reach me at me@example.com", n=4,
        angles=["editorial serif"], artifact="hero.html", session_id="s1", send=OK))
    vs = out["variants"]
    assert [v["provider"] for v in vs] == ["claude", "codex", "claude", "codex"]
    assert all(v["status"] == "ok" for v in vs)
    # Fence stripped; one file per variant; every provider (Claude too) got the scrubbed brief.
    assert vs[1]["text"] == "<h1>from codex</h1>"
    assert (runs.variant_dir(out["batch"], 2) / "hero.html").read_text() == "<h1>from codex</h1>"
    assert all("me@example.com" not in b for b in rig["briefs"])
    assert all("[email]" in p and "me@example.com" not in p for _, p in rig["claude"])
    assert sum("editorial serif" in p for _, p in rig["claude"]) == 1
    assert any("Antigravity" in n for n in out["not_ready"])
    rows = store.batch_runs(out["batch"])
    assert [(r["variant"], r["provider"], r["status"], r["file"]) for r in rows] == [
        (1, "claude", "ok", "hero.html"), (2, "codex", "ok", "hero.html"),
        (3, "claude", "ok", "hero.html"), (4, "codex", "ok", "hero.html")]
    assert (rows[1]["tokens_in"], rows[1]["tokens_cached"], rows[1]["tokens_out"]) == (120, 20, 40)
    assert rows[1]["redactions"] == "1 email" and rows[1]["session_id"] == "s1"
    used = {u["provider"]: u for u in store.usage()}
    assert used["codex"]["runs"] == 2 and used["codex"]["tokens_out"] == 80


def test_fan_out_refuses_a_provider_that_isnt_ready(rig):
    with pytest.raises(runs.DelegateError, match="brew install --cask antigravity-cli"):
        asyncio.run(runs.fan_out("x", mix=["claude", "antigravity"], send=OK))
    with pytest.raises(runs.DelegateError, match="no provider 'gemini'"):
        asyncio.run(runs.fan_out("x", mix=["claude", "gemini"], send=OK))
    with pytest.raises(runs.DelegateError, match="most"):
        asyncio.run(runs.fan_out("x", n=9))


def test_a_failed_variant_is_reported_not_dropped(rig):
    out = asyncio.run(runs.fan_out("please fail", mix=["codex", "claude"], send=OK))
    bad, good = out["variants"]
    assert bad["status"] == "failed" and "quota" in bad["error"] and good["status"] == "ok"
    assert store.batch_runs(out["batch"])[0]["status"] == "failed"


def test_delegate_is_for_outside_providers_only(rig):
    out = asyncio.run(runs.delegate("codex", "Summarise RFC 9110 in ten bullets",
                                    send=runs.Send("public-research", "learning")))
    assert out["status"] == "ok" and out["text"] == "<h1>from codex</h1>"
    with pytest.raises(runs.DelegateError, match="this one is you"):
        asyncio.run(runs.delegate("claude", "x", send=OK))
    with pytest.raises(runs.DelegateError, match="isn't ready"):
        asyncio.run(runs.delegate("antigravity", "x", send=OK))
    with pytest.raises(runs.DelegateError, match="no provider 'gemini'"):
        asyncio.run(runs.delegate("gemini", "x", send=OK))


def test_an_agent_that_writes_the_file_itself_wins(rig, monkeypatch):
    async def launch(argv_for, brief, scratch, **kw):
        (scratch / "hero.html").write_text("<p>written</p>")
        (scratch / "extra.css").write_text("p{}")
        return boundary.Finished(0, codex_json("Done, see hero.html"), "")
    monkeypatch.setattr(boundary, "launch", launch)
    out = asyncio.run(runs.delegate("codex", "make it", artifact="hero.html", send=OK))
    assert out["text"] == "<p>written</p>" and out["files"] == ["extra.css"]


def test_artifact_is_a_plain_file_name():
    assert runs.artifact_name(None) == "result.md"
    for bad in ("../x", "a/b.html", ".env", "x y"):
        with pytest.raises(runs.DelegateError):
            runs.artifact_name(bad)


def test_parsers_fall_back_to_the_raw_text(tmp_path):
    assert registry.PROVIDERS["codex"].parse("plain words", tmp_path).text == "plain words"
    (tmp_path / registry.CODEX_REPLY).write_text("the answer\n")
    got = registry.PROVIDERS["codex"].parse(
        '{"type":"turn.completed","usage":{"input_tokens":9,"output_tokens":4}}\nnoise', tmp_path)
    assert (got.text, got.tokens_in, got.tokens_out) == ("the answer", 9, 4)
    assert not (tmp_path / registry.CODEX_REPLY).exists()


# agy 1.5.15's --output-format stream-json, trimmed from a real run in the sandbox.
AGY_STREAM = [
    '{"event":"init","conversation_id":"9d77","init":{"cwd":"/x","tools":["write_file"]}}',
    '{"event":"step_update","step_update":{"step_index":0,"state":"DONE","step_type":"user_input"}}',
    '{"event":"step_update","step_update":{"step_index":1,"state":"DONE","step_type":"agent_response",'
    '"usage":{"input_tokens":8734,"output_tokens":317,"thinking_tokens":221}}}',
    '{"event":"step_update","step_update":{"step_index":2,"state":"DONE","step_type":"tool",'
    '"tool_name":"write_file"}}',
    '{"event":"step_update","step_update":{"step_index":4,"state":"ACTIVE","step_type":"agent_response",'
    '"text_delta":"done"}}',
    '{"event":"step_update","step_update":{"step_index":4,"state":"DONE","step_type":"agent_response",'
    '"text_delta":"\\n","usage":{"input_tokens":2510,"output_tokens":578,"thinking_tokens":577}}}',
    '{"event":"result","result":{"status":"SUCCESS","response":"done\\n","num_turns":1,'
    '"usage":{"input_tokens":11244,"output_tokens":895,"thinking_tokens":798,"total_tokens":12139}}}',
]


def test_antigravity_parses_what_agy_prints(tmp_path):
    p = registry.PROVIDERS["antigravity"]
    got = p.parse("\n".join(AGY_STREAM), tmp_path)
    assert (got.text, got.tokens_in, got.tokens_out, got.model) == ("done", 11244, 895, None)
    # Cut off before the result: last response, usage summed from the steps.
    cut = p.parse("\n".join(AGY_STREAM[:-1]), tmp_path)
    assert (cut.text, cut.tokens_in, cut.tokens_out) == ("done", 11244, 895)
    assert p.parse("plain words", tmp_path).text == "plain words"
    assert [p.ends_turn(line) for line in AGY_STREAM] == [False, False, True, False, False, True, False]


def test_a_run_cut_off_keeps_what_it_had(rig, monkeypatch):
    async def launch(argv_for, brief, scratch, **kw):
        assert kw["max_turns"] == registry.AGY_MAX_TURNS and kw["ends_turn"]
        return boundary.Finished(None, "\n".join(AGY_STREAM[:-1]), "", cut="turns")
    monkeypatch.setattr(boundary, "launch", launch)
    monkeypatch.setattr(registry, "_probe", lambda p: {
        "installed": True, "path": "/x", "version": "1", "signed_in": True, "auth_note": ""})
    registry.forget()
    store.add_account("antigravity-t", "antigravity", "Test", str(registry.ACCOUNT_HOMES / "t"),
                      accounts.ALL_TAGS)
    out = asyncio.run(runs.delegate("antigravity", "make it", send=OK))
    assert out["status"] == "ok" and out["text"] == "done" and out["account"] == "Test"
    assert out["cut_off"] == f"cut off after {registry.AGY_MAX_TURNS} turns"
    row = store.batch_runs(out["batch"])[0]
    assert row["status"] == "ok" and row["error"].startswith("cut off after")
    assert row["tokens_in"] == 11244
    text, _ = _call(console_tools.delegate, {"provider": "antigravity", "brief": "make it", **OK_ARGS})
    assert "CUT OFF" in text and "account Test" in text

    async def nothing(argv_for, brief, scratch, **kw):
        return boundary.Finished(None, "", "", cut="time")
    monkeypatch.setattr(boundary, "launch", nothing)
    out = asyncio.run(runs.delegate("antigravity", "make it", send=OK))
    assert out["status"] == "failed" and "nothing to hand back" in out["error"]

    # Cut off before the artifact but after other files: it names them.
    async def partway(argv_for, brief, scratch, **kw):
        (scratch / "a.txt").write_text("one")
        return boundary.Finished(None, "", "", cut="turns")
    monkeypatch.setattr(boundary, "launch", partway)
    out = asyncio.run(runs.delegate("antigravity", "make it", artifact="notes.md", send=OK))
    assert out["status"] == "failed"
    assert "before it wrote notes.md; it left a.txt in " in out["error"]


# What agy 1.5.15 printed for a bad --model, and for no sign-in (real runs).
AGY_BAD_MODEL = ('{"event":"result","result":{"status":"ERROR","response":"","error":"invalid model '
                 'selection (--model \\"no-such-model-x\\" --effort \\"\\"): model no-such-model-x is not '
                 'recognized\\nAvailable models:\\n  Gemini 3.8 Flash (High)\\n  GPT-OSS 120B (Medium)"}}')
AGY_SIGNED_OUT = ("Authentication required. Please visit the URL to log in:\n  https://accounts.google.com/"
                  "o/oauth2/auth?x=1\n\nWaiting for authentication (timeout 60s)...\n"
                  "Error: authentication timed out.\nerror: authentication failed or timed out\n")


def test_antigravity_failures_name_the_fix(rig, monkeypatch):
    """A bad model's error says what's wrong and what to do (not stderr's last line)."""
    explain = registry.PROVIDERS["antigravity"].explain
    bad = explain(AGY_BAD_MODEL, "Available models:\n  GPT-OSS 120B (Medium)\n")
    assert "no such model" in bad and "agy models" in bad and "Gemini 3.8 Flash (High)" in bad
    out = explain("", AGY_SIGNED_OUT)
    assert "isn't signed in" in out and registry.AGY_SETUP in out
    assert explain('{"event":"result","result":{"error":"quota exhausted for today"}}', "") == \
        "quota exhausted for today"

    async def launch(argv_for, brief, scratch, **kw):
        return boundary.Finished(1, AGY_BAD_MODEL, "Available models:\n  GPT-OSS 120B (Medium)\n")
    monkeypatch.setattr(boundary, "launch", launch)
    monkeypatch.setattr(registry, "_probe", lambda p: {
        "installed": True, "path": "/x", "version": "1", "signed_in": True, "auth_note": ""})
    registry.forget()
    store.add_account("antigravity-t", "antigravity", "Test", str(registry.ACCOUNT_HOMES / "t"),
                      accounts.ALL_TAGS)
    out = asyncio.run(runs.delegate("antigravity", "hi", model="no-such-model-x", send=OK))
    assert out["status"] == "failed" and "no such model" in out["error"]


def test_antigravity_signed_in_through_its_token_file(monkeypatch, tmp_path):
    """agy's token-file fallback (a failed keychain write) still counts as signed in."""
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    token = tmp_path / ".gemini" / "antigravity-cli" / "antigravity-oauth-token"
    monkeypatch.setattr(registry, "AGY_HOME", tmp_path)
    monkeypatch.setattr(registry, "AGY_TOKEN_FILE", token)
    ok, note = registry._agy_signed_in()
    assert not ok and "no sign-in of its own" in note
    token.parent.mkdir(parents=True)
    token.write_text("{}")
    assert registry._agy_signed_in()[0]


def test_an_agent_with_caps_is_told_them():
    """A capped agent is told the limits and to write the file early; Claude's one turn isn't."""
    capped = runs._compose("Make a card.", "card.html", None, 1, 1, max_turns=30)
    assert "at most 30 steps and 10 minutes" in capped and "`card.html`" in capped
    assert "Don't build tooling" in capped
    assert "at most" not in runs._compose("Make a card.", "card.html", None, 1, 1)


def test_codex_signed_out_shows_both_ways_round(rig, monkeypatch):
    """Signed-out codex always offers both ways in (a workspace-disabled CLI leaves no trace on disk)."""
    monkeypatch.delenv("CODEX_API_KEY", raising=False)
    monkeypatch.setattr(registry, "_probe", lambda p: {
        "installed": True, "path": "/x", "version": "1", "signed_in": p.id == "claude",
        "auth_note": "Not logged in"})
    row = {r["id"]: r for r in registry.status(fresh=True)}["codex"]
    assert len(row["login"]) == 2
    assert "codex_cli_workspace_disabled" in row["login"][0] and "Add account" in row["login"][0]
    assert "CODEX_API_KEY" in row["login"][1]


def _call(tool, args):
    out = asyncio.run(tool.handler(args))
    return out["content"][0]["text"], bool(out.get("is_error"))


def test_the_tools_end_to_end(rig):
    text, err = _call(console_tools.fan_out, {"task": "Hero for a dentist", "n": 2,
                                              "artifact": "hero.html", **OK_ARGS})
    assert not err and "2 of 2 variants" in text and "shortlist" in text
    batch = text.split()[1].rstrip(":")
    said, err = _call(console_tools.shortlist, {
        "batch": batch, "picks": [2], "reasons": ["cleaner"]})
    assert not err and "2 (codex)" in said
    said, err = _call(console_tools.shortlist, {"batch": batch, "picks": [7]})
    assert err and "isn't one" in said
    said, err = _call(console_tools.delegate, {"provider": "antigravity", "brief": "x", **OK_ARGS})
    assert err and "brew install --cask antigravity-cli" in said
    said, err = _call(console_tools.delegate, {
        "provider": "codex", "brief": "Draft alt text; ask sam@example.com", **OK_ARGS})
    assert not err and "1 email" in said and "<h1>from codex</h1>" in said


def test_routes(rig):
    app = FastAPI()
    app.include_router(api.router)
    web = TestClient(app)
    rows = {r["id"]: r for r in web.get("/api/delegates/providers").json()["providers"]}
    assert rows["codex"]["ready"] and "path" not in rows["codex"] and "gemini" not in rows
    assert web.post("/api/delegates/providers/claude", json={"enabled": False}).status_code == 400
    assert web.post("/api/delegates/providers/gemini", json={"enabled": False}).status_code == 404
    off = web.post("/api/delegates/providers/codex", json={"enabled": False}).json()
    assert not {r["id"]: r for r in off["providers"]}["codex"]["ready"]
    web.post("/api/delegates/providers/codex", json={"enabled": True})

    out = asyncio.run(runs.fan_out("Hero", n=2, artifact="hero.html", send=OK))
    got = web.get(f"/api/delegates/batches/{out['batch']}").json()
    assert [v["provider"] for v in got["variants"]] == ["claude", "codex"]
    page = web.get(f"/api/delegates/batches/{out['batch']}/2/file")
    assert page.text == "<h1>from codex</h1>"
    assert page.headers["content-type"].startswith("text/html")
    assert "sandbox" in page.headers["content-security-policy"]
    assert web.get(f"/api/delegates/batches/{out['batch']}/9/file").status_code == 404
    assert web.get("/api/delegates/batches/nope").status_code == 404
    assert web.get("/api/delegates/usage").json()["providers"]


def test_transcript_names_the_model_and_keeps_delegate_calls(tmp_path, monkeypatch):
    lines = [
        {"type": "user", "uuid": "u1", "message": {"role": "user", "content": "hi"}},
        {"type": "assistant", "message": {"role": "assistant", "model": "claude-haiku-5-5",
                                          "content": [{"type": "text", "text": "hello"}]}},
        {"type": "assistant", "message": {"role": "assistant", "model": "<synthetic>",
                                          "content": [{"type": "text", "text": "limit"}]}},
        {"type": "assistant", "message": {"role": "assistant", "model": "claude-opus-5-5", "content": [
            {"type": "tool_use", "id": "t1", "name": "mcp__vault__fan_out",
             "input": {"task": "Hero", "n": 2}}]}},
        {"type": "user", "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t1", "content": "batch b: 2 of 2"}]}},
    ]
    path = tmp_path / "abc.jsonl"
    path.write_text("\n".join(json.dumps(x) for x in lines))
    monkeypatch.setattr(sessions, "_session_file", lambda sid, cwd=None: path)
    out = sessions.session_transcript("abc")
    texts = [e for e in out if e["kind"] == "text"]
    assert texts[0]["model"] == "claude-haiku-5-5" and "model" not in texts[1]
    call = next(e for e in out if e["kind"] == "tool")
    assert json.loads(call["raw"])["n"] == 2 and call["result"] == "batch b: 2 of 2"


# ---------- the gate, through the tools ----------

def test_every_errand_passes_the_gate_before_anything_runs(rig):
    """delegate and fan_out refuse coursework, a missing purpose or whose-work,
    and a study tab's errands, with nothing launched and nothing logged."""
    said, err = _call(console_tools.delegate, {
        "provider": "codex", "brief": "Draft my WRIT 150 essay intro", **OK_ARGS})
    assert err and "coursework" in said
    said, err = _call(console_tools.delegate, {"provider": "codex", "brief": "tidy this",
                                               "work_for": "personal"})
    assert err and "purpose must be one of" in said
    said, err = _call(console_tools.delegate, {"provider": "codex", "brief": "tidy this",
                                               "purpose": "format"})
    assert err and "work_for must be one of" in said
    said, err = _call(console_tools.fan_out, {"task": "Solve my problem set", "n": 2, **OK_ARGS})
    assert err and "coursework" in said

    class StudyTab:
        profile, project, announced_id = "study", None, None
    token = console_tools.SESSION.set(StudyTab())
    try:
        said, err = _call(console_tools.delegate, {"provider": "codex", "brief": "Reformat this JSON",
                                                   **OK_ARGS})
    finally:
        console_tools.SESSION.reset(token)
    assert err and "study tab" in said
    assert rig["briefs"] == [] and store.usage() == []


def test_a_claude_only_fan_out_needs_no_purpose(rig):
    """The gate is about outside models: Claude's own variants aren't gated."""
    out = asyncio.run(runs.fan_out("Three openings for the WRIT 150 essay", mix=["claude", "claude"]))
    assert [v["status"] for v in out["variants"]] == ["ok", "ok"] and rig["briefs"] == []


# ---------- accounts: which one takes an errand ----------

def _acct(id, allowed=accounts.ALL_TAGS, **kw):
    store.add_account(id, "codex", id, str(registry.ACCOUNT_HOMES / id), allowed, **kw)
    return next(a for a in store.accounts("codex") if a["id"] == id)


def test_the_router_respects_allowed_for(rig, monkeypatch):
    monkeypatch.setattr(accounts, "reading", lambda a, fetch=True: None)
    _acct("school", accounts.SCHOOL_TAGS, email="me@school.edu")
    _acct("mine")
    chosen, via = accounts.pick("codex", "learning")
    assert chosen["id"] == "school" and via == "on account school"
    chosen, via = accounts.pick("codex", "commercial")
    assert chosen["id"] == "mine" and "skipped school (not allowed for commercial work" in via
    store.update_account("mine", enabled=0)
    # With an API key set, the key takes what no account may (it's billed to it).
    assert accounts.pick("codex", "commercial") == (None, "on its API key (CODEX_API_KEY): no account fits")
    monkeypatch.delenv("CODEX_API_KEY")
    with pytest.raises(accounts.AccountError, match="school: not allowed for commercial"):
        accounts.pick("codex", "commercial")


def test_ceilings_stop_an_account(rig):
    a = _acct("capped", monthly_credits=6500, credit_cap=6000, window_cap=80)
    full = accounts.Reading(0, windows=[{"key": "primary", "label": "5h", "percent": 85,
                                         "resets_at": None}])
    assert "5h window at 85%" in accounts.check(a, True, full, "personal")
    spent = accounts.Reading(0, credits={"balance": 400.0, "unlimited": False, "has_credits": True})
    assert accounts.credits_used(a, spent) == 6100
    assert "6,100 credits used" in accounts.check(a, True, spent, "personal")
    assert accounts.check(a, False, None, "personal") == "not signed in"
    assert accounts.check(a, True, None, "personal") is None


def test_codex_usage_and_identity_parse(tmp_path):
    got = accounts.parse_usage({
        "plan_type": "edu",
        "rate_limit": {"primary_window": {"used_percent": 12, "limit_window_seconds": 18000,
                                          "reset_at": 2_000_000_000},
                       "secondary_window": {"used_percent": 3, "limit_window_seconds": 604800,
                                            "reset_after_seconds": 60}},
        "credits": {"has_credits": True, "unlimited": False, "balance": "6123.5"}}, 1000.0)
    assert [(w["label"], w["percent"]) for w in got.windows] == [("5h", 12.0), ("Week", 3.0)]
    assert got.windows[1]["resets_at"] == 1060.0
    assert got.credits == {"balance": 6123.5, "unlimited": False, "has_credits": True}
    import base64
    claims = base64.urlsafe_b64encode(json.dumps({
        "email": "me@school.edu",
        "https://api.openai.com/auth": {"chatgpt_plan_type": "edu"}}).encode()).decode().rstrip("=")
    (tmp_path / "auth.json").write_text(json.dumps({"tokens": {"id_token": f"h.{claims}.s"}}))
    who = accounts.codex_identity(tmp_path)
    assert who == {"email": "me@school.edu", "plan": "edu"}
    assert accounts.school_account(who["email"], None) and accounts.school_account(None, "edu")


def test_a_signed_in_home_becomes_an_account_school_ones_limited(rig, monkeypatch, tmp_path):
    """The end of a sign-in: a school login is saved as personal and learning
    only; one that didn't sign in takes its home with it."""
    home = registry.ACCOUNT_HOMES / "codex-abc"
    home.mkdir(parents=True)
    import dataclasses
    monkeypatch.setitem(registry.PROVIDERS, "codex", dataclasses.replace(
        registry.PROVIDERS["codex"], home_signed_in=lambda h: (h == home, "Logged in using ChatGPT")))
    monkeypatch.setattr(accounts, "codex_identity", lambda h: {"email": "me@school.edu", "plan": "edu"})
    lg = accounts.Login("codex", "codex-abc", None, None)
    accounts._finish(lg, home)
    saved = store.accounts("codex")[0]
    assert lg.state == "done" and saved["allowed_for"] == "personal,learning"
    assert saved["label"] == "me@school.edu" and saved["home"] == str(home)
    other = registry.ACCOUNT_HOMES / "codex-def"
    other.mkdir()
    bad = accounts.Login("codex", "codex-def", None, None)
    accounts._finish(bad, other)
    assert bad.state == "failed" and not other.exists() and len(store.accounts("codex")) == 1


def test_an_account_run_names_and_records_codexs_model(rig, monkeypatch):
    """codex --json names no model: an account run passes the catalog's lowest-priority listed one and records it; a named model wins."""
    monkeypatch.setattr(accounts, "reading", lambda a, fetch=True: None)
    a = _acct("school", accounts.SCHOOL_TAGS, email="me@school.edu")
    home = Path(a["home"])
    home.mkdir(parents=True)
    (home / "models_cache.json").write_text(json.dumps({"models": [
        {"slug": "gpt-6.1-terra", "priority": 2, "visibility": "list"},
        {"slug": "gpt-reserve", "priority": 0, "visibility": "hide"},
        {"slug": "gpt-6.1-sol", "priority": 1, "visibility": "list"}]}))
    out = asyncio.run(runs.delegate("codex", "make it", send=OK))
    assert out["model"] == "gpt-6.1-sol" and out["account"] == "school"
    assert rig["argv"][-1][rig["argv"][-1].index("--model") + 1] == "gpt-6.1-sol"
    assert store.batch_runs(out["batch"])[0]["model"] == "gpt-6.1-sol"
    out = asyncio.run(runs.delegate("codex", "make it", model="gpt-6.1-terra", send=OK))
    assert store.batch_runs(out["batch"])[0]["model"] == "gpt-6.1-terra"
    assert registry.PROVIDERS["codex"].home_model(home / "nowhere") is None


def test_no_balance_means_no_credit_count_and_says_so(rig, monkeypatch):
    """An Edu workspace reports windows and a null balance: invent nothing, flag the credit stop as unable to apply, use Kestrel's own counts."""
    read = accounts.parse_usage({"plan_type": "education", "rate_limit": {
        "primary_window": {"used_percent": 1, "limit_window_seconds": 18000, "reset_at": 2e9}},
        "credits": {"has_credits": True, "unlimited": False, "balance": None}}, 1000.0)
    monkeypatch.setattr(accounts, "reading", lambda a, fetch=True: read)
    _acct("usc", accounts.SCHOOL_TAGS, email="me@school.edu", plan="education",
          monthly_credits=6500, credit_cap=6500)
    run = store.start_run("b", 1, "delegate", "codex", "gpt-6.1-sol", None, (), None, "usc")
    store.finish_run(run, status="ok", tokens_in=58432, tokens_out=302, tokens_cached=43264)
    [row] = accounts.listing()["codex"]
    assert row["credits_used"] is None and row["credit_cap_inert"] and row["why"] is None
    assert row["school"] and row["windows"][0]["percent"] == 1.0
    assert row["month"] == {"runs": 1, "failed": 0, "tokens": 58734, "tokens_in": 58432,
                            "tokens_out": 302, "tokens_cached": 43264, "models": ["gpt-6.1-sol"]}


def test_agys_first_home_becomes_an_account_with_its_runs(rig):
    """The antigravity_home.py home (token-file sign-in) is adopted once, with its earlier runs."""
    token = registry.AGY_HOME / registry.AGY_TOKEN_PATH
    token.parent.mkdir(parents=True)
    store.start_run("b", 1, "delegate", "antigravity", None, None, (), None)
    accounts.adopt_first_home()
    token.write_text("tok")
    accounts.adopt_first_home()
    accounts.adopt_first_home()
    [acct] = store.accounts("antigravity")
    assert acct["id"] == "antigravity-main" and acct["home"] == str(registry.AGY_HOME)
    assert store.batch_runs("b")[0]["account"] == "antigravity-main"


# What `codex login` 0.171 printed in a real run (the state and challenge cut).
CODEX_BROWSER = [
    "Starting local login server on http://localhost:1457.",
    "If your browser did not open, navigate to this URL to authenticate:",
    "",
    "https://auth.openai.com/oauth/authorize?response_type=code&client_id=app_x"
    "&redirect_uri=http%3A%2F%2F127.0.0.1%3A1457%2Fauth%2Fcallback&state=s",
    "On a remote or headless machine? Use `codex login --device-auth` instead.",
]


def test_codexs_browser_sign_in_is_read_off_its_output():
    lg = accounts.Login("codex", "codex-x", None, None, "browser")
    for line in CODEX_BROWSER:
        accounts._read_line(lg, line)
    assert lg.port == 1457 and lg.state == "waiting" and lg.error is None
    assert lg.url.startswith("https://auth.openai.com/oauth/authorize?")


def test_device_code_and_links_are_read_off_the_output():
    lg = accounts.Login("codex", "codex-x", None, None, "device")
    for line in ("\x1b[94mhttps://auth.openai.com/codex/device\x1b[0m",
                 "2. Enter this one-time code \x1b[90m(expires in 15 minutes)\x1b[0m",
                 "   \x1b[94mZO68-DV4M5\x1b[0m"):
        accounts._read_line(lg, line)
    assert (lg.url, lg.code, lg.state) == ("https://auth.openai.com/codex/device", "ZO68-DV4M5", "waiting")


@pytest.mark.parametrize("said, plain", [
    ("Please contact your workspace admin to enable device code authentication",
     "device-code sign-in turned off"),
    ("Codex is not enabled for your workspace. Contact your workspace administrator",
     "isn't allowed to use Codex"),
    ("Error logging in: Operation not permitted (os error 1)", "1455 and 1457 are both taken"),
    ("Sign-in was cancelled.", "closed before it finished"),
])
def test_sign_in_errors_are_said_plainly(said, plain):
    lg = accounts.Login("codex", "codex-x", None, None, "device")
    accounts._read_line(lg, said)
    assert plain in lg.error


class _FakeAcp:
    """`agy acp` on pipes: answers initialize with its auth methods and authenticate with `outcome`."""

    def __init__(self, outcome, methods=("oauth-personal", "gemini-api-key")):
        self.sent, self.outcome, self.methods = [], outcome, methods
        self.lines: asyncio.Queue = asyncio.Queue()
        self.stdin, self.stdout, self.returncode = self, self, None

    def write(self, data):
        msg = json.loads(data)
        self.sent.append(msg["method"])
        if msg["method"] == "initialize":
            self.lines.put_nowait(b"some log line\n")
            reply = {"id": 1, "result": {"authMethods": [{"id": m} for m in self.methods]}}
        else:
            reply = {"id": 2, **self.outcome}
        self.lines.put_nowait((json.dumps({"jsonrpc": "2.0", **reply}) + "\n").encode())

    async def drain(self):
        pass

    async def readline(self):
        return await self.lines.get()


@pytest.mark.parametrize("outcome, state, error", [
    ({"result": {}}, "checking", None),
    ({"error": {"code": -32800, "message": "Sign-in was cancelled."}}, "waiting", "closed before"),
])
def test_agys_sign_in_goes_over_acp(outcome, state, error):
    """initialize, then authenticate with Google's method; no code typed."""
    lg = accounts.Login("antigravity", "antigravity-x", None, None, "acp")
    lg.proc = _FakeAcp(outcome)
    asyncio.run(accounts._acp(lg))
    assert lg.proc.sent == ["initialize", "authenticate"] and lg.opened
    assert lg.state == state and (error in lg.error if error else lg.error is None)


def test_an_agy_without_the_google_method_is_said_plainly():
    lg = accounts.Login("antigravity", "antigravity-x", None, None, "acp")
    lg.proc = _FakeAcp({"result": {}}, methods=("gemini-api-key",))
    asyncio.run(accounts._acp(lg))
    assert lg.proc.sent == ["initialize"] and "update it" in lg.error


def test_each_provider_signs_in_through_a_browser_first():
    codex, agy = registry.PROVIDERS["codex"], registry.PROVIDERS["antigravity"]
    assert [w.id for w in codex.logins] == ["browser", "device"]
    assert codex.logins[0].ports == (1455, 1457) and not codex.logins[0].browser
    assert "--device-auth" not in codex.logins[0].argv
    assert [w.id for w in agy.logins] == ["acp"] and agy.logins[0].argv == ("agy", "acp")
    assert agy.logins[0].ports is None and agy.logins[0].browser


def test_account_routes(rig, monkeypatch):
    monkeypatch.setattr(accounts, "reading", lambda a, fetch=True: None)
    _acct("school", accounts.SCHOOL_TAGS, email="me@school.edu")
    _acct("mine")
    app = FastAPI()
    app.include_router(api.router)
    web = TestClient(app)
    page = web.get("/api/delegates/providers").json()
    assert page["tags"] == ["personal", "learning", "commercial"] and "image" in page["purposes"]
    codex = {r["id"]: r for r in page["providers"]}["codex"]
    # `login` stays the ways to sign in; a sign-in under way is `signing_in`.
    assert isinstance(codex["login"], list) and codex["login"] and codex["signing_in"] is None
    assert [a["id"] for a in codex["accounts"]] == ["school", "mine"]
    assert codex["accounts"][0]["school"] and codex["accounts"][0]["allowed_for"] == ["personal", "learning"]
    web.patch("/api/delegates/accounts/school", json={"allowed_for": ["commercial", "nonsense"],
                                                     "monthly_credits": 6500})
    assert store.accounts("codex")[0]["allowed_for"] == "commercial"
    assert web.patch("/api/delegates/accounts/school", json={"window_cap": 300}).status_code == 400
    assert web.post("/api/delegates/providers/codex/order", json={"ids": ["mine"]}).status_code == 400
    web.post("/api/delegates/providers/codex/order", json={"ids": ["mine", "school"]})
    assert [a["id"] for a in store.accounts("codex")] == ["mine", "school"]
    assert web.post("/api/delegates/providers/gemini/login", json={}).status_code == 404
    assert web.post("/api/delegates/providers/codex/login", json={"way": "carrier-pigeon"}).status_code == 409
    assert web.post("/api/delegates/providers/codex/login/reopen", json={}).status_code == 409
    assert [w["id"] for w in codex["sign_in_ways"]] == ["browser", "device"]
    web.delete("/api/delegates/accounts/mine")
    assert [a["id"] for a in store.accounts("codex")] == ["school"]
