"""agent.ask — the one-shot model call the brief, triage, extraction, reply
drafts and attachment vision all go through.

What's worth pinning is the failure path, because it's the one that reaches a
user. When a turn fails, the CLI emits a result message and *then* exits
non-zero on purpose; the SDK replaces that bare exit with the result's error
text, which for an API failure is the result's `subtype` — the literal word
"success". The desk read "Brief failed: Claude Code returned an error result:
success" for a spent usage window, which names neither the cause nor the fix.

So: the result message wins over the exit that trails it, and a genuine crash
with no error result still propagates untouched — losing a real traceback to
this translation would be a far worse bug than the one it fixes.
"""

import asyncio
import time
from datetime import datetime

import claude_agent_sdk
import pytest
from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock
from claude_agent_sdk._errors import ProcessError

from app import agent

# The trailing exit the CLI makes after any error result. Carries no
# information beyond "exit code 1" — that's the whole point.
EXIT = ProcessError("Command failed with exit code 1", 1)


def result(**kw):
    """A ResultMessage with the fields the CLI always sets, error by default."""
    return ResultMessage(**{"subtype": "success", "duration_ms": 1,
                            "duration_api_ms": 1, "is_error": True,
                            "num_turns": 1, "session_id": "s", **kw})


@pytest.fixture(autouse=True)
def closed_breaker(monkeypatch):
    """The breaker is module state; no test may inherit another's 429."""
    monkeypatch.setattr(agent, "_tripped", None)


@pytest.fixture
def transcript(monkeypatch):
    """Drive agent.ask with an exact CLI message sequence."""
    def install(*, blocks=(), res=None, boom=None):
        async def fake_query(*, prompt, options):
            for text in blocks:
                yield AssistantMessage(content=[TextBlock(text=text)], model="m")
            if res is not None:
                yield res
            if boom is not None:
                raise boom
        monkeypatch.setattr(claude_agent_sdk, "query", fake_query)
    return install


def ask():
    return asyncio.run(agent.ask("hi", model="m"))


def test_text_blocks_join_in_order(transcript):
    transcript(blocks=["one ", "two"], res=result(is_error=False))
    assert ask() == "one two"


def test_a_spent_window_says_so_instead_of_saying_success(transcript,
                                                          monkeypatch):
    """The bug this module exists for: 429 with subtype "success"."""
    monkeypatch.setattr(agent, "_reset_at", lambda: None)
    transcript(res=result(api_error_status=429), boom=EXIT)
    with pytest.raises(agent.AgentError, match="usage limit reached"):
        ask()


def test_the_reset_time_rides_along_when_the_cache_knows_it(transcript,
                                                            monkeypatch):
    at = time.time() + 20 * 60
    monkeypatch.setattr(agent, "_reset_at", lambda: at)
    transcript(res=result(api_error_status=429), boom=EXIT)
    expected = datetime.fromtimestamp(at).strftime("%-I:%M %p")
    with pytest.raises(agent.AgentError,
                       match=rf"usage limit reached — resets {expected}"):
        ask()


def test_a_stale_usage_cache_costs_the_hint_not_the_error(monkeypatch):
    """_reset_hint reads caches that can be half-written or hours old. Any of
    that going wrong must never turn a legible rate-limit message into a
    traceback."""
    def broken():
        raise RuntimeError("~/.claude.json was half-written")
    monkeypatch.setattr("app.claude_quota.console_usage", broken)
    assert agent._reset_hint() == ""


def test_a_reset_that_already_passed_is_not_offered_as_a_deadline(monkeypatch):
    """A "rejected" window whose reset has gone by is a stale reading of a
    window that has since rolled over. Repeating its time would promise a
    deadline that expired this morning — say nothing instead."""
    monkeypatch.setattr("app.claude_quota.console_usage", lambda: {"windows": [
        {"key": "five_hour", "status": "rejected",
         "resets_at": time.time() - 3.5 * 3600},
    ]})
    assert agent._reset_hint() == ""


def test_a_reset_still_ahead_is_worth_saying(monkeypatch):
    at = time.time() + 90 * 60
    monkeypatch.setattr("app.claude_quota.console_usage", lambda: {"windows": [
        {"key": "five_hour", "status": "rejected", "resets_at": at},
    ]})
    expected = datetime.fromtimestamp(at).strftime("%-I:%M %p")
    assert agent._reset_hint() == f" — resets {expected}"


@pytest.mark.parametrize("res,expected", [
    (result(api_error_status=529), "overloaded"),
    (result(api_error_status=418), "the API returned 418"),
    (result(subtype="error_max_turns"), "ran out of turns"),
    (result(errors=["model overloaded", "retry"]), "model overloaded; retry"),
    # No status, subtype "success", the reason only in the text.
    (result(result="Failed to authenticate: OAuth session expired"),
     "OAuth session expired"),
])
def test_every_failure_shape_names_itself(transcript, res, expected):
    transcript(res=res, boom=EXIT)
    with pytest.raises(agent.AgentError, match=expected):
        ask()


def test_a_real_crash_is_not_dressed_up_as_a_model_error(transcript):
    """No error result to supersede it, so the exception is the truth."""
    transcript(boom=RuntimeError("transport died"))
    with pytest.raises(RuntimeError, match="transport died"):
        ask()


#
# After a 429 every caller retries on its own clock — triage each poller
# cycle, the brain's catch-up, a re-asked brief — and each retry spawned a
# whole CLI just to be told 429 again. The breaker refuses up front instead.

