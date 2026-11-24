"""Pressing Stop is not a turn failing.

The CLI ends a stopped turn like a broken one, so the socket (which knows which turns
it stopped) must say so, for that turn only. Drives the real socket loop.
Design: DESIGN.md → `backend/tests/test_console_interrupt.py`.
"""

import asyncio
import json
import time

import pytest
from claude_agent_sdk import AssistantMessage, ResultMessage, SystemMessage, TextBlock

from app import console


def _error_result():
    return ResultMessage(subtype="error_during_execution", duration_ms=1,
                         duration_api_ms=1, is_error=True, num_turns=1,
                         session_id="s", result=None)


class FakeWS:
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

    def results(self):
        return [m for m in self.sent if m.get("type") == "result"]


class FakeClient:
    """Each query is one turn: `hang` runs until interrupted then ends as a stopped
    turn; any other ends at once with a genuine error. `wake` is a CLI-started turn."""

    instance: "FakeClient | None" = None

    def __init__(self, options=None):
        FakeClient.instance = self
        self.turns: list[str] = []
        self.stopped = asyncio.Event()
        self.queued: asyncio.Queue = asyncio.Queue()

    async def connect(self):
        return None

    async def disconnect(self):
        return None

    async def query(self, prompt):
        self.stopped = asyncio.Event()
        self.queued.put_nowait([self.turns.pop(0)])

    def wake(self, messages):
        self.stopped = asyncio.Event()
        self.queued.put_nowait(messages)

    async def interrupt(self):
        self.stopped.set()

    async def receive_messages(self):
        while True:
            for m in await self.queued.get():
                if m == "hang":
                    await self.stopped.wait()
                    yield _error_result()
                elif m == "error":
                    yield _error_result()
                else:
                    yield m


async def _until(cond, timeout=2.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if cond():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("timed out")


@pytest.fixture
async def socket(monkeypatch):
    recorded: list = []
    monkeypatch.setattr(console.failures, "record",
                        lambda *a, **kw: recorded.append((a, kw)))
    monkeypatch.setattr(console, "ClaudeSDKClient", FakeClient)
    monkeypatch.setattr(console, "build_prompt", lambda payload, state: "x")
    ws = FakeWS()

    class Spy(console.SessionState):        # the socket's own state
        def __init__(self):
            super().__init__()
            ws.state = self

    monkeypatch.setattr(console, "SessionState", Spy)
    task = asyncio.create_task(console.console_ws(ws))
    ws.feed({"type": "start", "permission_mode": "default"})
    await _until(lambda: FakeClient.instance is not None
                 and any(m.get("type") == "status" for m in ws.sent))
    try:
        yield ws, FakeClient.instance, recorded
    finally:
        FakeClient.instance = None
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass


@pytest.mark.anyio
async def test_a_stopped_turn_is_not_a_failure(socket):
    ws, client, recorded = socket
    client.turns = ["hang"]
    ws.feed({"type": "user", "text": "go"})
    await asyncio.sleep(0.05)               # the turn is in flight
    ws.feed({"type": "interrupt"})
    await _until(lambda: ws.results())

    (result,) = ws.results()
    assert result["interrupted"] is True
    assert recorded == [], "a Stop press wrote a failure row"


@pytest.mark.anyio
async def test_a_genuine_error_is_still_recorded(socket):
    ws, client, recorded = socket
    client.turns = ["error"]
    ws.feed({"type": "user", "text": "go"})
    await _until(lambda: ws.results())

    (result,) = ws.results()
    assert result["is_error"] is True and result["interrupted"] is False
    assert len(recorded) == 1
    assert recorded[0][0][:3] == ("console", "turn", "error_during_execution")


@pytest.mark.anyio
async def test_the_stop_does_not_leak_into_the_next_turn(socket):
    """A late second Stop after turn one ended: turn two's own failure is still filed."""
    ws, client, recorded = socket
    client.turns = ["hang", "error"]
    ws.feed({"type": "user", "text": "one"})
    await asyncio.sleep(0.05)
    ws.feed({"type": "interrupt"})
    await _until(lambda: len(ws.results()) == 1)
    await asyncio.sleep(0.05)               # let turn one's task finish
    ws.feed({"type": "interrupt"})          # nothing live to stop
    ws.feed({"type": "user", "text": "two"})
    await _until(lambda: len(ws.results()) == 2)

    first, second = ws.results()
    assert first["interrupted"] is True
    assert second["interrupted"] is False
    assert len(recorded) == 1, "the second turn's genuine error went unrecorded"


@pytest.mark.anyio
async def test_a_stop_landing_after_the_result_does_not_mark_the_next_turn(socket):
    """Voice window: Stop lands after the result was judged; the next query clears the flag."""
    ws, client, recorded = socket
    client.turns = ["error"]
    ws.feed({"type": "user", "text": "one"})
    await _until(lambda: len(ws.results()) == 1)
    assert len(recorded) == 1
    ws.state.interrupted = True             # the late Stop, after the verdict
    client.turns = ["error"]
    ws.feed({"type": "user", "text": "two"})
    await _until(lambda: len(ws.results()) == 2)
    assert ws.results()[1]["interrupted"] is False
    assert len(recorded) == 2


# ---------- turns the CLI starts on its own ----------
#
# A backgrounded Bash finishing makes the CLI run a turn unasked; read per query,
# every later answer arrives one turn late.


def _said(text):
    return AssistantMessage(content=[TextBlock(text=text)], model="haiku")


def _ok():
    return ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1,
                         is_error=False, num_turns=1, session_id="s")


