"""The `phase` lines a turn sends before it has text or a chip to show.

Measured: on a tool turn the thinking block opens ~0.7s in and the call's
block ~0.9s before its full input arrives, and without these the page shows
only a bare "working…" through all of it. A subagent's blocks are its own
business and send nothing.
"""

import asyncio

from claude_agent_sdk import ResultMessage
from claude_agent_sdk.types import StreamEvent

from app import console


def _start(block, parent=None):
    return StreamEvent(uuid="u", session_id="s", parent_tool_use_id=parent,
                       event={"type": "content_block_start", "index": 0,
                              "content_block": block})


def test_thinking_and_tool_blocks_send_a_phase():
    sent = []

    class Ws:
        async def send_json(self, payload):
            sent.append(payload)

    async def stream():
        for m in [
            _start({"type": "thinking", "thinking": ""}),
            _start({"type": "tool_use", "id": "t1", "name": "Read", "input": {}}),
            _start({"type": "tool_use", "id": "t2", "name": "Grep", "input": {}},
                   parent="agent-call"),
            _start({"type": "text", "text": ""}),
            ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1,
                          is_error=False, num_turns=1, session_id="s", result="ok"),
        ]:
            yield m

    asyncio.run(console._stream_messages(stream(), Ws(), console.SessionState(), None))

    phases = [p for p in sent if p["type"] == "phase"]
    assert phases == [{"type": "phase", "phase": "thinking"},
                      {"type": "phase", "phase": "tool", "name": "Read"}]
