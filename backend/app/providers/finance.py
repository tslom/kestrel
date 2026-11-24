"""Finance provider: Plaid balances, transactions, recurring streams, liabilities.

Plaid is truth; `fin_accounts`/`fin_transactions`/`fin_recurring`/`fin_liabilities`
are a droppable cache (a cursor reset re-pulls history). Credentials live in
`plaid_items` (not derived; see plaid.py). Amounts keep Plaid's raw sign (positive
= money OUT); `day` is Plaid's civil date, not day_key. Recurring and liabilities
are once-a-day legs. Design: DESIGN.md → §18 Finance (Plaid).
"""

from datetime import date, datetime, timedelta

from .. import activity, db
from . import heartbeat, plaid
from .ranges import Range

NAME = "finance"
METRICS = ("transactions", "spend", "cashflow", "networth", "balances",
           "recurring", "bills_due")
DEFAULT_METRIC = "transactions"
ALLOW_FUTURE = False    # banks post no next-week transactions

SYNC_PAGE_COUNT = 500   # transactions per /transactions/sync page

# Own-account moves aren't spending or cashflow (a card payment would count twice).
SPEND_EXCLUDED_CATEGORIES = ("TRANSFER_IN", "TRANSFER_OUT", "LOAN_PAYMENTS")

# Net-worth buckets by Plaid `type`; credit/loan `current` is the positive amount owed. "other" is left out.
NET_WORTH_ASSET_TYPES = ("depository", "investment", "brokerage")
NET_WORTH_LIABILITY_TYPES = ("credit", "loan")

# Both surface as needs_consent, not a hard error: the item otherwise works.
_LIABILITIES_CONSENT_ERRORS = {"PRODUCTS_NOT_SUPPORTED", "ADDITIONAL_CONSENT_REQUIRED"}

BILL_TASK_LEAD_DAYS = 5   # auto-task fires once due_day - 5 <= today <= due_day


def _set_status(con, item_id: str, status: str, error: str | None) -> None:
    with con:
        con.execute("UPDATE plaid_items SET status=?, error=? WHERE item_id=?",
                    (status, error, item_id))


def _sync_item(con, item: dict, *, _retried: bool = False) -> int:
    """One cursor loop for one item; returns applied row-change count.
    ITEM_LOGIN_REQUIRED parks the item; a pagination-mutation error restarts once."""
    added: list[dict] = []
    modified: list[dict] = []
    removed: list[dict] = []
    cursor = item["cursor"]
    try:
        while True:
            body = {"access_token": item["access_token"],
                    "count": SYNC_PAGE_COUNT}
            if cursor:
                body["cursor"] = cursor
            page = plaid.api("/transactions/sync", body)
            added += page.get("added", [])
            modified += page.get("modified", [])
            removed += page.get("removed", [])
            cursor = page["next_cursor"]
            if not page.get("has_more"):
                break
        accounts = plaid.api("/accounts/get",
                             {"access_token": item["access_token"]})["accounts"]
    except plaid.PlaidError as e:
        if e.error_code == "ITEM_LOGIN_REQUIRED":
            _set_status(con, item["item_id"], "login_required", str(e))
            return 0
        if (e.error_code == "TRANSACTIONS_SYNC_MUTATION_DURING_PAGINATION"
                and not _retried):
            return _sync_item(con, item, _retried=True)
        _set_status(con, item["item_id"], "error", str(e))
        raise

    now = datetime.now().isoformat(timespec="seconds")
    with con:
        for t in added + modified:
            cat = t.get("personal_finance_category") or {}
            con.execute(
                "INSERT OR REPLACE INTO fin_transactions (transaction_id,"
                " item_id, account_id, day, name, merchant, amount,"
                " iso_currency, category, category_detailed, pending, channel,"
                " logo_url, location, website, counterparties, refreshed_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (t["transaction_id"], item["item_id"], t["account_id"],
                 t["date"], t.get("name"), t.get("merchant_name"),
                 t["amount"], t.get("iso_currency_code"), cat.get("primary"),
                 cat.get("detailed"), int(bool(t.get("pending"))),
                 t.get("payment_channel"), t.get("logo_url"),
                 _format_location(t.get("location")), t.get("website"),
                 _format_counterparties(t.get("counterparties"),
                                        t.get("merchant_name")), now))
        # Claims on a pending charge move to its posted replacement before the pending row is deleted below.
        # Local import: expenses imports the providers package, which imports this.
        from .. import expenses
        expenses.follow_posted(con, added)
        for r in removed:
            con.execute("DELETE FROM fin_transactions WHERE transaction_id=?",
                        (r["transaction_id"],))
        con.execute("DELETE FROM fin_accounts WHERE item_id=?",
                    (item["item_id"],))
        for a in accounts:
            bal = a.get("balances") or {}
            con.execute(
                "INSERT INTO fin_accounts (account_id, item_id, name,"
                " official_name, mask, type, subtype, available, current,"
                " iso_currency, refreshed_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (a["account_id"], item["item_id"], a.get("name"),
                 a.get("official_name"), a.get("mask"), a.get("type"),
                 a.get("subtype"), bal.get("available"), bal.get("current"),
                 bal.get("iso_currency_code"), now))
        con.execute(
            "UPDATE plaid_items SET cursor=?, status='ok', error=NULL,"
            " last_synced_at=? WHERE item_id=?",
            (cursor, now, item["item_id"]))
    return len(added) + len(modified) + len(removed)


