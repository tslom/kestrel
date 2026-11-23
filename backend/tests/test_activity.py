"""The activity log: every unattended write, readable back and undoable."""

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import activity, db
from app.providers import poller

APP = Path(activity.__file__).resolve().parent

# create_task call sites that run because the user asked — a click or a
# console turn — so they report themselves and don't belong in the log.
USER_INITIATED = {"widgets.py", "people.py", "console_tools.py", "db.py"}
# Mention the call in prose, not make it.
NOT_CALLERS = {"activity.py"}


def test_every_unattended_task_write_is_logged():
    """A file that creates tasks on its own must also record them, or the
    card never hears about it."""
    missing = []
    for p in APP.rglob("*.py"):
        src = p.read_text()
        if p.name in USER_INITIATED | NOT_CALLERS or not re.search(r"\bdb\.create_task\(", src):
            continue
        if "activity.record(" not in src:
            missing.append(str(p.relative_to(APP)))
    assert missing == []


def test_the_poller_routes_self_texts():
    assert "selftexts" in [label for label, _ in poller.POST_STEPS]


def _task(title="Pay Visa", status="open"):
    t = db.create_task(title=title, source="finance")
    if status != "open":
        db.set_task_status(t["id"], status)
    return activity.record("finance", "task", f"New task: {title}", {"task_id": t["id"]}), t


def test_undo_drops_an_open_task(dbfile):
    row_id, t = _task()
    out = activity.undo(row_id)
    assert out["undone_at"] and not out["undoable"]
    assert db.list_tasks(status="dropped")[0]["id"] == t["id"]


def test_undo_will_not_erase_a_finished_task(dbfile):
    row_id, _ = _task(status="done")
    with pytest.raises(activity.UndoError):
        activity.undo(row_id)
    assert activity.get(row_id)["undone_at"] is None


def test_undo_twice_is_harmless(dbfile):
    row_id, _ = _task()
    first = activity.undo(row_id)["undone_at"]
    assert activity.undo(row_id)["undone_at"] == first


def test_a_keep_has_nothing_to_undo(dbfile):
    row_id = activity.record("selftext", "keep", "Kept as is")
    assert not activity.get(row_id)["undoable"]
    with pytest.raises(activity.UndoError):
        activity.undo(row_id)


def test_unseen_counts_until_marked_seen(dbfile):
    _task("one")
    assert activity.recent()["unseen"] == 1
    activity.mark_seen()
    assert activity.recent()["unseen"] == 0
    _task("two")
    items = activity.recent()["items"]
    assert [(i["summary"], i["unseen"]) for i in items] == \
        [("New task: two", True), ("New task: one", False)]


def test_routes(dbfile):
    from app.main import app
    client = TestClient(app)
    row_id, _ = _task()
    assert client.get("/api/activity").json()["unseen"] == 1
    assert client.post(f"/api/activity/{row_id}/undo").json()["undone_at"]
    assert client.post("/api/activity/999/undo").status_code == 404
    done_id, _ = _task("done one", status="done")
    r = client.post(f"/api/activity/{done_id}/undo")
    assert r.status_code == 409 and "already finished" in r.json()["detail"]
    client.post("/api/activity/seen")
    assert client.get("/api/activity").json()["unseen"] == 0
    r = client.post(f"/api/activity/{row_id}/redo", json={"kind": "task"})
    assert r.status_code == 409   # a bill's task isn't a text to yourself
