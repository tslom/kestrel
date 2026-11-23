"""A message typed mid-turn goes in after the current step, not the turn.

The CLI already does this: a user message sent while a turn runs is read at
the next tool boundary, in the same turn, and replayed back with the uuid it
was sent under; one sent while the turn is writing its last reply runs as a
turn of its own right after; and `cancel_async_message` takes one back unread
(all measured against CLI 2.1.318 on 11-17). The page used to hold the
message until the whole turn ended, so the only way to cut in was Stop.
These drive the real socket loop against a CLI that behaves that way.
"""

import asyncio
import json

import pytest
from claude_agent_sdk import (AssistantMessage, ResultMessage, TextBlock,
                              ToolResultBlock, UserMessage)

from app import console

Q1 = "3771d78a-a30a-40a1-b01c-1686ff2a7163"
Q2 = "c745464d-ec6d-4670-aa54-fbdfe35c221d"


def _said(text):
    return AssistantMessage(content=[TextBlock(text=text)], model="m")


def _result(is_error=False):
    return ResultMessage(subtype="error_during_execution" if is_error else "success",
                         duration_ms=1, duration_api_ms=1, is_error=is_error,
                         num_turns=1, session_id="s", result=None)


def _replay(uuid, text="queued words"):
    return UserMessage(content=text, uuid=uuid)


def _tool_result():
    return UserMessage(content=[ToolResultBlock(tool_use_id="t1", content="ok")],
                       uuid="tool-result-uuid")


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

    def of(self, kind):
        return [m for m in self.sent if m.get("type") == kind]


class FakeQuery:
    def __init__(self, client):
        self.client = client
        self.requests: list[dict] = []

    async def _send_control_request(self, request):
        self.requests.append(request)
        uid = request.get("message_uuid")
        if uid in self.client.pending:
            self.client.pending.remove(uid)
            return {"cancelled": True}
        return {"cancelled": False}


class FakeClient:
    """The test scripts what the CLI says (`push`); `query` only records.
    A uuid'd message is held in `pending`, as the CLI holds its queue."""

    instance: "FakeClient | None" = None

    def __init__(self, options=None):
        FakeClient.instance = self
        self.out: asyncio.Queue = asyncio.Queue()
        self.prompts: list = []
        self.pending: list[str] = []
        self.stopped = asyncio.Event()
        self._query = FakeQuery(self)

    async def connect(self):
        return None

    async def disconnect(self):
        return None

    async def query(self, prompt):
        if isinstance(prompt, str):
            self.prompts.append(prompt)
            return
        async for msg in prompt:
            self.prompts.append(msg)
            if msg.get("uuid"):
                self.pending.append(msg["uuid"])

    def push(self, messages):
        self.out.put_nowait(messages)

    async def interrupt(self):
        self.stopped.set()

    async def receive_messages(self):
        while True:
            for m in await self.out.get():
                if m == "hang":
                    await self.stopped.wait()
                    yield _result(is_error=True)
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
    monkeypatch.setattr(console.failures, "record", lambda *a, **kw: None)
    monkeypatch.setattr(console, "ClaudeSDKClient", FakeClient)
    monkeypatch.setattr(console, "build_prompt", lambda payload, state: payload.get("text", ""))

    async def plain(client, ws):     # the fake's `_query` only answers cancels
        async for m in client.receive_messages():
            yield m
    monkeypatch.setattr(console, "_with_suggestions", plain)
    ws = FakeWS()

    class Spy(console.SessionState):
        def __init__(self):
            super().__init__()
            ws.state = self

    monkeypatch.setattr(console, "SessionState", Spy)
    task = asyncio.create_task(console.console_ws(ws))
    ws.feed({"type": "start", "permission_mode": "default"})
    await _until(lambda: FakeClient.instance is not None and hasattr(ws, "state"))
    client = FakeClient.instance
    ws.feed({"type": "user", "text": "go"})
    await _until(lambda: client.prompts)
    client.push([_said("working on it")])
    await _until(lambda: ws.of("text_delta") or ws.state.in_turn)
    yield ws, client
    task.cancel()
    FakeClient.instance = None


