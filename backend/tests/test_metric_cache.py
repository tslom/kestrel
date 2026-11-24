"""providers/metric_cache.upsert_days: a conflict must overwrite value AND refreshed_at (a forgotten `refreshed_at=excluded...` is the bug), and the returned count matches rows written."""

from app import db
from app.providers import metric_cache


def test_insert_counts_rows(dbfile):
    con = db.connect()
    with con:
        n = metric_cache.upsert_days(
            con, "stub_days",
            {"2026-07-20": {"a": 1.0, "b": 2.0}, "2026-07-21": {"a": 3.0}},
            "2026-07-22T10:00:00")
    assert n == 3
    rows = con.execute("SELECT COUNT(*) c FROM stub_days").fetchone()["c"]
    assert rows == 3
    con.close()


def test_conflict_overwrites_value_and_refreshed_at(dbfile):
    con = db.connect()
    with con:
        metric_cache.upsert_days(con, "stub_days",
                                 {"2026-07-20": {"a": 1.0}}, "2026-07-22T10:00:00")
    with con:
        metric_cache.upsert_days(con, "stub_days",
                                 {"2026-07-20": {"a": 9.0}}, "2026-07-22T11:00:00")
    row = con.execute(
        "SELECT value, refreshed_at FROM stub_days WHERE day='2026-07-20' AND metric='a'"
    ).fetchone()
    assert row["value"] == 9.0
    assert row["refreshed_at"] == "2026-07-22T11:00:00"  # the guarded clause
    # Still one row — upsert, not insert.
    assert con.execute("SELECT COUNT(*) c FROM stub_days").fetchone()["c"] == 1
    con.close()


def test_empty_input_writes_nothing(dbfile):
    con = db.connect()
    with con:
        n = metric_cache.upsert_days(con, "stub_days", {}, "2026-07-22T10:00:00")
    assert n == 0
    con.close()
