"""Shared fixtures. `dbfile` points db.APP_DB at a throwaway copy of a schema migrated
once per session, so no test touches the real app.db. The autouse fixtures isolate
every test from the developer's real usage, keychain, model catalog, relay and vault."""

import shutil
import sqlite3

import pytest

from app import db


@pytest.fixture(scope="session")
def migrated_template(tmp_path_factory):
    """One fully migrated app.db per session (migrating per test was ~half the
    suite's wall time), checkpointed and closed so the main file holds the schema."""
    p = tmp_path_factory.mktemp("template") / "app.db"
    saved = db.APP_DB
    db.APP_DB = p
    try:
        db.connect().close()
    finally:
        db.APP_DB = saved
        db._ready.discard(str(p))
    con = sqlite3.connect(p)
    con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    con.close()
    return p


@pytest.fixture
def fresh_dbfile(tmp_path, monkeypatch):
    """db.APP_DB redirected to a path with nothing at it, for tests of migration
    itself or of whether the file gets created."""
    p = tmp_path / "app.db"
    monkeypatch.setattr(db, "APP_DB", p)
    return p


@pytest.fixture
def dbfile(fresh_dbfile, migrated_template):
    """db.APP_DB redirected to a disposable copy of the migrated template."""
    shutil.copyfile(migrated_template, fresh_dbfile)
    return fresh_dbfile


@pytest.fixture
def con(dbfile):
    """An open connection to the temp DB, closed after the test."""
    c = db.connect()
    yield c
    c.close()


@pytest.fixture
def router_client(dbfile):
    """Factory: a TestClient over a bare FastAPI carrying just these routers (so the
    real lifespan never runs), on the temp DB. Extra kwargs go to TestClient."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    def make(*routers, **client_kw):
        app = FastAPI()
        for r in routers:
            app.include_router(r)
        return TestClient(app, **client_kw)
    return make


@pytest.fixture
def freeze_day(monkeypatch):
    """Factory: pin db.day_key() to `day`. Keeps `dt=None`: production calls day_key(dt)."""
    def freeze(day):
        monkeypatch.setattr(db, "day_key", lambda dt=None: day)
        return day
    return freeze


@pytest.fixture(autouse=True)
def no_usage_budget(monkeypatch):
    """Blind the budget gate (it reads the real usage windows); test_agent_budget.py patches its own."""
    from app import agent
    monkeypatch.setattr(agent, "_budget_windows", lambda fetch: [])
    monkeypatch.setattr(agent, "_accounts_refusal", lambda: None)
    monkeypatch.setattr(agent, "_budget", None)


@pytest.fixture(autouse=True)
def fake_keychain(monkeypatch, tmp_path):
    """In-memory Keychain for claude_accounts and a throwaway ~/.claude.json, so a
    sign-in test never touches the developer's real Claude login."""
    from app import claude_accounts, claude_quota
    monkeypatch.setattr(claude_quota, "CLAUDE_CONFIG", tmp_path / ".claude.json")
    monkeypatch.setattr(claude_quota, "_active", "")
    monkeypatch.setattr(claude_quota, "_parked", {})
    monkeypatch.setattr(claude_quota, "_live", claude_quota._LiveUsage())
    monkeypatch.setattr(claude_accounts._Verdict, "refusal", None)
    monkeypatch.setattr(claude_accounts._Verdict, "whys", {})
    store: dict[tuple[str, str | None], dict] = {}

    def read(service, account=None):
        if account is None:
            hit = [v for (s, _), v in store.items() if s == service]
            return hit[0] if hit else None
        return store.get((service, account))

    def write(service, account, blob):
        store[(service, account)] = blob

    monkeypatch.setattr(claude_accounts, "_kc_read", read)
    monkeypatch.setattr(claude_accounts, "_kc_write", write)
    monkeypatch.setattr(claude_accounts, "_kc_delete",
                        lambda service, account: store.pop((service, account), None))
    return store


@pytest.fixture(autouse=True)
def empty_model_catalog(monkeypatch):
    """No model list, access facts or Settings defaults (their readers would open the
    real app.db); test_model_catalog.py puts them back."""
    from app import model_catalog
    monkeypatch.setattr(model_catalog, "catalog", lambda provider="claude", everything=False: [])
    monkeypatch.setattr(model_catalog, "access", lambda uuid: {})
    monkeypatch.setattr(model_catalog, "stored", lambda key: None)
    monkeypatch.setattr(model_catalog, "fetched", lambda provider: None)


@pytest.fixture(autouse=True)
def no_price_fetch(monkeypatch):
    """model_prices' web fetch fails, so no test downloads the price list;
    test_model_prices.py swaps in its own payloads."""
    from app import model_prices

    def offline(url):
        raise OSError("no network in tests")
    monkeypatch.setattr(model_prices, "_get", offline)
    model_prices._book.clear()
    yield
    model_prices._book.clear()


@pytest.fixture(autouse=True)
def no_location_relay(monkeypatch):
    """Unset the relay whatever .env says: refresh() would pull from and ack the real one."""
    from app.providers import location_relay
    monkeypatch.setattr(location_relay, "LOCATION_RELAY_URL", None)
    monkeypatch.setattr(location_relay, "LOCATION_RELAY_TOKEN", None)


@pytest.fixture(autouse=True)
def empty_index(monkeypatch):
    """Point indexer.session() at an empty in-memory notes table, so no test reads the
    developer's real index.db. A test wanting notes patches `indexer.session` itself."""
    import sqlite3
    from contextlib import contextmanager

    from app import indexer

    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.execute("CREATE TABLE notes (path TEXT, title TEXT, frontmatter TEXT)")

    @contextmanager
    def session():
        yield con

    monkeypatch.setattr(indexer, "session", session)
    return con


@pytest.fixture
def frozen_day(freeze_day):
    """Pin db.day_key() to a fixed day so seeded dates stay inside trailing windows."""
    return freeze_day("2026-08-01")


@pytest.fixture
def client(router_client, monkeypatch):
    """TestClient over the widgets router alone; regenerate_tasks_view (writes the vault) is stubbed."""
    from app import vault_views, widgets

    monkeypatch.setattr(vault_views, "regenerate_tasks_view", lambda: "stubbed")
    return router_client(widgets.router)
