"""Computer-use jobs: what the transcript reader reports, and the socket's door."""
import asyncio
import json
import os
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app import console, console_terminal as term


def _line(kind, content, **extra):
    message = {"role": kind, "content": content, **extra.pop("message", {})}
    return json.dumps({"type": kind, "message": message, **extra})


IMAGE = {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": "aGk="}}
CALL = {"type": "tool_use", "id": "t1", "name": "mcp__computer-use__screenshot", "input": {}}


def _feed(*lines):
    events = term.TranscriptEvents()
    return [f for line in lines for f in events.feed(line)]


def test_a_turn_reads_as_the_frames_console_would_send():
    frames = _feed(
        _line("user", "open calculator"),
        _line("assistant", [CALL], message={"stop_reason": "tool_use"}),
        _line("user", [{"type": "tool_result", "tool_use_id": "t1", "content": [IMAGE]}]),
        _line("assistant", [{"type": "text", "text": "done"}], message={"stop_reason": "end_turn"}),
    )
    assert [f["type"] for f in frames] == ["turn_start", "tool_use", "tool_result", "turn_end"]
    assert frames[1]["name"] == "mcp__computer-use__screenshot"
    assert frames[2]["image"] == "data:image/jpeg;base64,aGk="


def test_streaming_duplicates_report_once():
    use = _line("assistant", [CALL], message={"stop_reason": None})
    done = _line("user", [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}])
    assert [f["type"] for f in _feed(use, use, done, done)] == ["turn_start", "tool_use", "tool_result"]


@pytest.mark.parametrize("line", [
    "not json",
    _line("user", "<command-name>/mcp</command-name>"),
    _line("user", "hidden", isMeta=True),
    _line("user", "a subagent's prompt", isSidechain=True),
    json.dumps({"type": "summary"}),
])
def test_what_is_not_a_turn(line):
    assert _feed(line) == []


def test_an_interrupt_ends_the_turn():
    frames = _feed(_line("user", "go"),
                   _line("user", [{"type": "text", "text": "[Request interrupted by user]"}]))
    assert [f["type"] for f in frames] == ["turn_start", "turn_end"]


def test_a_job_is_given_its_task_and_never_touches_the_global_model():
    sid = "12345678-1234-1234-1234-123456789abc"
    argv = term.command(sid, "-open calculator")
    assert argv[1:] == ["--session-id", sid, "--model", term.COMPUTER_MODEL, "--", "-open calculator"]
    assert "--" not in term.command(sid, None), "setup is a bare claude, for /mcp"


def test_a_thinking_block_marked_end_turn_is_not_the_end():
    """Real order: the final message's thinking arrives first with end_turn on it, the answer ~2s later."""
    events = term.TranscriptEvents()
    events.feed(_line("user", "go"))
    events.feed(_line("assistant", [{"type": "text", "text": "Clicking the 7 button."}],
                      message={"stop_reason": "tool_use"}))
    thinking = events.feed(_line("assistant", [{"type": "thinking", "thinking": "hm"}],
                                 message={"stop_reason": "end_turn"}))
    assert thinking == []
    done = events.feed(_line("assistant", [{"type": "text", "text": "The display shows 7."}],
                             message={"stop_reason": "end_turn"}))
    assert done[-1]["type"] == "turn_end" and events.last_text == "The display shows 7."


def test_turn_duration_ends_a_turn_with_nothing_to_say():
    events = term.TranscriptEvents()
    events.feed(_line("user", "go"))
    assert events.feed(json.dumps({"type": "system", "subtype": "turn_duration"})) == [
        {"type": "turn_end", "error": False}]


def test_the_last_thing_said_is_the_answer():
    events = term.TranscriptEvents()
    events.feed(_line("user", "go"))
    events.feed(_line("assistant", [{"type": "text", "text": "working"}], message={"stop_reason": "tool_use"}))
    frames = events.feed(_line("assistant", [{"type": "text", "text": "the display shows 7"}],
                               message={"stop_reason": "end_turn"}))
    assert frames[-1]["type"] == "turn_end" and events.last_text == "the display shows 7"


def test_enabled_reads_the_folder_mcp_wrote_and_tolerates_anything(tmp_path, monkeypatch):
    monkeypatch.setattr(term.Path, "home", lambda: tmp_path)
    assert term.computer_enabled() is False                 # no file at all
    config = tmp_path / ".claude.json"
    config.write_text("not json")
    assert term.computer_enabled() is False
    key = str(term.COMPUTER_DIR.resolve())
    config.write_text(json.dumps({"projects": {key: {"enabledMcpServers": ["computer-use"]}}}))
    assert term.computer_enabled() is True


def test_a_second_job_is_refused_while_one_runs(monkeypatch):
    monkeypatch.setattr(term, "_job", object())
    ok, said = asyncio.run(term.run_computer("x", None, None))
    assert not ok and "one can at a time" in said


def test_the_pty_is_a_real_terminal_with_a_clean_env(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_CHILD_SESSION", "1")
    proc, master = term.spawn(
        ["/bin/sh", "-c", 'test -t 0 && echo TTY; stty size; echo "[${CLAUDE_CODE_CHILD_SESSION}]"'],
        tmp_path, 30, 90)
    out = b""
    try:
        while True:
            data = os.read(master, 4096)
            if not data:
                break
            out += data
    except OSError:                         # EIO: the child left
        pass
    proc.wait(5)
    os.close(master)
    assert b"TTY" in out and b"30 90" in out
    assert b"[]" in out, "the inherited marker turns transcript saving off"


@pytest.fixture
def api():
    app = FastAPI()
    app.include_router(term.router)
    app.include_router(console.router)
    return TestClient(app)


@pytest.mark.parametrize("path", ["/ws/console/computer?id=x", "/ws/console"])
def test_a_web_page_can_not_open_these_sockets(api, path):
    """CORS doesn't cover WebSockets. One of these runs a terminal."""
    with pytest.raises(WebSocketDisconnect):
        with api.websocket_connect(path, headers={"origin": "https://evil.test"}):
            pass


def test_the_terminal_socket_attaches_to_a_job_and_can_not_start_one(api):
    with pytest.raises(WebSocketDisconnect):
        with api.websocket_connect("/ws/console/computer?id=nothing-running"):
            pass


@pytest.mark.parametrize("origin", ["http://localhost:5173", "http://127.0.0.1:8788", None])
def test_the_app_and_the_dev_server_are_let_in(origin):
    class Sock:
        headers = {"origin": origin} if origin else {}
    assert console.origin_ok(Sock())


def test_a_terminal_mid_turn_holds_a_restart_until_it_goes_quiet(monkeypatch):
    monkeypatch.setattr(console, "_live", set())
    monkeypatch.setattr(console, "terminal_active", {"a": time.monotonic()})
    assert console.get_busy() == {"turns": 1}
    monkeypatch.setattr(console, "terminal_active",
                        {"a": time.monotonic() - console.TERMINAL_QUIET_S - 1})
    assert console.get_busy() == {"turns": 0}
