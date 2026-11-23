"""One way to ask the model a one-shot question.

Five callers wanted the same thing — the brief, email triage, email
extraction, reply drafts, attachment vision: run the subscription CLI with no
tools for a single turn and hand back the text. Each carried its own copy of
the loop, so each also carried its own copy of the loop's one sharp edge.

The edge: when a turn fails, the CLI emits a result message and *then* exits
non-zero, on purpose, for shell-script consumers. The SDK replaces that bare
"exit code 1" with the result's error text, and for an API failure that text
is the result's `subtype` — the literal word "success" (the turn ran; the API
call inside it didn't). So every caller rendered "Claude Code returned an
error result: success" at the user, which says nothing and blames the wrong
thing.

The result message is already in hand and knows what happened: `is_error`
with an `api_error_status` of 429 means the subscription window is spent, and
no retry inside that window is going to help. `ask()` translates that and
raises it instead. Nothing here talks to the API directly — subscription CLI
only, same posture as before: no tools, one turn, so untrusted text in a
prompt reaches a model with nothing to act with.

A 429 also trips a breaker. Every caller here is background-ish and retries
on its own clock — triage every poller cycle, the brain's lazy catch-up, a
brief someone keeps re-asking for — and each retry inside a spent window
spawned a whole CLI process just to be told 429 again (104 of them in one
log). So after a 429, ask() refuses up front with the same message until the
window's reset, or for a short cooldown when no usage source can say when
that is. The reset time is trusted only as far as `_reset_hint` trusts it,
and capped besides (BREAKER_MAX_SECONDS): a wrong reading costs at most one
probe per cap, never a day of silence. Any successful call clears it.

The breaker waits for the wall; the budget gate stops short of it. Every
caller here runs on its own unless it says otherwise, and none of that work
is urgent — triage and extraction catch up next cycle, the brain next open —
so once a window passes AUTO_PAUSE_PCT they stand down and leave the rest of
it for the console and for buttons. A path a person asked for (a reply
draft, a dropped file, Regenerate) runs inside `interactive()`, which the
gate ignores; the breaker still applies there, since a 429 is a 429.
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

# How long to stop asking after a 429 that no usage source can date. Short:
# it only has to absorb the retry storm, not guess the window.
BREAKER_FALLBACK_SECONDS = 15 * 60
# The longest a reported reset is believed before one real call probes again.
# The weekly window's reset can be days out, and the caches behind it can be
# wrong (or extra usage can be bought) — an hourly probe is cheap insurance.
BREAKER_MAX_SECONDS = 60 * 60

# (until epoch seconds, message) while calls are known to be futile.
_tripped: tuple[float, str] | None = None

# HTTP status of the failing API call -> what it means to whoever asked. 429
# is the one you actually hit on a subscription; the rest are worth a retry.
API_ERRORS = {
    429: "usage limit reached",
    500: "the API errored (500) — worth retrying",
    529: "the API is overloaded — worth retrying",
}


class AgentError(RuntimeError):
    """A one-shot call that produced no text. The message is written to be
    shown as-is — these land in `daily_brief.error` and on the desk."""


def _reset_at() -> float | None:
    """When the spent window resets (epoch seconds), when the usage sources
    can say so.

    Best-effort on purpose: every source behind console_usage is a cache that
    can be hours old, and a confidently wrong reset time is worse than no reset
    time. So only a window that is actually out of budget gets to speak, and
    only while its reset is still ahead of us — a "rejected" whose reset has
    already passed is a stale reading of a window that has since rolled over,
    and repeating its time would promise a deadline that went by this
    morning."""
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
    """" — resets 3:20 PM"; the weekday too once it's far enough out to be
    another day (the week's window)."""
    if at is None:
        return ""
    fmt = "%a %-I:%M %p" if at - time.time() > 12 * 3600 else "%-I:%M %p"
    return f" — resets {datetime.fromtimestamp(at).strftime(fmt)}"


def _reset_hint() -> str:
    """" — resets 3:20 PM", or "" when no source can say (see _reset_at)."""
    return _format_reset(_reset_at())


# --- the budget gate -----------------------------------------------------------

# Set by interactive(); a ContextVar so it follows the call into
# asyncio.to_thread and asyncio.run, which both copy the context.
_interactive: ContextVar[bool] = ContextVar("agent_interactive", default=False)

# How long one reading of the windows answers the gate. The poller asks per
# batch, and the CLI cache behind the reading is a multi-MB JSON file.
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


def over_budget(fetch: bool = True) -> str | None:
    """Why automatic calls are paused right now, or None.

    Fails open: no reading means no pause — the breaker still catches a real
    429. A window whose reset has passed is void (it rolled over), not full.
    A stale reading can only be low, since usage climbs until the reset, so
    trusting it never pauses anything that shouldn't be."""
    global _budget
    now = time.time()
    if _budget is not None and now - _budget[0] < BUDGET_TTL_SECONDS:
        return _budget[1]
    try:
        windows = _budget_windows(fetch)
    except Exception:  # a gate that raises would stop everything
        log.debug("usage windows unavailable for the budget gate", exc_info=True)
        windows = []
    why = None
    for w in windows:
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
    """Why a call would be refused right now — the breaker's usage-limit
    message, or (outside interactive()) the budget gate's — else None. For
    callers that want to skip expensive set-up (gathering context, fetching
    bodies) before a call that is going to be refused anyway.

    On the event loop the gate reads the cached windows only; off it (the
    poller's threads, ask()) it may refresh them."""
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
    # A cut-off the CLI words itself ("You've hit your session limit · resets
    # 2:10pm", "Failed to authenticate: OAuth session expired") arrives with
    # no status and subtype "success", the reason only in the result text —
    # which this used to drop for "the model call failed (success)".
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

    `prompt` is a string, or the SDK's async-iterable message form when the
    question carries an image (a plain string prompt cannot hold one).

    `thinking` is the SDK's ThinkingConfig, left to the CLI's default when
    None. A long extraction wants {"type": "disabled"}: on the Brain's first
    dry run the default spent the whole 32k output budget thinking over a
    week of texts and never reached the JSON."""
    global _tripped
    # Off the loop, so the gate may refresh its reading without stalling
    # whatever loop this is (the backend's, for the brain and the brief line).
    if (why := await asyncio.to_thread(blocked)) is not None:
        raise AgentError(why)
    # The SDK is heavy — import lazily, so a backend that never asks anything
    # never pays for it.
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
        # The non-zero exit that trails an error result — expected, and less
        # informative than the result itself, which we already have. Anything
        # else is a real crash and belongs to the caller.
        if result is None or not result.is_error:
            raise
        log.debug("post-error-result exit, superseded by the result: %s", e)

    if result is not None and result.is_error:
        if _is_limit(result):
            raise AgentError(_trip())
        raise AgentError(_why(result))
    _tripped = None  # a call got through: whatever tripped it has passed
    return "".join(chunks)
