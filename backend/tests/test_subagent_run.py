"""sessions.subagent_run: an Agent call's subagent, read off its own transcript
(#77). Background subagents never put their calls on the parent's stream; the
CLI files them under <session>/subagents/ with a meta naming the Agent call."""

import json

from app import sessions


def _line(role, *blocks):
    return json.dumps({"type": role, "message": {"role": role, "content": list(blocks)}})


def _store(tmp_path, monkeypatch, *, finished):
    monkeypatch.setattr(sessions, "CLAUDE_PROJECTS", tmp_path)
    proj = sessions.project_dir(None)
    sub = proj / "sess1" / "subagents"
    sub.mkdir(parents=True)
    (proj / "sess1.jsonl").write_text("")
    (sub / "agent-abc.meta.json").write_text(json.dumps({
        "agentType": "general-purpose", "description": "Capture screenshots",
        "toolUseId": "toolu_PARENT", "model": "haiku", "finished": finished}))
    # A second subagent of another call, which must not be picked up.
    (sub / "agent-zzz.meta.json").write_text(json.dumps({"toolUseId": "toolu_OTHER"}))
    (sub / "agent-zzz.jsonl").write_text(
        _line("assistant", {"type": "tool_use", "id": "o1", "name": "Read", "input": {}}) + "\n")
    (sub / "agent-abc.jsonl").write_text("\n".join([
        _line("user", {"type": "text", "text": "the prompt"}),
        _line("assistant", {"type": "text", "text": "Looking."},
              {"type": "tool_use", "id": "t1", "name": "Bash",
               "input": {"command": "ls site-kit", "description": "List the kit"}}),
        _line("user", {"type": "tool_result", "tool_use_id": "t1", "content": "ok"}),
        _line("assistant", {"type": "tool_use", "id": "t2", "name": "Read",
                            "input": {"file_path": "/x/y/notes.md"}}),
        _line("user", {"type": "tool_result", "tool_use_id": "t2", "is_error": True,
                       "content": "no such file"}),
        _line("assistant", {"type": "tool_use", "id": "t3", "name": "Write",
                            "input": {"file_path": "/x/out.md"}}),
        _line("assistant", {"type": "text", "text": "Captured 18 of 19."}),
        "not json",
    ]) + "\n")


def test_it_reads_the_calls_of_the_subagent_that_call_started(tmp_path, monkeypatch):
    _store(tmp_path, monkeypatch, finished=True)
    run = sessions.subagent_run("sess1", "toolu_PARENT")
    assert [(c["name"], c["state"]) for c in run["calls"]] == [
        ("Bash", "done"), ("Read", "error"), ("Write", "live")]
    assert run["calls"][0]["desc"] == "List the kit"
    assert run["total"] == 3 and run["finished"] is True
    assert run["report"] == "Captured 18 of 19." and run["said"] == ""
    assert run["model"] == "haiku" and run["description"] == "Capture screenshots"
    assert isinstance(run["idle_s"], int)


def test_while_running_the_last_words_are_progress_not_a_report(tmp_path, monkeypatch):
    _store(tmp_path, monkeypatch, finished=False)
    run = sessions.subagent_run("sess1", "toolu_PARENT")
    assert run["report"] == "" and run["said"] == "Captured 18 of 19."


def test_an_unknown_call_or_a_path_in_the_id_finds_nothing(tmp_path, monkeypatch):
    _store(tmp_path, monkeypatch, finished=True)
    assert sessions.subagent_run("sess1", "toolu_NOPE") is None
    assert sessions.subagent_run("../sess1", "toolu_PARENT") is None
    assert sessions.subagent_run("sess1", "../toolu_PARENT") is None
    assert sessions.subagent_run("missing", "toolu_PARENT") is None
