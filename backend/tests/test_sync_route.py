"""POST /api/sync (providers/api.py): stubs all six steps of poller.sync_all() and checks the result passes straight through.

Stubbing is mandatory: db.APP_DB is hardcoded, so an unstubbed step writes the real app.db (alarm rows, expiring notify rows, pmset, a ~100MB backup copy on the month's first run).
Design: DESIGN.md → `tests/test_sync_route.py`."""

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
    """Step order is pinned: alarms reconcile after providers write their caches, at-desk flush before wake, wake after reconcile and before the snapshot, snapshot last."""
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
