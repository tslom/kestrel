"""Stats for nerds: a turn's speed, measured off the stream (console.py).

Times the model calls themselves, not the turn: time to first token (message sent to
first content block; thinking counts), tok/s (output tokens over `message_start` to
`message_stop`, summed) and call count. Subagent events are never passed in.
Computed always (a few clock reads); shown behind Settings. Design: DESIGN.md → Console (Stats for nerds).
"""

from __future__ import annotations

import time


class TurnStats:
    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.reset()

    def reset(self) -> None:
        self.sent_at: float | None = None
        self.first_at: float | None = None
        self.call_start: float | None = None
        self.call_out = 0
        self.gen_s = 0.0
        self.out = 0
        self.calls = 0

    def sent(self) -> None:
        """A message went to the model with no turn running: a new turn."""
        self.reset()
        self.sent_at = self.clock()

    def event(self, ev: dict) -> None:
        """One raw stream event (StreamEvent.event) from the top level."""
        kind = ev.get("type")
        now = self.clock()
        if kind == "message_start":
            self.calls += 1
            self.call_start = now
            usage = (ev.get("message") or {}).get("usage") or {}
            self.call_out = int(usage.get("output_tokens") or 0)
        elif kind == "content_block_start":
            if self.first_at is None:
                self.first_at = now
        elif kind == "message_delta":
            usage = ev.get("usage") or {}
            if usage.get("output_tokens") is not None:
                self.call_out = int(usage["output_tokens"])
        elif kind == "message_stop" and self.call_start is not None:
            self.gen_s += max(0.0, now - self.call_start)
            self.out += self.call_out
            self.call_start, self.call_out = None, 0

    def finish(self) -> dict | None:
        """The footer's numbers, or None when nothing was timed (no stream events).
        Resets, so a turn the CLI starts itself is timed from zero with no TTFT."""
        calls, out, gen_s = self.calls, self.out, self.gen_s
        ttft = (round((self.first_at - self.sent_at) * 1000)
                if self.first_at is not None and self.sent_at is not None else None)
        self.reset()
        if not calls:
            return None
        rate = round(out / gen_s, 1) if gen_s > 0 and out else None
        return {"ttft_ms": ttft, "tok_s": rate, "calls": calls, "out": out or None}
