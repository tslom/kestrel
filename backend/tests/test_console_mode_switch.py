"""A permission-mode switch is live: same CLI, same session, same turn.

It used to reconnect, which killed the turn in flight, denied its cards and,
when the first turn hadn't written yet, came back as a fresh session — and
Shift+Tab cycling did it once per press. Now the socket stays up and the
switch reaches the running client through set_permission_mode.
"""

import asyncio

import pytest

from app import console
from tests.test_console_auto_mode import FakeClient, FakeWS


class ModeClient(FakeClient):
    built: list["ModeClient"] = []

    def __init__(self, options=None):
        super().__init__(options)
        self.modes: list[str] = []
        ModeClient.built.append(self)

    async def set_permission_mode(self, mode):
        self.modes.append(mode)


class BrokenModeClient(ModeClient):
    async def set_permission_mode(self, mode):
        raise RuntimeError("control request timed out")


async def _settle(cond):
    deadline = asyncio.get_running_loop().time() + 2
    while asyncio.get_running_loop().time() < deadline:
        if cond():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("never happened")


@pytest.fixture
async def socket(monkeypatch, request):
    cls = getattr(request, "param", ModeClient)
    ModeClient.built = []
    caught: dict = {}
    real_build = console.build_options

    def spy(session_id, model, mode, can_use_tool, *a, **k):
        caught["can_use_tool"] = can_use_tool
        return real_build(session_id, model, mode, can_use_tool, *a, **k)

    monkeypatch.setattr(console, "build_options", spy)
    monkeypatch.setattr(console, "ClaudeSDKClient", cls)
    ws = FakeWS()
    task = asyncio.create_task(console.console_ws(ws))
    ws.feed({"type": "start", "permission_mode": "default", "session_id": "abc"})
    await _settle(lambda: ws.of_type("mode"))
    try:
        yield ws, caught
    finally:
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass


async def _card(ws, n=1):
    await _settle(lambda: len(ws.of_type("permission_request")) >= n)
    return ws.of_type("permission_request")[n - 1]


@pytest.mark.anyio
async def test_a_switch_reaches_the_live_client_without_a_new_one(socket):
    ws, _ = socket
    ws.feed({"type": "mode", "permission_mode": "plan"})
    await _settle(lambda: len(ws.of_type("mode")) >= 2)
    assert len(ModeClient.built) == 1, "the switch built a second CLI"
    assert ModeClient.built[0].modes == ["plan"]
    assert ws.of_type("mode")[-1]["mode"] == "plan"


@pytest.mark.anyio
async def test_auto_reaches_the_sdk_as_default(socket):
    ws, _ = socket
    ws.feed({"type": "mode", "permission_mode": "auto"})
    await _settle(lambda: len(ws.of_type("mode")) >= 2)
    assert ModeClient.built[0].modes == ["default"]
    assert "undone" in ws.of_type("status")[-1]["text"]


@pytest.mark.anyio
async def test_the_next_call_is_asked_under_the_new_mode(socket):
    """Ask → auto: a write that carded a moment ago now runs on its own."""
    ws, caught = socket
    write = ("mcp__vault__task_create", {"title": "pay tuition"}, None)
    first = asyncio.ensure_future(caught["can_use_tool"](*write))
    card = await _card(ws)
    ws.feed({"type": "permission", "request_id": card["request_id"], "allow": True})
    await asyncio.wait_for(first, 2)

    ws.feed({"type": "mode", "permission_mode": "auto"})
    await _settle(lambda: len(ws.of_type("mode")) >= 2)
    result = await asyncio.wait_for(caught["can_use_tool"](*write), 2)
    assert type(result).__name__ == "PermissionResultAllow"
    assert len(ws.of_type("permission_request")) == 1, "auto mode carded it"


@pytest.mark.anyio
async def test_a_card_waiting_across_the_switch_still_answers(socket):
    """The old reconnect denied every waiting card. A live switch leaves it
    up, and the click still reaches the call."""
    ws, caught = socket
    waiting = asyncio.ensure_future(caught["can_use_tool"](
        "Bash", {"command": "rm -rf 06-Generated"}, None))
    card = await _card(ws)
    ws.feed({"type": "mode", "permission_mode": "auto"})
    await _settle(lambda: len(ws.of_type("mode")) >= 2)
    assert not waiting.done()
    ws.feed({"type": "permission", "request_id": card["request_id"], "allow": True})
    result = await asyncio.wait_for(waiting, 2)
    assert type(result).__name__ == "PermissionResultAllow"


@pytest.mark.anyio
async def test_an_unknown_mode_is_ignored(socket):
    ws, _ = socket
    ws.feed({"type": "mode", "permission_mode": "yolo"})
    ws.feed({"type": "mode", "permission_mode": "plan"})
    await _settle(lambda: len(ws.of_type("mode")) >= 2)
    assert ModeClient.built[0].modes == ["plan"]


@pytest.mark.anyio
@pytest.mark.parametrize("socket", [BrokenModeClient], indirect=True)
async def test_a_failed_switch_puts_the_picker_back(socket):
    ws, _ = socket
    ws.feed({"type": "mode", "permission_mode": "plan"})
    await _settle(lambda: len(ws.of_type("mode")) >= 2)
    assert ws.of_type("mode")[-1]["mode"] == "default"
    assert "Couldn't switch mode" in ws.of_type("status")[-1]["text"]
