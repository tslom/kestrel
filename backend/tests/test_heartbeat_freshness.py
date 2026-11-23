"""heartbeat.record() — the stale/freshened half of the event log.

`broke`/`recovered` read the refresh *outcome*, which cannot see a source that
keeps refreshing successfully while its cache stops advancing. Location can
do exactly that: ingest stops for days, OwnTracks' store-and-forward flushes
the backlog, and `provider_events` for location is empty afterwards — a real
multi-day outage with no trace left. These pin the trace.
"""

from datetime import timedelta

import pytest

from app import db
from app.providers import heartbeat


@pytest.fixture
def health_db(dbfile, monkeypatch, frozen_day):
    """Temp DB plus a FRESHNESS rule for a fake source, so these assert about
    their own provider rather than whichever real one happens to be behind."""
    monkeypatch.setitem(heartbeat.FRESHNESS, "fake", ("weather_days", "day", 2))
    return dbfile


def _seed_day(day: str, metric: str = "tmax") -> None:
    with db.session() as con, con:
        con.execute("INSERT OR REPLACE INTO weather_days (day, metric, value,"
                    " refreshed_at) VALUES (?,?,?,?)", (day, metric, 1.0, day))


def _kinds(provider: str = "fake") -> list[str]:
    return [e["kind"] for e in heartbeat.events(provider)]


def _day_offset(days: int) -> str:
    base = db.day_key()
    from datetime import date
    return (date.fromisoformat(base) - timedelta(days=days)).isoformat()


def test_a_cache_that_stops_advancing_logs_stale(health_db):
    """The whole point: the refresh succeeds every cycle, so ok is True and
    nothing else in the system would write a row."""
    _seed_day(_day_offset(5))
    heartbeat.record("fake", ok=True)

    assert "stale" in _kinds()
    detail = heartbeat.events("fake")[0]["detail"]
    assert "5d behind" in detail and "stale after 2d" in detail


def test_a_current_cache_logs_nothing(health_db):
    _seed_day(db.day_key())
    heartbeat.record("fake", ok=True)

    assert _kinds() == []


def test_stale_is_logged_once_not_every_cycle(health_db):
    """A source behind all week must not write a row per 5-minute poll — the
    same transitions-only discipline `broke` already has."""
    _seed_day(_day_offset(5))
    for _ in range(4):
        heartbeat.record("fake", ok=True)

    assert _kinds().count("stale") == 1


def test_catching_up_logs_freshened(health_db):
    _seed_day(_day_offset(5))
    heartbeat.record("fake", ok=True)
    _seed_day(db.day_key())
    heartbeat.record("fake", ok=True)

    assert _kinds()[:2] == ["freshened", "stale"], "newest first"


def test_going_stale_again_logs_again(health_db):
    """Transitions, not a latch: the second episode is its own row, which is
    what makes a recurring gap visible as recurring."""
    _seed_day(_day_offset(5))
    heartbeat.record("fake", ok=True)
    _seed_day(db.day_key())
    heartbeat.record("fake", ok=True)
    with db.session() as con, con:
        con.execute("DELETE FROM weather_days WHERE day=?", (db.day_key(),))
    heartbeat.record("fake", ok=True)

    assert _kinds().count("stale") == 2


def test_a_failing_refresh_does_not_also_log_stale(health_db):
    """While a source is down, `broke` already says so — a stale row next to
    it is the same news twice, and the panel ranks broken worse anyway."""
    _seed_day(_day_offset(5))
    heartbeat.record("fake", ok=False, error="boom")

    kinds = _kinds()
    assert "broke" in kinds
    assert "stale" not in kinds


def test_an_empty_cache_is_not_called_stale(health_db):
    """Never-synced and gone-stale are different states, and an empty cache
    with no rows cannot tell you which — so it says neither."""
    heartbeat.record("fake", ok=True)

    assert _kinds() == []


def test_a_provider_with_no_freshness_rule_is_left_alone(dbfile):
    """screen/messages/location carry reachability their cache can't show, so
    their own health() hook owns the verdict."""
    heartbeat.record("unruled", ok=True)

    assert _kinds("unruled") == []


def test_a_future_dated_cache_is_not_stale(health_db):
    """Weather and calendar cache days ahead of today; being early is not late
    (age_days clamps, and this pins that it stays clamped here)."""
    from datetime import date
    ahead = (date.fromisoformat(db.day_key()) + timedelta(days=6)).isoformat()
    _seed_day(ahead)
    heartbeat.record("fake", ok=True)

    assert _kinds() == []
