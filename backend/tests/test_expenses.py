"""Expenses claimed back from Dad (app/expenses.py): mark, send, paid."""

import pytest

from app import db, expenses


@pytest.fixture
def bank(dbfile, tmp_path, monkeypatch):
    """One checking account, one card, a few charges; exports go to a temp dir."""
    monkeypatch.setattr(expenses, "EXPENSES_DIR", tmp_path)
    now = "2026-09-01T00:00:00"
    with db.session() as con, con:
        con.execute("INSERT INTO plaid_items (item_id, access_token, env,"
                    " created_at) VALUES ('i', 'x', 'production', ?)", (now,))
        for acct, kind in (("chk", "depository"), ("card", "credit")):
            con.execute("INSERT INTO fin_accounts (account_id, item_id, name,"
                        " type, refreshed_at) VALUES (?, 'i', ?, ?, ?)",
                        (acct, acct, kind, now))
        for tid, acct, day, merchant, amount, cat, pending in (
            ("groc", "card", "2026-09-02", "Trader Joe's", 120.00, "FOOD_AND_DRINK", 0),
            ("book", "card", "2026-09-03", "USC Bookstore", 80.50, "GENERAL_MERCHANDISE", 0),
            ("pend", "card", "2026-09-04", "Lyft", 12.93, "TRANSPORTATION", 1),
            ("refund", "chk", "2026-09-05", "Amazon", -15.00, "GENERAL_MERCHANDISE", 0),
            # A card payment lands as an inflow on the card: never Dad's wire.
            ("cardpay", "card", "2026-09-10", None, -140.50, "LOAN_PAYMENTS", 0),
            ("wire", "chk", "2026-09-12", None, -140.50, "TRANSFER_IN", 0),
        ):
            con.execute(
                "INSERT INTO fin_transactions (transaction_id, item_id,"
                " account_id, day, name, merchant, amount, category, pending,"
                " refreshed_at) VALUES (?, 'i', ?, ?, ?, ?, ?, ?, ?, ?)",
                (tid, acct, day, merchant or "Transfer", merchant, amount,
                 cat, pending, now))
    return tmp_path


def _sent_day(batch_id, day):
    with db.session() as con, con:
        con.execute("UPDATE expense_batches SET sent_day=? WHERE id=?",
                    (day, batch_id))


def test_a_charge_is_claimed_whole_by_default(bank):
    row = expenses.add(transaction_id="book")
    assert (row["amount"], row["txn_amount"]) == (80.50, 80.50)
    assert row["merchant"] == "USC Bookstore"
    assert row["category"] == "General merchandise"


def test_part_of_a_charge_can_be_claimed_but_not_more_than_it(bank):
    assert expenses.add(transaction_id="groc", amount=40)["amount"] == 40
    e = expenses.add(transaction_id="book")
    with pytest.raises(ValueError, match="more than the whole"):
        expenses.update(e["id"], amount=81)


def test_a_charge_is_claimed_once(bank):
    expenses.add(transaction_id="groc")
    with pytest.raises(ValueError, match="already claimed"):
        expenses.add(transaction_id="groc", amount=10)


def test_incoming_money_is_refused(bank):
    with pytest.raises(ValueError, match="coming in"):
        expenses.add(transaction_id="refund")


# ---------- claims on pending charges ----------

def _post(con, pending_id, new_id, amount, day="2026-09-06"):
    """A Plaid posted charge naming its pending one, stored and followed as _sync_item does."""
    t = {"transaction_id": new_id, "pending_transaction_id": pending_id,
         "pending": False, "amount": amount, "date": day}
    con.execute(
        "INSERT INTO fin_transactions (transaction_id, item_id, account_id,"
        " day, name, merchant, amount, category, pending, refreshed_at)"
        " VALUES (?, 'i', 'card', ?, 'Lyft', 'Lyft', ?, 'TRANSPORTATION', 0,"
        " '2026-09-06T00:00:00')", (new_id, day, amount))
    moved = expenses.follow_posted(con, [t])
    con.execute("DELETE FROM fin_transactions WHERE transaction_id=?",
                (pending_id,))
    return moved


def test_a_pending_charge_can_be_claimed(bank):
    row = expenses.add(transaction_id="pend")
    assert row["claimed_pending"] == 1 and row["amount"] == 12.93
    [w] = expenses.summary()["waiting"]
    assert w["pending_state"] == "pending"


