"""weather.current() — the glance strip's chip, and what it does when
Open-Meteo is down.

The case these exist for is one the `failures` table recorded on four
separate days: upstream answered 502, the route turned that into a 502 of its
own, and the chip went blank. A cached
reading is the fix, and the thing worth pinning is that it is never passed off
as live — `stale` and `as_of` have to survive, and a reading too old to mean
anything has to stop being served at all.
"""

from datetime import UTC, datetime, timedelta

import pytest

from app.providers import weather


@pytest.fixture(autouse=True)
def _clean_cache(monkeypatch):
    """current() memoises into a module global — reset it per test, or the
    first test's reading silently satisfies the later ones."""
    monkeypatch.setattr(weather, "_current_cache", None)
    monkeypatch.setattr(weather, "_current_origin", None)
    # No test here wants a real second of latency for the retry sleep.
    monkeypatch.setattr(weather.time, "sleep", lambda *_: None)


def _api_returning(*results):
    """A fake _api that walks `results`, raising any exception it finds. Lets a
    test say "fail, then succeed" without counting calls by hand."""
    calls = {"n": 0}
    seq = list(results)

    def fake(_url):
        i = min(calls["n"], len(seq) - 1)
        calls["n"] += 1
        out = seq[i]
        if isinstance(out, Exception):
            raise out
        return out
    return fake, calls


_LIVE = {"current": {"temperature_2m": 71.0, "weather_code": 0, "is_day": 1,
                     "time": "2026-09-13T17:00"}}


def _stub_surroundings(monkeypatch):
    monkeypatch.setattr(weather, "_origin", lambda: (34.02, -112.28))
    monkeypatch.setattr(weather, "day_summary", lambda: {"tmax": 80, "tmin": 60})


def test_a_good_reading_is_marked_live_and_cached(monkeypatch):
    _stub_surroundings(monkeypatch)
    fake, _ = _api_returning(_LIVE)
    monkeypatch.setattr(weather, "_api", fake)

    out = weather.current()
    assert out["temp"] == 71.0
    assert out["stale"] is False
    assert out["as_of"]
    assert weather._current_cache["temp"] == 71.0


def test_one_failure_is_retried_before_giving_up(monkeypatch):
    """The observed 502 is transient, so the second attempt is the whole point
    — a single-shot call would have blanked the chip on all four of those days."""
    _stub_surroundings(monkeypatch)
    fake, calls = _api_returning(RuntimeError("weather API 502: nope"), _LIVE)
    monkeypatch.setattr(weather, "_api", fake)

    out = weather.current()
    assert calls["n"] == 2
    assert out["temp"] == 71.0
    assert out["stale"] is False


def test_both_attempts_failing_serves_the_cache_marked_stale(monkeypatch):
    _stub_surroundings(monkeypatch)
    good, _ = _api_returning(_LIVE)
    monkeypatch.setattr(weather, "_api", good)
    weather.current()          # prime the cache

    boom, calls = _api_returning(RuntimeError("weather API 502: nope"))
    monkeypatch.setattr(weather, "_api", boom)
    out = weather.current()

    assert calls["n"] == 2, "should still have retried before falling back"
    assert out["temp"] == 71.0
    assert out["stale"] is True, "a cached reading must never claim to be live"


def test_serving_stale_does_not_relabel_the_cached_copy(monkeypatch):
    """The fallback returns a copy. Mutating the cache in place would flip its
    own `stale` to True and lose the honest `as_of`, so the *second* outage
    would be serving a reading that no longer knows when it was taken."""
    _stub_surroundings(monkeypatch)
    good, _ = _api_returning(_LIVE)
    monkeypatch.setattr(weather, "_api", good)
    weather.current()
    taken = weather._current_cache["as_of"]

    boom, _ = _api_returning(RuntimeError("502"))
    monkeypatch.setattr(weather, "_api", boom)
    weather.current()

    assert weather._current_cache["stale"] is False
    assert weather._current_cache["as_of"] == taken
    assert weather.current()["stale"] is True


def test_a_reading_past_the_stale_window_is_not_served_at_all(monkeypatch):
    """Better a blank chip than an hours-old temperature presented as now."""
    _stub_surroundings(monkeypatch)
    old = datetime.now(UTC) - timedelta(seconds=weather._CURRENT_STALE_S + 60)
    monkeypatch.setattr(weather, "_current_cache",
                        {"temp": 71.0, "stale": False, "as_of": old.isoformat()})
    boom, _ = _api_returning(RuntimeError("502"))
    monkeypatch.setattr(weather, "_api", boom)

    assert weather.current() is None


def test_no_cache_and_a_dead_api_is_still_none(monkeypatch):
    """The route's 502 has to stay reachable — it's now the genuinely-nothing
    case, and turning it into a 200 with empty fields would be worse."""
    _stub_surroundings(monkeypatch)
    boom, _ = _api_returning(RuntimeError("502"))
    monkeypatch.setattr(weather, "_api", boom)

    assert weather.current() is None


