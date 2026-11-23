"""Provider health — did the last refresh work, and is the cache current?

The poller runs every provider every PROVIDER_REFRESH_SECONDS and already
knows whether each one raised. record() persists that outcome to app.db
`provider_health` (derived/droppable) so /api/status can say *when* a source
broke instead of leaving you to notice a widget looks wrong.

Four states, deliberately not two — red/green alone lies constantly:

  ok      last refresh succeeded and the cache reaches about today
  stale   refresh succeeded but the newest cached day is behind FRESHNESS's
          threshold. The state that catches quiet failures: a refresh that
          writes 0 rows (chat.db unreadable without Full Disk Access)
          reports success forever otherwise.
  broken  the last refresh raised, or the source's auth is gone
  off     never configured — Plaid with no creds, a source whose host
          isn't up. Not a failure, and must not render as one, or the panel
          becomes noise you learn to ignore.

Freshness thresholds are "when a gap becomes suspicious", not an SLA: you can
go three days without texting anyone, so `messages` only looks stale after
that. They mark where to *look*, so err generous — a false amber costs more
attention than a late one.
"""

import json
import sqlite3
from datetime import datetime

from .. import db

# source -> (cache table, day column, stale after N days without a newer row).
# Every provider cache table carries a `day`, so one declarative map covers
# the generic case; a provider needing more says so in its own health() hook
# (messages/location do — reachability and file permissions aren't
# visible in the cache at all).
FRESHNESS: dict[str, tuple[str, str, int]] = {
    "calendar": ("calendar_days", "day", 1),
    "email": ("email_days", "day", 1),
    "health": ("health_days", "day", 2),      # Fitbit's own sync lags a day
    "weather": ("weather_days", "day", 1),
    "news": ("news_items", "day", 1),
    # the marker table, not reddit_posts — a genuinely
    # quiet subreddit day writes no posts, and that isn't the cache going stale
    "reddit": ("reddit_sync", "day", 1),
    "spotify": ("plays", "day", 3),           # you don't listen every day
    "messages": ("messages", "day", 3),       # nor text every day
    "location": ("location_days", "day", 2),
    "finance": ("fin_transactions", "day", 4),  # no spend over a long weekend
    "features": ("features", "day", 1),
}


EVENT_LOG_KEEP = 500    # rows retained in provider_events, newest first


def log_event(provider: str, kind: str, detail: str | None = None) -> None:
    """Append one row to the event log — the durable half of debugging.

    Call this from anywhere a provider swallows a failure and carries on:
    those never reach provider_health (refresh() returned success, so the row
    says ok=1) and previously existed only as a print into a /tmp log that
    macOS eventually deletes. Same never-raise contract as record()."""
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
    """One refresh as a character of `recent`: x failed, . ran but had nothing
    to do (a refresh that reports `skipped` — news fetched once a day, reddit
    inside its interval), o otherwise."""
    if not ok:
        return "x"
    if isinstance(info, dict) and info.get("skipped"):
        return "."
    return "o"


def broken(recent: str) -> bool:
    """A source is broken only when its last two refreshes failed. One bad
    cycle in a row of good ones is weather, and flagging it would teach you to
    ignore the flag."""
    return recent.endswith("xx")


def record(provider: str, *, ok: bool, error: str | None = None,
           info: object = None, duration_ms: int | None = None) -> None:
    """Persist one refresh outcome. Called from the poller's worker thread, so
    it must never raise into the loop: a health write failing is not a reason
    to lose the refresh that succeeded.

    State *changes* also append to provider_events, so the history survives
    the next cycle overwriting this row."""
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
                    # a failure keeps the last good timestamp — "worked 6h ago"
                    # is the useful reading, not NULL
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

    # Transitions only. A provider down all night must not write one row per
    # 5-minute cycle; a *changed* error while still down is worth a row,
    # because "429 became invalid_grant" is the useful debugging detail.
    was_ok = bool(prev["ok"]) if prev else True
    if not ok and (was_ok or (prev and prev["error"] != error)):
        log_event(provider, "broke", error)
    elif ok and not was_ok:
        log_event(provider, "recovered", None)

    if ok:
        _record_freshness(provider)


def _record_freshness(provider: str) -> None:
    """Log the stale/freshened transition the ok/error pair cannot see.

    `broke`/`recovered` above read the *refresh outcome*, so a source that
    keeps refreshing successfully while its cache stops advancing leaves no
    trace at all: the `stale` state is computed live by routes/status.py and
    evaporates the moment the data catches up. It has happened:
    location ingest stopped for four days and then OwnTracks' store-and-forward
    flushed the backlog, and `provider_events` for location was *empty*
    afterwards. The outage was real, self-healing, and completely unrecoverable
    from the record. This is what makes such an episode findable later.

    Only evaluated when the refresh succeeded: while a source is failing,
    `broke` already says so and a stale row would be the same news twice.
    Previous staleness is read back out of the event log rather than kept in a
    new column, so this needs no migration and the log is its own state.
    """
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
    """(is_stale, newest_day, age_days, stale_after_days), or None when the
    question doesn't apply.

    None rather than False for the three non-answers, because each would be a
    different kind of lie as a boolean: a provider with no FRESHNESS rule has
    its own health() hook owning the verdict (messages and location both
    check reachability the cache can't show), an empty cache is never-synced
    rather than gone stale, and an unparseable day is a bug in the cache, not
    an outage. Same three-way discipline as alarms.py's `unknown`."""
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
    """(newest day present, total rows) for a cache table. Both matter: a
    newest day of None with 0 rows is an empty cache (never synced, or
    synced-but-silently-empty), which no error field would ever show."""
    try:
        r = con.execute(f"SELECT MAX({col}) AS newest, COUNT(*) AS n"
                        f" FROM {table}").fetchone()
    except sqlite3.Error:
        return None, 0     # table dropped — derived, so that's legal
    return r["newest"], r["n"]


def age_days(day: str | None) -> int | None:
    """Whole days between `day` and today's day_key. Future days (a weather
    forecast, next week's calendar) clamp to 0 — being ahead isn't stale."""
    if not day:
        return None
    try:
        then = datetime.fromisoformat(day[:10]).date()
    except ValueError:
        return None
    return max(0, (datetime.fromisoformat(db.day_key()).date() - then).days)


def since(ts: str | None) -> str | None:
    """'4m ago' / '2h ago' / '3d ago' — the only form of a timestamp anyone
    reads on a status panel."""
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
