"""What a failed console turn writes into the failure log.

The log answers two questions — how often does a turn fail, and on what — and
the first four rows it ever collected answered neither. Three were
`error_during_execution` with a NULL detail, which is the question restated.
The fourth was a session limit filed under the label **success**, because the
CLI reports a quota stop with is_error set and subtype "success", and the
subtype was being recorded verbatim.

So: a label that describes what happened, and a detail that is never empty
while the turn knows anything at all.
"""

import asyncio
from types import SimpleNamespace

from claude_agent_sdk import (AssistantMessage, ResultMessage, ToolResultBlock,
                              ToolUseBlock, UserMessage)

from app import console
from app.console import _fault_key, _result_text, _turn_fault


def _result(subtype="error_during_execution", text=""):
    return SimpleNamespace(subtype=subtype, result=text, is_error=True)



def test_the_subtype_is_the_label_when_it_describes_a_failure():
    label, _ = _turn_fault(_result(text="boom"), None, None)
    assert label == "error_during_execution"


def test_a_quota_stop_is_not_filed_under_success():
    """The row that started this: `success` as the name of a failure."""
    msg = _result(subtype="success",
                  text="You've hit your session limit · resets 5pm (America/Los_Angeles)")
    label, detail = _turn_fault(msg, None, None)
    assert label == "limit"
    assert "session limit" in detail


def test_the_other_limits_read_the_same_way():
    for text in ("You've hit your usage limit · resets 9am",
                 "rate limit reached, try again later",
                 "Quota limit exceeded for this organization"):
        label, _ = _turn_fault(_result(subtype="success", text=text), None, None)
        assert label == "limit", text


def test_the_windows_the_cli_names_now_are_limits_too():
    """Word for word from the log: after the CLI renamed its windows, every
    quota stop went back under `success`."""
    for text in ("You've hit your 5-hour limit · resets 6pm (America/Los_Angeles)",
                 "You've hit your weekly limit · resets 8pm (America/Los_Angeles)"):
        label, _ = _turn_fault(_result(subtype="success", text=text), None, None)
        assert label == "limit", text


def test_a_lost_login_has_its_own_name():
    msg = _result(subtype="success",
                  text="Failed to authenticate: OAuth session expired and "
                       "could not be refreshed")
    label, _ = _turn_fault(msg, None, None)
    assert label == "auth"


def test_success_never_names_a_failure():
    """Wording nobody has seen yet still can't land under `success`."""
    label, _ = _turn_fault(_result(subtype="success", text="Something new"),
                           None, None)
    assert label == "error"


def test_one_limit_stop_is_one_row_whatever_time_it_resets():
    a = _fault_key("limit", "You've hit your 5-hour limit · resets 6pm (America/Los_Angeles)")
    b = _fault_key("limit", "You've hit your 5-hour limit · resets 3:30am (America/Los_Angeles)")
    weekly = _fault_key("limit", "You've hit your weekly limit · resets 8pm (America/Los_Angeles)")
    assert a == b != weekly
    assert _fault_key("error_max_turns", "a · b") is None


def test_an_ordinary_failure_mentioning_a_limit_it_isnt():
    """`_LIMIT_RE` wants the phrase, not the word — a turn that failed while
    talking about a rate limit is still a failure of its own kind."""
    msg = _result(text="KeyError: 'rate_limit_type' while parsing the response")
    label, _ = _turn_fault(msg, None, None)
    assert label == "error_during_execution"



def test_result_text_is_kept_verbatim_when_there_is_some():
    _, detail = _turn_fault(_result(text="tool loop exceeded"), "Bash", None)
    assert detail == "tool loop exceeded"


def test_an_empty_result_falls_back_to_the_failing_tool():
    """The three NULL rows. What the turn was running and what it came back
    with is the answer to "on what" — it just wasn't being read."""
    _, detail = _turn_fault(_result(), "Bash", "sqlite3: database is locked")
    assert "Bash" in detail
    assert "database is locked" in detail


def test_a_tool_that_did_not_error_still_names_itself():
    _, detail = _turn_fault(_result(), "mcp__vault__note_write", None)
    assert "mcp__vault__note_write" in detail


def test_a_turn_that_never_called_anything_says_so():
    """Distinct from the above on purpose: "no tool calls" is a different
    failure from "the last tool blew up", and a shared detail would merge
    them into one fingerprint."""
    _, detail = _turn_fault(_result(), None, None)
    assert detail == "no result text, no tool calls"


def test_the_detail_is_never_empty():
    """The property the whole file is defending — every path, some signal."""
    for tool, err in ((None, None), ("Bash", None), ("Bash", "nope"),
                      (None, "orphaned error")):
        _, detail = _turn_fault(_result(), tool, err)
        assert detail.strip()



def test_result_text_handles_both_content_shapes():
    """Tool results arrive as a bare string or as content blocks depending on
    the tool; a fallback that only understood one would be empty for the other
    — which is the bug it exists to fix, one level down."""
    assert _result_text(SimpleNamespace(content="plain text")) == "plain text"
    assert _result_text(SimpleNamespace(
        content=[{"type": "text", "text": "line one"},
                 {"type": "text", "text": "line two"}])) == "line one line two"
    assert _result_text(SimpleNamespace(content=None)) == ""
    assert _result_text(SimpleNamespace(content=[{"type": "image"}])) == ""


