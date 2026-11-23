"""Snoozes and "cleared today" — the plan view's list. Times resolve against
db.day_key() and its 4am rollover, never the browser's clock."""

from datetime import datetime

import pytest

from app import db, snoozes, usage


def at(s):
    return datetime.fromisoformat(s)


@pytest.mark.parametrize("now, preset, want", [
    # tonight is 20:00 on today's day key
    ("2026-09-25T10:00", "tonight", "2026-09-25T20:00"),
    # tomorrow is next day key's morning
    ("2026-09-25T10:00", "tomorrow", "2026-09-26T08:00"),
    # 1am belongs to yesterday's day key, so "tomorrow" is *this* morning at 8
    ("2026-09-26T01:00", "tomorrow", "2026-09-26T08:00"),
    ("2026-09-26T01:00", None, None),
])
def test_presets_resolve_against_the_day_key(now, preset, want):
    if preset is None:
        # 1am on the 26th is still the night of the 25th: tonight has passed
        with pytest.raises(ValueError, match="already tonight"):
            snoozes.resolve("tonight", now=at(now))
        return
    assert snoozes.resolve(preset, now=at(now)) == at(want)


def test_a_deadline_day_snoozes_to_its_morning():
    assert snoozes.resolve(day="2026-10-01", now=at("2026-09-25T10:00")) == at("2026-10-01T08:00")


def test_an_explicit_until_must_be_in_the_future():
    now = at("2026-09-25T10:00")
    assert snoozes.resolve(until="2026-09-25T10:10", now=now) == at("2026-09-25T10:10")
    with pytest.raises(ValueError):
        snoozes.resolve(until="2026-09-25T09:59", now=now)
    with pytest.raises(ValueError):
        snoozes.resolve(now=now)


def test_active_prunes_what_has_run_out(con):
    snoozes.snooze("task:1", at("2026-09-25T12:00"), con=con)
    snoozes.snooze("people", at("2026-09-25T20:00"), con=con)
    assert snoozes.active(con, now=at("2026-09-25T11:00")) == {
        "task:1": "2026-09-25T12:00:00", "people": "2026-09-25T20:00:00"}
    assert snoozes.active(con, now=at("2026-09-25T12:00")) == {"people": "2026-09-25T20:00:00"}
    assert con.execute("SELECT COUNT(*) FROM snoozes").fetchone()[0] == 1


def test_routes_snooze_resnooze_and_wake(router_client):
    c = router_client(snoozes.router)
    r = c.post("/api/snoozes", json={"ask_id": "email:review", "preset": "tomorrow"})
    assert r.status_code == 200
    first = r.json()["until"]
    # snoozing again replaces, doesn't duplicate
    c.post("/api/snoozes", json={"ask_id": "email:review", "until": "2999-01-01T09:00"})
    assert c.get("/api/snoozes").json()["snoozes"] == {"email:review": "2999-01-01T09:00:00"}
    assert first != "2999-01-01T09:00:00"
    assert c.post("/api/snoozes/wake", json={"ask_id": "email:review"}).json()["woke"]
    assert c.get("/api/snoozes").json()["snoozes"] == {}
    assert not c.post("/api/snoozes/wake", json={"ask_id": "email:review"}).json()["woke"]


def test_routes_refuse_a_snooze_with_no_or_two_whens(router_client):
    c = router_client(snoozes.router)
    assert c.post("/api/snoozes", json={"ask_id": "x"}).status_code == 422
    assert c.post("/api/snoozes", json={"ask_id": "x", "preset": "tonight",
                                        "day": "2999-01-01"}).status_code == 422
    assert c.post("/api/snoozes", json={"ask_id": "x", "until": "2000-01-01T00:00"}).status_code == 400


def test_an_ask_with_a_slash_round_trips(router_client):
    c = router_client(snoozes.router)
    c.post("/api/snoozes", json={"ask_id": "note:01-Projects/Lease.md", "until": "2999-01-01T09:00"})
    assert "note:01-Projects/Lease.md" in c.get("/api/snoozes").json()["snoozes"]


def test_cleared_is_a_set_per_day_and_undo_takes_it_back(router_client):
    c = router_client(usage.router)
    assert c.get("/api/usage/cleared").json()["count"] == 0
    c.post("/api/usage/cleared", json={"ask_id": "task:7"})
    c.post("/api/usage/cleared", json={"ask_id": "task:7"})       # twice = once
    out = c.post("/api/usage/cleared", json={"ask_id": "habits"}).json()
    assert out == {"day": db.day_key(), "count": 2, "ask_ids": ["task:7", "habits"]}
    out = c.post("/api/usage/cleared", json={"ask_id": "task:7", "undo": True}).json()
    assert out["count"] == 1 and out["ask_ids"] == ["habits"]


def test_cleared_rows_stay_out_of_the_card_usage_summary(router_client):
    c = router_client(usage.router)
    usage.record("seen", "tasks", "needs")
    c.post("/api/usage/cleared", json={"ask_id": "task:7"})
    labels = [r["label"] for r in usage.summary()["labels"]]
    assert labels == ["tasks"]


def test_the_generic_usage_post_cannot_forge_a_cleared_row(router_client):
    c = router_client(usage.router)
    r = c.post("/api/usage", json={"events": [{"kind": "cleared", "label": "x"}]})
    assert r.status_code == 422
