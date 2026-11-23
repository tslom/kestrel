"""POST /api/sync (providers/api.py) — the on-demand refresh endpoint. It awaits
poller.sync_all(), which is refresh_all() plus the §21 alarm reconcile plus the
at-desk flush plus the wake schedule plus the monthly snapshot plus self-text
routing, so the test stubs all six (the real ones hit every provider's live
source and open app.db)
and checks the result is passed straight through.

Stubbing them is not incidental. db.APP_DB is hardcoded, so an unstubbed
reconcile here writes alarm rows into the *real* database from a test run —
which is exactly what happened the first time this route was rewired, and again
when the notify flush was added beside it (that one expires real rows and can
post a real banner). The wake reconcile is the third instance of the same
pattern: it opens the real app.db and shells out to pmset, so it is stubbed
here for the same reason and not because it is dangerous in some new way.

The backup step is the fourth, and it failed this test on the way in — which
is the file working. Unstubbed it does not merely open the real app.db, it
copies it: a hundred megabytes written into backend/data/backups/ by a unit
test, on any run that happens to be the month's first."""

import pytest

from app.providers import api as providers_api
from app.providers import poller


@pytest.fixture
def sync_client(router_client, monkeypatch):
    async def fake_refresh_all():
        return {"stub": {"rows": 5}, "weather": {"error": "offline"}}

    monkeypatch.setattr(poller, "refresh_all", fake_refresh_all)
    monkeypatch.setattr(poller, "_reconcile_alarms_sync",
                        lambda: {"create": 0, "dry_run": True})
    monkeypatch.setattr(poller, "_flush_notify_sync", lambda: {"sent": 0})
    monkeypatch.setattr(poller, "_reconcile_wake_sync",
                        lambda: {"add": 0, "dry_run": True})
    monkeypatch.setattr(poller, "_backup_sync",
                        lambda: {"skipped": "already backed up"})
    monkeypatch.setattr(poller, "_selftexts_sync", lambda: {"opened": 0})
    return router_client(providers_api.router)


def test_sync_returns_refresh_results(sync_client):
    r = sync_client.post("/api/sync")
    assert r.status_code == 200
    assert r.json() == {"stub": {"rows": 5}, "weather": {"error": "offline"},
                        "alarms": {"create": 0, "dry_run": True},
                        "notify": {"sent": 0},
                        "wake": {"add": 0, "dry_run": True},
                        "backup": {"skipped": "already backed up"},
                        "selftexts": {"opened": 0}}


def test_sync_reconciles_alarms_after_the_providers(sync_client, monkeypatch):
    """Order is the point three times over: alarms are derived from the caches
    the providers write, so reconciling first would diff against a stale store;
    the at-desk flush goes before the wake so a banner enqueued during this
    cycle's extraction is delivered in this cycle rather than the next one; and
    the wake schedule goes before the snapshot because it reads the alarms
    table the reconcile just wrote, so running it earlier would schedule wakes
    for the previous cycle's alarms; and the snapshot goes last because it is
    the only step that copies the whole database, which has no business
    sitting in front of a banner eleven cycles out of twelve for nothing."""
    order = []

    async def fake_refresh_all():
        order.append("providers")
        return {}

    monkeypatch.setattr(poller, "refresh_all", fake_refresh_all)
    monkeypatch.setattr(poller, "_reconcile_alarms_sync",
                        lambda: order.append("alarms") or {})
    monkeypatch.setattr(poller, "_flush_notify_sync",
                        lambda: order.append("notify") or {})
    monkeypatch.setattr(poller, "_reconcile_wake_sync",
                        lambda: order.append("wake") or {})
    monkeypatch.setattr(poller, "_backup_sync",
                        lambda: order.append("backup") or {})
    monkeypatch.setattr(poller, "_selftexts_sync",
                        lambda: order.append("selftexts") or {})
    sync_client.post("/api/sync")
    assert order == ["providers", "alarms", "notify", "wake", "backup", "selftexts"]
