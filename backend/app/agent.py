"""One way to ask the model a one-shot question: subscription CLI, no tools, one turn, text back.

Owns the 429 translation (`AgentError`, shown as-is on the desk), the breaker that refuses
calls until a spent window resets, and the budget gate (`AUTO_PAUSE_PCT`) that pauses
automatic callers; `interactive()` exempts person-asked paths from the gate, not the breaker.
Design: DESIGN.md → `app/agent.py`.
"""
import asyncio
import logging
import re
import time
from collections.abc import AsyncIterable
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime

from .config import AUTO_PAUSE_PCT

log = logging.getLogger(__name__)

# Pause after a 429 no usage source can date: short, it only has to absorb the retry storm.
BREAKER_FALLBACK_SECONDS = 15 * 60
# Longest a reported reset is believed before one real call probes (caches can be wrong; extra usage can be bought).
BREAKER_MAX_SECONDS = 60 * 60

# (until epoch seconds, message) while calls are known to be futile.
_tripped: tuple[float, str] | None = None

# HTTP status of the failing API call -> message for the asker; only 429 is futile to retry.
API_ERRORS = {
    429: "usage limit reached",
    500: "the API errored (500) — worth retrying",
    529: "the API is overloaded — worth retrying",
}


class AgentError(RuntimeError):
    """A one-shot call that produced no text. The message is shown as-is (`daily_brief.error`, the desk)."""


def _reset_at() -> float | None:
    """When the spent window resets (epoch seconds), if the usage sources can say.

    Best-effort: sources can be hours old and a wrong reset is worse than none, so only a
    "rejected" window whose reset is still ahead speaks."""
    try:
        # Local import, so tests can patch claude_quota.console_usage.
        from .claude_quota import console_usage
        for w in console_usage().get("windows") or []:
            if w.get("status") != "rejected" or not w.get("resets_at"):
                continue
            if w["resets_at"] <= time.time():
                continue
            return float(w["resets_at"])
    except Exception:  # a hint that raises is not a hint
        log.debug("usage reset hint unavailable", exc_info=True)
    return None


def _format_reset(at: float | None) -> str:
    """" — resets 3:20 PM", with the weekday once it's over 12h out."""
    if at is None:
        return ""
    fmt = "%a %-I:%M %p" if at - time.time() > 12 * 3600 else "%-I:%M %p"
    return f" — resets {datetime.fromtimestamp(at).strftime(fmt)}"


def _reset_hint() -> str:
    """" — resets 3:20 PM", or "" when no source can say."""
    return _format_reset(_reset_at())


# --- the budget gate -----------------------------------------------------------

# A ContextVar so it follows into asyncio.to_thread / asyncio.run (both copy the context).
_interactive: ContextVar[bool] = ContextVar("agent_interactive", default=False)

# How long one reading answers the gate (the poller asks per batch; the CLI cache is a multi-MB file).
BUDGET_TTL_SECONDS = 60

# (read at, why or None) — the last reading, and what it said.
_budget: tuple[float, str | None] | None = None


@contextmanager
def interactive():
    """Mark the calls inside as asked-for: the budget gate lets them through."""
    token = _interactive.set(True)
    try:
        yield
    finally:
        _interactive.reset(token)


def _on_loop() -> bool:
    try:
        asyncio.get_running_loop()
        return True
    except RuntimeError:
        return False


def _budget_windows(fetch: bool) -> list[dict]:
    """The usage windows, as the console bar sees them. Tests patch this."""
    from .claude_quota import usage_snapshot
    return usage_snapshot(fetch=fetch).get("windows") or []


def _accounts_refusal() -> str | None:
    """The saved accounts' rules (claude_accounts): when none is inside its
    rules, this guard is the one that stops. Tests patch this."""
    from .claude_accounts import refusal
    why = refusal()
    return f"automatic model calls paused — {why}" if why else None


def over_budget(fetch: bool = True) -> str | None:
    """Why automatic calls are paused right now, or None.

    Fails open (the breaker still catches a real 429). A window past its reset is void, not full.
    A stale reading can only be low, so trusting it never wrongly pauses."""
    global _budget
    now = time.time()
    if _budget is not None and now - _budget[0] < BUDGET_TTL_SECONDS:
        return _budget[1]
    try:
        windows = _budget_windows(fetch)
    except Exception:  # a gate that raises would stop everything
        log.debug("usage windows unavailable for the budget gate", exc_info=True)
        windows = []
    why = _accounts_refusal()
    for w in windows if why is None else []:
        limit, pct, at = AUTO_PAUSE_PCT.get(w.get("key")), w.get("percent"), w.get("resets_at")
        if limit is None or not isinstance(pct, (int, float)):
            continue
        if at is not None and at <= now:
            continue
        if pct >= limit:
            why = (f"automatic model calls paused — {w.get('label') or w['key']} usage"
                   f" at {pct:.0f}% (pauses at {limit:.0f}%){_format_reset(at)}")
            break
    if why != (_budget[1] if _budget else None):
        if why:
            log.warning("%s", why)
        elif _budget is not None:
            log.warning("automatic model calls resumed")
    _budget = (now, why)
    return why