def _leg_due(con, leg: str, item_id: str) -> bool:
    """True unless this leg already ran for the item today (unknown key = due)."""
    r = con.execute("SELECT value FROM sync_state WHERE key=?",
                    (f"finance:{leg}:{item_id}",)).fetchone()
    return not r or r["value"] != db.day_key()


def _mark_leg(con, leg: str, item_id: str) -> None:
    """Call only after a leg fetched; marking a failure would park it until tomorrow."""
    with con:
        con.execute(
            "INSERT INTO sync_state (key, value) VALUES (?,?)"
            " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (f"finance:{leg}:{item_id}", db.day_key()))


def _sync_recurring(con, item: dict, now: str) -> bool:
    """One row per stream_id, tagged inflow/outflow; runs regardless of item status.
    Returns whether the fetch landed (decides if the day's leg is marked)."""
    try:
        out = plaid.api("/transactions/recurring/get",
                         {"access_token": item["access_token"]})
    except plaid.PlaidError as e:
        print(f"[finance] recurring fetch failed for {item['item_id']}: {e}")
        heartbeat.log_event("finance", "degraded", f"recurring: {e}")
        return False
    streams = ([("outflow", s) for s in out.get("outflow_streams", [])]
               + [("inflow", s) for s in out.get("inflow_streams", [])])
    with con:
        for flow, s in streams:
            avg = (s.get("average_amount") or {}).get("amount")
            last = (s.get("last_amount") or {}).get("amount")
            con.execute(
                "INSERT INTO fin_recurring (stream_id, item_id, account_id,"
                " flow, desc, merchant, frequency, avg_amount, last_amount,"
                " last_day, predicted_next_date, is_active, status,"
                " refreshed_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(stream_id) DO UPDATE SET"
                " account_id=excluded.account_id, flow=excluded.flow,"
                " desc=excluded.desc, merchant=excluded.merchant,"
                " frequency=excluded.frequency, avg_amount=excluded.avg_amount,"
                " last_amount=excluded.last_amount, last_day=excluded.last_day,"
                " predicted_next_date=excluded.predicted_next_date,"
                " is_active=excluded.is_active, status=excluded.status,"
                " refreshed_at=excluded.refreshed_at",
                (s["stream_id"], item["item_id"], s.get("account_id"), flow,
                 s.get("description"), s.get("merchant_name"),
                 s.get("frequency"), avg, last, s.get("last_date"),
                 s.get("predicted_next_date"), int(bool(s.get("is_active"))),
                 s.get("status"), now))
    return True


def _maybe_create_bill_task(con, account_id: str, due_day: str | None,
                             min_payment: float | None, label: str) -> None:
    """The one finance auto-task: a card's minimum payment, deduped on source_ref=account_id:due_day."""
    if due_day is None or min_payment is None:
        return
    today = db.day_key()
    lead_day = (date.fromisoformat(due_day) - timedelta(days=BILL_TASK_LEAD_DAYS)).isoformat()
    if not (lead_day <= today <= due_day):
        return
    source_ref = f"{account_id}:{due_day}"
    if db.task_exists_for_source("finance", source_ref):
        return
    t = db.create_task(
        title=f"Pay {label} — min ${min_payment:.2f} due {due_day}",
        due=due_day, source="finance", source_kind="finance",
        source_ref=source_ref)
    activity.record("finance", "task", f"New task: {t['title']}",
                    {"task_id": t["id"]})


def _sync_liabilities(con, item: dict, now: str) -> bool:
    """Skipped for needs_consent items (POST /api/plaid/sync resets them); success heals the status.
    Returns whether the fetch landed; False leaves the day's leg unmarked."""
    if item["status"] == "needs_consent":
        return False
    try:
        out = plaid.api("/liabilities/get", {"access_token": item["access_token"]})
    except plaid.PlaidError as e:
        if e.error_code in _LIABILITIES_CONSENT_ERRORS:
            _set_status(con, item["item_id"], "needs_consent", str(e))
        else:
            print(f"[finance] liabilities fetch failed for {item['item_id']}: {e}")
        heartbeat.log_event("finance", "degraded", f"liabilities: {e}")
        return False

    liab = out.get("liabilities") or {}
    rows = []
    for c in liab.get("credit") or []:
        apr = next((a.get("apr_percentage") for a in c.get("aprs") or []
                    if a.get("apr_type") == "purchase_apr"), None)
        rows.append((c["account_id"], "credit", c.get("last_statement_balance"),
                     c.get("minimum_payment_amount"), c.get("next_payment_due_date"),
                     apr, bool(c.get("is_overdue"))))
    for s in liab.get("student") or []:
        rows.append((s["account_id"], "student",
                     s.get("last_statement_balance"), s.get("minimum_payment_amount"),
                     s.get("next_payment_due_date"), s.get("interest_rate_percentage"),
                     bool(s.get("is_overdue"))))
    for m in liab.get("mortgage") or []:
        rate = (m.get("interest_rate") or {}).get("percentage")
        rows.append((m["account_id"], "mortgage", None,
                     m.get("next_monthly_payment"), m.get("next_payment_due_date"),
                     rate, bool(m.get("is_overdue"))))

    with con:
        con.execute("DELETE FROM fin_liabilities WHERE item_id=?", (item["item_id"],))
        for account_id, kind, stmt_bal, min_pay, due_day, apr, overdue in rows:
            con.execute(
                "INSERT INTO fin_liabilities (account_id, item_id, kind,"
                " statement_balance, min_payment, due_day, apr, is_overdue,"
                " refreshed_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (account_id, item["item_id"], kind, stmt_bal, min_pay,
                 due_day, apr, int(overdue), now))
    if item["status"] == "needs_consent":
        _set_status(con, item["item_id"], "ok", None)

    for account_id, kind, _stmt_bal, min_pay, due_day, _apr, _overdue in rows:
        if kind != "credit":
            continue
        acct = con.execute("SELECT name, mask FROM fin_accounts WHERE account_id=?",
                            (account_id,)).fetchone()
        label = _account_label(acct["name"] if acct else None,
                               acct["mask"] if acct else None)
        _maybe_create_bill_task(con, account_id, due_day, min_pay, label)
    return True


