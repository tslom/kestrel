"""Serving the cache when the network isn't there.

A read's live backfill runs inside best_effort(): a network-shaped failure is noted and
swallowed, the read answers from cache, and providers.get() stamps `stale: true`. An expired
token or a bug still raises. Never writes provider_health (the poller records outages).
Design: DESIGN.md → `app/providers/offline.py`.
"""

import http.client
import socket
import ssl
import threading
import urllib.error
from collections.abc import Iterator
from contextlib import contextmanager

NETWORK_ERRORS = (urllib.error.URLError, http.client.HTTPException,
                  ConnectionError, TimeoutError, socket.gaierror, ssl.SSLError)

_local = threading.local()


def is_network_error(exc: BaseException) -> bool:
    """A lost network, not an answer: HTTPError (401, 429) means the source replied and must surface."""
    if isinstance(exc, urllib.error.HTTPError):
        return False
    return isinstance(exc, NETWORK_ERRORS)


def _sink() -> list[str] | None:
    return getattr(_local, "sink", None)


@contextmanager
def collect() -> Iterator[list[str]]:
    """Gather the degradations noted during one read; thread-local (concurrent reads), nests."""
    outer = _sink()
    sink: list[str] = []
    _local.sink = sink
    try:
        yield sink
    finally:
        _local.sink = outer


@contextmanager
def best_effort(what: str) -> Iterator[None]:
    """Run a live fetch the cached read can answer without; `what` names the fetch (it reaches a tooltip)."""
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
