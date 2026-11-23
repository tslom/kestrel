"""Shared fixtures. The one thing every db-touching test needs is an app.db
that is NOT the real one — so `dbfile` points db.APP_DB at a throwaway file in
a tmp dir, pre-built from a schema migrated once per session."""

import shutil
import sqlite3

import pytest

from app import db


@pytest.fixture(scope="session")
def migrated_template(tmp_path_factory):
    """One app.db run through the full migration set, built once per session.

    Migrating from nothing costs ~17ms (WAL switch + every migration), and
    half the suite opens a DB — so paying it per test was about half the
    suite's wall time. The template is checkpointed and closed before anyone
    copies it, so the main file alone holds the whole schema (no -wal to carry
    along) and its header already says WAL."""
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
    """Redirect db.APP_DB to a path with nothing at it. connect() creates and
    migrates it from scratch on first open — for tests about migration itself
    (test_migration_race) or about whether the file gets created at all."""
    p = tmp_path / "app.db"
    monkeypatch.setattr(db, "APP_DB", p)
    return p


@pytest.fixture
def dbfile(fresh_dbfile, migrated_template):
    """Redirect db.APP_DB to a temp copy of the migrated template: real and
    current, just empty and disposable. The copy's path is new to db._ready, so
    its first connect() still runs _enable_wal and _migrate — each a cheap
    no-op read against a DB that is already WAL and fully applied."""
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
    """Factory: a TestClient over a bare FastAPI carrying just these routers,
    backed by the temp DB. Bare so the real app's lifespan — vault indexing,
    the provider poller — never runs. Extra kwargs go to TestClient."""
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
    """Factory: pin db.day_key() to `day`. Keeps the `dt=None` parameter —
    production calls day_key(dt) too, and a zero-arg stub TypeErrors there."""
    def freeze(day):
        monkeypatch.setattr(db, "day_key", lambda dt=None: day)
        return day
    return freeze


@pytest.fixture(autouse=True)
def no_usage_budget(monkeypatch):
    """Blind the budget gate. It reads the real usage windows — the live
    OAuth fetch and ~/.claude.json — so without this a test's model call
    would pass or pause depending on how much of the week the developer has
    spent. test_agent_budget.py patches the windows it wants."""
    from app import agent
    monkeypatch.setattr(agent, "_budget_windows", lambda fetch: [])
    monkeypatch.setattr(agent, "_budget", None)


@pytest.fixture(autouse=True)
def no_location_relay(monkeypatch):
    """Unset the relay whatever .env says. config loads backend/.env, so once
    LOCATION_RELAY_URL is set there, every location.refresh() in the suite
    would pull from — and ack, i.e. delete from — the real relay."""
    from app.providers import location_relay
    monkeypatch.setattr(location_relay, "LOCATION_RELAY_URL", None)
    monkeypatch.setattr(location_relay, "LOCATION_RELAY_TOKEN", None)


@pytest.fixture(autouse=True)
def empty_index(monkeypatch):
    """Point indexer.session() at an empty in-memory notes table.

    Autouse because it is an isolation bug, not a convenience: readers of the
    vault index (app/places.py, app/courses.py) otherwise open the *real*
    index.db, so a test's result depends on which notes happen to exist in the
    developer's vault. Declaring two homes there is what first made
    test_never_left_home fail — the config fallback it asserts on had been
    quietly replaced by a note.

    A test that wants notes monkeypatches `indexer.session` itself; that runs
    after this fixture and wins."""
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
    """Pin db.day_key() so date-relative reads stay deterministic. Anything
    that seeds fixed calendar dates and then reads back through a trailing
    window (habit_history, streaks) otherwise passes only until real time
    walks those dates out of the window."""
    return freeze_day("2026-08-01")


@pytest.fixture
def client(router_client, monkeypatch):
    """TestClient over a minimal app carrying only the widgets router, backed by
    the temp DB. The task routes' vault_views.regenerate_tasks_view() side
    effect writes into the (possibly absent) vault, so it's stubbed to a no-op
    here."""
    from app import vault_views, widgets

    monkeypatch.setattr(vault_views, "regenerate_tasks_view", lambda: "stubbed")
    return router_client(widgets.router)