def _snapshot_net_worth(con, env: str, now: str) -> None:
    """Upsert one net-worth point per (day_key, env) after all items sync; no-op without a current balance."""
    rows = con.execute(
        "SELECT a.type, a.current FROM fin_accounts a"
        " JOIN plaid_items i ON i.item_id=a.item_id AND i.env=?"
        " WHERE a.current IS NOT NULL", (env,)).fetchall()
    if not rows:
        return
    assets = sum(r["current"] for r in rows
                 if r["type"] in NET_WORTH_ASSET_TYPES)
    liabilities = sum(r["current"] for r in rows
                      if r["type"] in NET_WORTH_LIABILITY_TYPES)
    con.execute(
        "INSERT INTO fin_balance_history (day, env, assets, liabilities,"
        " net_worth, refreshed_at) VALUES (?,?,?,?,?,?)"
        " ON CONFLICT(day, env) DO UPDATE SET assets=excluded.assets,"
        " liabilities=excluded.liabilities, net_worth=excluded.net_worth,"
        " refreshed_at=excluded.refreshed_at",
        (db.day_key(), env, round(assets, 2), round(liabilities, 2),
         round(assets - liabilities, 2), now))


def refresh() -> dict:
    """Poller entrypoint: sync every item in the env except parked login_required ones.
    Quiet no-op without creds or items."""
    try:
        env = plaid.creds()[3]
    except plaid.NotConfigured as e:
        return {"skipped": str(e)}
    items = [it for it in plaid.list_items(env)
             if it["status"] != "login_required"]
    if not items:
        return {"skipped": f"no syncable {env} items — connect a bank first"}
    txns = 0
    legs = 0
    with db.session() as con:
        for item in items:
            txns += _sync_item(con, item)
            now = datetime.now().isoformat(timespec="seconds")
            for leg, fn in (("recurring", _sync_recurring),
                            ("liabilities", _sync_liabilities)):
                if _leg_due(con, leg, item["item_id"]) and fn(con, item, now):
                    _mark_leg(con, leg, item["item_id"])
                    legs += 1
        _snapshot_net_worth(con, env, datetime.now().isoformat(timespec="seconds"))
    return {"items": len(items), "txns": txns, "daily_legs": legs}


