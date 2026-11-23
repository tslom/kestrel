"""Delegates: the registry's availability report, a fan-out batch, the tools
and the routes. No real CLI runs here: outside providers are stand-ins."""

import asyncio
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import agent, console_tools, sessions
from app.delegates import api, boundary, registry, runs, store


@pytest.fixture
def rig(dbfile, tmp_path, monkeypatch):
    """gemini 'installed' and signed in, codex absent; Claude answers from a
    stub; outside launches are recorded instead of run."""
    monkeypatch.setattr(runs, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(boundary, "DATA_DIR", tmp_path / "data")
    registry.forget()

    def probe(p):
        if p.id == "codex":
            return {"installed": False, "path": None, "version": None,
                    "signed_in": False, "auth_note": ""}
        return {"installed": True, "path": f"/x/{p.binary}", "version": "1.0",
                "signed_in": True, "auth_note": "ok"}
    monkeypatch.setattr(registry, "_probe", probe)

    seen = {"briefs": [], "claude": []}

    async def launch(argv_for, brief, scratch, **kw):
        assert isinstance(brief, boundary.Brief)
        seen["briefs"].append(brief.text)
        argv = argv_for(brief.text)
        if "fail" in brief.text:
            return boundary.Finished(1, "", "quota exceeded for today")
        return boundary.Finished(0, json.dumps({
            "response": f"```html\n<h1>from {argv[0]}</h1>\n```",
            "stats": {"models": {"gemini-3-pro": {"tokens": {"prompt": 120, "candidates": 40}}}}}), "")
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
    assert rows["gemini"]["ready"] and rows["gemini"]["needs"] is None
    assert not rows["codex"]["ready"]
    assert "npm install -g @openai/codex" in rows["codex"]["needs"]
    store.set_enabled("gemini", False)
    rows = {r["id"]: r for r in registry.status()}
    assert not rows["gemini"]["ready"] and "switched off" in rows["gemini"]["needs"]


def test_signed_out_names_the_login(rig, monkeypatch):
    monkeypatch.setattr(registry, "_probe", lambda p: {
        "installed": True, "path": "/x", "version": "1", "signed_in": p.id == "claude",
        "auth_note": ""})
    rows = {r["id"]: r for r in registry.status(fresh=True)}
    assert "codex login" in rows["codex"]["needs"]


def test_mix_takes_turns_claude_first():
    assert runs.plan_mix(["gemini", "claude"], 5, None) == [
        "claude", "gemini", "claude", "gemini", "claude"]
    assert runs.plan_mix(["claude"], None, None) == ["claude"] * 3
    assert runs.plan_mix(["claude"], 2, ["Gemini", "claude"]) == ["gemini", "claude"]


def test_fan_out_runs_the_mix_and_logs_it(rig):
    out = asyncio.run(runs.fan_out(
        "Hero section for Acme Dental; reach me at me@example.com", n=4,
        angles=["editorial serif"], artifact="hero.html", session_id="s1"))
    vs = out["variants"]
    assert [v["provider"] for v in vs] == ["claude", "gemini", "claude", "gemini"]
    assert all(v["status"] == "ok" for v in vs)
    # The fence is stripped, each variant has its own file, and everyone got
    # the scrubbed brief, Claude included.
    assert vs[1]["text"] == "<h1>from gemini</h1>"
    assert (runs.variant_dir(out["batch"], 2) / "hero.html").read_text() == "<h1>from gemini</h1>"
    assert all("me@example.com" not in b for b in rig["briefs"])
    assert all("[email]" in p and "me@example.com" not in p for _, p in rig["claude"])
    assert sum("editorial serif" in p for _, p in rig["claude"]) == 1
    assert any("ChatGPT" in n for n in out["not_ready"])
    rows = store.batch_runs(out["batch"])
    assert [(r["variant"], r["provider"], r["status"], r["file"]) for r in rows] == [
        (1, "claude", "ok", "hero.html"), (2, "gemini", "ok", "hero.html"),
        (3, "claude", "ok", "hero.html"), (4, "gemini", "ok", "hero.html")]
    assert rows[1]["tokens_in"] == 120 and rows[1]["model"] == "gemini-3-pro"
    assert rows[1]["redactions"] == "1 email" and rows[1]["session_id"] == "s1"
    used = {u["provider"]: u for u in store.usage()}
    assert used["gemini"]["runs"] == 2 and used["gemini"]["tokens_out"] == 80


def test_fan_out_refuses_a_provider_that_isnt_ready(rig):
    with pytest.raises(runs.DelegateError, match="npm install -g @openai/codex"):
        asyncio.run(runs.fan_out("x", mix=["claude", "codex"]))
    with pytest.raises(runs.DelegateError, match="most"):
        asyncio.run(runs.fan_out("x", n=9))


def test_a_failed_variant_is_reported_not_dropped(rig):
    out = asyncio.run(runs.fan_out("please fail", mix=["gemini", "claude"]))
    bad, good = out["variants"]
    assert bad["status"] == "failed" and "quota" in bad["error"] and good["status"] == "ok"
    assert store.batch_runs(out["batch"])[0]["status"] == "failed"


def test_delegate_is_for_outside_providers_only(rig):
    out = asyncio.run(runs.delegate("gemini", "Summarise RFC 9110 in ten bullets"))
    assert out["status"] == "ok" and out["text"] == "<h1>from gemini</h1>"
    with pytest.raises(runs.DelegateError, match="this one is you"):
        asyncio.run(runs.delegate("claude", "x"))
    with pytest.raises(runs.DelegateError, match="isn't ready"):
        asyncio.run(runs.delegate("codex", "x"))


def test_an_agent_that_writes_the_file_itself_wins(rig, monkeypatch):
    async def launch(argv_for, brief, scratch, **kw):
        (scratch / "hero.html").write_text("<p>written</p>")
        (scratch / "extra.css").write_text("p{}")
        return boundary.Finished(0, json.dumps({"response": "Done, see hero.html"}), "")
    monkeypatch.setattr(boundary, "launch", launch)
    out = asyncio.run(runs.delegate("gemini", "make it", artifact="hero.html"))
    assert out["text"] == "<p>written</p>" and out["files"] == ["extra.css"]


def test_artifact_is_a_plain_file_name():
    assert runs.artifact_name(None) == "result.md"
    for bad in ("../x", "a/b.html", ".env", "x y"):
        with pytest.raises(runs.DelegateError):
            runs.artifact_name(bad)


def test_parsers_fall_back_to_the_raw_text(tmp_path):
    assert registry.PROVIDERS["gemini"].parse("plain words", tmp_path).text == "plain words"
    (tmp_path / registry.CODEX_REPLY).write_text("the answer\n")
    got = registry.PROVIDERS["codex"].parse(
        '{"type":"turn.completed","usage":{"input_tokens":9,"output_tokens":4}}\nnoise', tmp_path)
    assert (got.text, got.tokens_in, got.tokens_out) == ("the answer", 9, 4)
    assert not (tmp_path / registry.CODEX_REPLY).exists()


def _call(tool, args):
    out = asyncio.run(tool.handler(args))
    return out["content"][0]["text"], bool(out.get("is_error"))


def test_the_tools_end_to_end(rig):
    text, err = _call(console_tools.fan_out, {"task": "Hero for a dentist", "n": 2,
                                              "artifact": "hero.html"})
    assert not err and "2 of 2 variants" in text and "shortlist" in text
    batch = text.split()[1].rstrip(":")
    said, err = _call(console_tools.shortlist, {
        "batch": batch, "picks": [2], "reasons": ["cleaner"]})
    assert not err and "2 (gemini)" in said
    said, err = _call(console_tools.shortlist, {"batch": batch, "picks": [7]})
    assert err and "isn't one" in said
    said, err = _call(console_tools.delegate, {"provider": "codex", "brief": "x"})
    assert err and "npm install -g @openai/codex" in said
    said, err = _call(console_tools.delegate, {
        "provider": "gemini", "brief": "Draft alt text; ask sam@example.com"})
    assert not err and "1 email" in said and "<h1>from gemini</h1>" in said


def test_routes(rig):
    app = FastAPI()
    app.include_router(api.router)
    web = TestClient(app)
    rows = {r["id"]: r for r in web.get("/api/delegates/providers").json()["providers"]}
    assert rows["gemini"]["ready"] and "path" not in rows["gemini"]
    assert web.post("/api/delegates/providers/claude", json={"enabled": False}).status_code == 400
    off = web.post("/api/delegates/providers/gemini", json={"enabled": False}).json()
    assert not {r["id"]: r for r in off["providers"]}["gemini"]["ready"]
    web.post("/api/delegates/providers/gemini", json={"enabled": True})

    out = asyncio.run(runs.fan_out("Hero", n=2, artifact="hero.html"))
    got = web.get(f"/api/delegates/batches/{out['batch']}").json()
    assert [v["provider"] for v in got["variants"]] == ["claude", "gemini"]
    page = web.get(f"/api/delegates/batches/{out['batch']}/2/file")
    assert page.text == "<h1>from gemini</h1>"
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
