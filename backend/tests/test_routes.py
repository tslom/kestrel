"""widgets.py route layer via TestClient — the HTTP contract over the db
helpers: status codes, request/response shapes, and the ValueError->400 /
None->404 translations. The DB is the temp one from `dbfile`; the tasks.md
regeneration side effect is stubbed (see the `client` fixture)."""


def test_post_and_get_task(client):
    r = client.post("/api/tasks", json={"title": "write route tests"})
    assert r.status_code == 200
    task = r.json()
    assert task["title"] == "write route tests"
    assert task["status"] == "open"

    listed = client.get("/api/tasks").json()
    assert [t["id"] for t in listed] == [task["id"]]


def test_get_tasks_defaults_to_open(client):
    keep = client.post("/api/tasks", json={"title": "keep"}).json()
    gone = client.post("/api/tasks", json={"title": "gone"}).json()
    client.post(f"/api/tasks/{gone['id']}/status", json={"status": "done"})
    ids = [t["id"] for t in client.get("/api/tasks").json()]
    assert ids == [keep["id"]]


def test_post_task_bad_parent_returns_400(client):
    r = client.post("/api/tasks", json={"title": "orphan", "parent_id": 999})
    assert r.status_code == 400
    assert "no such parent" in r.json()["detail"]


def test_patch_task(client):
    t = client.post("/api/tasks", json={"title": "old", "priority": 3}).json()
    r = client.patch(f"/api/tasks/{t['id']}", json={"title": "new"})
    assert r.status_code == 200
    body = r.json()
    assert body["title"] == "new"
    assert body["priority"] == 3  # exclude_unset keeps it


def test_patch_missing_task_returns_404(client):
    r = client.patch("/api/tasks/999", json={"title": "x"})
    assert r.status_code == 404


def test_patch_invalid_parent_returns_400(client):
    parent = client.post("/api/tasks", json={"title": "parent"}).json()
    client.post("/api/tasks", json={"title": "child", "parent_id": parent["id"]})
    other = client.post("/api/tasks", json={"title": "other"}).json()
    r = client.patch(f"/api/tasks/{parent['id']}", json={"parent_id": other["id"]})
    assert r.status_code == 400


def test_status_missing_task_returns_404(client):
    r = client.post("/api/tasks/999/status", json={"status": "done"})
    assert r.status_code == 404


def test_status_done_cascades_via_api(client):
    parent = client.post("/api/tasks", json={"title": "parent"}).json()
    child = client.post(
        "/api/tasks", json={"title": "child", "parent_id": parent["id"]}).json()
    client.post(f"/api/tasks/{parent['id']}/status", json={"status": "done"})
    # Parent's only nested subtask now shows done.
    top = client.get("/api/tasks", params={"status": "done"}).json()
    parent_row = next(t for t in top if t["id"] == parent["id"])
    assert parent_row["subtasks"][0]["id"] == child["id"]
    assert parent_row["subtasks"][0]["status"] == "done"


def test_tasks_today_shape(client):
    client.post("/api/tasks", json={"title": "a"})
    body = client.get("/api/tasks/today").json()
    assert set(["overdue", "due_today", "available"]).issubset(body.keys())


def test_post_task_validation_error_422(client):
    # title is required by the pydantic model.
    r = client.post("/api/tasks", json={"due": "2026-01-01"})
    assert r.status_code == 422


def test_post_and_get_habit(client):
    r = client.post("/api/habits", json={"name": "meditate"})
    assert r.status_code == 200
    h = r.json()
    body = client.get("/api/habits").json()
    assert "day" in body
    assert any(x["id"] == h["id"] for x in body["habits"])


def test_checkin_round_trip(client):
    h = client.post("/api/habits", json={"name": "floss"}).json()
    r = client.post(f"/api/habits/{h['id']}/checkin", json={"value": 1, "day": "2026-07-22"})
    assert r.status_code == 200
    assert r.json() == {"ok": True, "day": "2026-07-22"}
    # list_habits for the app's real "today" won't reflect a 2026-07-22 checkin,
    # so read it back through history, which covers a span of days.
    hist = client.get("/api/habits/history", params={"days": 7}).json()
    assert any(x["id"] == h["id"] for x in hist["habits"])


def test_patch_habit_rename(client):
    h = client.post("/api/habits", json={"name": "old"}).json()
    r = client.patch(f"/api/habits/{h['id']}", json={"name": "new"})
    assert r.status_code == 200
    assert r.json()["name"] == "new"


def test_patch_missing_habit_returns_404(client):
    r = client.patch("/api/habits/999", json={"name": "x"})
    assert r.status_code == 404
    assert r.json()["detail"] == "no such habit"


def test_habits_history_clamps_days(client):
    # days is clamped to [1, 31]; an over-large request comes back at 31.
    body = client.get("/api/habits/history", params={"days": 999}).json()
    assert len(body["span"]) == 31
    body = client.get("/api/habits/history", params={"days": 0}).json()
    assert len(body["span"]) == 1
