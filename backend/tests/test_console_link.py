"""console_link: a session outlives its socket, so a reload doesn't end a turn.

A page arriving mid-turn rebuilds the log: transcript up to `cut`, then the turn's
events (your message first, text deltas joined, answered cards out, open ones in).
"""

import asyncio

import pytest
from fastapi import WebSocketDisconnect

from app import console_link
from app.console_link import Link


class Sock:
    def __init__(self):
        self.sent, self.closed = [], None

    async def send_json(self, msg):
        self.sent.append(msg)

    async def close(self, code=1000):
        self.closed = code


def run(coro):
    return asyncio.run(coro)


def test_a_session_keeps_sending_with_nobody_attached_and_replays_the_turn():
    link = Link("tab1")
    link.transcript_at = lambda: ("sess", 4096)
    open_ids = {"r2"}
    link.open_request = lambda rid: rid in open_ids

    async def go():
        a = Sock()
        await link.attach(a, resumed=False)
        await link.send_json({"type": "session", "id": "sess"})
        link.begin_turn({"type": "replay_user", "text": "do the thing"})
        await link.send_json({"type": "text_delta", "text": "Look"})
        await link.send_json({"type": "text_delta", "text": "ing."})
        await link.send_json({"type": "phase", "phase": "tool"})
        await link.send_json({"type": "tool_use", "id": "t1", "name": "Bash"})
        await link.send_json({"type": "permission_request", "request_id": "r1"})
        link.detach(a)                       # the reload
        await link.send_json({"type": "tool_result", "id": "t1"})   # nobody there: no error
        await link.send_json({"type": "permission_request", "request_id": "r2"})
        b = Sock()
        await link.attach(b, resumed=True)
        return a, b

    a, b = run(go())
    assert len(a.sent) == 6                  # nothing reached the closed socket
    attached = b.sent[0]
    assert attached["type"] == "attached" and attached["turn"] is True
    assert (attached["cut"], attached["cut_session"]) == (4096, "sess")
    replay = attached["replay"]
    assert replay[0] == {"type": "session", "id": "sess"}       # sticky first
    assert [m["type"] for m in replay[1:]] == [
        "replay_user", "text_delta", "tool_use", "tool_result", "permission_request"]
    assert replay[2]["text"] == "Looking."                      # deltas joined
    assert replay[-1]["request_id"] == "r2"                     # r1 was answered


def test_a_finished_turn_replays_nothing_but_state():
    link = Link("tab1")

    async def go():
        link.begin_turn({"type": "replay_user", "text": "hi"})
        await link.send_json({"type": "mode", "mode": "auto"})
        await link.send_json({"type": "text_delta", "text": "hello"})
        await link.send_json({"type": "result", "duration_ms": 10})
        b = Sock()
        await link.attach(b, resumed=True)
        return b

    attached = run(go()).sent[0]
    assert attached["turn"] is False and attached["cut"] is None
    assert attached["replay"] == [{"type": "mode", "mode": "auto"}]


def test_a_new_sessions_first_turn_is_all_replay():
    """The id arrives mid-turn; before that turn there was no transcript."""
    link = Link("tab1")

    async def go():
        link.begin_turn({"type": "replay_user", "text": "hi"})
        await link.send_json({"type": "session", "id": "new"})

    run(go())
    assert (link.cut_session, link.cut) == ("new", 0)


def test_a_woken_turn_starts_its_own_log_and_a_queued_message_keeps_its_text():
    link = Link("tab1")
    link.transcript_at = lambda: ("s", 10)

    async def go():
        await link.send_json({"type": "turn_start", "why": "agent finished"})
        link.queue_text("q1", "also this")
        await link.send_json({"type": "queued_in", "id": "q1"})

    run(go())
    assert link.cut == 10
    assert [m["type"] for m in link.replay()] == ["turn_start", "replay_user"]
    assert link.replay()[1] == {"type": "replay_user", "text": "also this", "queued": True}


def test_a_second_page_takes_the_tab_over_and_the_first_is_told_not_to_retry():
    link = Link("tab1")
    a, b = Sock(), Sock()
    run(link.attach(a, resumed=False))
    run(link.attach(b, resumed=True))
    assert a.closed == 4001 and link.ws is b


def test_detached_sessions_end_when_idle_past_the_grace_not_while_busy(monkeypatch):
    busy = {"v": True}
    link = Link("tab1")
    link.busy = lambda: busy["v"]
    sock = Sock()
    run(link.attach(sock, resumed=False))
    link.detach(sock)
    t0 = link.detached_at
    assert not link.expired(t0 + 1)
    assert not link.expired(t0 + console_link.IDLE_GRACE_S + 5)        # still working
    busy["v"] = False
    assert link.expired(t0 + console_link.IDLE_GRACE_S + 5)
    busy["v"] = True
    assert link.expired(t0 + console_link.DETACHED_MAX_S + 1)          # the cap


def test_reaping_closes_the_session_loop():
    link = Link("tabR")
    console_link.links["tabR"] = link
    link.detached_at = 0.0

    async def go():
        console_link.reap_once(console_link.IDLE_GRACE_S + 1)
        with pytest.raises(WebSocketDisconnect):
            await link.receive_text()

    run(go())
    assert "tabR" not in console_link.links


def test_a_deliberate_new_session_replaces_the_old_one():
    old, new = Link("tabN"), Link("tabN")

    async def go():
        console_link.register(old)
        console_link.register(new)
        with pytest.raises(WebSocketDisconnect):
            await old.receive_text()

    run(go())
    assert console_link.links["tabN"] is new
    console_link.unregister(new)
