"""Background refresh loop: an asyncio task in the FastAPI app.

Every PROVIDER_REFRESH_SECONDS each provider's refresh() runs concurrently in a worker
thread; a failure logs and never kills the loop. Post-provider steps run in POST_STEPS order.
Design: DESIGN.md → `app/providers/poller.py`: the refresh loop.
"""

import asyncio
import time
import traceback
from collections.abc import AsyncIterator

from . import REGISTRY, heartbeat
from .. import failures
from ..config import PROVIDER_REFRESH_SECONDS


def _refresh_sync(name: str, fn) -> object:
    """One provider's refresh plus its health row, whole in the worker thread (the sqlite write blocks)."""
    began = time.monotonic()
    try:
        info = fn()
    except Exception as e:  # keep the loop alive
        print(f"[providers] {name} refresh FAILED: {e!r}")
        heartbeat.record(name, ok=False, error=repr(e),
                         duration_ms=round((time.monotonic() - began) * 1000))
        return {"error": repr(e)}
    print(f"[providers] {name} refreshed: {info}")
    heartbeat.record(name, ok=True, info=info,
                     duration_ms=round((time.monotonic() - began) * 1000))
    return info


async def _refresh_one(name: str, fn) -> object:
    return await asyncio.to_thread(_refresh_sync, name, fn)


def _refreshable() -> list[tuple[str, object]]:
    return [(name, fn) for name, mod in REGISTRY.items()
            if (fn := getattr(mod, "refresh", None)) is not None]


async def refresh_all() -> dict:
    """One concurrent refresh of every provider -> {name: info | {"error": ...}}; shared with POST /api/sync."""
    items = _refreshable()
    results = await asyncio.gather(*(_refresh_one(n, fn) for n, fn in items))
    return {name: info for (name, _), info in zip(items, results)}


def _reconcile_alarms_sync() -> object:
    """Alarm reconcile, in a worker thread (it opens app.db)."""
    from ..alarms import reconcile
    return reconcile()


def _flush_notify_sync() -> object:
    """The at-desk queue, drained after the providers so their banners go out this cycle (shells out to osascript)."""
    from ..notify import flush
    return flush()


def _reconcile_wake_sync() -> object:
    """The scheduled wake; must follow _reconcile_alarms_sync, whose table it reads (shells out to pmset)."""
    from ..wake import reconcile
    return reconcile()


def _backup_sync() -> object:
    """This month's app.db snapshot if not yet taken (app/backup.py); late because it copies the whole database."""
    from ..backup import run
    info = run()
    # Print only on the cycle that takes one.
    if "skipped" not in info:
        print(f"[backup] {info}")
    return info


def _selftexts_sync() -> object:
    """Route texts you sent yourself (app/selftexts.py); after messages syncs, last (may spend a model call)."""
    from ..selftexts import run
    return run()


def _guarded(label: str, fn) -> object:
    """Run one post-provider step; a failure becomes `{"error": ...}` plus a failures row, never a lost cycle."""
    try:
        return fn()
    except Exception as e:  # never sink the cycle over one step
        print(f"[{label}] FAILED: {e!r}")
        failures.record("poller", "step", label,
                        detail=repr(e), trace=traceback.format_exc())
        return {"error": repr(e)}


# Post-provider steps, in order. Lambdas so the name resolves at run time: tests stub these attributes.
POST_STEPS = (
    ("alarms", lambda: _reconcile_alarms_sync()),
    ("notify", lambda: _flush_notify_sync()),
    ("wake", lambda: _reconcile_wake_sync()),
    ("backup", lambda: _backup_sync()),
    ("selftexts", lambda: _selftexts_sync()),
)


async def sync_all() -> dict:
    """A whole cycle: every provider, then POST_STEPS. Not folded into refresh_all(), whose map is provider -> info."""
    out = await refresh_all()
    for label, fn in POST_STEPS:
        out[label] = await asyncio.to_thread(_guarded, label, fn)
    return out


async def refresh_stream() -> AsyncIterator[dict]:
    """refresh_all() yielding an event as each provider lands: `start` (roster), `provider`, one per
    POST_STEPS label, `done`."""
    items = _refreshable()
    began = time.monotonic()
    yield {"event": "start", "providers": [n for n, _ in items]}

    pending = {asyncio.create_task(_refresh_one(n, fn)): n for n, fn in items}
    while pending:
        finished, _ = await asyncio.wait(
            pending, return_when=asyncio.FIRST_COMPLETED)
        for task in finished:
            name = pending.pop(task)
            info = task.result()
            error = info.get("error") if isinstance(info, dict) else None
            yield {"event": "provider", "provider": name,
                   "ok": error is None, "error": error, "info": info,
                   "elapsed_ms": round((time.monotonic() - began) * 1000)}

    # Own events, not `provider`: the UI's pending list comes from the `start` roster.
    for label, fn in POST_STEPS:
        yield {"event": label, "info": await asyncio.to_thread(_guarded, label, fn)}

    yield {"event": "done", "elapsed_ms": round((time.monotonic() - began) * 1000)}


async def refresh_loop() -> None:
    """The cycle, forever; guarded as a whole, since a silently dead poller looks like a quiet day."""
    while True:
        try:
            await sync_all()
        except asyncio.CancelledError:
            raise                       # shutdown, not a fault
        except Exception as e:          # the loop outlives it
            print(f"[providers] refresh cycle FAILED: {e!r}")
            heartbeat.log_event("poller", "broke", repr(e))
        await asyncio.sleep(PROVIDER_REFRESH_SECONDS)
