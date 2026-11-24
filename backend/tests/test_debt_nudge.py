"""Debts that ask again — the nudge cycle.

An open IOU is the money plus whether you've asked lately. Cycle pinned: never asked
-> asked (quiet a week) -> overdue -> asked again (quiet another week); settling is the
only exit. Design: DESIGN.md → `backend/tests/test_debt_nudge.py`.
"""

from datetime import datetime, timedelta


from app import db
from app.config import DEBT_NUDGE_DAYS


def _debt(person="Sam", amount=20.0, direction="owed_to_me"):
    return db.create_debt(person, amount, direction=direction)


def _only(summary):
    assert len(summary["debts"]) == 1
    return summary["debts"][0]


def _ask_days_ago(debt_id: int, days: int) -> None:
    """Backdate the last ask through the column remind_debt uses."""
    when = (datetime.now() - timedelta(days=days)).isoformat(timespec="seconds")
    with db.session() as con, con:
        con.execute("UPDATE debts SET reminded_at=? WHERE id=?", (when, debt_id))


def test_an_unasked_debt_is_your_move(dbfile):
    _debt()
    assert _only(db.debt_summary())["nudge"] == "never"


def test_asking_buys_a_week_of_quiet(dbfile):
    d = _debt()
    db.remind_debt(d["id"])
    row = _only(db.debt_summary())

    assert row["nudge"] == "waiting"
    assert row["reminded_days_ago"] == 0


def test_it_comes_back_after_the_window(dbfile):
    """An unanswered ask comes back after the window."""
    d = _debt()
    _ask_days_ago(d["id"], DEBT_NUDGE_DAYS)
    row = _only(db.debt_summary())

    assert row["nudge"] == "overdue"
    assert row["reminded_days_ago"] == DEBT_NUDGE_DAYS


def test_the_day_before_the_window_is_still_quiet(dbfile):
    d = _debt()
    _ask_days_ago(d["id"], DEBT_NUDGE_DAYS - 1)
    assert _only(db.debt_summary())["nudge"] == "waiting"


def test_asking_again_resets_the_clock(dbfile):
    """"To make it go away I would have to click that I reminded them again."""
    d = _debt()
    _ask_days_ago(d["id"], DEBT_NUDGE_DAYS + 3)
    assert _only(db.debt_summary())["nudge"] == "overdue"

    db.remind_debt(d["id"])
    row = _only(db.debt_summary())
    assert row["nudge"] == "waiting"
    assert row["reminded_count"] == 1


def test_every_ask_is_counted(dbfile):
    """Every ask is counted (a timestamp alone is overwritten)."""
    d = _debt()
    for _ in range(3):
        db.remind_debt(d["id"])
    assert _only(db.debt_summary())["reminded_count"] == 3


def test_a_debt_you_owe_is_never_nudged(dbfile):
    """A debt you owe is never nudged."""
    _debt(direction="i_owe")
    row = _only(db.debt_summary())

    assert row["nudge"] is None
    assert db.debt_summary()["to_chase"] == 0


def test_settling_takes_it_out_of_the_chase(dbfile):
    d = _debt()
    db.remind_debt(d["id"])
    _ask_days_ago(d["id"], DEBT_NUDGE_DAYS + 1)
    assert db.debt_summary()["to_chase"] == 1

    with db.session() as con, con:
        con.execute("UPDATE debts SET settled=1 WHERE id=?", (d["id"],))
    assert db.debt_summary()["to_chase"] == 0


def test_to_chase_counts_both_actionable_states(dbfile):
    """to_chase counts both `never` and `overdue` (the card's band uses the total)."""
    a = _debt(person="Sam")
    _debt(person="Ravi")                    # never asked
    _ask_days_ago(a["id"], DEBT_NUDGE_DAYS + 2)
    quiet = _debt(person="Dana")
    db.remind_debt(quiet["id"])

    s = db.debt_summary()
    assert s["to_chase"] == 2
    assert s["nudge_days"] == DEBT_NUDGE_DAYS


def test_an_unreadable_timestamp_reads_as_unasked(dbfile):
    """An unreadable timestamp reads as unasked (fail toward asking)."""
    d = _debt()
    with db.session() as con, con:
        con.execute("UPDATE debts SET reminded_at='not a date' WHERE id=?",
                    (d["id"],))
    assert _only(db.debt_summary())["nudge"] == "never"


def test_reminding_a_missing_debt_is_none_not_an_error(dbfile):
    """The route turns None into a 404; a raise would be a 500."""
    assert db.remind_debt(9999) is None


def test_totals_still_come_out_right_alongside_the_nudge(dbfile):
    """Totals survive the annotation pass (it rebuilds every row)."""
    _debt(person="Sam", amount=30.0)
    _debt(person="Ravi", amount=12.5, direction="i_owe")

    s = db.debt_summary()
    assert s["owed_to_me"] == 30.0
    assert s["i_owe"] == 12.5
    assert s["net"] == 17.5
