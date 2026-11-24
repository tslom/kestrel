"""poller.refresh_all: one provider raising never sinks the others (each guarded in _refresh_one).

Every test takes `dbfile`: refresh writes a `provider_health` row and the fake providers must stay out of the real app.db."""

import asyncio
import types

from app.providers import poller


def _mod(refresh=None):
    """A stand-in provider module; omit `refresh` to model a read-only one (skipped)."""
    m = types.SimpleNamespace()
    if refresh is not None:
        m.refresh = refresh
    return m


def _run(registry):
    poller.REGISTRY.clear()
    poller.REGISTRY.update(registry)
    return asyncio.run(poller.refresh_all())


def test_returns_info_per_provider(dbfile, monkeypatch):
    monkeypatch.setattr(poller, "REGISTRY", {})
    out = _run({"a": _mod(lambda: {"rows": 1}), "b": _mod(lambda: {"rows": 2})})
    assert out == {"a": {"rows": 1}, "b": {"rows": 2}}


def test_skips_providers_without_refresh(dbfile, monkeypatch):
    monkeypatch.setattr(poller, "REGISTRY", {})
    out = _run({"readonly": _mod(None), "live": _mod(lambda: {"ok": True})})
    assert out == {"live": {"ok": True}}


def test_one_failure_does_not_sink_others(dbfile, monkeypatch):
    monkeypatch.setattr(poller, "REGISTRY", {})

    def boom():
        raise RuntimeError("rate limited")

    out = _run({"bad": _mod(boom), "good": _mod(lambda: {"rows": 3})})
    assert out["good"] == {"rows": 3}
    assert "error" in out["bad"]
    assert "rate limited" in out["bad"]["error"]


def _stub_post_steps(monkeypatch):
    """Stub all four post-steps: the real ones write the real app.db (see test_sync_route.py)."""
    def alarms():
        raise RuntimeError("alarm table locked")

    monkeypatch.setattr(poller, "_reconcile_alarms_sync", alarms)
    monkeypatch.setattr(poller, "_flush_notify_sync", lambda: {"sent": 1})
    monkeypatch.setattr(poller, "_reconcile_wake_sync", lambda: {"add": 0})
    monkeypatch.setattr(poller, "_backup_sync", lambda: {"skipped": "done"})
    monkeypatch.setattr(poller, "_selftexts_sync", lambda: {"opened": 0})


def test_post_steps_run_in_order_on_both_paths_and_a_failure_is_logged(
        dbfile, monkeypatch):
    """sync_all() and refresh_stream() iterate one POST_STEPS; a failing step becomes {"error": ...} on both, doesn't stop later steps, and reaches the failure log."""
    from app import failures

    monkeypatch.setattr(poller, "REGISTRY", {"a": _mod(lambda: {"rows": 1})})
    _stub_post_steps(monkeypatch)

    out = asyncio.run(poller.sync_all())
    assert list(out) == ["a", "alarms", "notify", "wake", "backup", "selftexts"]
    assert "alarm table locked" in out["alarms"]["error"]
    assert out["notify"] == {"sent": 1}
    assert out["backup"] == {"skipped": "done"}

    async def drain():
        return [e async for e in poller.refresh_stream()]

    events = asyncio.run(drain())
    tail = [e for e in events if e["event"] not in ("start", "provider", "done")]
    assert [e["event"] for e in tail] == ["alarms", "notify", "wake", "backup", "selftexts"]
    assert "alarm table locked" in tail[0]["info"]["error"]
    assert events[-1]["event"] == "done"

    rows = failures.recent(origin="poller")
    assert len(rows) == 1                       # same failure twice: one row
    assert rows[0]["label"] == "alarms" and rows[0]["count"] == 2
    assert "alarm table locked" in rows[0]["detail"]


def test_backup_prints_only_when_it_took_one(dbfile, monkeypatch, capsys):
    import app.backup

    monkeypatch.setattr(app.backup, "run", lambda: {"skipped": "already"})
    assert poller._backup_sync() == {"skipped": "already"}
    assert "[backup]" not in capsys.readouterr().out
    monkeypatch.setattr(app.backup, "run", lambda: {"path": "x.db"})
    poller._backup_sync()
    assert "[backup] {'path': 'x.db'}" in capsys.readouterr().out
