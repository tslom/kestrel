"""Provider health: did the last refresh work, and is the cache current?

record() persists the poller's per-provider outcome to `provider_health` (derived,
droppable). States: ok / stale (refresh succeeded but the newest cached day is behind
FRESHNESS; catches a refresh that writes 0 rows) / broken / off (never configured, not a
failure). Thresholds mark where to look, not an SLA: err generous.
Design: DESIGN.md → Source status (`/api/status`).
"""

import json
import sqlite3
from datetime import datetime

from .. import db

# source -> (cache table, day column, stale after N days without a newer row).
# What a cache can't show (reachability, permissions) goes in the provider's own health() hook.
FRESHNESS: dict[str, tuple[str, str, int]] = {
    "calendar": ("calendar_days", "day", 1),
    "email": ("email_days", "day", 1),
    "health": ("health_days", "day", 2),      # Fitbit's own sync lags a day
    "weather": ("weather_days", "day", 1),
    "news": ("news_items", "day", 1),
    # the marker table: a quiet subreddit day writes no posts
    "reddit": ("reddit_sync", "day", 1),
    "spotify": ("plays", "day", 3),           # you don't listen every day
    "messages": ("messages", "day", 3),       # nor text every day
    "location": ("location_days", "day", 2),
    "finance": ("fin_transactions", "day", 4),  # no spend over a long weekend
    "features": ("features", "day", 1),
}


EVENT_LOG_KEEP = 500    # rows retained in provider_events, newest first


def log_event(provider: str, kind: str, detail: str | None = None) -> None:
    """Append one row to the event log. Call where a provider swallows a failure and carries on
    (provider_health never sees those). Never raises, like record()."""
    try:
        with db.session() as con, con:
            con.execute(
                "INSERT INTO provider_events (provider, at, kind, detail)"
                " VALUES (?,?,?,?)",
                (provider, datetime.now().isoformat(timespec="seconds"),
                 kind, detail))
            con.execute(
                "DELETE FROM provider_events WHERE id <= "
                " (SELECT MAX(id) - ? FROM provider_events)", (EVENT_LOG_KEEP,))
    except sqlite3.Error as e:  # logging is never load-bearing
        print(f"[heartbeat] could not log {provider}/{kind}: {e!r}")


def events(provider: str | None = None, limit: int = 50) -> list[dict]:
    """Newest events first, optionally for one provider."""
    where, args = ("WHERE provider=?", [provider]) if provider else ("", [])
    with db.session() as con:
        return [dict(r) for r in con.execute(
            f"SELECT * FROM provider_events {where} ORDER BY id DESC LIMIT ?",
            [*args, limit])]


# provider_health.recent: the last RECENT_KEEP outcomes, oldest first.
RECENT_KEEP = 24


def outcome_char(ok: bool, info: object) -> str:
    """One refresh as a character of `recent`: x failed, . reported `skipped`, o otherwise."""
    if not ok:
        return "x"
    if isinstance(info, dict) and info.get("skipped"):
        return "."
    return "o"


def broken(recent: str) -> bool:
    """Broken = the last two refreshes failed; one bad cycle is weather."""
    return recent.endswith("xx")


