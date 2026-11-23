"""One turn's parallel tool calls become one approval card.

The console asks the browser for permission from inside `can_use_tool`, once
per call, and an assistant turn can emit several calls at once. A card apiece
meant the user answered the first while the rest sat queued: three
`event_create` calls in one turn all came back "AbortError: Stream closed"
with the later cards never answered, and that error accounts for 15 of the
console's failed tool calls on record.

These drive the real socket. `build_options` is patched only to catch the
`can_use_tool` it is handed, and the calls made against it are the ones the
SDK itself would make — concurrently, on the socket's own event loop.
"""

import asyncio
import json

import pytest

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
    """Stands in for ClaudeSDKClient: connects, and says nothing."""

    def __init__(self, options=None):
        self.options = options

    async def connect(self):
        return None

    async def disconnect(self):
        return None

    async def interrupt(self):
        return None

    async def receive_messages(self):
        await asyncio.Event().wait()
        yield


@pytest.fixture
async def socket(monkeypatch):
    """A started console socket, with the `can_use_tool` it built."""
    caught: dict = {}
    real_build = console.build_options

    def spy(session_id, model, mode, can_use_tool, ask_questions, *a, **k):
        caught["can_use_tool"] = can_use_tool
        return real_build(session_id, model, mode, can_use_tool,
                          ask_questions, *a, **k)

    # The socket's own SessionState, so a test can put the model's narration
    # where a real turn would have left it — `can_use_tool` reads it from
    # there to fill the card's reason.
    class Spy(console.SessionState):
        def __init__(self):
            super().__init__()
            caught["state"] = self

    monkeypatch.setattr(console, "build_options", spy)
    monkeypatch.setattr(console, "ClaudeSDKClient", FakeClient)
    monkeypatch.setattr(console, "SessionState", Spy)

    ws = FakeWS()
    task = asyncio.create_task(console.console_ws(ws))
    ws.feed({"type": "start", "permission_mode": "default"})
    for _ in range(200):                       # let the start message land
        await asyncio.sleep(0)
        if "can_use_tool" in caught:
            break
    assert "can_use_tool" in caught, "the socket never built its options"
    ws.state = caught.get("state")
    try:
        yield ws, caught["can_use_tool"]
    finally:
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass


async def _card(ws, timeout=2.0):
    """The permission card the socket sends, once it sends one."""
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        cards = ws.of_type("permission_request")
        if cards:
            return cards[0]
        await asyncio.sleep(0.01)
    raise AssertionError("no permission_request arrived")


@pytest.mark.anyio
async def test_a_burst_becomes_one_card_carrying_every_call(socket):
    ws, can_use_tool = socket
    burst = asyncio.gather(*[
        can_use_tool("mcp__vault__event_create", {"summary": f"Lecture {i}"}, None)
        for i in range(3)
    ])

    card = await _card(ws)
    assert len(ws.of_type("permission_request")) == 1, "one card, not three"
    assert [c["summary"] for c in card["inputs"]] == ["Lecture 0", "Lecture 1",
                                                      "Lecture 2"]
    # `input` stays the first of them, so a client reading only the old field
    # still renders something true.
    assert card["input"] == card["inputs"][0]

    ws.feed({"type": "permission", "request_id": card["request_id"],
             "allow": True})
    results = await asyncio.wait_for(burst, timeout=2)
    assert len(results) == 3
    assert all(type(r).__name__ == "PermissionResultAllow" for r in results), \
        "one answer has to settle the whole burst"


@pytest.mark.anyio
async def test_one_answer_denies_the_whole_burst(socket):
    ws, can_use_tool = socket
    burst = asyncio.gather(*[
        can_use_tool("mcp__vault__event_create", {"summary": f"Lecture {i}"}, None)
        for i in range(2)
    ])
    card = await _card(ws)
    ws.feed({"type": "permission", "request_id": card["request_id"],
             "allow": False, "reason": "wrong week"})
    results = await asyncio.wait_for(burst, timeout=2)
    assert all(type(r).__name__ == "PermissionResultDeny" for r in results)
    assert all("wrong week" in r.message for r in results)


@pytest.mark.anyio
async def test_different_tools_still_get_their_own_cards(socket):
    """Coalescing is per tool — two unrelated calls are two decisions."""
    ws, can_use_tool = socket
    burst = asyncio.gather(
        can_use_tool("mcp__vault__event_create", {"summary": "Lecture"}, None),
        can_use_tool("mcp__vault__task_create", {"title": "HW 1"}, None),
    )
    deadline = asyncio.get_running_loop().time() + 2.0
    while (len(ws.of_type("permission_request")) < 2
           and asyncio.get_running_loop().time() < deadline):
        await asyncio.sleep(0.01)
    cards = ws.of_type("permission_request")
    assert len(cards) == 2
    assert {c["tool"] for c in cards} == {"mcp__vault__event_create",
                                          "mcp__vault__task_create"}
    for c in cards:
        ws.feed({"type": "permission", "request_id": c["request_id"],
                 "allow": True})
    results = await asyncio.wait_for(burst, timeout=2)
    assert all(type(r).__name__ == "PermissionResultAllow" for r in results)


