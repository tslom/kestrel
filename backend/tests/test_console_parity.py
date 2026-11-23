"""Claude Code parity in the console: the task list, effort, rewind, MCP.

The task list is read off the CLI's own files, so what's pinned is the file
shapes it tolerates; the rest is the session each start message builds.
"""

import json

import pytest

from app import console, console_tasks


@pytest.fixture
def tasks_dir(tmp_path):
    sid = "292aff17-b903-4709-a236-cbbec673b839"
    d = tmp_path / sid
    d.mkdir()

    def write(n, **t):
        (d / f"{n}.json").write_text(json.dumps({"id": str(n), **t}))
    return sid, tmp_path, write, d


def test_tasks_in_creation_order_numerically(tasks_dir):
    sid, root, write, _ = tasks_dir
    for n in (10, 2, 1):
        write(n, subject=f"t{n}", status="pending")
    write(2, subject="t2", status="in_progress", activeForm="Doing t2", blockedBy=["1"])
    got = console_tasks.read_tasks(sid, root)
    assert [t["id"] for t in got] == ["1", "2", "10"]
    assert got[1] == {"id": "2", "subject": "t2", "active_form": "Doing t2",
                      "status": "in_progress", "blocked_by": ["1"]}


def test_deleted_and_half_written_tasks_are_skipped(tasks_dir):
    sid, root, write, d = tasks_dir
    write(1, subject="keep", status="completed")
    write(2, subject="gone", status="deleted")
    (d / "3.json").write_text('{"id": "3", "subj')
    (d / ".lock").write_text("")
    assert [t["subject"] for t in console_tasks.read_tasks(sid, root)] == ["keep"]


def test_no_session_or_a_path_is_no_tasks(tasks_dir):
    _, root, _, _ = tasks_dir
    assert console_tasks.read_tasks(None, root) == []
    assert console_tasks.read_tasks("../../etc", root) == []
    assert console_tasks.read_tasks("aaaaaaaa-0000", root) == []      # no such dir


def _opts(**kw):
    return console.build_options(None, None, "default", None, None, **kw)


def test_dev_checkpoints_files_and_lets_tasks_through(tmp_path):
    o = _opts(profile="dev", cwd=tmp_path)
    assert o.enable_file_checkpointing
    assert "replay-user-messages" in o.extra_args
    assert set(console.TASK_TOOLS) <= set(o.tools) and set(console.TASK_TOOLS) <= set(o.allowed_tools)
    assert "TodoWrite" not in o.tools                      # gone from the CLI
    general = _opts()
    assert not general.enable_file_checkpointing and "TaskCreate" not in general.tools


def test_effort_is_passed_only_when_it_is_a_level(tmp_path):
    assert _opts(effort="high").effort == "high"
    assert _opts(profile="dev", cwd=tmp_path, effort="max").effort == "max"
    assert _opts(effort="ludicrous").effort is None
    assert _opts().effort is None


def test_a_rewind_is_told_to_the_next_message_once():
    state = console.SessionState()
    state.day = console.day_key()        # no rollover note in the way
    state.rewound = True
    first = console.build_prompt({"text": "go on"}, state)
    assert "<rewind>" in first and first.endswith("go on")
    assert "<rewind>" not in console.build_prompt({"text": "again"}, state)


def test_every_mode_replays_messages_and_resume_at_forks(tmp_path):
    for o in (_opts(), _opts(profile="study"), _opts(profile="dev", cwd=tmp_path)):
        assert "replay-user-messages" in o.extra_args
        assert "resume-session-at" not in o.extra_args and not o.fork_session
    o = console.build_options("sess-1", None, "default", None, None, resume_at="rec-9")
    assert o.resume == "sess-1" and o.fork_session
    assert o.extra_args["resume-session-at"] == "rec-9"
    # resume_at means nothing without a session to cut
    assert "resume-session-at" not in _opts(resume_at="rec-9").extra_args


def test_output_style_rides_settings_and_only_a_name_does():
    assert json.loads(_opts(output_style="Explanatory").settings) == {"outputStyle": "Explanatory"}
    assert _opts(output_style='x", "permissions": {"allow": ["Bash"]}').settings is None
    assert _opts().settings is None


def _transcript(tmp_path, monkeypatch, sid, records):
    from app import sessions
    monkeypatch.setattr(sessions, "project_dir", lambda cwd=None: tmp_path)
    (tmp_path / f"{sid}.jsonl").write_text("\n".join(json.dumps(r) for r in records) + "\n")
    return sessions


def test_rewind_point_is_the_message_before(tmp_path, monkeypatch):
    sessions = _transcript(tmp_path, monkeypatch, "s1", [
        {"type": "user", "uuid": "u1", "parentUuid": None, "message": {"role": "user", "content": "one"}},
        {"type": "assistant", "uuid": "a1", "parentUuid": "u1", "message": {"role": "assistant", "content": "ok"}},
        {"type": "attachment", "uuid": "x1", "parentUuid": "a1"},
        {"type": "user", "uuid": "u2", "parentUuid": "x1", "message": {"role": "user", "content": "two"}},
    ])
    assert sessions.rewind_point("s1", "u2") == {"found": True, "at": "x1"}
    assert sessions.rewind_point("s1", "u1") == {"found": True, "at": None}   # first: a new session
    assert sessions.rewind_point("s1", "nope")["found"] is False
    assert sessions.rewind_point("../s1", "u2")["found"] is False
    assert sessions.rewind_point("missing", "u2")["found"] is False
