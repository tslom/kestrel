"""provider_health.recent — the last 24 refresh outcomes the plan view's map
draws as a ring around each source's node."""

from app import db
from app.providers import heartbeat


def recent(provider):
    with db.session() as con:
        return con.execute("SELECT recent FROM provider_health WHERE provider=?",
                           (provider,)).fetchone()["recent"]


def test_outcomes_append_oldest_first(dbfile):
    heartbeat.record("email", ok=True, info={"new": 3})
    heartbeat.record("email", ok=False, error="boom")
    heartbeat.record("email", ok=True, info={"skipped": "synced 5m ago"})
    assert recent("email") == "ox."


def test_only_the_last_24_are_kept(dbfile):
    for i in range(30):
        heartbeat.record("news", ok=i % 2 == 0)
    r = recent("news")
    assert len(r) == 24 and r == "ox" * 12


def test_broken_means_the_last_two_failed():
    assert heartbeat.broken("oooxx")
    assert not heartbeat.broken("oooxo")
    assert not heartbeat.broken("x")          # one failure is weather
    assert not heartbeat.broken("")


def test_status_carries_it(dbfile, router_client):
    from app.routes import status
    heartbeat.record("weather", ok=False, error="timeout")
    heartbeat.record("weather", ok=False, error="timeout")
    row = next(s for s in router_client(status.router).get("/api/status").json()["sources"]
               if s["source"] == "weather")
    assert row["recent"] == "xx" and row["persistently_failing"] is True