@pytest.mark.anyio
async def test_a_later_call_is_not_folded_into_a_settled_card(socket):
    """The window is one turn's worth. A call that arrives after the card has
    gone out gets a card of its own, not a silent ride on an old answer."""
    ws, can_use_tool = socket
    first = asyncio.ensure_future(
        can_use_tool("mcp__vault__event_create", {"summary": "One"}, None))
    card = await _card(ws)
    ws.feed({"type": "permission", "request_id": card["request_id"],
             "allow": True})
    assert type(await asyncio.wait_for(first, timeout=2)).__name__ == \
        "PermissionResultAllow"

    second = asyncio.ensure_future(
        can_use_tool("mcp__vault__event_create", {"summary": "Two"}, None))
    deadline = asyncio.get_running_loop().time() + 2.0
    while (len(ws.of_type("permission_request")) < 2
           and asyncio.get_running_loop().time() < deadline):
        await asyncio.sleep(0.01)
    cards = ws.of_type("permission_request")
    assert len(cards) == 2, "the second call needs its own card"
    assert cards[1]["inputs"] == [{"summary": "Two"}]
    ws.feed({"type": "permission", "request_id": cards[1]["request_id"],
             "allow": True})
    assert type(await asyncio.wait_for(second, timeout=2)).__name__ == \
        "PermissionResultAllow"


# ---------- what the card says ----------
#
# The same card, asked to be answerable. It used to carry the tool's raw
# input and nothing else, so a delete arrived as a calendar id and an event
# id — no title, no hour, no calendar, and no word about what saying yes
# would cost. These assert the three fields that fixed that.


def _event(day="2026-09-22"):
    from app import db
    with db.session() as con, con:
        con.execute(
            "INSERT INTO events (day, event_id, calendar_id, calendar_name,"
            " summary, start_at, end_at, all_day, location) VALUES"
            " (?,'ev1','gym@g','Gym/Workouts','Lift',?,?,0,'Lyon Center')",
            (day, f"{day}T17:00:00-07:00", f"{day}T18:00:00-07:00"))


@pytest.mark.anyio
async def test_the_card_carries_the_event_not_just_its_ids(socket, dbfile):
    ws, can_use_tool = socket
    _event()
    call = asyncio.ensure_future(can_use_tool(
        "mcp__vault__event_delete", {"event_id": "ev1", "calendar_id": "gym@g"},
        None))
    card = await _card(ws)
    assert [c["title"] for c in card["preview"]] == ["Lift"]
    assert ["calendar", "Gym/Workouts"] in card["preview"][0]["rows"]
    # and the arguments are still there, because the preview is a reading of
    # them and a reading can be wrong.
    assert card["inputs"] == [{"event_id": "ev1", "calendar_id": "gym@g"}]
    ws.feed({"type": "permission", "request_id": card["request_id"], "allow": True})
    await asyncio.wait_for(call, timeout=2)


@pytest.mark.anyio
async def test_a_delete_says_what_saying_yes_costs(socket, dbfile):
    """In every mode, not only auto's. "ask" was rendering a delete and a
    rename in the same neutral card."""
    ws, can_use_tool = socket
    call = asyncio.ensure_future(can_use_tool(
        "mcp__vault__event_delete", {"event_id": "ev1"}, None))
    card = await _card(ws)
    assert card["danger"] and "no undo" in card["danger"]
    assert card["guard"] is None, "the guard line belongs to auto mode"
    ws.feed({"type": "permission", "request_id": card["request_id"], "allow": True})
    await asyncio.wait_for(call, timeout=2)


@pytest.mark.anyio
async def test_a_reversible_write_gets_no_warning(socket, dbfile):
    """A card that warned about every write is a card nobody reads the
    warning on."""
    ws, can_use_tool = socket
    call = asyncio.ensure_future(can_use_tool(
        "mcp__vault__task_create", {"title": "Order supplies"}, None))
    card = await _card(ws)
    assert card["danger"] is None
    ws.feed({"type": "permission", "request_id": card["request_id"], "allow": True})
    await asyncio.wait_for(call, timeout=2)


