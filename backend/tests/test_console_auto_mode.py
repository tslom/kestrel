"""Auto mode: no cards, except the ones that matter — and a receipt after.

Tested through the two real paths: `can_use_tool` (card raised or not) and
`_stream_messages` (summary built from what the tools did). The summary is a
record, not a claim: a call never completed or returning an error is not in it.
"""

import asyncio
import json
import re

import pytest
from claude_agent_sdk import (AssistantMessage, ResultMessage, ToolResultBlock,
                              ToolUseBlock, UserMessage)

from app import console


class FakeWS:
    """Enough of starlette's WebSocket for console_ws to run against."""

    headers: dict[str, str] = {}

    def __init__(self):
        self.inbox: asyncio.Queue[str] = asyncio.Queue()
        self.sent: list[dict] = []

    async def accept(self):
        return None

    async def send_json(self, payload):
        self.sent.append(payload)

    async def receive_text(self):
        return await self.inbox.get()

    def feed(self, payload):
        self.inbox.put_nowait(json.dumps(payload))

    def of_type(self, want):
        return [m for m in self.sent if m.get("type") == want]


class FakeClient:
    def __init__(self, options=None):
        self.options = options

    async def connect(self):
        return None

    async def disconnect(self):
        return None

    async def interrupt(self):
        return None

    async def receive_messages(self):     # a session that never says anything
        await asyncio.Event().wait()
        yield


@pytest.fixture
async def auto_socket(monkeypatch):
    """A console socket started in auto mode, with its `can_use_tool`."""
    caught: dict = {}
    real_build = console.build_options

    def spy(session_id, model, mode, can_use_tool, ask_questions, *a, **k):
        caught["can_use_tool"] = can_use_tool
        caught["sdk_mode"] = mode
        return real_build(session_id, model, mode, can_use_tool,
                          ask_questions, *a, **k)

    monkeypatch.setattr(console, "build_options", spy)
    monkeypatch.setattr(console, "ClaudeSDKClient", FakeClient)

    ws = FakeWS()
    task = asyncio.create_task(console.console_ws(ws))
    ws.feed({"type": "start", "permission_mode": "auto"})
    for _ in range(200):
        await asyncio.sleep(0)
        if "can_use_tool" in caught:
            break
    assert "can_use_tool" in caught, "the socket never built its options"
    try:
        yield ws, caught
    finally:
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass


# ---------- the mode ----------


def test_auto_is_translated_for_an_sdk_that_has_never_heard_of_it():
    assert console.sdk_permission_mode("plan") == "plan"
    assert console.is_auto("auto") and not console.is_auto("default")


def test_auto_runs_under_the_mode_that_asks_us_about_everything():
    """Not `acceptEdits`: the CLI then settles in-workspace Bash writes itself and skips can_use_tool."""
    assert console.sdk_permission_mode("auto") == "default"


def test_never_ask_reaches_the_sdk_as_itself():
    assert console.sdk_permission_mode(console.BYPASS_MODE) == "bypassPermissions"
    assert not console.is_auto(console.BYPASS_MODE), \
        "the guard list belongs to auto; never-ask has none"


def test_never_ask_keeps_the_approval_callback_for_switching_out():
    """can_use_tool is registered under bypass anyway: the mode switches live, and a CLI without it denies every call after."""
    options = console.build_options(None, None, console.BYPASS_MODE,
                                    lambda *a: None, lambda *a: None)
    assert options.permission_mode == "bypassPermissions"
    assert getattr(options, "can_use_tool", None) is not None


def test_a_mode_that_runs_tools_unattended_says_so_on_the_start_line():
    assert "can't be undone still asks" in console.mode_note("auto")
    assert "nothing will ask" in console.mode_note(console.BYPASS_MODE)
    assert console.mode_note("plan") == ""
    assert console.mode_note("default") == ""


def test_a_retired_mode_string_still_starts_a_session():
    """The picker dropped acceptEdits; a pinned client or an old
    CONSOLE_PERMISSION_MODE shouldn't be an error, just an SDK mode."""
    assert console.sdk_permission_mode("acceptEdits") == "acceptEdits"


@pytest.mark.anyio
async def test_auto_keeps_can_use_tool_wired(auto_socket):
    """Not bypassPermissions: it drops the approval callback, the guard list and the ledger."""
    _, caught = auto_socket
    assert caught["sdk_mode"] != "bypassPermissions"
    options = console.build_options(None, None, "auto", caught["can_use_tool"],
                                    lambda *a: None)
    assert getattr(options, "can_use_tool", None) is not None


