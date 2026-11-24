"""providers.offline — the read path surviving a missing network.

Network-shaped failures are swallowed (cache answers, stamped stale); reachable-source errors still raise.
Design: DESIGN.md → `providers/offline.py`: test-pinned behaviours.
"""

import socket
import types
import urllib.error

import pytest

from app.providers import offline


def _urlerror() -> urllib.error.URLError:
    """A dead resolver as it reaches us: urlopen wrapping gaierror."""
    return urllib.error.URLError(
        socket.gaierror(8, "nodename nor servname provided, or not known"))


def _http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError("https://api.example", code, "nope", {}, None)


def test_swallows_network_error_and_notes_it():
    with offline.collect() as degraded:
        with offline.best_effort("health backfill"):
            raise _urlerror()
    assert len(degraded) == 1
    assert "health backfill" in degraded[0]


def test_body_after_a_swallowed_fetch_still_runs():
    """The cached read below the backfill continues."""
    reached = []
    with offline.collect():
        with offline.best_effort("weather backfill"):
            raise _urlerror()
        reached.append("cache read")
    assert reached == ["cache read"]


@pytest.mark.parametrize("exc", [
    ConnectionRefusedError("no route"),
    TimeoutError("timed out"),
    socket.gaierror(8, "nodename nor servname"),
])
def test_every_network_shape_is_swallowed(exc):
    with offline.collect() as degraded:
        with offline.best_effort("fetch"):
            raise exc
    assert len(degraded) == 1


def test_http_error_still_raises():
    """A 401 from a reachable source still raises."""
    with pytest.raises(urllib.error.HTTPError):
        with offline.collect():
            with offline.best_effort("email backfill"):
                raise _http_error(401)


def test_value_error_still_raises():
    """A ValueError (from oauth.NotAuthorized) is a 400, not a stale read."""
    with pytest.raises(ValueError):
        with offline.collect():
            with offline.best_effort("calendar backfill"):
                raise ValueError("no tokens for google/x@y.z")


def test_notes_do_not_leak_between_reads():
    with offline.collect() as first:
        with offline.best_effort("health backfill"):
            raise _urlerror()
    with offline.collect() as second:
        pass
    assert len(first) == 1 and second == []


def test_nested_collect_keeps_the_outer_notes():
    """A provider reading through another keeps the caller's notes."""
    with offline.collect() as outer:
        with offline.best_effort("outer fetch"):
            raise _urlerror()
        with offline.collect() as inner:
            with offline.best_effort("inner fetch"):
                raise _urlerror()
    assert len(outer) == 1 and "outer fetch" in outer[0]
    assert len(inner) == 1 and "inner fetch" in inner[0]


def _mod(payload, raises=None):
    """A stand-in provider module with the provider interface."""
    def get(rng, metric=None):
        if raises is not None:
            with offline.best_effort("stub backfill"):
                raise raises
        return {"metric": metric, "data": payload}
    return types.SimpleNamespace(
        NAME="stub_offline", METRICS=None, DEFAULT_METRIC=None,
        ALLOW_FUTURE=False, get=get)


def test_envelope_is_clean_when_the_network_is_up(monkeypatch):
    from app import providers
    monkeypatch.setitem(providers.REGISTRY, "stub_offline", _mod([{"day": "x"}]))
    out = providers.get("stub_offline", "today")
    assert "stale" not in out and "offline" not in out


def test_envelope_is_stamped_stale_when_the_backfill_could_not_reach(monkeypatch):
    from app import providers
    monkeypatch.setitem(providers.REGISTRY, "stub_offline",
                        _mod([{"day": "x"}], raises=_urlerror()))
    out = providers.get("stub_offline", "today")
    assert out["stale"] is True
    assert "stub backfill" in out["offline"][0]
    assert out["data"] == [{"day": "x"}]   # cached rows still served
