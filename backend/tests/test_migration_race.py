"""db._migrate concurrency — connect() migrates on every open, and the backend
opens many connections at once (the poller refreshes all providers in parallel
threads). On a fresh/behind DB, without the in-process lock two threads both
read schema_version=0 and both run the CREATE TABLE migration; the loser wakes
from busy_timeout to "table already exists". The lock serializes them: first
migrates, rest re-read the bumped version and fall through.

A barrier fires all threads into connect() simultaneously to maximize
contention — with the lock removed this fails ~every run (verified at 20/20 for
8+ threads), so it's a real guard, not a hopeful one."""

import logging
import sqlite3
import threading

import pytest

from app import db

N_THREADS = 12


@pytest.fixture
def dbfile(fresh_dbfile):
    """These tests are about migrating, so they get an empty path rather than
    conftest's pre-migrated copy."""
    return fresh_dbfile


def test_concurrent_connects_on_fresh_db_dont_race(dbfile):
    barrier = threading.Barrier(N_THREADS)
    errors: list[Exception] = []
    versions: list[int] = []
    lock = threading.Lock()  # guards the shared result lists, not the DB

    def worker():
        barrier.wait()  # everyone hits connect() at once
        try:
            con = db.connect()
            v = con.execute("SELECT v FROM schema_version").fetchone()["v"]
            con.close()
            with lock:
                versions.append(v)
        except Exception as e:  # collect, assert in the main thread
            with lock:
                errors.append(e)

    threads = [threading.Thread(target=worker) for _ in range(N_THREADS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == [], f"migration raced: {errors!r}"
    # Every thread converged on the final schema version.
    assert versions == [len(db.MIGRATIONS)] * N_THREADS


class _FakeCon:
    """A connection whose WAL conversion is busy the first `fails` times.

    Faked rather than staged with real connections on purpose. The failure only
    happens when two connections are *both* converting at once: SQLite returns
    SQLITE_BUSY immediately for that (it won't run the busy handler on a lock
    upgrade, to avoid deadlocking), which is exactly why busy_timeout doesn't
    cover it. A merely-locked database isn't enough — a squatter holding SHARED
    or EXCLUSIVE makes the conversion *wait* and succeed, so staging one tests
    nothing. Two real simultaneous converters is a ~4% coin flip, which is a
    detector the barrier test above already provides.
    """

    def __init__(self, fails: int = 0, mode: str = "delete"):
        self.fails, self.mode, self.calls = fails, mode, []

    def execute(self, sql: str):
        self.calls.append(sql)
        if sql == "PRAGMA journal_mode":
            return _FakeCursor(self.mode)
        if sql == "PRAGMA journal_mode=WAL":
            if self.fails > 0:
                self.fails -= 1
                raise sqlite3.OperationalError("database is locked")
            self.mode = "wal"
            return _FakeCursor(self.mode)
        raise AssertionError(f"unexpected statement {sql!r}")


class _FakeCursor:
    def __init__(self, value):
        self._value = value

    def fetchone(self):
        return (self._value,)


def test_enable_wal_retries_a_busy_conversion():
    con = _FakeCon(fails=3)
    db._enable_wal(con)          # must not raise
    assert con.calls.count("PRAGMA journal_mode=WAL") == 4
    assert con.mode == "wal"


def test_enable_wal_gives_up_rather_than_spinning_forever(monkeypatch):
    monkeypatch.setattr(db, "BUSY_TIMEOUT_MS", 30)
    con = _FakeCon(fails=10**6)
    with pytest.raises(sqlite3.OperationalError):
        db._enable_wal(con)


def test_enable_wal_does_nothing_when_already_wal():
    """The common path: every connection after the first. One read, no write,
    no lock — so the retry loop can never cost anything in steady state."""
    con = _FakeCon(fails=10**6, mode="wal")
    db._enable_wal(con)
    assert con.calls == ["PRAGMA journal_mode"]


def test_migrate_is_idempotent_on_current_db(dbfile):
    # Second connect() on an already-current DB must not re-run any migration
    # (would blow up on CREATE TABLE) and must leave exactly one version row.
    db.connect().close()
    con = db.connect()
    rows = con.execute("SELECT v FROM schema_version").fetchall()
    assert len(rows) == 1
    assert rows[0]["v"] == len(db.MIGRATIONS)
    # Core tables are present and queryable.
    for table in ("tasks", "habits", "habit_checkins", "stub_days"):
        con.execute(f"SELECT COUNT(*) FROM {table}")
    con.close()


# ---------- migrations are identified by content, not by position ----------
#
# The bug this replaces: `feat/alarms-reconciler` and `main` would each have
# added a 40th migration. The DB records "40 applied" either way, and the old
# `_schema_version(con) >= len(MIGRATIONS)` reads that as current — so
# whichever branch you were on, its own 40th never ran and never said so.

def _extra(name: str) -> str:
    return f"CREATE TABLE {name} (id INTEGER PRIMARY KEY);"


def _has(con, table: str) -> bool:
    return con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                       (table,)).fetchone() is not None


def test_a_migration_added_on_another_branch_still_applies(dbfile, monkeypatch):
    """The regression proper: two branches, same slot, different SQL."""
    db.connect().close()                                  # DB at len(MIGRATIONS)
    monkeypatch.setattr(db, "MIGRATIONS", db.MIGRATIONS + [_extra("branch_a")])
    db._ready.clear()
    db.connect().close()
    monkeypatch.setattr(db, "MIGRATIONS", db.MIGRATIONS[:-1] + [_extra("branch_b")])
    db._ready.clear()
    con = db.connect()
    assert _has(con, "branch_a") and _has(con, "branch_b")
    con.close()


def test_switching_back_does_not_re_run_a_migration(dbfile, monkeypatch):
    """The other half: the slot-sharing migration this branch already ran must
    not be applied twice, which would fail on CREATE TABLE."""
    base = list(db.MIGRATIONS)
    monkeypatch.setattr(db, "MIGRATIONS", base + [_extra("branch_a")])
    db.connect().close()
    monkeypatch.setattr(db, "MIGRATIONS", base + [_extra("branch_b")])
    db._ready.clear()
    db.connect().close()
    monkeypatch.setattr(db, "MIGRATIONS", base + [_extra("branch_a")])
    db._ready.clear()
    db.connect().close()          # must not raise "table branch_a already exists"


def test_editing_a_comment_does_not_re_run_a_migration(dbfile, monkeypatch):
    """Identity is the SQL, not the prose wrapped around it — these scripts
    carry a lot of prose, and re-running one would fail on CREATE TABLE."""
    monkeypatch.setattr(db, "MIGRATIONS", db.MIGRATIONS + [_extra("extra")])
    db.connect().close()
    monkeypatch.setattr(db, "MIGRATIONS",
                        db.MIGRATIONS[:-1] + ["-- reworded\n" + _extra("extra")])
    db._ready.clear()
    db.connect().close()          # must not raise


def test_a_counted_db_is_adopted_without_re_running_anything(dbfile):
    """Upgrade path: a DB that only ever knew `schema_version` keeps its
    tables and gains the fingerprint record."""
    db.connect().close()
    con = db.connect()
    con.execute("DELETE FROM schema_applied")             # back to counting only
    con.commit()
    con.close()
    db._ready.clear()
    con = db.connect()
    assert con.execute("SELECT COUNT(*) FROM schema_applied").fetchone()[0] \
        == len(db.MIGRATIONS)
    con.execute("SELECT COUNT(*) FROM tasks")             # nothing was dropped
    con.close()


def test_a_db_counted_past_this_branch_defers_rather_than_guessing(dbfile, monkeypatch, caplog):
    """A DB migrated by a longer branch: this checkout cannot fingerprint the
    scripts it doesn't have, so it must neither claim nor reuse their slots."""
    monkeypatch.setattr(db, "MIGRATIONS", db.MIGRATIONS + [_extra("only_there")])
    db.connect().close()
    con = db.connect()
    con.execute("DELETE FROM schema_applied")             # simulate pre-upgrade
    con.commit()
    con.close()
    monkeypatch.setattr(db, "MIGRATIONS", db.MIGRATIONS[:-1])   # the shorter branch
    db._ready.clear()
    with caplog.at_level(logging.WARNING):
        con = db.connect()
    assert con.execute("SELECT COUNT(*) FROM schema_applied").fetchone()[0] == 0
    assert "migrated by another branch" in caplog.text
    con.close()


def test_busy_timeout_set_before_wal(dbfile):
    # Regression for the ordering fix: busy_timeout must be in place so the
    # journal_mode=WAL write-lock waits instead of failing outright.
    con = db.connect()
    assert con.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
    assert con.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    con.close()


def test_connect_returns_row_factory(dbfile):
    con = db.connect()
    row = con.execute("SELECT 1 AS one").fetchone()
    assert row["one"] == 1  # sqlite3.Row keyed access
    assert isinstance(row, sqlite3.Row)
    con.close()
