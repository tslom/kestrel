"""Stats for nerds: a turn's speed, measured off the stream (console.py).

The turn footer already says how long a turn took and how many tokens it
read and wrote. Neither is a speed: a dev turn is a dozen model calls with
tool runs between them, so tokens over wall-clock time measures the tools as
much as the model. So this times the model calls themselves.

  * **time to first token**: from the message leaving (`sent`) to the first
    content block of the turn's first reply. A thinking block counts: that's
    the model answering, even when the thinking text itself isn't shown.
  * **tok/s**: output tokens over the time spent *inside* model calls,
    `message_start` to `message_stop`, summed. `message_start` arrives with
    the response's first bytes, so this is the generation rate: the wait
    before it is what TTFT reports, not part of this.
  * **calls**: model requests the turn made (`message_start`s).

Subagent calls are left out (console.py only passes top-level events): they
run on other models, often in parallel, and would blur both numbers.

Shown only behind Settings → "Stats for nerds"; computed always, since it's a
few clock reads per event.
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
        """The footer's numbers, or None when nothing was timed (a turn with
        no stream events: partial messages off, or an error before any).
        Starts the count over, so a turn the CLI begins on its own (a
        background task finishing) is timed from zero, with no TTFT."""
        calls, out, gen_s = self.calls, self.out, self.gen_s
        ttft = (round((self.first_at - self.sent_at) * 1000)
                if self.first_at is not None and self.sent_at is not None else None)
        self.reset()
        if not calls:
            return None
        rate = round(out / gen_s, 1) if gen_s > 0 and out else None
        return {"ttft_ms": ttft, "tok_s": rate, "calls": calls, "out": out or None}