@pytest.mark.anyio
async def test_the_start_line_says_what_auto_still_asks_about(auto_socket):
    ws, _ = auto_socket
    for _ in range(200):
        await asyncio.sleep(0)
        if ws.of_type("status"):
            break
    line = ws.of_type("status")[-1]["text"]
    assert "mode=auto" in line
    assert "undone" in line, "the one line on screen has to name the exception"


# ---------- no cards, except ----------


@pytest.mark.anyio
async def test_an_ordinary_write_runs_without_a_card(auto_socket):
    ws, caught = auto_socket
    result = await caught["can_use_tool"](
        "mcp__vault__task_create", {"title": "pay tuition"}, None)
    assert type(result).__name__ == "PermissionResultAllow"
    assert ws.of_type("permission_request") == [], "auto mode asked anyway"


@pytest.mark.anyio
async def test_a_file_edit_runs_without_a_card(auto_socket):
    ws, caught = auto_socket
    result = await caught["can_use_tool"](
        "Write", {"file_path": "/tmp/whatever.md", "content": "x"}, None)
    assert type(result).__name__ == "PermissionResultAllow"
    assert ws.of_type("permission_request") == []


@pytest.mark.anyio
async def test_sending_mail_still_asks_and_says_why(auto_socket):
    ws, caught = auto_socket
    pending = asyncio.ensure_future(caught["can_use_tool"](
        "mcp__claude_ai_Gmail__send_message",
        {"to": "someone@example.com", "subject": "hi"}, None))

    deadline = asyncio.get_running_loop().time() + 2
    while asyncio.get_running_loop().time() < deadline:
        if ws.of_type("permission_request"):
            break
        await asyncio.sleep(0.01)
    cards = ws.of_type("permission_request")
    assert cards, "auto mode ran a send with nobody watching"
    assert "send mail" in cards[0]["guard"]

    ws.feed({"type": "permission", "request_id": cards[0]["request_id"],
             "allow": False})
    assert type(await asyncio.wait_for(pending, 2)).__name__ \
        == "PermissionResultDeny"


@pytest.mark.anyio
async def test_rm_still_asks(auto_socket):
    ws, caught = auto_socket
    pending = asyncio.ensure_future(caught["can_use_tool"](
        "Bash", {"command": "rm -rf 06-Generated"}, None))
    deadline = asyncio.get_running_loop().time() + 2
    while asyncio.get_running_loop().time() < deadline:
        if ws.of_type("permission_request"):
            break
        await asyncio.sleep(0.01)
    cards = ws.of_type("permission_request")
    assert cards and cards[0]["guard"]
    ws.feed({"type": "permission", "request_id": cards[0]["request_id"],
             "allow": False})
    await asyncio.wait_for(pending, 2)


@pytest.mark.anyio
async def test_a_safe_read_never_reaches_the_guard(auto_socket):
    _, caught = auto_socket
    result = await caught["can_use_tool"](
        "Bash", {"command": "curl -s http://localhost:8788/api/tasks"}, None)
    assert type(result).__name__ == "PermissionResultAllow"


# ---------- the summary ----------


class ScriptedClient:
    """Replays a fixed list of SDK messages as one turn."""

    def __init__(self, messages):
        self.messages = messages

    async def receive_response(self):
        for msg in self.messages:
            yield msg

    def __aiter__(self):        # _stream_messages reads the stream, not the client
        return self.receive_response()


def _result():
    return ResultMessage(subtype="success", duration_ms=10, duration_api_ms=8,
                         is_error=False, num_turns=1, session_id="s1")


def _used(tool_id, name, tool_input):
    return AssistantMessage(content=[ToolUseBlock(id=tool_id, name=name,
                                                  input=tool_input)],
                            model="sonnet")


def _returned(tool_id, is_error=False):
    return UserMessage(content=[ToolResultBlock(tool_use_id=tool_id,
                                                content="ok",
                                                is_error=is_error)])


async def _run(messages):
    ws = FakeWS()
    state = console.SessionState()
    await console._stream_messages(ScriptedClient(messages), ws, state, None)
    return ws


