"""db._migrate concurrency, WAL entry and content-fingerprinted migrations.

A barrier fires all threads into connect() at once; with the lock removed it fails ~every run (20/20 at 8+ threads).
Design: DESIGN.md → Schema history (`db.py` MIGRATIONS), Migration lock and WAL skip.
"""

import logging
import sqlite3
import threading

import pytest

from app import db

N_THREADS = 12


@pytest.fixture
def dbfile(fresh_dbfile):
    """An empty path, not conftest's pre-migrated copy."""
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

    Faked because only two simultaneous converters trigger SQLITE_BUSY; a lock squatter just makes it wait."""

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
    """Already-WAL costs one read: no write, no lock."""
    con = _FakeCon(fails=10**6, mode="wal")
    db._enable_wal(con)
    assert con.calls == ["PRAGMA journal_mode"]


def test_migrate_is_idempotent_on_current_db(dbfile):
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
    """A slot-sharing migration this branch already ran is not applied twice."""
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
    """Identity is the SQL, not its comments."""
    monkeypatch.setattr(db, "MIGRATIONS", db.MIGRATIONS + [_extra("extra")])
    db.connect().close()
    monkeypatch.setattr(db, "MIGRATIONS",
                        db.MIGRATIONS[:-1] + ["-- reworded\n" + _extra("extra")])
    db._ready.clear()
    db.connect().close()          # must not raise


def test_a_counted_db_is_adopted_without_re_running_anything(dbfile):
    """A DB that only knew `schema_version` keeps its tables and gains the fingerprints."""
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
    """A DB counted past this checkout's migrations is neither claimed nor reused."""
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
    # busy_timeout must precede the WAL switch so its write lock waits.
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
