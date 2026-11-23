"""Background refresh loop — an asyncio task in the FastAPI app (no celery).

Every PROVIDER_REFRESH_SECONDS it calls each registered provider's refresh()
(source -> app.db cache) in a worker thread, so widget reads and CLI calls
are always cache-fast. A provider failing (rate limit, expired token, wifi)
logs and never kills the loop or the app. Providers refresh concurrently —
cycle time is the slowest provider, not the sum, and one hung provider no
longer stalls the rest.
"""

import asyncio
import time
import traceback
from collections.abc import AsyncIterator

from . import REGISTRY, heartbeat
from .. import failures
from ..config import PROVIDER_REFRESH_SECONDS


def _refresh_sync(name: str, fn) -> object:
    """One provider's refresh, plus its health row. Runs whole in the worker
    thread — the sqlite write would otherwise block the event loop for up to
    busy_timeout while twelve providers land at once."""
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
    """Run one refresh cycle across every refreshable provider, concurrently,
    and return {name: info | {"error": ...}}. Shared by the background loop
    and the on-demand POST /api/sync — a failing provider never sinks the
    others (each is already guarded in _refresh_one)."""
    items = _refreshable()
    results = await asyncio.gather(*(_refresh_one(n, fn) for n, fn in items))
    return {name: info for (name, _), info in zip(items, results)}


def _reconcile_alarms_sync() -> object:
    """§21 alarm reconcile, in a worker thread like the providers — it opens
    app.db and would otherwise block the event loop on the same write lock."""
    from ..alarms import reconcile
    return reconcile()


def _flush_notify_sync() -> object:
    """§21 step 4's at-desk queue, drained last in the cycle so a banner a
    provider enqueued a moment ago goes out in the same cycle rather than
    waiting for the next one. Same worker-thread and same guard as the alarms
    reconcile — this one shells out to osascript, so it has more ways to hang
    than to fail."""
    from ..notify import flush
    return flush()


def _reconcile_wake_sync() -> object:
    """§21 step 5's scheduled wake, last because it reads the alarms table that
    _reconcile_alarms_sync just wrote — running it first would schedule wakes
    for the previous cycle's alarms. Shells out to pmset, so same guard as the
    others: a power-management refusal must not cost the cycle."""
    from ..wake import reconcile
    return reconcile()


def _backup_sync() -> object:
    """This month's app.db snapshot, if it hasn't been taken yet — the lazy
    stand-in for the nightly cron that was never installed (see app/backup.py).

    Last in the cycle, and not because it depends on anything above it: it is
    the only step that copies the whole database, so running it earlier would
    put a hundred megabytes of I/O in front of a notification banner eleven
    times out of twelve for nothing. Same worker thread and same guard as the
    others — a full disk must not cost the cycle.
    """
    from ..backup import run
    info = run()
    # Printed on the cycle that takes one and silent on the eight thousand
    # that don't: a hundred megabytes moving is worth a line, and "did this
    # ever actually run" should be answerable from the log rather than only
    # from a directory listing.
    if "skipped" not in info:
        print(f"[backup] {info}")
    return info


def _selftexts_sync() -> object:
    """Route texts you sent yourself (app/selftexts.py). After the providers,
    because it reads what the messages provider just synced; last, because
    it may spend a model call per burst and nothing above should wait on
    that."""
    from ..selftexts import run
    return run()


def _guarded(label: str, fn) -> object:
    """Run one post-provider step; a failure becomes `{"error": ...}` and a
    row in the failure log, never a lost cycle. The steps used to print and
    nothing else, so an alarm reconcile failing every cycle for a week left no
    trace outside a /tmp log — the gap failures.py exists to close."""
    try:
        return fn()
    except Exception as e:  # never sink the cycle over one step
        print(f"[{label}] FAILED: {e!r}")
        failures.record("poller", "step", label,
                        detail=repr(e), trace=traceback.format_exc())
        return {"error": repr(e)}


# The steps after the providers, in the order both sync_all() and
# refresh_stream() run them — see each function for why it sits where it
# does. Lambdas, not the functions themselves, so the name is looked up when
# the step runs: the sync-route tests stub these module attributes, and a
# tuple holding the originals would run the real ones against the real app.db.
POST_STEPS = (
    ("alarms", lambda: _reconcile_alarms_sync()),
    ("notify", lambda: _flush_notify_sync()),
    ("wake", lambda: _reconcile_wake_sync()),
    ("backup", lambda: _backup_sync()),
    ("selftexts", lambda: _selftexts_sync()),
)


async def sync_all() -> dict:
    """A whole cycle: every provider, the §21 alarm reconcile, the at-desk
    queue, then the wake schedule that keeps the next cycle possible.

    Kept distinct from refresh_all() rather than folded into it. `refresh_all`
    means "provider name -> info" and callers read it that way; alarms are not a
    provider (no range, no metric, no source of their own) and adding a key that
    is not one would make the map mean two things. Reconciling runs *after* the
    providers and not alongside them because alarms are derived from the caches
    those providers write — diffing mid-cycle would read half-updated trips and
    churn a fire time that was never wrong.
    """
    out = await refresh_all()
    for label, fn in POST_STEPS:
        out[label] = await asyncio.to_thread(_guarded, label, fn)
    return out


async def refresh_stream() -> AsyncIterator[dict]:
    """refresh_all(), but yielding an event as each provider lands, so the UI
    can show a sync in progress instead of a spinner that means nothing.

    Providers still run concurrently — this only changes when results are
    reported, not how they're run. Events: one `start` with the roster, one
    `provider` per finish, one `done` at the end.
    """
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

    # Its own event rather than a `provider` one: alarms are not a source and
    # the UI's pending list is built from the `start` roster, which never named
    # them. An unrecognised event is ignored by the strip, so this is additive.
    for label, fn in POST_STEPS:
        yield {"event": label, "info": await asyncio.to_thread(_guarded, label, fn)}

    yield {"event": "done", "elapsed_ms": round((time.monotonic() - began) * 1000)}


async def refresh_loop() -> None:
    """The cycle, forever. Guarded as a whole and not just per provider: a
    failure *outside* _refresh_sync's own try (a thread pool that can't hand
    out a worker, an OOM in gather) would otherwise end the task silently, and
    a dead poller looks exactly like a quiet day — every cache simply stops
    advancing, with nothing on /api/status to say why."""
    while True:
        try:
            await sync_all()
        except asyncio.CancelledError:
            raise                       # shutdown, not a fault
        except Exception as e:          # the loop outlives it
            print(f"[providers] refresh cycle FAILED: {e!r}")
            heartbeat.log_event("poller", "broke", repr(e))
        await asyncio.sleep(PROVIDER_REFRESH_SECONDS)