def _image(data="aGk=", media="image/png"):
    return {"type": "image", "source": {"type": "base64", "data": data, "media_type": media}}


def _sent_image(content, is_error=False):
    return console.result_image(ToolResultBlock(tool_use_id="t", content=content,
                                                 is_error=is_error))


def test_a_result_image_is_the_latest_picture_the_model_was_given():
    assert _sent_image([_image("Zmlyc3Q="), {"type": "text", "text": "x"}, _image("bGFzdA==")]
                       ) == "data:image/png;base64,bGFzdA=="


@pytest.mark.parametrize("content,is_error", [
    ("plain text", False),
    ([{"type": "text", "text": "x"}, "not a dict"], False),
    ([_image()], True),                                # a failed call showed nothing
    ([_image(media="image/svg+xml")], False),          # lands in an <img src>
    ([_image("x" * (console.RESULT_IMAGE_MAX_CHARS + 1))], False),
    ([{"type": "image", "source": {"type": "url", "url": "http://x"}}], False),
])
def test_what_is_not_a_result_image(content, is_error):
    assert _sent_image(content, is_error) is None


@pytest.mark.anyio
async def test_the_picture_rides_on_the_tool_result_frame():
    ws = await _run([
        _used("t1", "Read", {"file_path": "/v/shot.png"}),
        UserMessage(content=[ToolResultBlock(tool_use_id="t1", content=[_image()])]),
        _used("t2", "Read", {"file_path": "/v/a.md"}),
        _returned("t2"),
        _result(),
    ])
    frames = [m for m in ws.sent if m["type"] == "tool_result"]
    assert frames[0]["image"] == "data:image/png;base64,aGk="
    assert "image" not in frames[1], "a text result must not repeat the last picture"


@pytest.mark.anyio
async def test_a_turn_that_changed_things_reports_them():
    ws = await _run([
        _used("t1", "mcp__vault__task_create", {"title": "pay tuition"}),
        _returned("t1"),
        _used("t2", "Edit", {"file_path": "/v/notes/today.md"}),
        _returned("t2"),
        _result(),
    ])
    summaries = ws.of_type("changes")
    assert len(summaries) == 1
    assert [(i["verb"], i["scope"]) for i in summaries[0]["items"]] == [
        ("created", "task"), ("edited", "file")]
    assert summaries[0]["items"][0]["detail"] == "pay tuition"


@pytest.mark.anyio
async def test_the_summary_arrives_before_the_turn_closes():
    """It belongs to the turn that made it, not to whatever is asked next."""
    ws = await _run([
        _used("t1", "mcp__vault__task_create", {"title": "x"}),
        _returned("t1"),
        _result(),
    ])
    kinds = [m["type"] for m in ws.sent if m["type"] in ("changes", "result")]
    assert kinds == ["changes", "result"]


@pytest.mark.anyio
async def test_a_turn_that_only_read_reports_nothing():
    ws = await _run([
        _used("t1", "Read", {"file_path": "/v/notes/today.md"}),
        _returned("t1"),
        _used("t2", "Bash", {"command": "git status"}),
        _returned("t2"),
        _result(),
    ])
    assert ws.of_type("changes") == [], \
        "a question that changed nothing gets no receipt"


@pytest.mark.anyio
async def test_a_failed_call_is_not_a_change():
    """A denied card and a tool that threw are error results; neither happened."""
    ws = await _run([
        _used("t1", "mcp__vault__task_create", {"title": "x"}),
        _returned("t1", is_error=True),
        _result(),
    ])
    assert ws.of_type("changes") == []


@pytest.mark.anyio
async def test_a_call_with_no_result_is_not_a_change():
    """An interrupted turn's unanswered last call may not have run: the summary omits it."""
    ws = await _run([
        _used("t1", "mcp__vault__task_create", {"title": "x"}),
        _result(),
    ])
    assert ws.of_type("changes") == []


@pytest.mark.anyio
async def test_the_ledger_does_not_carry_into_the_next_turn():
    ws = FakeWS()
    state = console.SessionState()
    first = [_used("t1", "mcp__vault__task_create", {"title": "x"}),
             _returned("t1"), _result()]
    await console._stream_messages(ScriptedClient(first), ws, state, None)
    ws.sent.clear()
    await console._stream_messages(
        ScriptedClient([_used("t2", "Read", {"file_path": "/v/a.md"}),
                        _returned("t2"), _result()]), ws, state, None)
    assert ws.of_type("changes") == [], "last turn's changes came back"