@pytest.mark.anyio
async def test_the_card_quotes_why_in_the_models_own_words(socket, dbfile):
    ws, can_use_tool = socket
    ws.state.narration.append("The 9am is a duplicate of the 9:30, so I'll "
                              "drop it.")
    call = asyncio.ensure_future(can_use_tool(
        "mcp__vault__event_delete", {"event_id": "ev1"}, None))
    card = await _card(ws)
    assert card["why"] == ("The 9am is a duplicate of the 9:30, so I'll drop it.")
    ws.feed({"type": "permission", "request_id": card["request_id"], "allow": True})
    await asyncio.wait_for(call, timeout=2)


@pytest.mark.anyio
async def test_a_reason_is_used_once_and_not_re_attached(socket, dbfile):
    """The failure this prevents is the worst one available here: the
    sentence explaining the first delete rendered under a second, different
    delete the model said nothing about."""
    ws, can_use_tool = socket
    ws.state.narration.append("Dropping the duplicate 9am.")
    first = asyncio.ensure_future(
        can_use_tool("mcp__vault__event_delete", {"event_id": "a"}, None))
    card = await _card(ws)
    assert card["why"] == "Dropping the duplicate 9am."
    ws.feed({"type": "permission", "request_id": card["request_id"], "allow": True})
    await asyncio.wait_for(first, timeout=2)

    second = asyncio.ensure_future(
        can_use_tool("mcp__vault__event_delete", {"event_id": "b"}, None))
    deadline = asyncio.get_running_loop().time() + 2.0
    while (len(ws.of_type("permission_request")) < 2
           and asyncio.get_running_loop().time() < deadline):
        await asyncio.sleep(0.01)
    cards = ws.of_type("permission_request")
    assert len(cards) == 2
    assert cards[1]["why"] is None, "silence is not a justification"
    ws.feed({"type": "permission", "request_id": cards[1]["request_id"],
             "allow": True})
    await asyncio.wait_for(second, timeout=2)


# ---------- Claude in Chrome: one card per web task, not per click (#55) ----------

CLICK = ("mcp__claude-in-chrome__computer", {"action": "left_click", "coordinate": [1, 2]})


async def _answer(ws, n, **answer):
    deadline = asyncio.get_running_loop().time() + 2.0
    while len(ws.of_type("permission_request")) < n:
        assert asyncio.get_running_loop().time() < deadline, "no card arrived"
        await asyncio.sleep(0.01)
    card = ws.of_type("permission_request")[n - 1]
    ws.feed({"type": "permission", "request_id": card["request_id"], **answer})
    return card


@pytest.mark.anyio
async def test_a_browser_card_offers_a_yes_for_the_rest_of_the_turn(socket):
    ws, can_use_tool = socket
    first = asyncio.create_task(can_use_tool(*CLICK, None, gate=console.BROWSER_GUARD))
    card = await _answer(ws, 1, allow=True, always=True)
    assert card["always_scope"] == "turn" and "turn" in card["always_label"]
    await asyncio.wait_for(first, 2)
    # the next click in the same turn goes through without a card
    again = await asyncio.wait_for(can_use_tool(*CLICK, None, gate=console.BROWSER_GUARD), 2)
    assert type(again).__name__ == "PermissionResultAllow"
    assert len(ws.of_type("permission_request")) == 1


@pytest.mark.anyio
async def test_the_turn_yes_ends_with_the_turn_and_never_covers_script(socket, monkeypatch):
    ws, can_use_tool = socket

    async def query(self, *a, **k):            # a user turn goes out and stays open
        return None
    monkeypatch.setattr(FakeClient, "query", query, raising=False)
    ws.state.browser_turn_ok = True
    script = asyncio.create_task(can_use_tool(
        "mcp__claude-in-chrome__javascript_tool", {"text": "1"}, None, gate=console.BROWSER_GUARD))
    card = await _answer(ws, 1, allow=True)
    assert card["always_label"] is None, "script asks every time"
    await asyncio.wait_for(script, 2)

    ws.feed({"type": "user", "text": "next thing", "typed": True})   # a new turn
    for _ in range(100):
        await asyncio.sleep(0)
        if not ws.state.browser_turn_ok:
            break
    assert not ws.state.browser_turn_ok
    click = asyncio.create_task(can_use_tool(*CLICK, None, gate=console.BROWSER_GUARD))
    await _answer(ws, 2, allow=True)
    await asyncio.wait_for(click, 2)


@pytest.mark.anyio
async def test_other_gated_calls_still_cant_earn_a_standing_yes(socket):
    ws, can_use_tool = socket
    job = asyncio.create_task(can_use_tool(
        console.USE_COMPUTER_TOOL, {"task": "x"}, None, gate=console.COMPUTER_GUARD))
    card = await _answer(ws, 1, allow=True, always=True)
    assert card["always_label"] is None
    await asyncio.wait_for(job, 2)
    assert not ws.state.browser_turn_ok
