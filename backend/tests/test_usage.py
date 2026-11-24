"""Desk usage (v45) — the counters that say whether salience.js is right.

Design: DESIGN.md → `app/usage.py`: desk usage counters, test-pinned.
"""

import sqlite3

import pytest

from app import db, usage


@pytest.fixture
def api(router_client):
    return router_client(usage.router)


def _seed(day, kind, label, band="", count=1, at=None):
    """Write a row for an arbitrary day — record() can only write today's."""
    with db.session() as con, con:
        con.execute(
            "INSERT INTO usage (day, kind, label, band, count, last_at)"
            " VALUES (?,?,?,?,?,?)"
            " ON CONFLICT(day, kind, label, band) DO UPDATE SET"
            "   count=usage.count+excluded.count, last_at=excluded.last_at",
            (day, kind, label, band, count, at or f"{day}T09:00:00"))


def _by_label(summary):
    return {r["label"]: r for r in summary["labels"]}


def test_repeat_bumps_count_on_one_row_per_day(dbfile):
    """One row per (day, kind, label, band), not per event."""
    for _ in range(20):
        usage.record("open", "finance")

    with db.session() as con:
        rows = [dict(r) for r in con.execute("SELECT * FROM usage")]
    assert len(rows) == 1
    assert rows[0]["count"] == 20
    assert rows[0]["day"] == db.day_key()


def test_band_splits_rows_for_the_same_card(dbfile):
    """A card going quiet then needs in one day is two rows."""
    usage.record("seen", "email", "quiet")
    usage.record("seen", "email", "needs")

    with db.session() as con:
        bands = sorted(r["band"] for r in con.execute(
            "SELECT band FROM usage WHERE label='email'"))
    assert bands == ["needs", "quiet"]


def test_record_rejects_unknown_kinds_and_bands(dbfile):
    usage.record("hover", "email")
    usage.record("seen", "email", "urgent")
    usage.record("seen", "")

    with db.session() as con:
        assert con.execute("SELECT COUNT(*) c FROM usage").fetchone()["c"] == 0


def test_record_never_raises(dbfile, monkeypatch):
    """A broken DB costs a log line, not a 500."""
    def boom():
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(db, "session", boom)
    usage.record("seen", "email", "needs")  # must not raise


def test_summary_survives_a_broken_db(dbfile, monkeypatch):
    def boom():
        raise sqlite3.OperationalError("no such table: usage")

    monkeypatch.setattr(db, "session", boom)
    out = usage.summary()
    assert out == {"labels": [], "commands": [], "days_covered": 0, "since": None}


def test_summary_is_empty_before_anything_is_recorded(dbfile):
    assert usage.summary()["labels"] == []
    assert usage.summary()["since"] is None


def test_glanceable_card_is_not_marked_idle(dbfile):
    """A card with something to say daily and never clicked is working, not dead."""
    for d in range(1, 15):
        _seed(f"2026-08-{d:02d}", "seen", "calendar", "needs")

    row = _by_label(usage.summary(days=30))["calendar"]
    assert row["touches"] == 0
    assert row["days_active"] == 14
    assert row["idle"] is False


def test_quiet_and_untouched_is_the_only_idle_case(dbfile):
    for d in range(1, 15):
        _seed(f"2026-08-{d:02d}", "seen", "watchlist", "quiet")

    row = _by_label(usage.summary(days=30))["watchlist"]
    assert row["days_seen"] == 14
    assert row["days_active"] == 0
    assert row["idle"] is True


def test_one_open_clears_the_idle_flag(dbfile):
    """One unfold is the strongest signal and spares the card."""
    for d in range(1, 15):
        _seed(f"2026-08-{d:02d}", "seen", "watchlist", "quiet")
    _seed("2026-08-09", "open", "watchlist")

    row = _by_label(usage.summary(days=30))["watchlist"]
    assert row["touches"] == 1
    assert row["idle"] is False