def brief_line(it: dict) -> str:
    """One line per item for --format brief; outflows render negative.
    Order matters: bills_due and transactions both carry 'amount', so bills_due goes first."""
    if "due_day" in it and "kind" in it:   # bills_due
        amt = "?" if it["amount"] is None else f"{it['amount']:.2f}"
        flag = " OVERDUE" if it.get("is_overdue") else ""
        return f"{it['due_day']} {it['account']} {amt} due ({it['type']}){flag}"
    if "predicted_next_date" in it:        # recurring
        avg = "?" if it["avg_amount"] is None else f"{-it['avg_amount']:+.2f}"
        return (f"{it.get('merchant') or it.get('desc')} {it['frequency']} {avg}"
                f" next~{it['predicted_next_date']}")
    if "amount" in it:            # transactions
        pend = " pending" if it.get("pending") else ""
        return (f"{it['day']} {-it['amount']:+.2f} "
                f"{it.get('merchant') or it['name']} ({it['account']}){pend}")
    if "current" in it:           # balances
        cur = "—" if it["current"] is None else f"{it['current']:.2f}"
        return f"{it['account']} {it.get('subtype') or it.get('type') or ''} current {cur}"
    if "net_worth" in it:         # networth
        return (f"{it['day']} net {it['net_worth']:.2f}"
                f" (assets {it['assets']:.2f} − liab {it['liabilities']:.2f})")
    if "net" in it and "week" in it:   # cashflow
        return (f"week {it['week']} net {it['net']:+.2f}"
                f" (in {it['inflow']:.2f} / out {it['outflow']:.2f})")
    return f"{it['day']} {it['value']}"


def _account_label(name: str | None, mask: str | None) -> str:
    label = name or "account"
    return f"{label} ··{mask}" if mask else label


def _format_location(loc: dict | None) -> str | None:
    """A "City, ST" string; Plaid fills location only for in-store purchases, else None."""
    if not loc:
        return None
    parts = [p for p in (loc.get("city"), loc.get("region")) if p]
    return ", ".join(parts) or None


def _format_counterparties(cps: list | None, merchant: str | None) -> str | None:
    """Counterparties other than the merchant and financial_institution rails; None when empty."""
    if not cps:
        return None
    m = (merchant or "").strip().lower()
    names: list[str] = []
    for c in cps:
        name = (c.get("name") or "").strip()
        if (name and name.lower() != m and name not in names
                and c.get("type") != "financial_institution"):
            names.append(name)
    return ", ".join(names) or None


def _week_start(day: str) -> str:
    """Monday of the ISO week containing `day` (YYYY-MM-DD)."""
    d = date.fromisoformat(day)
    return (d - timedelta(days=d.weekday())).isoformat()


