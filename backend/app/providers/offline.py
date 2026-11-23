"""Serving the cache when the network isn't there.

A widget read backfills any never-fetched days from the source before it
answers — health, weather, calendar, email and news all do. That backfill is
the one part of a read that fails for a reason the cache can outlive: a lid
opened thirty seconds ago with DNS not up yet, wifi mid-handover, a source
refusing the connection. Left unguarded it raised straight through
providers.get() into a bare 500 on every provider-backed widget at once, which
on the desk reads exactly like the backend having died — and sends you looking
for a process that was running the whole time.

So the backfill runs inside best_effort(). A network-shaped failure is noted
and swallowed, the read carries on against whatever the cache already held,
and providers.get() stamps the envelope `stale: true` with the reason. An
expired token or a bug still raises: those don't improve by being handed
yesterday's number, and the widget should say so out loud.

Nothing here writes to provider_health. The poller meets the same dead network
on its own cycle and records it there, which is what /api/status and the glance
strip's dot already read — a read path that also wrote would count one outage
twice, and count it again on every page load.
"""

import http.client
import socket
import ssl
import threading
import urllib.error
from collections.abc import Iterator
from contextlib import contextmanager

# Every way "there is no network" arrives here. urlopen wraps most of them in
# URLError; the rest reach us raw from a socket someone opened directly.
NETWORK_ERRORS = (urllib.error.URLError, http.client.HTTPException,
                  ConnectionError, TimeoutError, socket.gaierror, ssl.SSLError)

_local = threading.local()


def is_network_error(exc: BaseException) -> bool:
    """A lost network, as opposed to an answer you didn't like.

    HTTPError is a URLError subclass but means the source *replied* — 401 for a
    dead token, 429 for a rate limit. Those are real answers from a reachable
    service, and hiding them behind stale data is how a credential stays
    expired for a week.
    """
    if isinstance(exc, urllib.error.HTTPError):
        return False
    return isinstance(exc, NETWORK_ERRORS)


def _sink() -> list[str] | None:
    return getattr(_local, "sink", None)


@contextmanager
def collect() -> Iterator[list[str]]:
    """Gather the degradations noted during one read.

    Thread-local rather than global: FastAPI runs sync endpoints in a worker
    pool, so a dozen widget reads are in flight at once and a shared list would
    stamp one provider's outage onto another's envelope.

    Nests — an inner collect() takes the notes for its own block and the outer
    one keeps what it had, so a provider reading through another provider can't
    swallow the caller's.
    """
    outer = _sink()
    sink: list[str] = []
    _local.sink = sink
    try:
        yield sink
    finally:
        _local.sink = outer


@contextmanager
def best_effort(what: str) -> Iterator[None]:
    """Run a live fetch that the cached read can answer without.

    `what` names the fetch rather than the provider — it ends up in the
    envelope and then in a tooltip, so "health backfill" beats "sync".
    """
    try:
        yield
    except Exception as e:  # re-raised unless it's the network
        if not is_network_error(e):
            raise
        note = f"{what}: {e}"
        print(f"[providers] {note} — network down, serving cache")
        sink = _sink()
        if sink is not None and note not in sink:
            sink.append(note)
