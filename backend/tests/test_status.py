"""GET /api/status: a refresh that succeeds while writing nothing must not read ok, a never-configured source must not read broken, a benign skip note must not read as failure.

Design: DESIGN.md → `tests/test_status.py`.
"""

import json
import types
from datetime import date, timedelta

import pytest

from app import db
from app.routes import status as status_routes
from app.providers import heartbeat


def _ok_infra():
    return [{"name": "vault", "state": "ok", "detail": "stub"},
            {"name": "app.db", "state": "ok", "detail": "stub"}]


@pytest.fixture
def status_client(router_client, monkeypatch):
    """Status routes over the temp DB with a one-provider registry; auth lookups stubbed off.

    `_infra` is stubbed healthy: live, it reads the real VAULT_PATH and app.db, so `counts` and `worst` would depend on the machine. Tests that care pass their own rows.
    """
    def build(registry, auth=None, infra=None):
        monkeypatch.setattr(status_routes, "REGISTRY", registry)
        monkeypatch.setattr(status_routes, "_auth_for", lambda source: auth)
        monkeypatch.setattr(status_routes, "_infra",
                            lambda: infra if infra is not None else _ok_infra())
        return router_client(status_routes.router)
    return build


def _mod(refresh=True, health=None):
    m = types.SimpleNamespace()
    if refresh:
        m.refresh = lambda: {}
    if health is not None:
        m.health = health
    return m


def _source(client, name="stub"):
    body = client.get("/api/status").json()
    return next(s for s in body["sources"] if s["source"] == name)


def _seed_days(table, day, n=1):
    con = db.connect()
    with con:
        for i in range(n):
            con.execute(f"INSERT INTO {table} (day, metric, value, refreshed_at)"
                        " VALUES (?,?,?,?)", (day, f"m{i}", 1.0, "now"))
    con.close()


def test_empty_cache_is_stale_even_when_the_refresh_succeeded(status_client, monkeypatch):
    """chat.db unreadable or aw-server down: refresh() returns cleanly, writes nothing, everything else is green."""
    monkeypatch.setitem(heartbeat.FRESHNESS, "stub", ("stub_days", "day", 1))
    heartbeat.record("stub", ok=True, info={}, duration_ms=5)
    row = _source(status_client({"stub": _mod()}))
    assert row["state"] == "stale"
    assert "empty" in row["detail"]
    assert row["rows"] == 0


def test_current_cache_plus_good_refresh_is_ok(status_client, monkeypatch):
    monkeypatch.setitem(heartbeat.FRESHNESS, "stub", ("stub_days", "day", 1))
    _seed_days("stub_days", db.day_key())
    heartbeat.record("stub", ok=True, info={}, duration_ms=5)
    row = _source(status_client({"stub": _mod()}))
    assert row["state"] == "ok"
    assert row["days_behind"] == 0
    assert row["last_ok_ago"]


def test_data_older_than_the_threshold_is_stale(status_client, monkeypatch):
    monkeypatch.setitem(heartbeat.FRESHNESS, "stub", ("stub_days", "day", 2))
    old = (date.fromisoformat(db.day_key()) - timedelta(days=5)).isoformat()
    _seed_days("stub_days", old)
    heartbeat.record("stub", ok=True, info={}, duration_ms=5)
    row = _source(status_client({"stub": _mod()}))
    assert row["state"] == "stale"
    assert row["days_behind"] == 5
    assert "5d old" in row["detail"]


def test_future_days_are_not_stale(status_client, monkeypatch):
    """weather/calendar cache forecasts — being *ahead* of today is not a gap."""
    monkeypatch.setitem(heartbeat.FRESHNESS, "stub", ("stub_days", "day", 1))
    ahead = (date.fromisoformat(db.day_key()) + timedelta(days=3)).isoformat()
    _seed_days("stub_days", ahead)
    heartbeat.record("stub", ok=True, info={}, duration_ms=5)
    assert _source(status_client({"stub": _mod()}))["state"] == "ok"