def _finished(summary):
    return SystemMessage(subtype="task_notification", data={"summary": summary})


@pytest.mark.anyio
async def test_a_turn_nobody_asked_for_still_renders(socket):
    ws, client, _ = socket
    client.wake([_finished('Background command "send.py --cap 5" completed'),
                 _said("All 5 messages went out."), _ok()])
    await _until(lambda: ws.results())

    kinds = [m["type"] for m in ws.sent if m["type"] in ("turn_start", "text_delta", "result")]
    assert kinds == ["turn_start", "text_delta", "result"]
    start = next(m for m in ws.sent if m["type"] == "turn_start")
    assert "send.py" in start["why"]
    assert ws.state.in_turn is False


@pytest.mark.anyio
async def test_the_next_answer_is_not_a_turn_late(socket):
    """The reply to a question is that question's, not the leftover turn's."""
    ws, client, recorded = socket
    client.wake([_said("background done"), _ok()])
    await _until(lambda: len(ws.results()) == 1)

    client.turns = ["error"]
    ws.feed({"type": "user", "text": "next question"})
    await _until(lambda: len(ws.results()) == 2)
    assert ws.results()[1]["is_error"] is True
    # The query is never answered from the wake's leftovers, and nothing is
    # announced as unprompted: it was asked.
    assert sum(m["type"] == "turn_start" for m in ws.sent) == 1


@pytest.mark.anyio
async def test_an_unprompted_turn_can_be_stopped_and_holds_the_input(socket):
    ws, client, recorded = socket
    client.wake([_said("checking the log"), "hang"])
    await _until(lambda: ws.state.in_turn)

    ws.feed({"type": "user", "text": "hello?"})
    await _until(lambda: any("Still working" in m.get("text", "") for m in ws.sent))
    ws.feed({"type": "interrupt"})
    await _until(lambda: ws.results())

    (result,) = ws.results()
    assert result["interrupted"] is True
    assert recorded == []


# ---------- Stop always answers ----------
#
# A reconnect kills the turn but the page still shows it running: Stop on an idle
# socket must still reply.


@pytest.mark.anyio
async def test_stop_with_nothing_running_says_so(socket):
    ws, client, _ = socket
    ws.feed({"type": "interrupt"})
    await _until(lambda: any(m["type"] == "idle" for m in ws.sent))
    assert ws.results() == []               # no turn to put a footer on


@pytest.mark.anyio
async def test_a_stop_the_cli_never_acknowledges_still_unlocks(socket, monkeypatch):
    ws, client, _ = socket
    monkeypatch.setattr(console, "INTERRUPT_TIMEOUT_S", 0.05)

    async def wedged():
        await asyncio.sleep(3600)
    client.interrupt = wedged
    client.turns = ["hang"]
    ws.feed({"type": "user", "text": "go"})
    await _until(lambda: ws.state.in_turn)
    ws.feed({"type": "interrupt"})
    await _until(lambda: ws.results())
    assert ws.results()[0]["interrupted"] is True
    assert any("Stop didn't land" in m.get("text", "") for m in ws.sent)


@pytest.mark.anyio
async def test_busy_counts_turns_in_flight_for_the_reloader(socket):
    """launch_app.py holds a backend restart while this is nonzero."""
    ws, client, _ = socket
    assert console.get_busy() == {"turns": 0}
    client.turns = ["hang"]
    ws.feed({"type": "user", "text": "go"})
    await _until(lambda: ws.state.in_turn)
    assert console.get_busy() == {"turns": 1}
    ws.feed({"type": "interrupt"})
    await _until(lambda: ws.results())
    await _until(lambda: not ws.state.in_turn)
    assert console.get_busy() == {"turns": 0}


@pytest.mark.anyio
async def test_a_long_turn_holds_the_restart_until_it_goes_quiet(socket):
    """Time since the CLI last spoke decides it, not turn length."""
    ws, client, _ = socket
    client.turns = ["hang"]
    ws.feed({"type": "user", "text": "go"})
    await _until(lambda: ws.state.in_turn)
    ws.state.moved_at = time.monotonic() - (console.TURN_QUIET_S - 60)
    assert console.get_busy() == {"turns": 1}
    ws.state.moved_at = time.monotonic() - (console.TURN_QUIET_S + 60)
    assert console.get_busy() == {"turns": 0}
    ws.feed({"type": "interrupt"})
    await _until(lambda: ws.results())


@pytest.mark.anyio
async def test_a_closed_socket_leaves_the_busy_count(monkeypatch):
    monkeypatch.setattr(console, "ClaudeSDKClient", FakeClient)
    ws = FakeWS()

    async def gone():
        raise console.WebSocketDisconnect()
    ws.receive_text = gone
    await console.console_ws(ws)
    assert console._live == set()
