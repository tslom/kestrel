"""Shared write path for the (day, metric, value, refreshed_at) cache tables, plus the
sync-marker providers' missing_days()/oldest_refresh(). Each provider keeps its own
missing-day logic in get() (marker table vs row presence differ); row-presence providers
(weather, news) don't use the marker helpers. Design: DESIGN.md → Context providers (`app/providers/`).
"""


def upsert_days(con, table: str, days: dict[str, dict[str, float]], now: str) -> int:
    """Upsert {day: {metric: value}}, stamping `refreshed_at=now`; the caller owns the transaction. Returns rows written."""
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
    """Sorted days of `rng` with no row in the `day`-keyed `marker_table`; `upto` drops later days first."""
    synced = {r["day"] for r in con.execute(
        f"SELECT day FROM {marker_table} WHERE day BETWEEN ? AND ?",
        (rng.start_key, rng.end_key))}
    return sorted({d for d in rng.days() if upto is None or d <= upto} - synced)


def oldest_refresh(con, marker_table: str, rng) -> str | None:
    """The stalest `refreshed_at` among the range's synced days (for "refreshed Nm ago"); None if none synced."""
    return con.execute(
        f"SELECT MIN(refreshed_at) AS r FROM {marker_table}"
        " WHERE day BETWEEN ? AND ?",
        (rng.start_key, rng.end_key)).fetchone()["r"]