@pytest.fixture
def spawns(monkeypatch):
    """Count CLI spawns; each call replays whatever `reply` currently holds."""
    reply = {"res": result(api_error_status=429), "boom": EXIT, "blocks": ()}
    n = {"spawns": 0, "reply": reply}

    async def fake_query(*, prompt, options):
        n["spawns"] += 1
        for text in reply["blocks"]:
            yield AssistantMessage(content=[TextBlock(text=text)], model="m")
        yield reply["res"]
        if reply["boom"] is not None:
            raise reply["boom"]
    monkeypatch.setattr(claude_agent_sdk, "query", fake_query)
    return n


@pytest.fixture
def clock(monkeypatch):
    now = {"t": 1_800_000_000.0}
    monkeypatch.setattr(agent.time, "time", lambda: now["t"])
    return now


def test_after_a_429_the_next_call_spawns_nothing(spawns, clock, monkeypatch):
    at = clock["t"] + 30 * 60
    monkeypatch.setattr(agent, "_reset_at", lambda: at)
    with pytest.raises(agent.AgentError, match="usage limit reached") as first:
        ask()
    with pytest.raises(agent.AgentError) as second:
        ask()
    assert spawns["spawns"] == 1
    assert str(second.value) == str(first.value)  # same legible message
    assert agent.blocked() == str(first.value)


def test_the_breaker_closes_at_the_reported_reset(spawns, clock, monkeypatch):
    at = clock["t"] + 30 * 60
    monkeypatch.setattr(agent, "_reset_at", lambda: at)
    with pytest.raises(agent.AgentError):
        ask()
    clock["t"] = at - 1
    with pytest.raises(agent.AgentError):
        ask()
    assert spawns["spawns"] == 1
    clock["t"] = at
    spawns["reply"].update(res=result(is_error=False), boom=None, blocks=("ok",))
    assert ask() == "ok"
    assert spawns["spawns"] == 2


def test_an_undated_429_holds_for_the_fallback_only(spawns, clock, monkeypatch):
    """No usage source can say when the window resets: a short cooldown, not
    a guess."""
    monkeypatch.setattr(agent, "_reset_at", lambda: None)
    with pytest.raises(agent.AgentError, match=r"^usage limit reached$"):
        ask()
    clock["t"] += agent.BREAKER_FALLBACK_SECONDS - 1
    assert agent.blocked() is not None
    clock["t"] += 1
    assert agent.blocked() is None


def test_a_far_reset_is_believed_only_up_to_the_cap(spawns, clock, monkeypatch):
    """A weekly reset days out — or a cache that is simply wrong — costs one
    probe per cap, never days of silence."""
    monkeypatch.setattr(agent, "_reset_at", lambda: clock["t"] + 4 * 86400)
    with pytest.raises(agent.AgentError):
        ask()
    clock["t"] += agent.BREAKER_MAX_SECONDS
    assert agent.blocked() is None
    with pytest.raises(agent.AgentError):   # still spent: a real probe, re-trips
        ask()
    assert spawns["spawns"] == 2
    assert agent.blocked() is not None


def test_a_stale_reset_never_opens_the_breaker_into_the_past(spawns, clock,
                                                              monkeypatch):
    """The real reader drops a "rejected" whose reset already passed, so the
    trip falls back to the cooldown rather than to a deadline gone by."""
    monkeypatch.setattr("app.claude_quota.console_usage", lambda: {"windows": [
        {"key": "five_hour", "status": "rejected", "resets_at": clock["t"] - 3600},
    ]})
    with pytest.raises(agent.AgentError, match=r"^usage limit reached$"):
        ask()
    assert agent._tripped[0] == clock["t"] + agent.BREAKER_FALLBACK_SECONDS


def test_a_limit_worded_only_in_the_text_trips_the_breaker(spawns, clock,
                                                          monkeypatch):
    """The CLI's own "You've hit your session limit" comes with no 429 status
    at all — it read "the model call failed (success)" and tripped nothing."""
    monkeypatch.setattr(agent, "_reset_at", lambda: None)
    spawns["reply"].update(res=result(
        result="You've hit your session limit · resets 2:10pm"))
    with pytest.raises(agent.AgentError, match=r"^usage limit reached$"):
        ask()
    with pytest.raises(agent.AgentError):
        ask()
    assert spawns["spawns"] == 1


def test_other_failures_do_not_trip_the_breaker(spawns, monkeypatch):
    spawns["reply"].update(res=result(api_error_status=529))
    for _ in range(2):
        with pytest.raises(agent.AgentError, match="overloaded"):
            ask()
    assert spawns["spawns"] == 2
    assert agent.blocked() is None


def test_a_success_clears_an_open_breaker(clock, monkeypatch):
    """A call already in flight when another one tripped the breaker came back
    fine — the window is open, so the trip must not outlive that proof."""
    async def fake_query(*, prompt, options):
        agent._tripped = (clock["t"] + 600, "usage limit reached")  # concurrent trip
        yield AssistantMessage(content=[TextBlock(text="ok")], model="m")
        yield result(is_error=False)
    monkeypatch.setattr(claude_agent_sdk, "query", fake_query)
    assert ask() == "ok"
    assert agent.blocked() is None


def test_a_crash_after_a_good_result_still_propagates(transcript):
    """The swallow is scoped to the exit that *trails an error result*. A turn
    that succeeded and then blew up is a fresh failure, not that exit."""
    transcript(blocks=["done"], res=result(is_error=False),
               boom=RuntimeError("transport died"))
    with pytest.raises(RuntimeError, match="transport died"):
        ask()
