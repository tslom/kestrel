"""Held messages (app/console_queue.py): typed mid-turn in
"queue" mode, kept in app.db, sent as their own turn when a clean turn ends,
editable and reorderable until then, handed to the CLI by "steer now", back
to the prompt on Stop, and still there after a backend reload. Drives the
real socket loop against the fake CLI from test_console_queue."""

import asyncio

import pytest

from app import console, console_link, console_queue
from tests.test_console_queue import FakeClient, FakeWS, _replay, _result, _said, _until

TAB = "heldtab1"


@pytest.fixture
async def socket(monkeypatch, dbfile):
    monkeypatch.setattr(console.failures, "record", lambda *a, **kw: None)
    monkeypatch.setattr(console, "ClaudeSDKClient", FakeClient)
    monkeypatch.setattr(console, "build_prompt", lambda payload, state: payload.get("text", ""))

    async def plain(client, ws):
        async for m in client.receive_messages():
            yield m
    monkeypatch.setattr(console, "_with_suggestions", plain)
    tasks = []

    async def open_tab(first_text="go"):
        ws = FakeWS()

        class Spy(console.SessionState):
            def __init__(self):
                super().__init__()
                ws.state = self
        monkeypatch.setattr(console, "SessionState", Spy)
        FakeClient.instance = None
        tasks.append(asyncio.create_task(console.console_ws(ws)))
        ws.feed({"type": "start", "tab": TAB, "permission_mode": "default"})
        await _until(lambda: FakeClient.instance is not None and hasattr(ws, "state"))
        client = FakeClient.instance
        await _until(lambda: ws.of("mode"))
        if first_text:
            ws.feed({"type": "user", "text": first_text})
            await _until(lambda: client.prompts)
            client.push([_said("working on it")])
            await _until(lambda: ws.state.in_turn)
        return ws, client
    yield open_tab
    for t in tasks:
        t.cancel()
    for link in list(console_link.links.values()):
        link.close()
    FakeClient.instance = None


def _held(ws):
    return ws.of("held")[-1]["items"] if ws.of("held") else None


async def _hold(ws, text, n):
    ws.feed({"type": "user", "text": text, "hold": True, "context": [{"label": "pill A", "data": {}}]})
    await _until(lambda: _held(ws) is not None and len(_held(ws)) == n)


@pytest.mark.anyio
async def test_held_messages_wait_then_go_one_turn_each(socket):
    ws, client = await socket()
    await _hold(ws, "first after", 1)
    await _hold(ws, "second after", 2)
    # Held, not given to the CLI: the turn running never sees them.
    assert client.prompts == ["go"] and client.pending == []
    assert [i["text"] for i in _held(ws)] == ["first after", "second after"]
    assert _held(ws)[0]["pills"] == ["pill A"]

    client.push([_said("done"), _result()])
    await _until(lambda: "first after" in client.prompts)
    # The page is told, since nobody typed it just now.
    assert {"type": "replay_user", "text": "first after", "pills": ["pill A"], "held": True} in ws.sent
    assert [i["text"] for i in _held(ws)] == ["second after"]
    client.push([_said("ok"), _result()])
    await _until(lambda: "second after" in client.prompts)
    assert _held(ws) == []


@pytest.mark.anyio
async def test_edit_and_reorder_before_they_go(socket):
    ws, client = await socket()
    await _hold(ws, "a", 1)
    await _hold(ws, "b", 2)
    a, b = [i["id"] for i in _held(ws)]
    ws.feed({"type": "held_edit", "id": a, "text": "a, edited"})
    await _until(lambda: _held(ws)[0]["text"] == "a, edited")
    ws.feed({"type": "held_move", "id": b, "to": 0})
    await _until(lambda: _held(ws)[0]["id"] == b)
    ws.feed({"type": "held_drop", "id": b})
    await _until(lambda: [i["id"] for i in _held(ws)] == [a])
    client.push([_said("done"), _result()])
    await _until(lambda: "a, edited" in client.prompts)


@pytest.mark.anyio
async def test_steer_now_hands_one_to_the_cli_mid_turn(socket):
    ws, client = await socket()
    await _hold(ws, "actually, stop at the tests", 1)
    qid = _held(ws)[0]["id"]
    ws.feed({"type": "held_steer", "id": qid})
    await _until(lambda: qid in client.pending)
    assert ws.of("steered")[-1]["id"] == qid and _held(ws) == []
    client.push([_replay(qid, "actually, stop at the tests"), _said("stopping"), _result()])
    await _until(lambda: ws.of("result"))
    assert ws.of("queued_in") == [{"type": "queued_in", "id": qid}]


@pytest.mark.anyio
async def test_stop_hands_them_back_and_an_error_doesnt_drain(socket):
    ws, client = await socket()
    await _hold(ws, "later one", 1)
    ws.feed({"type": "interrupt"})
    await _until(lambda: ws.of("held_back"))
    assert ws.of("held_back")[-1]["texts"] == ["later one"] and _held(ws) == []
    assert console_queue.count(TAB) == 0

    # A turn that errors leaves the queue alone: the next would hit the same wall.
    client.push([_result(is_error=True)])
    ws2, client2 = ws, client
    await _until(lambda: not ws2.state.in_turn)
    ws2.feed({"type": "user", "text": "again"})
    await _until(lambda: "again" in client2.prompts)
    client2.push([_said("x")])
    await _hold(ws2, "behind an error", 1)
    client2.push([_result(is_error=True)])
    await _until(lambda: len(ws2.of("result")) >= 2)
    await asyncio.sleep(0.1)
    assert "behind an error" not in client2.prompts and console_queue.count(TAB) == 1


@pytest.mark.anyio
async def test_a_reload_keeps_them_and_send_now_sends_one(socket):
    ws, client = await socket()
    await _hold(ws, "survive the reload", 1)
    # The backend goes away mid-turn: the table is what's left.
    for link in list(console_link.links.values()):
        link.close()
    await asyncio.sleep(0.05)
    assert console_queue.count(TAB) == 1
    ws2, client2 = await socket(first_text=None)
    await _until(lambda: _held(ws2) is not None)
    assert [i["text"] for i in _held(ws2)] == ["survive the reload"]
    # Idle, it waits; "send now" makes it a turn.
    ws2.feed({"type": "held_steer", "id": _held(ws2)[0]["id"]})
    await _until(lambda: "survive the reload" in client2.prompts)
    assert _held(ws2) == []


def test_store_caps_and_orders(dbfile):
    ids = [console_queue.add("t", {"text": f"m{i}"}) for i in range(console_queue.MAX_HELD)]
    assert all(ids) and console_queue.add("t", {"text": "one too many"}) is None
    assert console_queue.move("t", ids[-1], 0)
    assert console_queue.take("t")["text"] == f"m{console_queue.MAX_HELD - 1}"
    assert console_queue.items("other") == []
    assert len(console_queue.clear("t")) == console_queue.MAX_HELD - 1