@pytest.mark.anyio
async def test_a_prompt_suggestion_reaches_the_page_and_nothing_else_is_lost():
    """The SDK's parser drops this message type, so it's caught ahead of it."""
    class Query:
        async def receive_messages(self):
            yield {"type": "prompt_suggestion", "suggestion": "run the tests"}
            yield {"type": "a_type_nobody_knows"}
            yield {"type": "assistant", "message": {
                "role": "assistant", "model": "sonnet",
                "content": [{"type": "text", "text": "still here"}]}}

    class Client:
        _query = Query()

    ws = FakeWS()
    got = [m async for m in console._with_suggestions(Client(), ws)]
    assert {"type": "suggestion", "text": "run the tests"} in ws.sent
    assert [type(m).__name__ for m in got] == ["AssistantMessage"]


@pytest.mark.anyio
async def test_a_client_without_the_private_query_still_streams():
    class Client:
        async def receive_messages(self):
            yield "a"
            yield "b"

    assert [m async for m in console._with_suggestions(Client(), FakeWS())] == ["a", "b"]


# ---------- what a change tells the cards ----------


def _refreshes(ws):
    return [m["topics"] for m in ws.sent if m["type"] == "refresh"]


@pytest.mark.anyio
async def test_a_clean_write_refreshes_its_card_then_the_turn_again():
    ws = await _run([
        _used("t1", "mcp__vault__task_update", {"id": 1}),
        _returned("t1"),
        _used("t2", "mcp__vault__list_add", {"name": "x"}),
        _returned("t2"),
        _result(),
    ])
    assert _refreshes(ws) == [["tasks"], ["lists"], ["lists", "tasks"]]
    order = [m["type"] for m in ws.sent]
    assert order.index("tool_result") < order.index("refresh"), \
        "a card can't reload before the call it's reloading for came back"
    assert order[-2:] == ["refresh", "result"]


@pytest.mark.anyio
async def test_a_failed_or_unfinished_write_refreshes_nothing():
    ws = await _run([
        _used("t1", "mcp__vault__task_update", {"id": 1}),
        _returned("t1", is_error=True),
        _used("t2", "mcp__vault__habit_checkin", {"name": "x"}),   # never comes back
        _result(),
    ])
    assert _refreshes(ws) == []


@pytest.mark.anyio
async def test_a_read_refreshes_nothing():
    ws = await _run([
        _used("t1", "mcp__vault__db_query", {"sql": "SELECT 1"}),
        _returned("t1"),
        _result(),
    ])
    assert _refreshes(ws) == []


@pytest.mark.anyio
async def test_a_subagents_write_refreshes_too():
    use = _used("t1", "mcp__vault__task_create", {"title": "x"})
    use.parent_tool_use_id = "agent1"
    done = _returned("t1")
    done.parent_tool_use_id = "agent1"
    ws = await _run([use, done, _result()])
    assert _refreshes(ws) == [["tasks"], ["tasks"]]


@pytest.mark.anyio
async def test_a_dev_tabs_scripts_leave_the_desk_alone():
    ws = FakeWS()
    state = console.SessionState()
    state.profile = "dev"
    await console._stream_messages(ScriptedClient([
        _used("t1", "Bash", {"command": "npm run build"}),
        _returned("t1"),
        _result(),
    ]), ws, state, None)
    assert _refreshes(ws) == []


def test_only_what_moves_mail_re_pulls_the_inbox():
    move = re.compile(console.EMAIL_MOVE_TOOLS)
    for tool in ("trash_thread", "untrash_message", "mark_thread_spam", "unmark_message_spam",
                 "label_thread", "unlabel_message", "update_message_labels"):
        assert move.fullmatch(f"mcp__claude_ai_Gmail__{tool}"), tool
    for tool in ("send_message", "reply", "create_draft", "search_threads", "create_label"):
        assert not move.fullmatch(f"mcp__claude_ai_Gmail__{tool}"), tool


@pytest.mark.anyio
async def test_the_inbox_re_pull_never_breaks_the_turn(monkeypatch):
    from app.providers import email

    def boom():
        raise RuntimeError("gmail is down")

    monkeypatch.setattr(email, "refresh", boom)
    assert await console.refresh_email_cache({}, None, None) == {}
