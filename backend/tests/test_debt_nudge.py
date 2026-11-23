"""Debts that ask again — the nudge cycle.

An open IOU is two facts, not one: the money, and whether you've said anything
about it lately. Only the first was stored, so the card could only ever sit in
HAS — "someone owes you $106" is true every day and actionable on none of them,
which is how it earned 44 impressions and 3 opens across nine days.

The cycle these pin: never asked -> asked (quiet a week) -> overdue -> asked
again (quiet another week), with settling the only exit.
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
    """Backdate the last ask. Written through the same column remind_debt
    uses, so this exercises the real read path rather than a parallel one."""
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
    """The point of the whole feature: an ask that went unanswered has to
    return, or it's the July conversation you never thought about again."""
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
    """The count is the part an overwritten timestamp loses, and it's what
    decides whether to keep asking or write it off."""
    d = _debt()
    for _ in range(3):
        db.remind_debt(d["id"])
    assert _only(db.debt_summary())["reminded_count"] == 3


def test_a_debt_you_owe_is_never_nudged(dbfile):
    """"I asked them" has no meaning pointed the other way — a debt you owe is
    chased by the other person, and a button for it would do nothing."""
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
    """`never` and `overdue` are different actions but both are your move, and
    the card's band depends on the total."""
    a = _debt(person="Sam")
    _debt(person="Ravi")                    # never asked
    _ask_days_ago(a["id"], DEBT_NUDGE_DAYS + 2)
    quiet = _debt(person="Dana")
    db.remind_debt(quiet["id"])

    s = db.debt_summary()
    assert s["to_chase"] == 2
    assert s["nudge_days"] == DEBT_NUDGE_DAYS


def test_an_unreadable_timestamp_reads_as_unasked(dbfile):
    """Fail toward asking. The opposite default would silence a debt forever
    on one bad write."""
    d = _debt()
    with db.session() as con, con:
        con.execute("UPDATE debts SET reminded_at='not a date' WHERE id=?",
                    (d["id"],))
    assert _only(db.debt_summary())["nudge"] == "never"


def test_reminding_a_missing_debt_is_none_not_an_error(dbfile):
    """The route turns None into a 404; a raise would be a 500."""
    assert db.remind_debt(9999) is None


def test_totals_still_come_out_right_alongside_the_nudge(dbfile):
    """The annotation pass rebuilds every row, so the sums are the thing most
    likely to break silently."""
    _debt(person="Sam", amount=30.0)
    _debt(person="Ravi", amount=12.5, direction="i_owe")

    s = db.debt_summary()
    assert s["owed_to_me"] == 30.0
    assert s["i_owe"] == 12.5
    assert s["net"] == 17.5