#
# `_turn_fault` above is a pure function and the tests for it are cheap, which
# is exactly the condition under which the interesting bug lives one layer
# out: the fallback is worth nothing if `last_tool` is not actually carrying
# the turn's last tool by the time the result arrives. So this drives the real
# stream loop with a real message sequence and reads what landed in the log.


def _messages():
    return [
        AssistantMessage(model="m", content=[
            ToolUseBlock(id="t1", name="Bash", input={"command": "ls"})]),
        UserMessage(content=[
            ToolResultBlock(tool_use_id="t1", content="ok", is_error=False)]),
        AssistantMessage(model="m", content=[
            ToolUseBlock(id="t2", name="mcp__vault__task_create",
                         input={"title": "x"})]),
        UserMessage(content=[
            ToolResultBlock(tool_use_id="t2", is_error=True,
                            content=[{"type": "text",
                                      "text": "database is locked"}])]),
        ResultMessage(subtype="error_during_execution", duration_ms=1,
                      duration_api_ms=1, is_error=True, num_turns=1,
                      session_id="s", result=None),
    ]


def test_the_stream_records_the_tool_the_turn_died_on(monkeypatch):
    recorded = []
    monkeypatch.setattr(console.failures, "record",
                        lambda *a, **kw: recorded.append((a, kw)))

    class Ws:
        async def send_json(self, _payload):
            pass

    class Client:
        async def receive_response(self):
            for m in _messages():
                yield m

    asyncio.run(console._stream_messages(
        Client().receive_response(), Ws(), console.SessionState(), None))

    assert len(recorded) == 1
    (origin, kind, label), kw = recorded[0]
    assert (origin, kind, label) == ("console", "turn", "error_during_execution")
    # The whole point: the second tool, not the first, and its error text.
    assert "mcp__vault__task_create" in kw["detail"]
    assert "database is locked" in kw["detail"]


def test_a_clean_turn_records_nothing(monkeypatch):
    """The guard that keeps the log worth reading — `is_error` false writes no
    row, however many tools the turn called."""
    recorded = []
    monkeypatch.setattr(console.failures, "record",
                        lambda *a, **kw: recorded.append(a))

    msgs = _messages()[:-1] + [
        ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1,
                      is_error=False, num_turns=1, session_id="s", result="done")]

    class Ws:
        async def send_json(self, _payload):
            pass

    class Client:
        async def receive_response(self):
            for m in msgs:
                yield m

    asyncio.run(console._stream_messages(
        Client().receive_response(), Ws(), console.SessionState(), None))
    assert recorded == []


def test_a_reader_that_dies_closes_the_turn_instead_of_hanging_it(monkeypatch):
    """Nothing awaits the stream task. An SDK reader dying mid-turn (a tool
    result over its JSON buffer) used to end as an asyncio "never retrieved"
    line and a UI spinning forever."""
    recorded, sent = [], []
    monkeypatch.setattr(console.failures, "record",
                        lambda *a, **kw: recorded.append((a, kw)))

    class Ws:
        async def send_json(self, payload):
            sent.append(payload)

    class Client:
        async def receive_response(self):
            yield _messages()[0]
            raise Exception("Failed to decode JSON: JSON message exceeded "
                            "maximum buffer size of 1048576 bytes...")

    asyncio.run(console.stream_response(Client().receive_response(), Ws(),
                                        console.SessionState()))

    assert sent[-1] == {"type": "result", "duration_ms": None,
                        "is_error": True, "interrupted": False}
    assert sent[-2]["type"] == "status" and "maximum buffer" in sent[-2]["text"]
    (origin, kind, label), kw = recorded[0]
    assert (origin, kind, label) == ("console", "turn", "stream")


def test_the_buffer_is_sized_for_a_big_tool_result():
    opts = console.build_options(None, None, None, None, None)
    assert opts.max_buffer_size == console.MAX_BUFFER_SIZE > 1024 * 1024


def _result_payload(monkeypatch, result_msg):
    monkeypatch.setattr(console.failures, "record", lambda *a, **kw: None)
    sent = []

    class Ws:
        async def send_json(self, payload):
            sent.append(payload)

    async def stream():
        yield result_msg

    asyncio.run(console._stream_messages(stream(), Ws(), console.SessionState(), None))
    return next(p for p in sent if p["type"] == "result")


def test_a_turn_the_usage_window_cut_off_says_so(monkeypatch):
    """The tab offers to resume it once the window resets."""
    cut = ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1, is_error=True,
                        num_turns=1, session_id="s",
                        result="You've hit your 5-hour limit · resets 3:40am (America/Los_Angeles)")
    assert _result_payload(monkeypatch, cut)["limited"] is True
    broke = ResultMessage(subtype="error_during_execution", duration_ms=1, duration_api_ms=1,
                          is_error=True, num_turns=1, session_id="s", result="boom")
    assert _result_payload(monkeypatch, broke)["limited"] is False