def test_failed_refresh_is_broken_and_keeps_the_last_good_time(status_client, monkeypatch):
    monkeypatch.setitem(heartbeat.FRESHNESS, "stub", ("stub_days", "day", 1))
    _seed_days("stub_days", db.day_key())
    heartbeat.record("stub", ok=True, info={}, duration_ms=5)
    heartbeat.record("stub", ok=False, error="RuntimeError('429')", duration_ms=9)
    heartbeat.record("stub", ok=False, error="RuntimeError('429')", duration_ms=9)
    row = _source(status_client({"stub": _mod()}))
    assert row["state"] == "broken"
    assert row["fails"] == 2 and "429" in row["error"]
    # the point of COALESCEing last_ok_at: "worked 3m ago" beats a null
    assert row["last_ok_at"] and row["last_ok_ago"]


def test_recovery_clears_the_failure_streak(status_client, monkeypatch):
    monkeypatch.setitem(heartbeat.FRESHNESS, "stub", ("stub_days", "day", 1))
    _seed_days("stub_days", db.day_key())
    heartbeat.record("stub", ok=False, error="boom")
    heartbeat.record("stub", ok=True, info={})
    row = _source(status_client({"stub": _mod()}))
    assert row["state"] == "ok" and row["fails"] == 0 and row["error"] is None


def test_benign_skip_note_does_not_break_the_source(status_client, monkeypatch):
    """news skips because it already fetched today: a note, not a fault (no generic way to read a skip as failure)."""
    monkeypatch.setitem(heartbeat.FRESHNESS, "stub", ("stub_days", "day", 1))
    _seed_days("stub_days", db.day_key())
    heartbeat.record("stub", ok=True, info={"skipped": "already fetched today"})
    row = _source(status_client({"stub": _mod()}))
    assert row["state"] == "ok"
    assert row["notes"] == ["skipped: already fetched today"]
    assert "skipped" not in row["detail"]


def test_unreachable_host_note_marks_partial_data_stale(status_client, monkeypatch):
    monkeypatch.setitem(heartbeat.FRESHNESS, "stub", ("stub_days", "day", 1))
    _seed_days("stub_days", db.day_key())
    heartbeat.record("stub", ok=True, info={"unreachable": {"pc": "urlopen error ..."}})
    row = _source(status_client({"stub": _mod()}))
    assert row["state"] == "stale"
    # names, not the raw dict repr of urllib errors
    assert row["notes"] == ["unreachable: pc"]


def test_provider_probe_can_condemn_a_current_cache(status_client, monkeypatch):
    """messages: the cache looks fine because it synced before Full Disk Access was lost; only the probe knows."""
    monkeypatch.setitem(heartbeat.FRESHNESS, "stub", ("stub_days", "day", 1))
    _seed_days("stub_days", db.day_key())
    heartbeat.record("stub", ok=True, info={})
    mod = _mod(health=lambda: {"state": "broken", "detail": "chat.db unreadable"})
    row = _source(status_client({"stub": mod}))
    assert row["state"] == "broken"
    assert "chat.db unreadable" in row["detail"]


def test_probe_off_state_is_not_a_failure(status_client, monkeypatch):
    """An unconfigured source is grey, never red."""
    mod = _mod(health=lambda: {"state": "off", "detail": "no AW_HOSTS configured"})
    row = _source(status_client({"stub": mod}))
    assert row["state"] == "off"


def test_a_raising_probe_cannot_500_the_panel(status_client, monkeypatch):
    def boom():
        raise RuntimeError("aw exploded")
    row = _source(status_client({"stub": _mod(health=boom)}))
    assert row["state"] == "broken"
    assert "aw exploded" in row["detail"]


