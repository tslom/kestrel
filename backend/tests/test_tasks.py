"""db task helpers: CRUD, the one-level subtask guard, the done/dropped cascade to open children, tasks_today() buckets; all via public functions on the `dbfile` fixture."""

from datetime import date, timedelta

import pytest

from app import db


def _future(days=3):
    return (date.fromisoformat(db.day_key()) + timedelta(days=days)).isoformat()


def _past(days=3):
    return (date.fromisoformat(db.day_key()) - timedelta(days=days)).isoformat()


def test_create_and_list(dbfile):
    t = db.create_task("write tests")
    assert t["title"] == "write tests"
    assert t["status"] == "open"
    listed = db.list_tasks()
    assert len(listed) == 1
    assert listed[0]["id"] == t["id"]
    assert listed[0]["subtasks"] == []


def test_title_is_stripped(dbfile):
    t = db.create_task("  padded  ")
    assert t["title"] == "padded"


def test_list_open_excludes_done(dbfile):
    keep = db.create_task("keep")
    gone = db.create_task("finish me")
    db.set_task_status(gone["id"], "done")
    ids = [t["id"] for t in db.list_tasks(status="open")]
    assert keep["id"] in ids
    assert gone["id"] not in ids


def test_subtask_nested_not_top_level(dbfile):
    parent = db.create_task("parent")
    child = db.create_task("child", parent_id=parent["id"])
    top = db.list_tasks()
    # Only the parent is a top-level row; the child rides along nested.
    assert [t["id"] for t in top] == [parent["id"]]
    assert [s["id"] for s in top[0]["subtasks"]] == [child["id"]]


def test_checked_off_subtask_leaves_open_parent(dbfile):
    parent = db.create_task("parent")
    keep = db.create_task("still to do", parent_id=parent["id"])
    checked = db.create_task("check me off", parent_id=parent["id"])
    db.set_task_status(checked["id"], "done")
    top = db.list_tasks(status="open")
    # The checked-off child is gone; re-serving it made the box look stuck.
    assert [s["id"] for s in top[0]["subtasks"]] == [keep["id"]]


def test_dropped_subtask_leaves_open_parent(dbfile):
    parent = db.create_task("parent")
    dropped = db.create_task("never mind", parent_id=parent["id"])
    db.set_task_status(dropped["id"], "dropped")
    assert db.list_tasks(status="open")[0]["subtasks"] == []


def test_closed_parent_keeps_its_subtasks(dbfile):
    parent = db.create_task("parent")
    child = db.create_task("child", parent_id=parent["id"])
    db.set_task_status(parent["id"], "done")
    top = db.list_tasks(status="done")
    assert [s["id"] for s in top[0]["subtasks"]] == [child["id"]]


def test_reopening_a_parent_reverses_its_cascade_only(dbfile, monkeypatch):
    """Undoing a check-off restores the subtasks the close took; one finished earlier stays finished."""
    import itertools
    from datetime import datetime as real
    ticks = itertools.count()

    class Clock(real):
        @classmethod
        def now(cls, tz=None):
            return real(2026, 9, 25, 10, 0, next(ticks))
    monkeypatch.setattr(db, "datetime", Clock)

    parent = db.create_task("parent")
    earlier = db.create_task("done before", parent_id=parent["id"])
    swept = db.create_task("swept up", parent_id=parent["id"])
    db.set_task_status(earlier["id"], "done")
    db.set_task_status(parent["id"], "done")
    db.set_task_status(parent["id"], "open")
    with db.session() as con:
        status = {r["id"]: r["status"] for r in con.execute("SELECT id, status FROM tasks")}
    assert status[parent["id"]] == "open"
    assert status[swept["id"]] == "open"
    assert status[earlier["id"]] == "done"


def test_today_hides_checked_off_subtask(dbfile):
    parent = db.create_task("parent")
    checked = db.create_task("check me off", parent_id=parent["id"])
    db.set_task_status(checked["id"], "done")
    row = next(t for t in db.tasks_today()["available"] if t["id"] == parent["id"])
    assert row["subtasks"] == []


def test_check_parent_rejects_three_levels(dbfile):
    parent = db.create_task("parent")
    child = db.create_task("child", parent_id=parent["id"])
    with pytest.raises(ValueError, match="one level deep"):
        db.create_task("grandchild", parent_id=child["id"])


def test_check_parent_rejects_missing_parent(dbfile):
    with pytest.raises(ValueError, match="no such parent"):
        db.create_task("orphan", parent_id=999)


def test_update_reparent_of_task_with_children_rejected(dbfile):
    parent = db.create_task("parent")
    db.create_task("child", parent_id=parent["id"])
    other = db.create_task("other")
    with pytest.raises(ValueError, match="subtasks of its own"):
        db.update_task(parent["id"], parent_id=other["id"])


def test_update_self_parent_rejected(dbfile):
    t = db.create_task("solo")
    with pytest.raises(ValueError, match="cannot parent itself"):
        db.update_task(t["id"], parent_id=t["id"])


def test_update_partial_fields(dbfile):
    t = db.create_task("old", priority=3)
    updated = db.update_task(t["id"], title="new")
    assert updated["title"] == "new"
    assert updated["priority"] == 3  # untouched


def test_update_no_known_fields_returns_current(dbfile):
    t = db.create_task("unchanged")
    out = db.update_task(t["id"], bogus="x")
    assert out["title"] == "unchanged"


def test_update_missing_task_returns_none(dbfile):
    assert db.update_task(999, title="x") is None


def test_done_cascades_to_open_subtasks(dbfile):
    parent = db.create_task("parent")
    child = db.create_task("child", parent_id=parent["id"])
    db.set_task_status(parent["id"], "done")
    row = db.update_task(child["id"])  # read-back
    assert row["status"] == "done"


def test_waiting_does_not_cascade(dbfile):
    parent = db.create_task("parent")
    child = db.create_task("child", parent_id=parent["id"])
    db.set_task_status(parent["id"], "waiting", waiting_on="Bob")
    assert db.update_task(child["id"])["status"] == "open"


def test_done_sets_completed_at(dbfile):
    t = db.create_task("finish")
    done = db.set_task_status(t["id"], "done")
    assert done["completed_at"] is not None


def test_task_exists_for_source(dbfile):
    assert db.task_exists_for_source("email", "abc") is False
    db.create_task("from email", source_kind="email", source_ref="abc")
    assert db.task_exists_for_source("email", "abc") is True
    # Matches regardless of status.
    t = db.create_task("card pmt", source_kind="finance", source_ref="ref1")
    db.set_task_status(t["id"], "done")
    assert db.task_exists_for_source("finance", "ref1") is True


def test_tasks_today_buckets(dbfile):
    today = db.day_key()
    overdue = db.create_task("overdue", due=_past())
    due = db.create_task("due today", due=today)
    later = db.create_task("someday", due=_future())
    out = db.tasks_today()
    assert [t["id"] for t in out["overdue"]] == [overdue["id"]]
    assert [t["id"] for t in out["due_today"]] == [due["id"]]
    assert later["id"] in [t["id"] for t in out["available"]]


def test_tasks_today_hides_deferred(dbfile):
    deferred = db.create_task("later", defer=_future())
    out = db.tasks_today()
    all_ids = [t["id"] for t in out["overdue"] + out["due_today"] + out["available"]]
    assert deferred["id"] not in all_ids


def test_list_flags_deferred(dbfile):
    t = db.create_task("later", defer=_future())
    listed = {x["id"]: x for x in db.list_tasks()}
    assert listed[t["id"]]["deferred"] is True