def get(rng: Range, metric: str | None = None) -> dict:
    """Read the slice from cache; no API calls (ranges older than the pulled history are empty)."""
    metric = metric or DEFAULT_METRIC
    try:
        env = plaid.creds()[3]
    except plaid.NotConfigured as e:
        raise ValueError(str(e)) from None
    with db.session() as con:
        items = con.execute(
            "SELECT item_id, last_synced_at FROM plaid_items WHERE env=?",
            (env,)).fetchall()
        if not items:
            raise ValueError(
                f"no bank connected ({env}) — use the dashboard Connect"
                " button, or in sandbox: python -m app.providers.plaid"
                " sandbox-item")
        synced = [r["last_synced_at"] for r in items if r["last_synced_at"]]
        refreshed = min(synced) if synced else None

        if metric == "balances":
            rows = con.execute(
                "SELECT a.account_id, a.name, a.mask, a.type, a.subtype,"
                " a.available, a.current, a.iso_currency"
                " FROM fin_accounts a JOIN plaid_items i"
                "  ON i.item_id=a.item_id AND i.env=?"
                " ORDER BY a.name, a.mask", (env,)).fetchall()
            data = [{
                "account_id": r["account_id"],
                "account": _account_label(r["name"], r["mask"]),
                "name": r["name"], "mask": r["mask"],
                "type": r["type"], "subtype": r["subtype"],
                "available": r["available"], "current": r["current"],
                "currency": r["iso_currency"],
            } for r in rows]
            return {"metric": metric, "data": data, "refreshed_at": refreshed}

        if metric == "recurring":
            # Range-independent like balances; soonest predicted payment first.
            rows = con.execute(
                "SELECT r.stream_id, r.flow, r.desc, r.merchant, r.frequency,"
                " r.avg_amount, r.last_amount, r.last_day,"
                " r.predicted_next_date, r.status"
                " FROM fin_recurring r JOIN plaid_items i"
                "  ON i.item_id=r.item_id AND i.env=?"
                " WHERE r.is_active=1"
                " ORDER BY r.predicted_next_date IS NULL, r.predicted_next_date",
                (env,)).fetchall()
            data = [{
                "stream_id": r["stream_id"], "flow": r["flow"],
                "desc": r["desc"], "merchant": r["merchant"],
                "frequency": r["frequency"], "avg_amount": r["avg_amount"],
                "last_amount": r["last_amount"], "last_day": r["last_day"],
                "predicted_next_date": r["predicted_next_date"],
                "status": r["status"],
            } for r in rows]
            return {"metric": metric, "data": data, "refreshed_at": refreshed}

        if metric == "bills_due":
            # Liabilities due <=14d + recurring outflow predicted <=7d, one list.
            today = db.day_key()
            liab_horizon = (date.fromisoformat(today) + timedelta(days=14)).isoformat()
            rec_horizon = (date.fromisoformat(today) + timedelta(days=7)).isoformat()
            liab_rows = con.execute(
                "SELECT l.account_id, l.kind, l.statement_balance,"
                " l.min_payment, l.due_day, l.is_overdue,"
                " a.name AS acct_name, a.mask AS acct_mask"
                " FROM fin_liabilities l JOIN plaid_items i"
                "  ON i.item_id=l.item_id AND i.env=?"
                " LEFT JOIN fin_accounts a ON a.account_id=l.account_id"
                " WHERE l.due_day IS NOT NULL AND l.due_day <= ?"
                " ORDER BY l.due_day", (env, liab_horizon)).fetchall()
            # Peer transfers (monthly Zelle) are outflow streams with merchant '' (not NULL); not bills.
            rec_rows = con.execute(
                "SELECT r.merchant, r.desc, r.avg_amount, r.predicted_next_date"
                " FROM fin_recurring r JOIN plaid_items i"
                "  ON i.item_id=r.item_id AND i.env=?"
                " WHERE r.flow='outflow' AND r.is_active=1"
                " AND TRIM(COALESCE(r.merchant, '')) != ''"
                " AND r.predicted_next_date IS NOT NULL"
                " AND r.predicted_next_date <= ?"
                " ORDER BY r.predicted_next_date", (env, rec_horizon)).fetchall()
            data = [{
                "kind": "liability", "type": r["kind"],
                "account": _account_label(r["acct_name"], r["acct_mask"]),
                "due_day": r["due_day"], "amount": r["min_payment"],
                "statement_balance": r["statement_balance"],
                "is_overdue": bool(r["is_overdue"]),
            } for r in liab_rows] + [{
                "kind": "recurring", "type": "subscription",
                "account": r["merchant"] or r["desc"],
                "due_day": r["predicted_next_date"], "amount": r["avg_amount"],
                "statement_balance": None, "is_overdue": False,
            } for r in rec_rows]
            data.sort(key=lambda d: d["due_day"])
            return {"metric": metric, "data": data, "refreshed_at": refreshed}

        if metric == "spend":
            # Daily outflow, pending included; days up to today zero-fill for sparklines.
            marks = ",".join("?" * len(SPEND_EXCLUDED_CATEGORIES))
            rows = con.execute(
                f"SELECT t.day, ROUND(SUM(t.amount), 2) AS value"
                f" FROM fin_transactions t JOIN plaid_items i"
                f"  ON i.item_id=t.item_id AND i.env=?"
                f" WHERE t.day BETWEEN ? AND ? AND t.amount > 0"
                f"  AND (t.category IS NULL OR t.category NOT IN ({marks}))"
                f" GROUP BY t.day",
                (env, rng.start_key, rng.end_key,
                 *SPEND_EXCLUDED_CATEGORIES)).fetchall()
            by_day = {r["day"]: r["value"] for r in rows}
            today = db.day_key()
            data = [{"day": d, "value": by_day.get(d, 0)}
                    for d in rng.days() if d <= today]
            return {"metric": metric, "data": data, "refreshed_at": refreshed}

        if metric == "cashflow":
            # Weekly net (in − out) by ISO week; empty weeks zero-fill so the bar chart has no gaps.
            marks = ",".join("?" * len(SPEND_EXCLUDED_CATEGORIES))
            rows = con.execute(
                f"SELECT t.day,"
                f" ROUND(SUM(CASE WHEN t.amount > 0 THEN t.amount ELSE 0 END), 2) AS outflow,"
                f" ROUND(SUM(CASE WHEN t.amount < 0 THEN -t.amount ELSE 0 END), 2) AS inflow"
                f" FROM fin_transactions t JOIN plaid_items i"
                f"  ON i.item_id=t.item_id AND i.env=?"
                f" WHERE t.day BETWEEN ? AND ?"
                f"  AND (t.category IS NULL OR t.category NOT IN ({marks}))"
                f" GROUP BY t.day",
                (env, rng.start_key, rng.end_key,
                 *SPEND_EXCLUDED_CATEGORIES)).fetchall()
            today = db.day_key()
            weeks: dict[str, dict] = {}
            for d in rng.days():
                if d <= today:
                    weeks.setdefault(_week_start(d),
                                     {"week": _week_start(d), "inflow": 0.0, "outflow": 0.0})
            for r in rows:
                b = weeks.setdefault(_week_start(r["day"]),
                                     {"week": _week_start(r["day"]), "inflow": 0.0, "outflow": 0.0})
                b["inflow"] += r["inflow"] or 0
                b["outflow"] += r["outflow"] or 0
            data = []
            for wk in sorted(weeks):
                b = weeks[wk]
                b["inflow"] = round(b["inflow"], 2)
                b["outflow"] = round(b["outflow"], 2)
                b["net"] = round(b["inflow"] - b["outflow"], 2)
                data.append(b)
            return {"metric": metric, "data": data, "refreshed_at": refreshed}

        if metric == "networth":
            # fin_balance_history only exists from the first snapshot; earlier ranges are empty.
            rows = con.execute(
                "SELECT day, assets, liabilities, net_worth"
                " FROM fin_balance_history"
                " WHERE env=? AND day BETWEEN ? AND ? ORDER BY day",
                (env, rng.start_key, rng.end_key)).fetchall()
            data = [{"day": r["day"], "assets": r["assets"],
                     "liabilities": r["liabilities"], "net_worth": r["net_worth"]}
                    for r in rows]
            return {"metric": metric, "data": data, "refreshed_at": refreshed}

        rows = con.execute(
            "SELECT t.transaction_id, t.day, t.name, t.merchant, t.amount,"
            " t.iso_currency, t.category, t.pending, t.logo_url, t.location,"
            " t.website, t.counterparties,"
            " a.name AS acct_name, a.mask AS acct_mask"
            " FROM fin_transactions t JOIN plaid_items i"
            "  ON i.item_id=t.item_id AND i.env=?"
            " LEFT JOIN fin_accounts a ON a.account_id=t.account_id"
            " WHERE t.day BETWEEN ? AND ?"
            " ORDER BY t.day ASC, t.transaction_id",
            (env, rng.start_key, rng.end_key)).fetchall()
    data = [{
        "id": r["transaction_id"],
        "day": r["day"],
        "name": r["name"],
        "merchant": r["merchant"],
        "amount": r["amount"],
        "currency": r["iso_currency"],
        "category": r["category"],
        "account": _account_label(r["acct_name"], r["acct_mask"]),
        "pending": bool(r["pending"]),
        "logo_url": r["logo_url"],
        "location": r["location"],
        "website": r["website"],
        "counterparties": r["counterparties"],
    } for r in rows]
    return {"metric": metric, "data": data, "count": len(data),
            "total_out": round(sum(t["amount"] for t in data
                                   if t["amount"] > 0), 2),
            "refreshed_at": refreshed}
