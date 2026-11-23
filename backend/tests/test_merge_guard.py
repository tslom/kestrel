"""The merge guard (merge_guard.py): snapshot before a migration merge, the
launcher's check, and the rollback — against a real git repo and real DBs."""

import json
import sqlite3
import subprocess
from pathlib import Path

import pytest

from app import console_worktree as cw
from app import db, merge_guard


def git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], check=True,
                          capture_output=True, text=True).stdout.strip()


DB_PY = 'MIGRATIONS: list[str] = [\n    """CREATE TABLE a (x);""",\n{extra}]\n'


@pytest.fixture
def repo(tmp_path, monkeypatch, dbfile):
    monkeypatch.setattr(cw, "CLAUDE_PROJECTS", tmp_path / "claude-projects")
    monkeypatch.setattr(cw, "ENABLED", True)
    monkeypatch.setattr(cw, "_live", cw.Counter())
    r = tmp_path / "proj"
    (r / "backend/app").mkdir(parents=True)
    git(r, "init", "-q", "-b", "main")
    git(r, "config", "user.email", "t@example.com")
    git(r, "config", "user.name", "t")
    (r / ".gitignore").write_text("data/\n")
    (r / "backend/app/db.py").write_text(DB_PY.format(extra=""))
    git(r, "add", "-A")
    git(r, "commit", "-qm", "init")
    monkeypatch.setattr(merge_guard, "APP_REPO", r.resolve())
    monkeypatch.delenv(merge_guard.GUARD_ENV, raising=False)
    with db.session() as con, con:
        con.execute("CREATE TABLE IF NOT EXISTS keepme (v TEXT)")
        con.execute("INSERT INTO keepme VALUES ('alarm')")
    return r


def tab_commit(repo, text, msg="change", name="backend/app/db.py"):
    tree = Path(cw.create(repo)["path"])
    (tree / name).write_text(text)
    git(tree, "add", "-A")
    git(tree, "commit", "-qm", msg)
    return tree


def test_counts_only_new_migrations(repo):
    base = git(repo, "rev-parse", "HEAD")
    tree = tab_commit(repo, DB_PY.format(extra='    """CREATE TABLE b (y);""",\n'))
    head = git(tree, "rev-parse", "HEAD")
    assert merge_guard.new_migrations(repo, base, head) == 1
    # db.py changed, MIGRATIONS didn't: no guard.
    tree2 = tab_commit(repo, DB_PY.format(extra="") + "\nX = 1\n")
    assert merge_guard.new_migrations(repo, base, git(tree2, "rev-parse", "HEAD")) == 0


def test_a_migration_merge_snapshots_and_arms(repo, monkeypatch):
    monkeypatch.setenv(merge_guard.GUARD_ENV, "1")
    old = git(repo, "rev-parse", "HEAD")
    tree = tab_commit(repo, DB_PY.format(extra='    """CREATE TABLE b (y);""",\n'))
    res = cw.merge(tree, push=True)
    assert res["ok"] and "merge guard: 1 new migration" in res["guard"]
    # Held for the check, not pushed (and there's no upstream anyway).
    assert res["pushed"] is False and "held" in res["push_note"]
    mark = merge_guard.pending()
    assert mark["old_head"] == old and mark["push"] is True
    snap = Path(mark["snapshot"])
    assert snap.exists() and snap.name.startswith("premerge-")
    with sqlite3.connect(snap) as c:
        assert c.execute("SELECT v FROM keepme").fetchall() == [("alarm",)]
    assert "merge guard" in cw.describe_merge(res)


def test_a_plain_merge_is_unguarded(repo):
    tree = tab_commit(repo, "hi\n", name="b.txt")
    res = cw.merge(tree)
    assert res["ok"] and "guard" not in res and merge_guard.pending() is None


def test_snapshot_failure_refuses_the_merge(repo, monkeypatch):
    def boom(tag):
        raise OSError("disk full")
    monkeypatch.setattr(merge_guard, "snapshot", boom)
    head = git(repo, "rev-parse", "HEAD")
    tree = tab_commit(repo, DB_PY.format(extra='    """CREATE TABLE b (y);""",\n'))
    res = cw.merge(tree)
    assert not res["ok"] and "couldn't be backed up" in res["reason"]
    assert git(repo, "rev-parse", "HEAD") == head


def _armed(repo):
    tree = tab_commit(repo, DB_PY.format(extra='    """CREATE TABLE b (y);""",\n'))
    assert cw.merge(tree)["ok"]
    return merge_guard.pending()


def test_healthy_restart_settles_ok(repo):
    check = _armed(repo)
    procs = []
    proc, res = merge_guard.verify_restart(
        check, start=lambda: procs.append("p") or "p", stop=lambda p: None,
        healthy=lambda p: True, db_path=db.APP_DB, log=lambda s: None)
    assert res["ok"] and procs == ["p"] and merge_guard.pending() is None
    last = json.loads((merge_guard._backups() / merge_guard.LAST).read_text())
    assert last["ok"] is True


def test_failed_restart_rolls_code_and_db_back(repo):
    old = git(repo, "rev-parse", "HEAD")
    check = merge_guard.refresh_snapshot(_armed(repo))
    # The "migration" wrecks the data before the server dies.
    with db.session() as con, con:
        con.execute("DELETE FROM keepme")
    stopped, starts = [], []
    health = iter([False, True])
    proc, res = merge_guard.verify_restart(
        check, start=lambda: starts.append(1) or len(starts), stop=stopped.append,
        healthy=lambda p: next(health), db_path=db.APP_DB, log=lambda s: None)
    assert not res["ok"] and "restored" in res["detail"]
    assert git(repo, "rev-parse", "HEAD") == old
    assert stopped == [1] and proc == 2
    db._ready.discard(str(db.APP_DB))
    with db.session() as con:
        assert [r[0] for r in con.execute("SELECT v FROM keepme")] == ["alarm"]


def test_rollback_leaves_a_moved_main_alone(repo):
    check = _armed(repo)
    (repo / "c.txt").write_text("later\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "later")
    moved = git(repo, "rev-parse", "HEAD")
    with db.session() as con, con:
        con.execute("DELETE FROM keepme")
    proc, res = merge_guard.verify_restart(
        check, start=lambda: "p", stop=lambda p: None, healthy=lambda p: False,
        db_path=db.APP_DB, log=lambda s: None)
    assert not res["ok"] and "not rolled back" in res["detail"]
    assert git(repo, "rev-parse", "HEAD") == moved
    with db.session() as con:   # app.db left alone too
        assert con.execute("SELECT count(*) FROM keepme").fetchone()[0] == 0