def test_worst_first_ordering_and_counts(status_client, monkeypatch):
    monkeypatch.setattr(heartbeat, "FRESHNESS", {})
    reg = {
        "fine": _mod(health=lambda: {"state": "ok", "detail": "up"}),
        "gone": _mod(health=lambda: {"state": "broken", "detail": "down"}),
        "meh": _mod(health=lambda: {"state": "stale", "detail": "behind"}),
        "unset": _mod(health=lambda: {"state": "off", "detail": "not set up"}),
    }
    body = status_client(reg).get("/api/status").json()
    assert [s["source"] for s in body["sources"]] == ["gone", "meh", "unset", "fine"]
    assert body["worst"] == "broken"
    assert body["counts"]["broken"] == 1
    assert {r["name"] for r in body["infra"]} == {"vault", "app.db"}


def test_infra_is_counted_and_ranked_with_the_sources(status_client):
    """The vault and app.db break like a provider, so a broken one reaches `worst` and `counts` even when every source is fine."""
    body = status_client(
        {"fine": _mod(health=lambda: {"state": "ok", "detail": "up"})},
        infra=[{"name": "vault", "state": "broken", "detail": "gone"},
               {"name": "app.db", "state": "ok", "detail": "stub"}],
    ).get("/api/status").json()
    assert body["worst"] == "broken"
    assert body["counts"]["broken"] == 1
    assert body["counts"]["ok"] == 2


def test_heartbeat_info_is_stored_as_json(dbfile):
    heartbeat.record("stub", ok=True, info={"days": ["2026-07-25"]})
    assert json.loads(heartbeat.rows()["stub"]["info"]) == {"days": ["2026-07-25"]}


# provider_health is overwritten every cycle; the log is the only durable record of a break that healed.

def test_breaking_and_recovering_both_land_in_the_log(dbfile):
    heartbeat.record("stub", ok=True)
    heartbeat.record("stub", ok=False, error="boom")
    heartbeat.record("stub", ok=True)
    assert [(e["kind"], e["detail"]) for e in heartbeat.events("stub")] == [
        ("recovered", None), ("broke", "boom")]


def test_a_source_down_all_night_logs_once_not_every_cycle(dbfile):
    """288 identical rows a day would bury the transition that matters."""
    for _ in range(20):
        heartbeat.record("stub", ok=False, error="boom")
    assert len(heartbeat.events("stub")) == 1


def test_a_changed_error_while_still_down_is_worth_a_row(dbfile):
    """"429 became invalid_grant" is the useful detail: a rate limit turned into dead auth needs a different fix."""
    heartbeat.record("stub", ok=False, error="429")
    heartbeat.record("stub", ok=False, error="429")
    heartbeat.record("stub", ok=False, error="invalid_grant")
    assert [e["detail"] for e in heartbeat.events("stub")] == ["invalid_grant", "429"]


def test_swallowed_substep_failures_are_logged_without_a_health_row(dbfile):
    """A Plaid leg 500s, refresh() catches it and returns success, so provider_health says ok; only the log knows."""
    heartbeat.log_event("finance", "degraded", "liabilities: boom")
    heartbeat.record("finance", ok=True)
    assert heartbeat.rows()["finance"]["ok"] == 1
    assert heartbeat.events("finance")[0]["kind"] == "degraded"


def test_the_log_is_trimmed_to_a_bound(dbfile, monkeypatch):
    monkeypatch.setattr(heartbeat, "EVENT_LOG_KEEP", 5)
    for i in range(12):
        heartbeat.log_event("stub", "degraded", f"e{i}")
    kept = heartbeat.events("stub", limit=99)
    assert len(kept) <= 6 and kept[0]["detail"] == "e11"


def test_status_attaches_recent_events_to_the_source(status_client, monkeypatch):
    monkeypatch.setattr(heartbeat, "FRESHNESS", {})
    heartbeat.record("stub", ok=False, error="boom")
    heartbeat.record("stub", ok=True)
    row = _source(status_client({"stub": _mod()}))
    assert [e["kind"] for e in row["events"]] == ["recovered", "broke"]
    assert row["events"][1]["ago"]
