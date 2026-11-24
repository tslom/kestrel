"""agent's budget gate: automatic calls stand down past AUTO_PAUSE_PCT; interactive() calls pass,
a rolled-over window never pauses, no reading fails open, a paused call spawns no CLI."""

import asyncio
import time

import claude_agent_sdk
import pytest
from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock

from app import agent


@pytest.fixture(autouse=True)
def limits(monkeypatch):
    monkeypatch.setattr(agent, "AUTO_PAUSE_PCT", {"five_hour": 80, "seven_day": 85})
    monkeypatch.setattr(agent, "_tripped", None)


@pytest.fixture
def windows(monkeypatch):
    """Set the reading the gate sees; records whether it was allowed to fetch."""
    state = {"windows": [], "fetches": []}

    def read(fetch):
        state["fetches"].append(fetch)
        return state["windows"]
    monkeypatch.setattr(agent, "_budget_windows", read)
    return state


@pytest.fixture
def spawns(monkeypatch):
    n = {"spawns": 0}

    async def fake_query(*, prompt, options):
        n["spawns"] += 1
        yield AssistantMessage(content=[TextBlock(text="ok")], model="m")
        yield ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1,
                            is_error=False, num_turns=1, session_id="s")
    monkeypatch.setattr(claude_agent_sdk, "query", fake_query)
    return n


def win(key, pct, resets_in=3600, label=None):
    return {"key": key, "label": label or key, "percent": pct,
            "resets_at": time.time() + resets_in}


def ask():
    return asyncio.run(agent.ask("q", model="m"))


def test_a_full_week_pauses_background_calls_without_spawning(windows, spawns):
    windows["windows"] = [win("five_hour", 20, label="5h"),
                          win("seven_day", 91, resets_in=3 * 86400, label="7d")]
    with pytest.raises(agent.AgentError, match=r"paused — 7d usage at 91% \(pauses at 85%\)"):
        ask()
    assert spawns["spawns"] == 0
    assert "paused" in agent.blocked()


def test_under_the_line_calls_go_through(windows, spawns):
    windows["windows"] = [win("five_hour", 79), win("seven_day", 84)]
    assert ask() == "ok"
    assert agent.blocked() is None


def test_an_interactive_call_ignores_the_gate(windows, spawns):
    windows["windows"] = [win("five_hour", 95)]
    with agent.interactive():
        assert agent.blocked() is None
        assert ask() == "ok"
    assert agent.blocked() is not None   # and the flag doesn't leak out


def test_interactive_follows_the_call_into_a_thread(windows, spawns):
    """The interactive flag survives the hop to a worker thread (Regenerate, attachment tool)."""
    windows["windows"] = [win("five_hour", 95)]

    async def go():
        with agent.interactive():
            return await asyncio.to_thread(agent.blocked)
    assert asyncio.run(go()) is None


def test_the_429_breaker_still_applies_to_interactive_calls(windows, spawns):
    agent._tripped = (time.time() + 600, "usage limit reached")
    with agent.interactive():
        assert agent.blocked() == "usage limit reached"


def test_a_rolled_over_window_is_void_not_full(windows):
    windows["windows"] = [win("five_hour", 99, resets_in=-60)]
    assert agent.blocked() is None


def test_no_reading_fails_open(windows):
    assert agent.blocked() is None
    windows["windows"] = [{"key": "five_hour", "percent": None, "resets_at": None}]
    agent._budget = None
    assert agent.blocked() is None


def test_unwatched_windows_never_pause(windows):
    """Credits and the per-model weeks aren't in AUTO_PAUSE_PCT."""
    windows["windows"] = [win("overage", 100), win("seven_day_opus", 100)]
    assert agent.blocked() is None


def test_a_broken_reading_fails_open(monkeypatch):
    def boom(fetch):
        raise RuntimeError("~/.claude.json was half-written")
    monkeypatch.setattr(agent, "_budget_windows", boom)
    assert agent.blocked() is None


def test_one_reading_answers_for_a_while(windows, monkeypatch):
    now = {"t": 1_800_000_000.0}
    monkeypatch.setattr(agent.time, "time", lambda: now["t"])
    windows["windows"] = [{"key": "five_hour", "percent": 90, "resets_at": now["t"] + 3600}]
    assert agent.blocked() is not None
    windows["windows"] = []
    now["t"] += agent.BUDGET_TTL_SECONDS - 1
    assert agent.blocked() is not None      # cached
    now["t"] += 2
    assert agent.blocked() is None          # re-read
    assert len(windows["fetches"]) == 2


def test_on_the_event_loop_the_gate_never_fetches(windows):
    """blocked() (called from routes) never does the 10s urlopen; ask(), on a thread, may."""
    async def on_loop():
        agent.blocked()
        agent._budget = None
        await asyncio.to_thread(agent.blocked)
    asyncio.run(on_loop())
    assert windows["fetches"] == [False, True]
