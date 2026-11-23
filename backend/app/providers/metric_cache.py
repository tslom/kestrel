"""Shared write path for the (day, metric, value, refreshed_at) cache tables
(health_days, weather_days, location_days, …).

Each provider still owns its table name, its own _fetch/_sync, and — crucially
— its own missing-day logic in get(): those genuinely differ (a sync-marker
table vs row-presence; future days allowed for the forecast vs not; day_key
vs civil date). What was copy-pasted verbatim in five places was only the
upsert statement itself, and forgetting the `refreshed_at=excluded...` clause
is an easy, silent bug — so that one piece lives here.

The sync-marker providers (calendar, email, health, reddit) also shared the
read half of that bargain — "which days in this range were never synced" and
"how stale is the oldest synced day" — so missing_days()/oldest_refresh()
live here too. Row-presence providers (weather, news) don't use them: their
gap means something different and stays in their own get().
"""


def upsert_days(con, table: str, days: dict[str, dict[str, float]], now: str) -> int:
    """Upsert {day: {metric: value}} into a metric cache table, stamping each
    row `refreshed_at=now`. Caller owns the transaction (`with con:`); returns
    the number of (day, metric) rows written."""
    n = 0
    for day, metrics in days.items():
        for metric, value in metrics.items():
            con.execute(
                f"INSERT INTO {table} (day, metric, value, refreshed_at)"
                " VALUES (?,?,?,?)"
                " ON CONFLICT(day, metric) DO UPDATE SET value=excluded.value,"
                " refreshed_at=excluded.refreshed_at",
                (day, metric, value, now))
            n += 1
    return n


def missing_days(con, marker_table: str, rng, upto: str | None = None) -> list[str]:
    """Days of `rng` with no row in `marker_table` (a `day`-keyed sync
    marker), sorted. `upto` drops days after it first — health's "never ask
    Google about tomorrow"; the others pass nothing."""
    synced = {r["day"] for r in con.execute(
        f"SELECT day FROM {marker_table} WHERE day BETWEEN ? AND ?",
        (rng.start_key, rng.end_key))}
    return sorted({d for d in rng.days() if upto is None or d <= upto} - synced)


def oldest_refresh(con, marker_table: str, rng) -> str | None:
    """The stalest `refreshed_at` among the range's synced days — the honest
    number for a "refreshed Nm ago" label. None when nothing is synced."""
    return con.execute(
        f"SELECT MIN(refreshed_at) AS r FROM {marker_table}"
        " WHERE day BETWEEN ? AND ?",
        (rng.start_key, rng.end_key)).fetchone()["r"]