def test_a_new_card_is_never_idle(dbfile):
    """Too little history convicts nothing."""
    for d in range(1, 4):
        _seed(f"2026-08-{d:02d}", "seen", "courses", "quiet")

    assert _by_label(usage.summary(days=30))["courses"]["idle"] is False


def test_touches_sum_opens_and_sheets(dbfile):
    _seed("2026-08-01", "seen", "music", "quiet")
    _seed("2026-08-01", "open", "music", count=2)
    _seed("2026-08-02", "sheet", "music", count=3)

    row = _by_label(usage.summary(days=30))["music"]
    assert (row["opens"], row["sheet_opens"], row["touches"]) == (2, 3, 5)
    assert row["days_touched"] == 1  # opened on one day, sheeted on one day


def test_window_counts_recorded_days_not_calendar_days(dbfile):
    """The window counts back from the newest recorded day, not today."""
    for d in (1, 2, 3, 20, 21):
        _seed(f"2026-08-{d:02d}", "seen", "tasks", "needs")

    out = usage.summary(days=3)
    assert out["since"] == "2026-08-03"      # 3rd-newest recorded day
    assert out["days_covered"] == 3
    assert _by_label(out)["tasks"]["days_seen"] == 3


def test_least_touched_sorts_first(dbfile):
    """The cut list is ordered bottom first."""
    _seed("2026-08-01", "seen", "watchlist", "quiet")
    _seed("2026-08-01", "seen", "email", "needs")
    _seed("2026-08-01", "sheet", "email", count=9)

    assert [r["label"] for r in usage.summary()["labels"]] == ["watchlist", "email"]


def test_commands_are_reported_separately(dbfile):
    """A ⌘K command stays out of the card label table."""
    _seed("2026-08-01", "command", "act:refresh", count=4)
    _seed("2026-08-02", "command", "act:refresh", count=1)
    _seed("2026-08-01", "seen", "email", "needs")

    out = usage.summary()
    assert [r["label"] for r in out["labels"]] == ["email"]
    assert out["commands"] == [
        {"label": "act:refresh", "runs": 5, "days": 2, "last_at": "2026-08-02T09:00:00"}]


def test_sheets_with_no_card_stay_out_of_the_card_table(dbfile):
    """`status`, `review` and the usage sheet are openable but not desk cards, so are excluded."""
    _seed("2026-08-01", "seen", "email", "needs")
    _seed("2026-08-01", "sheet", "status", count=6)

    assert [r["label"] for r in usage.summary()["labels"]] == ["email"]


def test_post_records_a_batch(api):
    r = api.post("/api/usage", json={"events": [
        {"kind": "seen", "label": "email", "band": "needs"},
        {"kind": "seen", "label": "tasks", "band": "quiet"},
        {"kind": "open", "label": "tasks", "count": 2},
    ]})
    assert r.status_code == 200
    assert r.json()["recorded"] == 3

    rows = _by_label(usage.summary())
    assert rows["tasks"]["opens"] == 2
    assert rows["email"]["days_active"] == 1


def test_post_rejects_an_unknown_kind(api):
    r = api.post("/api/usage", json={"events": [
        {"kind": "hover", "label": "email"}]})
    assert r.status_code == 422


def test_post_rejects_an_oversized_batch(api):
    r = api.post("/api/usage", json={"events": [
        {"kind": "seen", "label": "x", "band": "quiet"}] * (usage.MAX_BATCH + 1)})
    assert r.status_code == 422


def test_summary_endpoint_clamps_the_range(api):
    _seed("2026-08-01", "seen", "email", "needs")
    assert api.get("/api/usage/summary?days=0").status_code == 200
    assert api.get("/api/usage/summary?days=99999").status_code == 200


def test_delete_clears_everything(api):
    _seed("2026-08-01", "seen", "email", "needs")
    assert api.delete("/api/usage").json()["cleared"] == 1
    assert usage.summary()["labels"] == []
