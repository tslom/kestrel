"""Expenses claimed back from Dad: mark them, send a spreadsheet, see it paid.

Three steps, each one a table (schema and why in db.py's migration):

  * **Mark.** An expense is a claim, not a transaction. It can come off a bank
    charge (whole or in part) or be typed in by hand for cash and Venmo. It
    snapshots what it was made from, so the Plaid cache stays droppable.
  * **Send.** Export writes the waiting rows to an .xlsx and stamps them with
    a batch. A sent row is frozen from then on, because it has to keep
    matching the file Dad has. The same export records every posted
    transaction as reviewed, which is where the Money sheet draws its
    "reviewed through here" line.
  * **Paid.** A batch is paid when the money arrives. `_wire_for` suggests the
    deposit that looks like it, but only you mark it paid. A coincidental
    $200 transfer that closed a $200 batch on its own would be a silent wrong
    answer about money.

The backend writes the file into EXPENSES_DIR (~/Downloads) instead of the page
downloading it: the desktop shell's webview has downloads switched off, and
this way the browser and the app behave the same.
"""

from datetime import date, datetime
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from . import db, macos
from .config import EXPENSES_DIR
from .providers import ranges

router = APIRouter(prefix="/api/expenses")

# How close a deposit has to be to a batch's total to be suggested as its
# payment: a dollar, or 2% on a big batch. That leaves room for a rounded-up
# transfer or a wire fee, and is still tight enough that the weekly $40
# allowance doesn't look like every small batch's payment.
_WIRE_SLACK_ABS = 1.00
_WIRE_SLACK_PCT = 0.02

# Sent batches shown on the card once they're paid. Unpaid ones always show.
_PAID_SHOWN = 3


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _money(v) -> float:
    return round(float(v), 2)


def pretty_category(c: str | None) -> str | None:
    """Plaid's FOOD_AND_DRINK as Food and drink. Mirrors prettyCategory in
    MoneySheet.jsx, so a claimed row starts out named the way its charge
    already reads on screen."""
    if not c:
        return None
    words = c.replace("_", " ").lower()
    return words[:1].upper() + words[1:]


def _row(con, expense_id: int) -> dict | None:
    r = con.execute("SELECT * FROM expenses WHERE id=?", (expense_id,)).fetchone()
    return dict(r) if r else None


def _unsent(con, expense_id: int) -> dict:
    """The row, if it can still be changed. Raises LookupError for no such row
    and ValueError for one already sent."""
    row = _row(con, expense_id)
    if row is None:
        raise LookupError(f"no expense {expense_id}")
    if row["batch_id"] is not None:
        raise ValueError(f"expense {expense_id} was already sent (batch "
                         f"{row['batch_id']}) — unsend the batch first to "
                         "change it")
    return row


def _check_amount(amount: float, cap: float | None) -> float:
    amount = _money(amount)
    if amount <= 0:
        raise ValueError("amount must be greater than zero")
    if cap is not None and amount > cap + 0.005:
        raise ValueError(f"amount ${amount:.2f} is more than the whole "
                         f"charge (${cap:.2f})")
    return amount


# ---------- mark ----------

