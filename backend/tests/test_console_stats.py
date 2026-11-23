"""Stats for nerds (console_stats.py): TTFT and tok/s off the raw stream."""

from app.console_stats import TurnStats


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def test_ttft_and_rate_over_model_time_only():
    clk = Clock()
    s = TurnStats(clock=clk)
    s.sent()
    clk.t = 0.5
    s.event({"type": "message_start", "message": {"usage": {"output_tokens": 1}}})
    clk.t = 1.5
    s.event({"type": "content_block_start"})
    clk.t = 2.5
    s.event({"type": "message_delta", "usage": {"output_tokens": 100}})
    s.event({"type": "message_stop"})
    clk.t = 12.5   # a ten-second tool run: not model time
    s.event({"type": "message_start", "message": {"usage": {}}})
    s.event({"type": "content_block_start"})
    clk.t = 14.5
    s.event({"type": "message_delta", "usage": {"output_tokens": 100}})
    s.event({"type": "message_stop"})
    assert s.finish() == {"ttft_ms": 1500, "tok_s": 50.0, "calls": 2, "out": 200}
    # Started over: a turn the CLI begins itself has no TTFT.
    assert s.finish() is None
    s.event({"type": "message_start"})
    clk.t = 15.5
    s.event({"type": "message_delta", "usage": {"output_tokens": 10}})
    s.event({"type": "message_stop"})
    assert s.finish()["ttft_ms"] is None
