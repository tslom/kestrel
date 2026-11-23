"""The delegates' two tables (see the migration in db.py): which providers
are switched on, and the log of every run."""

from __future__ import annotations

from datetime import datetime, timedelta

from .. import db

RUN_FIELDS = ("status", "error", "file", "chars", "tokens_in", "tokens_out",
              "duration_ms", "model")


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
              angle: str | None, redactions: tuple[str, ...], session_id: str | None) -> int:
    with db.session() as con, con:
        cur = con.execute(
            "INSERT INTO delegate_runs (batch, variant, kind, provider, model, angle,"
            " status, redactions, session_id, started_at, day)"
            " VALUES (?,?,?,?,?,?, 'running', ?,?,?,?)",
            (batch, variant, kind, provider, model, angle, ", ".join(redactions) or None,
             session_id, datetime.now().isoformat(timespec="seconds"), db.day_key()))
        return int(cur.lastrowid)


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


def finished_runs(providers: tuple[str, ...], since: str | None = None) -> list[dict]:
    """Every finished run by these providers, one row each, started at or
    after `since` (local ISO): the model usage page's rows for models that
    aren't Claude (token_usage.delegate_calls)."""
    if not providers:
        return []
    marks = ",".join("?" * len(providers))
    with db.session() as con:
        return [dict(r) for r in con.execute(
            "SELECT provider, model, tokens_in, tokens_out, started_at, session_id, status"
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