def _stub_refresh_db(monkeypatch):
    """refresh()'s day-window half, out of the way — these tests are about the
    reading it takes on the way past, not about weather_days."""
    monkeypatch.setattr(weather, "_sync", lambda *a: 0)
    monkeypatch.setattr(weather.db, "day_key", lambda dt=None: "2026-09-19")


def test_the_poller_cycle_warms_the_reading(dbfile, monkeypatch):
    _stub_surroundings(monkeypatch)
    _stub_refresh_db(monkeypatch)
    good, _ = _api_returning(_LIVE)
    monkeypatch.setattr(weather, "_api", good)

    assert weather.refresh()["current"] == "live"
    assert weather._current_cache["temp"] == 71.0


def test_without_the_warm_the_first_load_of_the_day_still_blanks(dbfile, monkeypatch):
    """The state the fix was written against, kept as a test so the next one
    can see what changed. The cache was fillable only by current() itself —
    i.e. only by a page load — and a served-stale reading expires after an
    hour, so the fallback could cover a blip during a *repeat* visit and never
    the first load of the day. Which is when all four recorded 502s landed."""
    _stub_surroundings(monkeypatch)
    boom, _ = _api_returning(RuntimeError("weather API 502: nope"))
    monkeypatch.setattr(weather, "_api", boom)

    assert weather.current() is None        # nothing warmed it, nothing to serve


def test_a_warmed_reading_covers_an_outage_at_the_next_page_load(dbfile, monkeypatch):
    """The same moment as the test above, with one poller cycle in front of
    it: the chip degrades to a stale marker instead of a 502."""
    _stub_surroundings(monkeypatch)
    _stub_refresh_db(monkeypatch)
    good, _ = _api_returning(_LIVE)
    monkeypatch.setattr(weather, "_api", good)
    weather.refresh()                       # the cycle, five minutes ago

    boom, _ = _api_returning(RuntimeError("weather API 502: nope"))
    monkeypatch.setattr(weather, "_api", boom)
    out = weather.current()                 # you open the desk; upstream is down

    assert out is not None, "the poller's reading should have covered this"
    assert out["stale"] is True
    assert out["temp"] == 71.0


def test_a_dead_api_does_not_sink_the_refresh(dbfile, monkeypatch):
    """The day window is the point of refresh(); the reading is a passenger.
    A passenger that can fail the cycle would trade a blank chip for a stale
    forecast, which is the worse of the two."""
    _stub_surroundings(monkeypatch)
    _stub_refresh_db(monkeypatch)
    boom, _ = _api_returning(RuntimeError("weather API 502: nope"))
    monkeypatch.setattr(weather, "_api", boom)

    assert weather.refresh()["current"] is None     # reported, not raised


def test_a_fresh_reading_is_reused_without_asking(monkeypatch):
    """The page-load path: upstream costs ~0.8s and only moves in 15-minute
    steps, so a reading the poller just took is the answer — still live."""
    _stub_surroundings(monkeypatch)
    good, calls = _api_returning(_LIVE)
    monkeypatch.setattr(weather, "_api", good)
    first = weather.current()

    out = weather.current(fresh_s=weather.CURRENT_FRESH_S)
    assert calls["n"] == 1, "a fresh reading should not have asked again"
    assert out == first and out["stale"] is False
    out["temp"] = 0                          # a copy: the caller can't edit the cache
    assert weather._current_cache["temp"] == 71.0


def test_a_reading_from_elsewhere_is_not_reused(monkeypatch):
    """Origin follows the latest location fix — drive to another city and the
    old city's reading is a wrong answer, however young."""
    _stub_surroundings(monkeypatch)
    good, calls = _api_returning(_LIVE)
    monkeypatch.setattr(weather, "_api", good)
    weather.current()

    monkeypatch.setattr(weather, "_origin", lambda: (35.91, -106.06))
    weather.current(fresh_s=weather.CURRENT_FRESH_S)
    assert calls["n"] == 2


def test_a_reading_past_the_fresh_window_is_asked_again(monkeypatch):
    _stub_surroundings(monkeypatch)
    good, calls = _api_returning(_LIVE)
    monkeypatch.setattr(weather, "_api", good)
    weather.current()
    old = datetime.now(UTC) - timedelta(seconds=weather.CURRENT_FRESH_S + 5)
    weather._current_cache["as_of"] = old.isoformat()

    out = weather.current(fresh_s=weather.CURRENT_FRESH_S)
    assert calls["n"] == 2
    assert out["as_of"] != old.isoformat()


def test_a_reply_with_no_current_block_counts_as_a_failure(monkeypatch):
    """200 carrying no `current` key is the shape a proxy error page takes.
    It must reach the cache fallback, not return a payload of Nones."""
    _stub_surroundings(monkeypatch)
    good, _ = _api_returning(_LIVE)
    monkeypatch.setattr(weather, "_api", good)
    weather.current()

    empty, _ = _api_returning({}, {})
    monkeypatch.setattr(weather, "_api", empty)
    out = weather.current()

    assert out is not None and out["stale"] is True