def add(transaction_id: str | None = None, amount: float | None = None,
        category: str | None = None, note: str | None = None,
        day: str | None = None, merchant: str | None = None) -> dict:
    """Claim a bank charge (by transaction_id) or add a manual expense.

    From a charge, amount defaults to the whole thing, and a smaller one is a
    partial claim. A pending charge can be claimed: Plaid gives it a new id
    when it posts, and follow_posted moves the claim onto that one."""
    now = _now()
    with db.session() as con:
        if transaction_id:
            t = con.execute(
                "SELECT transaction_id, day, name, merchant, amount, category,"
                " pending FROM fin_transactions WHERE transaction_id=?",
                (transaction_id,)).fetchone()
            if t is None:
                raise LookupError(f"no transaction {transaction_id}")
            if t["amount"] <= 0:
                raise ValueError("that's money coming in, not an expense")
            taken = con.execute("SELECT id FROM expenses WHERE transaction_id=?",
                                (transaction_id,)).fetchone()
            if taken:
                raise ValueError(f"already claimed as expense {taken['id']} — "
                                 "change its amount instead")
            txn_amount = _money(t["amount"])
            amount = _check_amount(txn_amount if amount is None else amount,
                                   txn_amount)
            values = (transaction_id, t["day"],
                      (t["merchant"] or t["name"] or "Unknown").strip(),
                      txn_amount, amount,
                      category or pretty_category(t["category"]), note,
                      int(bool(t["pending"])))
        else:
            if amount is None:
                raise ValueError("amount is required for a manual expense")
            if not (merchant or "").strip():
                raise ValueError("say what it was for (merchant) for a manual "
                                 "expense")
            day = day or db.day_key()
            date.fromisoformat(day)      # ValueError on a malformed day
            values = (None, day, merchant.strip(), None,
                      _check_amount(amount, None), category, note, 0)
        with con:
            cur = con.execute(
                "INSERT INTO expenses (transaction_id, day, merchant,"
                " txn_amount, amount, category, note, claimed_pending,"
                " created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (*values, now, now))
        return _row(con, cur.lastrowid)


def follow_posted(con, posted: list[dict]) -> int:
    """Move claims made on pending charges onto the charges that replaced
    them. Called by finance's sync with Plaid's raw `added` rows, inside the
    same write transaction that stores them. Returns how many moved.

    What moves with the claim depends on whether it was sent. An unsent
    claim takes the posted day and amount. A claim for the whole charge
    follows the new total, and a partial one keeps its amount, capped at the
    new total. A sent claim keeps its amount and day because they're in the
    spreadsheet already. A different final amount is recorded in
    `posted_amount` for the card to flag."""
    moved = 0
    now = _now()
    for t in posted:
        old = t.get("pending_transaction_id")
        if not old or t.get("pending"):
            continue
        row = con.execute("SELECT * FROM expenses WHERE transaction_id=?",
                          (old,)).fetchone()
        # The posted id already claimed would trip the UNIQUE constraint, and
        # this runs inside the sync's write: it must never be what fails it.
        if row is None or con.execute(
                "SELECT 1 FROM expenses WHERE transaction_id=?",
                (t["transaction_id"],)).fetchone():
            continue
        new_total = _money(t["amount"])
        if row["batch_id"] is None and new_total > 0:
            whole = row["txn_amount"] is not None and \
                abs(row["amount"] - row["txn_amount"]) < 0.005
            amount = new_total if whole else min(row["amount"], new_total)
            con.execute(
                "UPDATE expenses SET transaction_id=?, day=?, txn_amount=?,"
                " amount=?, claimed_pending=0, updated_at=? WHERE id=?",
                (t["transaction_id"], t["date"], new_total, amount, now,
                 row["id"]))
        else:
            differs = row["txn_amount"] is None or \
                abs(new_total - row["txn_amount"]) >= 0.005
            con.execute(
                "UPDATE expenses SET transaction_id=?, claimed_pending=0,"
                " posted_amount=?, updated_at=? WHERE id=?",
                (t["transaction_id"], new_total if differs else None, now,
                 row["id"]))
        moved += 1
    return moved


def update(expense_id: int, **fields) -> dict:
    """Change an unsent expense's amount, category, note, day or merchant.
    Passing "" for category or note clears it."""
    allowed = {"amount", "category", "note", "day", "merchant"}
    fields = {k: v for k, v in fields.items() if k in allowed and v is not None}
    if not fields:
        raise ValueError("nothing to change — pass amount, category, note, "
                         "day or merchant")
    with db.session() as con:
        row = _unsent(con, expense_id)
        if "amount" in fields:
            fields["amount"] = _check_amount(fields["amount"], row["txn_amount"])
        if "day" in fields:
            date.fromisoformat(fields["day"])
        if "merchant" in fields and not str(fields["merchant"]).strip():
            raise ValueError("merchant can't be empty")
        for k in ("category", "note"):
            if k in fields:
                fields[k] = str(fields[k]).strip() or None
        sets = ", ".join(f"{k}=?" for k in fields)
        with con:
            con.execute(f"UPDATE expenses SET {sets}, updated_at=? WHERE id=?",
                        (*fields.values(), _now(), expense_id))
        return _row(con, expense_id)


def remove(expense_id: int) -> dict:
    with db.session() as con:
        row = _unsent(con, expense_id)
        with con:
            con.execute("DELETE FROM expenses WHERE id=?", (expense_id,))
        return row


# ---------- review ----------

def mark_reviewed(con) -> int:
    """Record every posted charge as looked at. Pending ones are skipped:
    they get a new id when they post, and that new id is what needs looking
    at."""
    return con.execute(
        "INSERT OR IGNORE INTO expense_reviewed (transaction_id, reviewed_at)"
        " SELECT transaction_id, ? FROM fin_transactions WHERE pending=0",
        (_now(),)).rowcount


def review() -> dict:
    """"Nothing for Dad in this lot": move the line up without sending."""
    with db.session() as con, con:
        return {"reviewed": mark_reviewed(con)}


def marks(range_token: str) -> dict:
    """What the Money sheet's transaction list needs to draw claims and the
    reviewed line, for the charges in one range."""
    rng = ranges.parse_range(range_token)
    with db.session() as con:
        claimed = con.execute(
            "SELECT e.transaction_id, e.id, e.amount, e.batch_id,"
            " b.paid_at IS NOT NULL AS paid"
            " FROM expenses e JOIN fin_transactions t USING (transaction_id)"
            " LEFT JOIN expense_batches b ON b.id=e.batch_id"
            " WHERE t.day BETWEEN ? AND ?",
            (rng.start_key, rng.end_key)).fetchall()
        reviewed = con.execute(
            "SELECT r.transaction_id FROM expense_reviewed r"
            " JOIN fin_transactions t USING (transaction_id)"
            " WHERE t.day BETWEEN ? AND ?",
            (rng.start_key, rng.end_key)).fetchall()
    return {
        "claimed": {r["transaction_id"]: {
            "id": r["id"], "amount": r["amount"],
            "status": ("paid" if r["paid"] else
                       "sent" if r["batch_id"] is not None else "waiting"),
        } for r in claimed},
        "reviewed": [r["transaction_id"] for r in reviewed],
    }


# ---------- send ----------

def _write_xlsx(path: Path, rows: list[dict], sent_day: str) -> None:
    from openpyxl import Workbook
    from openpyxl.styles import Font

    money_fmt = '"$"#,##0.00'
    bold = Font(bold=True)
    wb = Workbook()
    ws = wb.active
    ws.title = "Expenses"
    ws.append(["Date", "Merchant", "Category", "Note", "Amount"])
    for c in ws[1]:
        c.font = bold
    for r in rows:
        note = r["note"] or ""
        if r["txn_amount"] is not None and r["amount"] < r["txn_amount"] - 0.005:
            part = f"part of a ${r['txn_amount']:,.2f} charge"
            note = f"{note} ({part})" if note else part
        ws.append([date.fromisoformat(r["day"]), r["merchant"],
                   r["category"], note or None, r["amount"]])
    last = len(rows) + 1
    ws.append([])
    ws.append(["Total", None, None, None, f"=SUM(E2:E{last})"])
    total_row = ws.max_row
    ws.cell(total_row, 1).font = bold
    ws.cell(total_row, 5).font = bold
    for row in ws.iter_rows(min_row=2, max_row=total_row):
        row[0].number_format = "mmm d, yyyy"
        row[4].number_format = money_fmt
    for col, width in zip("ABCDE", (14, 30, 20, 40, 12)):
        ws.column_dimensions[col].width = width
    ws.freeze_panes = "A2"

    # The same money grouped by category. SUMIF formulas, not typed-in
    # numbers, so the two sheets can't disagree even if a row is edited.
    cats = sorted({r["category"] or "" for r in rows})
    by = wb.create_sheet("By category")
    by.append(["Category", "Amount"])
    for c in by[1]:
        c.font = bold
    for cat in cats:
        # A blank criterion matches the blank cells; quotes double up to stay
        # inside the formula's string.
        crit = cat.replace('"', '""')
        by.append([cat or "(none)",
                   f'=SUMIF(Expenses!C2:C{last},"{crit}",Expenses!E2:E{last})'])
    by.append(["Total", f"=SUM(B2:B{len(cats) + 1})"])
    by.cell(by.max_row, 1).font = bold
    for row in by.iter_rows(min_row=2):
        row[1].number_format = money_fmt
    by.column_dimensions["A"].width = 24
    by.column_dimensions["B"].width = 12
    wb.properties.title = f"Expenses, {sent_day}"
    wb.save(path)


def _free_path(stem: str) -> Path:
    EXPENSES_DIR.mkdir(parents=True, exist_ok=True)
    path = EXPENSES_DIR / f"{stem}.xlsx"
    n = 2
    while path.exists():
        path = EXPENSES_DIR / f"{stem} ({n}).xlsx"
        n += 1
    return path


def export() -> dict:
    """Write every waiting expense to a spreadsheet, then stamp them sent and
    move the reviewed line up.

    The file is written first and the stamping happens after. A failed write
    therefore leaves nothing marked sent that Dad never got, and a failed
    stamp deletes the file, so the next export doesn't send the same rows
    twice under two names."""
    with db.session() as con:
        rows = [dict(r) for r in con.execute(
            "SELECT * FROM expenses WHERE batch_id IS NULL ORDER BY day, id")]
        if not rows:
            raise ValueError("nothing waiting to send")
        sent_day = db.day_key()
        total = _money(sum(r["amount"] for r in rows))
        path = _free_path(f"Expenses {sent_day}")
        _write_xlsx(path, rows, sent_day)
        try:
            with con:
                cur = con.execute(
                    "INSERT INTO expense_batches (sent_at, sent_day, total, file)"
                    " VALUES (?,?,?,?)", (_now(), sent_day, total, str(path)))
                batch_id = cur.lastrowid
                marks_ = ",".join("?" * len(rows))
                con.execute(f"UPDATE expenses SET batch_id=? WHERE id IN ({marks_})",
                            (batch_id, *[r["id"] for r in rows]))
                mark_reviewed(con)
        except Exception:
            path.unlink(missing_ok=True)
            raise
    return {"batch_id": batch_id, "file": str(path), "name": path.name,
            "total": total, "count": len(rows)}


def unsend(batch_id: int) -> dict:
    """Undo an export: the rows go back to waiting and the batch is gone. The
    file stays where it is, since it may already be in an email. Refused once
    the batch is paid."""
    with db.session() as con:
        b = con.execute("SELECT * FROM expense_batches WHERE id=?",
                        (batch_id,)).fetchone()
        if b is None:
            raise LookupError(f"no batch {batch_id}")
        if b["paid_at"]:
            raise ValueError(f"batch {batch_id} is already paid — mark it "
                             "unpaid first")
        with con:
            n = con.execute("UPDATE expenses SET batch_id=NULL WHERE batch_id=?",
                            (batch_id,)).rowcount
            con.execute("DELETE FROM expense_batches WHERE id=?", (batch_id,))
    return {"batch_id": batch_id, "returned": n, "file": b["file"]}


def reveal(batch_id: int) -> str:
    with db.session() as con:
        b = con.execute("SELECT file FROM expense_batches WHERE id=?",
                        (batch_id,)).fetchone()
    if b is None:
        raise LookupError(f"no batch {batch_id}")
    if not b["file"] or not Path(b["file"]).exists():
        raise ValueError("the spreadsheet isn't where it was saved any more")
    macos.reveal(b["file"])
    return b["file"]


# ---------- paid ----------

def _inflows(con):
    """Money that came in on a bank account, newest first. A credit card's
    payment shows up as an inflow on the card, and it is never a transfer
    from Dad."""
    return con.execute(
        "SELECT t.transaction_id, t.day, t.name, t.merchant, -t.amount AS amount"
        " FROM fin_transactions t"
        " LEFT JOIN fin_accounts a ON a.account_id=t.account_id"
        " WHERE t.amount < 0 AND t.pending=0"
        " AND COALESCE(a.type, '') != 'credit'"
        " AND COALESCE(t.category, '') != 'LOAN_PAYMENTS'"
        " ORDER BY t.day DESC").fetchall()


def _wire_for(batch: dict, inflows, used: set[str]) -> dict | None:
    """The deposit that most looks like this batch's payment: on or after the
    day it was sent, within slack of its total, not already taken by another
    batch. The closest amount wins, then the earliest day."""
    slack = max(_WIRE_SLACK_ABS, batch["total"] * _WIRE_SLACK_PCT)
    fits = [t for t in inflows
            if t["day"] >= batch["sent_day"]
            and t["transaction_id"] not in used
            and abs(t["amount"] - batch["total"]) <= slack]
    if not fits:
        return None
    t = min(fits, key=lambda t: (abs(t["amount"] - batch["total"]), t["day"]))
    return {"transaction_id": t["transaction_id"], "day": t["day"],
            "amount": _money(t["amount"]),
            "name": t["merchant"] or t["name"]}


def set_paid(batch_id: int, paid: bool = True,
             transaction_id: str | None = None) -> dict:
    """Mark a batch paid, optionally naming the deposit that paid it, or
    re-open it."""
    with db.session() as con:
        if con.execute("SELECT 1 FROM expense_batches WHERE id=?",
                       (batch_id,)).fetchone() is None:
            raise LookupError(f"no batch {batch_id}")
        amount = None
        if paid and transaction_id:
            t = con.execute("SELECT amount FROM fin_transactions"
                            " WHERE transaction_id=?", (transaction_id,)).fetchone()
            if t is None or t["amount"] >= 0:
                raise ValueError(f"{transaction_id} isn't a deposit")
            other = con.execute(
                "SELECT id FROM expense_batches WHERE paid_txn_id=? AND id!=?",
                (transaction_id, batch_id)).fetchone()
            if other:
                raise ValueError(f"that deposit already paid batch {other['id']}")
            amount = _money(-t["amount"])
        with con:
            con.execute(
                "UPDATE expense_batches SET paid_at=?, paid_txn_id=?,"
                " paid_amount=? WHERE id=?",
                (_now() if paid else None, transaction_id if paid else None,
                 amount, batch_id))
    return next(b for b in summary()["batches_all"] if b["id"] == batch_id)


# ---------- read ----------

def _pending_state(con, rows: list[dict]) -> None:
    """Tag each row claimed while pending: `pending` while its charge is
    still waiting to post, `vanished` once the charge is gone without a
    posted replacement (a hold that was released, a cancelled order).
    Every other row gets None.

    An empty fin_transactions means the cache was dropped, not that every
    charge was cancelled, so nothing is called vanished then."""
    ids = [r["transaction_id"] for r in rows if r.get("claimed_pending")]
    have = set()
    if ids:
        have = {r[0] for r in con.execute(
            f"SELECT transaction_id FROM fin_transactions WHERE transaction_id"
            f" IN ({','.join('?' * len(ids))})", ids)}
    cache = bool(ids) and con.execute(
        "SELECT 1 FROM fin_transactions LIMIT 1").fetchone() is not None
    for r in rows:
        r["pending_state"] = None
        if r.get("claimed_pending"):
            if r["transaction_id"] in have:
                r["pending_state"] = "pending"
            elif cache:
                r["pending_state"] = "vanished"


def summary() -> dict:
    """Everything the card draws: what's waiting to go, and the batches that
    went (every unpaid one, plus the last few paid), each unpaid one carrying
    its suggested deposit."""
    with db.session() as con:
        waiting = [dict(r) for r in con.execute(
            "SELECT * FROM expenses WHERE batch_id IS NULL ORDER BY day DESC, id DESC")]
        batches = [dict(r) for r in con.execute(
            "SELECT * FROM expense_batches ORDER BY sent_at DESC, id DESC")]
        items: dict[int, list[dict]] = {}
        for r in con.execute("SELECT * FROM expenses WHERE batch_id IS NOT NULL"
                             " ORDER BY day, id"):
            items.setdefault(r["batch_id"], []).append(dict(r))
        inflows = _inflows(con) if any(not b["paid_at"] for b in batches) else []
        categories = [r[0] for r in con.execute(
            "SELECT category FROM expenses WHERE category IS NOT NULL"
            " GROUP BY category ORDER BY COUNT(*) DESC, category")]
        _pending_state(con, waiting + [r for rs in items.values() for r in rs])

    used = {b["paid_txn_id"] for b in batches if b["paid_txn_id"]}
    # Oldest unpaid first, so when two batches could match one deposit the
    # one that has waited longer gets it.
    for b in sorted(batches, key=lambda b: (b["sent_at"], b["id"])):
        b["items"] = items.get(b["id"], [])
        b["wire"] = None
        if not b["paid_at"]:
            b["wire"] = _wire_for(b, inflows, used)
            if b["wire"]:
                used.add(b["wire"]["transaction_id"])
    unpaid = [b for b in batches if not b["paid_at"]]
    paid = [b for b in batches if b["paid_at"]]
    return {
        "waiting": waiting,
        "waiting_total": _money(sum(r["amount"] for r in waiting)),
        "batches": unpaid + paid[:_PAID_SHOWN],
        "batches_all": batches,
        "outstanding": _money(sum(b["total"] for b in unpaid)),
        "wires": sum(1 for b in unpaid if b["wire"]),
        "categories": categories,
    }


# ---------- routes ----------

def _http(fn, *a, **kw):
    try:
        return fn(*a, **kw)
    except LookupError as e:
        raise HTTPException(404, str(e).strip("'\""))
    except ValueError as e:
        raise HTTPException(400, str(e))


class ExpenseIn(BaseModel):
    transaction_id: str | None = None
    amount: float | None = None
    category: str | None = None
    note: str | None = None
    day: str | None = None
    merchant: str | None = None


class ExpensePatch(BaseModel):
    amount: float | None = None
    category: str | None = None
    note: str | None = None
    day: str | None = None
    merchant: str | None = None


class PaidIn(BaseModel):
    paid: bool = True
    transaction_id: str | None = None


@router.get("")
def get_expenses():
    out = summary()
    out.pop("batches_all")
    return out


@router.get("/marks")
def get_marks(range: str = "this-month"):
    return _http(marks, range)


@router.post("")
def post_expense(body: ExpenseIn):
    return _http(add, **body.model_dump())


@router.patch("/{expense_id}")
def patch_expense(expense_id: int, body: ExpensePatch):
    return _http(update, expense_id, **body.model_dump())


@router.delete("/{expense_id}")
def delete_expense(expense_id: int):
    return _http(remove, expense_id)


@router.post("/export")
def post_export():
    return _http(export)


@router.post("/review")
def post_review():
    return review()


@router.post("/batches/{batch_id}/paid")
def post_paid(batch_id: int, body: PaidIn):
    return _http(set_paid, batch_id, body.paid, body.transaction_id)


@router.delete("/batches/{batch_id}")
def delete_batch(batch_id: int):
    return _http(unsend, batch_id)


@router.post("/batches/{batch_id}/reveal")
def post_reveal(batch_id: int):
    try:
        return {"file": _http(reveal, batch_id)}
    except RuntimeError as e:
        raise HTTPException(500, str(e))