def test_the_claim_follows_the_charge_when_it_posts(bank):
    """A tip added at posting: the whole-charge claim follows the new total and the posted row shows claimed."""
    expenses.add(transaction_id="pend")
    with db.session() as con, con:
        assert _post(con, "pend", "pend-posted", 15.50) == 1
    [w] = expenses.summary()["waiting"]
    assert (w["transaction_id"], w["amount"], w["txn_amount"], w["day"]) == (
        "pend-posted", 15.50, 15.50, "2026-09-06")
    assert w["pending_state"] is None
    claimed = expenses.marks("2026-09-01..2026-09-30")["claimed"]
    assert "pend-posted" in claimed and "pend" not in claimed


def test_a_partial_claim_keeps_its_amount_but_not_past_the_total(bank):
    e = expenses.add(transaction_id="pend", amount=10)
    with db.session() as con, con:
        _post(con, "pend", "pend-posted", 11.00)
    assert expenses.summary()["waiting"][0]["amount"] == 10
    expenses.update(e["id"], amount=11)
    with db.session() as con, con:
        con.execute("UPDATE expenses SET claimed_pending=1,"
                    " transaction_id='pend2' WHERE id=?", (e["id"],))
        _post(con, "pend2", "pend2-posted", 8.00)
    assert expenses.summary()["waiting"][0]["amount"] == 8.00


def test_a_sent_claim_stays_frozen_and_flags_the_new_amount(bank):
    """A sent claim keeps its exported $12.93 and flags the new settled amount."""
    expenses.add(transaction_id="pend")
    b = expenses.export()
    with db.session() as con, con:
        _post(con, "pend", "pend-posted", 15.50)
    [item] = expenses.summary()["batches"][0]["items"]
    assert (item["transaction_id"], item["amount"], item["posted_amount"]) == (
        "pend-posted", 12.93, 15.50)
    assert expenses.summary()["batches"][0]["total"] == b["total"]


def test_a_sent_claim_that_posts_unchanged_raises_no_flag(bank):
    expenses.add(transaction_id="pend")
    expenses.export()
    with db.session() as con, con:
        _post(con, "pend", "pend-posted", 12.93)
    assert expenses.summary()["batches"][0]["items"][0]["posted_amount"] is None


def test_a_hold_that_never_posts_is_flagged(bank):
    expenses.add(transaction_id="pend")
    with db.session() as con, con:
        con.execute("DELETE FROM fin_transactions WHERE transaction_id='pend'")
    assert expenses.summary()["waiting"][0]["pending_state"] == "vanished"


def test_a_dropped_cache_is_not_a_cancelled_charge(bank):
    expenses.add(transaction_id="pend")
    with db.session() as con, con:
        con.execute("DELETE FROM fin_transactions")
    assert expenses.summary()["waiting"][0]["pending_state"] is None


def test_following_never_breaks_the_sync(bank):
    """A posted id already claimed is skipped, not a UNIQUE error inside finance's write transaction."""
    expenses.add(transaction_id="pend")
    expenses.add(transaction_id="groc")
    with db.session() as con, con:
        moved = expenses.follow_posted(con, [{
            "transaction_id": "groc", "pending_transaction_id": "pend",
            "pending": False, "amount": 12.93, "date": "2026-09-04"}])
    assert moved == 0


def test_the_bank_sync_moves_the_claim(bank, monkeypatch):
    """End to end through finance._sync_item with a faked Plaid page (posted in `added`, pending in `removed`)."""
    from app.providers import finance, plaid

    expenses.add(transaction_id="pend")

    def api(path, _body):
        if path == "/transactions/sync":
            return {"added": [{
                "transaction_id": "pend-posted", "pending_transaction_id": "pend",
                "account_id": "card", "date": "2026-09-05", "name": "Lyft",
                "merchant_name": "Lyft", "amount": 14.00, "pending": False}],
                "modified": [], "removed": [{"transaction_id": "pend"}],
                "next_cursor": "c2", "has_more": False}
        return {"accounts": []}

    monkeypatch.setattr(plaid, "api", api)
    with db.session() as con:
        finance._sync_item(con, {"item_id": "i", "access_token": "x",
                                 "cursor": None})
    [w] = expenses.summary()["waiting"]
    assert (w["transaction_id"], w["amount"]) == ("pend-posted", 14.00)


def test_manual_expense_needs_an_amount_and_a_merchant(bank):
    with pytest.raises(ValueError):
        expenses.add(merchant="Venmo — haircut")
    with pytest.raises(ValueError):
        expenses.add(amount=20)
    row = expenses.add(merchant="Venmo — haircut", amount=20, category="Personal")
    assert row["transaction_id"] is None and row["day"] == db.day_key()


