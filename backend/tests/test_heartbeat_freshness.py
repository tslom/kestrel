"""heartbeat.record(): the stale/freshened half of the event log.

`broke`/`recovered` can't see a source that refreshes OK while its cache stops advancing (location ingest gaps).
Design: DESIGN.md → Source status (`/api/status`), "stale".
"""

from datetime import timedelta

import pytest

from app import db
from app.providers import heartbeat


@pytest.fixture
def health_db(dbfile, monkeypatch, frozen_day):
    """Temp DB plus a FRESHNESS rule for a fake source."""
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
    """The refresh succeeds every cycle (ok is True), yet a stale row is written."""
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
    """Transitions only: a source behind all week writes one row, not one per poll."""
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
    """Not a latch: a second episode is its own row."""
    _seed_day(_day_offset(5))
    heartbeat.record("fake", ok=True)
    _seed_day(db.day_key())
    heartbeat.record("fake", ok=True)
    with db.session() as con, con:
        con.execute("DELETE FROM weather_days WHERE day=?", (db.day_key(),))
    heartbeat.record("fake", ok=True)

    assert _kinds().count("stale") == 2


def test_a_failing_refresh_does_not_also_log_stale(health_db):
    """No stale row while the source is broken (`broke` already says so)."""
    _seed_day(_day_offset(5))
    heartbeat.record("fake", ok=False, error="boom")

    kinds = _kinds()
    assert "broke" in kinds
    assert "stale" not in kinds


def test_an_empty_cache_is_not_called_stale(health_db):
    """An empty cache says neither never-synced nor stale."""
    heartbeat.record("fake", ok=True)

    assert _kinds() == []


def test_a_provider_with_no_freshness_rule_is_left_alone(dbfile):
    """screen/messages/location own their verdict through their health() hook."""
    heartbeat.record("unruled", ok=True)

    assert _kinds("unruled") == []


def test_a_future_dated_cache_is_not_stale(health_db):
    """Weather and calendar cache ahead of today: early is not late (age_days clamps)."""
    from datetime import date
    ahead = (date.fromisoformat(db.day_key()) + timedelta(days=6)).isoformat()
    _seed_day(ahead)
    heartbeat.record("fake", ok=True)

    assert _kinds() == []
