"""The delegates' two tables (see the migration in db.py): which providers
are switched on, and the log of every run."""

from __future__ import annotations

from datetime import datetime, timedelta

from .. import db

RUN_FIELDS = ("status", "error", "file", "chars", "tokens_in", "tokens_out",
              "tokens_cached", "duration_ms", "model")


def switches() -> dict[str, bool]:
    with db.session() as con:
        return {r["id"]: bool(r["enabled"])
                for r in con.execute("SELECT id, enabled FROM delegate_providers")}


def set_enabled(provider: str, enabled: bool) -> None:
    with db.session() as con, con:
        con.execute(
            "INSERT INTO delegate_providers (id, enabled, updated_at) VALUES (?,?,?)"
            " ON CONFLICT (id) DO UPDATE SET enabled = excluded.enabled,"
            " updated_at = excluded.updated_at",
            (provider, int(enabled), datetime.now().isoformat(timespec="seconds")))


def start_run(batch: str, variant: int, kind: str, provider: str, model: str | None,
              angle: str | None, redactions: tuple[str, ...], session_id: str | None,
              account: str | None = None) -> int:
    with db.session() as con, con:
        cur = con.execute(
            "INSERT INTO delegate_runs (batch, variant, kind, provider, model, angle,"
            " status, redactions, session_id, started_at, day, account)"
            " VALUES (?,?,?,?,?,?, 'running', ?,?,?,?,?)",
            (batch, variant, kind, provider, model, angle, ", ".join(redactions) or None,
             session_id, datetime.now().isoformat(timespec="seconds"), db.day_key(), account))
        return int(cur.lastrowid)


# ---------- accounts (accounts.py) ----------

ACCOUNT_FIELDS = ("label", "email", "plan", "allowed_for", "priority", "enabled",
                  "monthly_credits", "credit_cap", "window_cap")


def accounts(provider: str | None = None) -> list[dict]:
    """Saved accounts, in priority order (one provider's, or all)."""
    with db.session() as con:
        rows = con.execute(
            "SELECT * FROM delegate_accounts" + (" WHERE provider = ?" if provider else "")
            + " ORDER BY provider, priority, created_at", (provider,) if provider else ())
        return [dict(r) for r in rows]


def add_account(id: str, provider: str, label: str, home: str, allowed_for: str,
                **fields) -> None:
    with db.session() as con, con:
        last = con.execute("SELECT COALESCE(MAX(priority), -1) FROM delegate_accounts"
                           " WHERE provider = ?", (provider,)).fetchone()[0]
        con.execute(
            "INSERT INTO delegate_accounts (id, provider, label, home, allowed_for, priority,"
            " created_at) VALUES (?,?,?,?,?,?,?)",
            (id, provider, label, home, allowed_for, last + 1,
             datetime.now().isoformat(timespec="seconds")))
    if fields:
        update_account(id, **fields)


def update_account(id: str, **fields) -> None:
    sets = {k: v for k, v in fields.items() if k in ACCOUNT_FIELDS}
    if not sets:
        return
    with db.session() as con, con:
        con.execute(f"UPDATE delegate_accounts SET {', '.join(f'{k} = ?' for k in sets)}"
                    " WHERE id = ?", (*sets.values(), id))


def claim_runs(provider: str, account: str) -> int:
    """Put a provider's runs from before accounts on the account they ran on."""
    with db.session() as con, con:
        return con.execute("UPDATE delegate_runs SET account = ? WHERE provider = ?"
                           " AND account IS NULL", (account, provider)).rowcount


def delete_account(id: str) -> None:
    with db.session() as con, con:
        con.execute("DELETE FROM delegate_accounts WHERE id = ?", (id,))


def account_usage(since_day: str) -> dict[str, dict]:
    """Per account since a day key: runs, failures, tokens (in includes
    cached), and the models that ran (in runs that worked: a refused model
    name isn't one that ran), comma-joined."""
    with db.session() as con:
        return {r["account"]: dict(r) for r in con.execute(
            "SELECT account, COUNT(*) AS runs, SUM(status = 'failed') AS failed,"
            " COALESCE(SUM(tokens_in), 0) AS tokens_in,"
            " COALESCE(SUM(tokens_out), 0) AS tokens_out,"
            " COALESCE(SUM(tokens_cached), 0) AS tokens_cached,"
            " GROUP_CONCAT(DISTINCT CASE WHEN status = 'ok' THEN model END) AS models,"
            " MAX(started_at) AS last_run"
            " FROM delegate_runs WHERE account IS NOT NULL AND day >= ?"
            " AND status != 'running' GROUP BY account", (since_day,))}


def finish_run(run_id: int, **fields) -> None:
    sets = {k: v for k, v in fields.items() if k in RUN_FIELDS}
    with db.session() as con, con:
        con.execute(f"UPDATE delegate_runs SET {', '.join(f'{k} = ?' for k in sets)} WHERE id = ?",
                    (*sets.values(), run_id))


def batch_runs(batch: str) -> list[dict]:
    with db.session() as con:
        return [dict(r) for r in con.execute(
            "SELECT * FROM delegate_runs WHERE batch = ? ORDER BY variant", (batch,))]


def contact_names() -> tuple[str, ...]:
    """The people in the user's contacts, by name: what a brief is scrubbed
    of (boundary.scrub takes the full names)."""
    with db.session() as con:
        return tuple(r["name"] for r in con.execute(
            "SELECT name FROM people WHERE name IS NOT NULL AND name != ''"))


def model_id(provider: str, label: str) -> str | None:
    """A model's id from the name its CLI shows ("Gemini 3.8 Flash (High)"),
    off the stored model list (model_catalog's table)."""
    with db.session() as con:
        row = con.execute("SELECT id FROM model_catalog WHERE provider = ? AND label = ?",
                          (provider, label)).fetchone()
    return row["id"] if row else None


def finished_runs(providers: tuple[str, ...], since: str | None = None) -> list[dict]:
    """Every finished run by these providers, one row each, started at or
    after `since` (local ISO): the model usage page's rows for models that
    aren't Claude (token_usage.delegate_calls)."""
    if not providers:
        return []
    marks = ",".join("?" * len(providers))
    with db.session() as con:
        return [dict(r) for r in con.execute(
            "SELECT provider, model, tokens_in, tokens_out, tokens_cached, started_at, session_id,"
            " status, account"
            f" FROM delegate_runs WHERE status != 'running' AND provider IN ({marks})"
            " AND started_at >= ? ORDER BY id", (*providers, since or ""))]


def usage(days: int = 30) -> list[dict]:
    """Per provider over the last `days` day keys: runs, failures, tokens
    where the CLI reported them, and time spent."""
    since = db.day_key(datetime.now() - timedelta(days=days - 1))
    with db.session() as con:
        return [dict(r) for r in con.execute(
            "SELECT provider, COUNT(*) AS runs,"
            " SUM(status = 'failed') AS failed,"
            " COALESCE(SUM(tokens_in), 0) AS tokens_in,"
            " COALESCE(SUM(tokens_out), 0) AS tokens_out,"
            " COALESCE(SUM(duration_ms), 0) AS duration_ms,"
            " MAX(started_at) AS last_run"
            " FROM delegate_runs WHERE day >= ? AND status != 'running'"
            " GROUP BY provider ORDER BY runs DESC", (since,))]