def record(provider: str, *, ok: bool, error: str | None = None,
           info: object = None, duration_ms: int | None = None) -> None:
    """Persist one refresh outcome from the poller's worker thread; must never raise.
    State changes also append to provider_events."""
    now = datetime.now().isoformat(timespec="seconds")
    try:
        with db.session() as con:
            prev = con.execute("SELECT ok, error FROM provider_health"
                               " WHERE provider=?", (provider,)).fetchone()
            with con:
                con.execute(
                    "INSERT INTO provider_health (provider, ok, error, info,"
                    " last_attempt_at, last_ok_at, duration_ms, fails, recent)"
                    " VALUES (?,?,?,?,?,?,?,?,?)"
                    " ON CONFLICT(provider) DO UPDATE SET"
                    " ok=excluded.ok, error=excluded.error, info=excluded.info,"
                    " last_attempt_at=excluded.last_attempt_at,"
                    # a failure keeps the last good timestamp
                    " last_ok_at=COALESCE(excluded.last_ok_at, provider_health.last_ok_at),"
                    " duration_ms=excluded.duration_ms,"
                    " fails=CASE WHEN excluded.ok THEN 0 ELSE provider_health.fails + 1 END,"
                    f" recent=substr(provider_health.recent || excluded.recent, -{RECENT_KEEP})",
                    (provider, int(ok), error,
                     json.dumps(info, default=str) if info is not None else None,
                     now, now if ok else None, duration_ms, 0 if ok else 1,
                     outcome_char(ok, info)))
    except sqlite3.Error as e:  # health is never load-bearing
        print(f"[heartbeat] could not record {provider}: {e!r}")
        return

    # Transitions only (plus a changed error while still down), not a row per cycle.
    was_ok = bool(prev["ok"]) if prev else True
    if not ok and (was_ok or (prev and prev["error"] != error)):
        log_event(provider, "broke", error)
    elif ok and not was_ok:
        log_event(provider, "recovered", None)

    if ok:
        _record_freshness(provider)


def _record_freshness(provider: str) -> None:
    """Log stale/freshened transitions (only after a successful refresh; `broke` covers failures).
    Prior state is read back from the event log, so no extra column."""
    try:
        with db.session() as con:
            verdict = _staleness(con, provider)
            if verdict is None:
                return
            stale, newest, age, max_age = verdict
            prev = con.execute(
                "SELECT kind FROM provider_events WHERE provider=?"
                " AND kind IN ('stale','freshened') ORDER BY id DESC LIMIT 1",
                (provider,)).fetchone()
            was_stale = prev is not None and prev["kind"] == "stale"
    except sqlite3.Error as e:  # health is never load-bearing
        print(f"[heartbeat] could not check freshness for {provider}: {e!r}")
        return

    if stale and not was_stale:
        log_event(provider, "stale",
                  f"newest cached day {newest} is {age}d behind"
                  f" (stale after {max_age}d)")
    elif not stale and was_stale:
        log_event(provider, "freshened", f"newest cached day {newest}")


def _staleness(con: sqlite3.Connection,
               provider: str) -> tuple[bool, str | None, int, int] | None:
    """(is_stale, newest_day, age_days, stale_after_days), or None for the non-answers:
    no FRESHNESS rule (health() owns it), an empty cache (never synced), an unparseable day."""
    spec = FRESHNESS.get(provider)
    if spec is None:
        return None
    table, col, max_age = spec
    newest, rows_n = newest_day(con, table, col)
    if newest is None or rows_n == 0:
        return None
    age = age_days(newest)
    if age is None:
        return None
    return age > max_age, newest, age, max_age


def rows() -> dict[str, dict]:
    """provider -> stored health row (absent = has never run this boot)."""
    with db.session() as con:
        return {r["provider"]: dict(r) for r in
                con.execute("SELECT * FROM provider_health")}


def newest_day(con: sqlite3.Connection, table: str, col: str) -> tuple[str | None, int]:
    """(newest day present, total rows) for a cache table; (None, 0) is an empty cache."""
    try:
        r = con.execute(f"SELECT MAX({col}) AS newest, COUNT(*) AS n"
                        f" FROM {table}").fetchone()
    except sqlite3.Error:
        return None, 0     # table dropped — derived, so that's legal
    return r["newest"], r["n"]


def age_days(day: str | None) -> int | None:
    """Whole days between `day` and today's day_key; future days clamp to 0."""
    if not day:
        return None
    try:
        then = datetime.fromisoformat(day[:10]).date()
    except ValueError:
        return None
    return max(0, (datetime.fromisoformat(db.day_key()).date() - then).days)


def since(ts: str | None) -> str | None:
    """'4m ago' / '2h ago' / '3d ago'."""
    if not ts:
        return None
    try:
        then = datetime.fromisoformat(ts)
    except ValueError:
        return None
    secs = (datetime.now() - then).total_seconds()
    if secs < 0:
        return "just now"
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if secs >= size:
            return f"{int(secs // size)}{unit} ago"
    return f"{int(secs)}s ago"