async def _queue(ws, client, qid, text="queued words"):
    ws.feed({"type": "user", "text": text, "queue_id": qid})
    await _until(lambda: qid in client.pending)


@pytest.mark.anyio
async def test_a_queued_message_goes_in_at_the_next_step(socket):
    ws, client = socket
    await _queue(ws, client, Q1, "also check the tests")
    # Sent as it was typed, under its uuid — not refused as "still working".
    sent = client.prompts[-1]
    assert sent["uuid"] == Q1 and sent["message"]["content"] == "also check the tests"
    assert not [m for m in ws.of("status") if "Still working" in m["text"]]

    client.push([_tool_result(), _replay(Q1), _said("done, tests too"), _result()])
    await _until(lambda: ws.of("result"))
    kinds = [m["type"] for m in ws.sent]
    # Read after the step that was running, inside the same turn.
    assert kinds.index("tool_result") < kinds.index("queued_in") < kinds.index("result")
    assert ws.of("queued_in") == [{"type": "queued_in", "id": Q1}]
    assert ws.of("turn_start") == [] and len(ws.of("result")) == 1
    assert ws.state.queued == set()


@pytest.mark.anyio
async def test_one_the_turn_ended_before_reading_opens_the_next_turn(socket):
    ws, client = socket
    await _queue(ws, client, Q1)
    client.push([_said("all done"), _result()])
    await _until(lambda: ws.of("result"))
    client.push([_replay(Q1), _said("42"), _result()])
    await _until(lambda: len(ws.of("result")) == 2)
    # Its own turn, announced as the queued message's — not as a background
    # task waking ("A background task finished" under your own question).
    assert ws.of("turn_start") == [{"type": "turn_start", "queued": True}]
    assert ws.of("queued_in") == [{"type": "queued_in", "id": Q1}]


@pytest.mark.anyio
async def test_stop_takes_unread_messages_back(socket):
    ws, client = socket
    client.push(["hang"])
    await _queue(ws, client, Q1)
    await _queue(ws, client, Q2)
    ws.feed({"type": "interrupt"})
    await _until(lambda: ws.of("result"))
    # Cancelled before the interrupt, or the CLI would run them as a fresh
    # turn the moment it landed.
    assert ws.of("unqueued") == [{"type": "unqueued", "ids": sorted([Q1, Q2])}]
    assert client.pending == [] and ws.state.queued == set()
    assert ws.of("result")[0]["interrupted"] is True


@pytest.mark.anyio
async def test_ctrl_enter_stop_keeps_the_queue(socket):
    ws, client = socket
    client.push(["hang"])
    await _queue(ws, client, Q1)
    ws.feed({"type": "interrupt", "keep_queue": True})
    await _until(lambda: ws.of("result"))
    assert ws.of("unqueued") == [] and client._query.requests == []
    assert client.pending == [Q1]


@pytest.mark.anyio
async def test_up_takes_one_back_only_while_unread(socket):
    ws, client = socket
    await _queue(ws, client, Q1)
    ws.feed({"type": "unqueue", "id": Q1})
    await _until(lambda: ws.of("unqueued"))
    assert ws.of("unqueued")[0]["ids"] == [Q1]
    # Already read: nothing comes back, and the page keeps it in the log.
    await _queue(ws, client, Q2)
    client.push([_tool_result(), _replay(Q2)])
    await _until(lambda: ws.of("queued_in"))
    ws.feed({"type": "unqueue", "id": Q2})
    await _until(lambda: len(ws.of("unqueued")) == 2)
    assert ws.of("unqueued")[1]["ids"] == []


@pytest.mark.anyio
async def test_a_bad_queue_id_is_an_ordinary_message(socket):
    ws, client = socket
    ws.feed({"type": "user", "text": "hi", "queue_id": "not-a-uuid"})
    await _until(lambda: [m for m in ws.of("status") if "Still working" in m["text"]])
    assert client.pending == []