def blocked() -> str | None:
    """Why a call would be refused now (breaker, or outside interactive() the budget gate), else None.
    For skipping expensive set-up. On the event loop it reads cached windows only; off it, it may refresh."""
    global _tripped
    if _tripped is not None:
        until, why = _tripped
        if time.time() < until:
            return why
        _tripped = None  # time's up: the next call is a real probe
    if _interactive.get():
        return None
    return over_budget(fetch=not _on_loop())


def raise_if_blocked() -> None:
    """AgentError now, without spawning anything, while the breaker is open."""
    if (why := blocked()) is not None:
        raise AgentError(why)


def _trip() -> str:
    """Open the breaker after a 429; returns the message to raise."""
    global _tripped
    now = time.time()
    at = _reset_at()
    if at is None:
        until = now + BREAKER_FALLBACK_SECONDS
    else:
        until = min(at, now + BREAKER_MAX_SECONDS)
    why = API_ERRORS[429] + _format_reset(at)
    _tripped = (until, why)
    log.warning("usage limit hit; holding model calls for %d min",
                round((until - now) / 60))
    return why


def _why(result) -> str:
    """The result message, in a sentence the caller can show."""
    if result.errors:
        return "; ".join(result.errors)
    if status := result.api_error_status:
        if status == 429:
            return API_ERRORS[429] + _reset_hint()
        return API_ERRORS.get(status, f"the API returned {status}")
    if result.subtype == "error_max_turns":
        return "the model ran out of turns"
    # CLI-worded cut-offs ("session limit · resets 2:10pm", "OAuth session expired") come with
    # no status and subtype "success"; the reason is only in the result text.
    if text := str(result.result or "").strip():
        return text
    return f"the model call failed ({result.subtype})"


# The CLI's own wording for a spent window, when it sends no 429 status.
_LIMIT_RE = re.compile(r"\b(session|usage|weekly|rate) limit\b", re.IGNORECASE)


def _is_limit(result) -> bool:
    return result.api_error_status == 429 or bool(
        _LIMIT_RE.search(str(result.result or "")))


async def ask(prompt: str | AsyncIterable[dict], *, model: str,
              system: str | None = None, thinking: dict | None = None) -> str:
    """One turn, no tools, text back.

    `prompt`: a string, or the SDK's async-iterable form when it carries an image. `thinking`:
    the SDK's ThinkingConfig (None = CLI default; long extractions want {"type": "disabled"}).
    `model`: any model_catalog name (tier, alias, id); one the account can't run is swapped
    (logged), and an unknown refusal is learnt and retried once on the fallback."""
    global _tripped
    # Off the loop so the gate may refresh its reading without stalling it.
    if (why := await asyncio.to_thread(blocked)) is not None:
        raise AgentError(why)
    from . import model_catalog
    chosen = model_catalog.pick(model)
    if chosen.notes:
        log.warning("model for a one-shot call: %s", "; ".join(chosen.notes))
    run_on = chosen.model

    for attempt in (1, 2):
        chunks, result = await _once(prompt, run_on, system, thinking)
        if result is None or not result.is_error:
            break
        said = " ".join([str(result.result or ""), *chunks])
        if attempt == 1 and model_catalog.refusal_in(said):
            # Not a spent window: this account may not run this model.
            alt = await asyncio.to_thread(model_catalog.learn_refusal, run_on or model, said)
            # An image prompt is an iterator the first try used up; the
            # next call picks the fallback by itself.
            if alt and isinstance(prompt, str):
                log.warning("%s refused on this account; asking %s instead", run_on, alt)
                run_on = alt
                continue
            raise AgentError(said.strip())
        if _is_limit(result):
            raise AgentError(_trip())
        raise AgentError(_why(result))
    _tripped = None  # a call got through: whatever tripped it has passed
    return "".join(chunks)


async def _once(prompt, model: str | None, system: str | None,
                thinking: dict | None) -> tuple[list[str], object]:
    """One query: (text chunks, the ResultMessage or None)."""
    # Lazy: the SDK is heavy.
    from claude_agent_sdk import (AssistantMessage, ClaudeAgentOptions,
                                  ResultMessage, TextBlock, query)

    options = ClaudeAgentOptions(
        model=model, system_prompt=system,
        tools=[],  # strips the built-in tool schemas from the prompt (~12k tokens)
        allowed_tools=[], max_turns=1,
        **({"thinking": thinking} if thinking is not None else {}))

    chunks: list[str] = []
    result = None
    try:
        async for msg in query(prompt=prompt, options=options):
            if isinstance(msg, AssistantMessage):
                chunks += [b.text for b in msg.content if isinstance(b, TextBlock)]
            elif isinstance(msg, ResultMessage):
                result = msg
    except Exception as e:
        # The non-zero exit that trails an error result is expected; anything else is a real crash.
        if result is None or not result.is_error:
            raise
        log.debug("post-error-result exit, superseded by the result: %s", e)
    return chunks, result