def test_export_writes_the_sheet_and_freezes_what_it_sent(bank):
    from openpyxl import load_workbook

    a = expenses.add(transaction_id="groc", amount=60, note="split w/ roommate")
    expenses.add(merchant="Cash — laundry", amount=20.00, day="2026-09-01")
    out = expenses.export()

    assert (out["count"], out["total"]) == (2, 80.00)
    ws = load_workbook(out["file"])["Expenses"]
    rows = list(ws.iter_rows(values_only=True))
    assert rows[0] == ("Date", "Merchant", "Category", "Note", "Amount")
    assert [r[1] for r in rows[1:3]] == ["Cash — laundry", "Trader Joe's"]
    assert "part of a $120.00 charge" in rows[2][3]
    assert rows[-1][0] == "Total" and rows[-1][4] == "=SUM(E2:E3)"

    with pytest.raises(ValueError, match="already sent"):
        expenses.update(a["id"], category="Groceries")
    with pytest.raises(ValueError, match="nothing waiting"):
        expenses.export()


def test_export_never_overwrites_an_earlier_sheet(bank):
    expenses.add(transaction_id="groc")
    first = expenses.export()["file"]
    expenses.add(transaction_id="book")
    second = expenses.export()["file"]
    assert first != second and second.endswith("(2).xlsx")


def test_export_moves_the_reviewed_line_past_posted_charges_only(bank):
    expenses.add(transaction_id="groc")
    expenses.export()
    reviewed = set(expenses.marks("2026-09-01..2026-09-30")["reviewed"])
    assert {"groc", "book", "refund", "wire"} <= reviewed
    assert "pend" not in reviewed


def test_marks_carry_each_claims_status(bank):
    expenses.add(transaction_id="groc")
    expenses.export()
    expenses.add(transaction_id="book", amount=10)
    claimed = expenses.marks("2026-09-01..2026-09-30")["claimed"]
    assert claimed["groc"]["status"] == "sent"
    assert claimed["book"] == {"id": claimed["book"]["id"], "amount": 10,
                               "status": "waiting"}


def test_the_wire_is_suggested_and_the_card_payment_is_not(bank):
    expenses.add(transaction_id="groc", amount=60)
    expenses.add(transaction_id="book")
    b = expenses.export()
    _sent_day(b["batch_id"], "2026-09-08")

    s = expenses.summary()
    assert s["outstanding"] == 140.50 and s["wires"] == 1
    assert s["batches"][0]["wire"]["transaction_id"] == "wire"

    expenses.set_paid(b["batch_id"], transaction_id="wire")
    s = expenses.summary()
    assert s["outstanding"] == 0 and s["batches"][0]["paid_amount"] == 140.50


def test_a_deposit_before_the_batch_went_out_is_not_its_payment(bank):
    expenses.add(transaction_id="groc", amount=60)
    expenses.add(transaction_id="book")
    b = expenses.export()
    _sent_day(b["batch_id"], "2026-09-13")
    assert expenses.summary()["batches"][0]["wire"] is None


def test_one_deposit_pays_one_batch(bank):
    ids = []
    for tid in ("groc", "book"):
        expenses.add(transaction_id=tid, amount=70.25)
        ids.append(expenses.export()["batch_id"])
        _sent_day(ids[-1], "2026-09-08")
    # Both batches total 70.25 and neither matches 140.50: no suggestion.
    assert expenses.summary()["wires"] == 0

    with db.session() as con, con:
        con.execute("UPDATE fin_transactions SET amount=-70.25 WHERE"
                    " transaction_id='wire'")
    s = expenses.summary()
    # Only the older batch gets the one matching deposit.
    wires = {b["id"]: b["wire"] for b in s["batches"]}
    assert wires[ids[0]]["transaction_id"] == "wire" and wires[ids[1]] is None

    expenses.set_paid(ids[0], transaction_id="wire")
    with pytest.raises(ValueError, match="already paid batch"):
        expenses.set_paid(ids[1], transaction_id="wire")


def test_unsend_puts_the_rows_back(bank):
    e = expenses.add(transaction_id="groc")
    b = expenses.export()
    expenses.unsend(b["batch_id"])
    assert [r["id"] for r in expenses.summary()["waiting"]] == [e["id"]]
    assert expenses.summary()["batches"] == []


def test_a_paid_batch_cant_be_unsent(bank):
    expenses.add(transaction_id="groc")
    b = expenses.export()
    expenses.set_paid(b["batch_id"])
    with pytest.raises(ValueError, match="already paid"):
        expenses.unsend(b["batch_id"])


def test_a_claim_outlives_the_plaid_cache(bank):
    """fin_transactions is droppable. What was claimed is not."""
    expenses.add(transaction_id="groc", amount=40)
    with db.session() as con, con:
        con.execute("DELETE FROM fin_transactions")
    [row] = expenses.summary()["waiting"]
    assert (row["merchant"], row["amount"]) == ("Trader Joe's", 40)
