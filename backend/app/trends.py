"""Internal series for the trends widget: a derived daily energy score
(readiness blend of sleep, resting HR and HRV from the health cache) and
per-habit checkins (app.db). Health series stay on /api/health — the health
provider owns them; this serves only what no provider covers. Not a provider:
nothing external, nothing to refresh. Range tokens parse in
providers/ranges.py, as everywhere.
"""

from collections import defaultdict
from datetime import date, timedelta
from statistics import median

from fastapi import APIRouter, HTTPException

from . import db
from .providers import features, health, offline, ranges

router = APIRouter(prefix="/api")

BASE_METRICS = ("energy",)

# energy = 0-100 readiness. Signals score 0..1: sleep vs a fixed target, resting HR
# and HRV vs the median of the trailing BASELINE_DAYS. Missing signals drop out and
# weights renormalize; no signals = null gap, not 0.
ENERGY_WEIGHTS = {"sleep_hours": 0.5, "resting_hr": 0.25, "hrv": 0.25}
BASELINE_DAYS = 30
SLEEP_TARGET_HOURS = 8.0


def _score(metric: str, value: float, baseline: float | None) -> float | None:
    if metric == "sleep_hours":
        return min(value / SLEEP_TARGET_HOURS, 1.0)
    if not baseline:
        return None
    if metric == "resting_hr":  # lower than baseline is better
        return max(0.0, min(2.0 - value / baseline, 1.0))
    return max(0.0, min(value / baseline, 1.0))  # hrv: higher is better


def _energy_series(rng: ranges.Range, days: list[str]) -> list[dict]:
    base_start = (date.fromisoformat(rng.start_key)
                  - timedelta(days=BASELINE_DAYS)).isoformat()
    # One read over range + baseline syncs all health metrics for cold days (get() backfills whole days).
    sync_end = min(rng.end_key, db.day_key())
    sync = ranges.parse_range(f"{base_start}..{sync_end}")
    # Sync only tops up cold days; a lapsed Google login (Testing-mode token ~7 days)
    # must not withhold cached days (the status panel shows Connect).
    try:
        health.get(sync, "sleep_hours")
    except ValueError as e:
        print(f"[trends] energy from cached days only: {e}")
    with db.session() as con:
        rows = con.execute(
            "SELECT day, metric, value FROM health_days"
            " WHERE metric IN (?,?,?) AND day BETWEEN ? AND ?",
            (*ENERGY_WEIGHTS, base_start, rng.end_key)).fetchall()

    by_metric: dict[str, dict[str, float]] = defaultdict(dict)
    for r in rows:
        by_metric[r["metric"]][r["day"]] = r["value"]
    baseline = {m: median(vals.values()) if vals else None
                for m, vals in by_metric.items()}

    data = []
    for d in days:
        num = den = 0.0
        for metric, weight in ENERGY_WEIGHTS.items():
            value = by_metric[metric].get(d)
            s = None if value is None else _score(metric, value,
                                                  baseline.get(metric))
            if s is not None:
                num += weight * s
                den += weight
        data.append({"day": d,
                     "value": round(100 * num / den) if den else None})
    return data


@router.get("/trends/metrics")
def trends_metrics():
    """Everything the expand can chart, split by which endpoint serves it."""
    habits = [h["name"] for h in db.list_habits()]
    return {
        "trends": list(BASE_METRICS) + [f"habit:{n}" for n in habits],
        "health": list(health.METRICS),
        "ranges": list(ranges.PAST_RANGES),
        "custom": ranges.CUSTOM_GRAMMAR,
    }


@router.get("/trends/correlations")
def get_correlations(window: int = features.CORR_WINDOW_DAYS,
                     focus: str | None = None, cross_domain: bool = True):
    """Significant cross-source feature relationships over the trailing
    `window` days (see providers/features.correlations). `focus` is a
    comma-separated feature allow-list (e.g. 'listen_min,novelty')."""
    day = db.day_key()
    focus_set = {f for f in (focus or "").split(",") if f} or None
    pairs = features.correlations(day, max(30, min(window, 1825)),
                                  focus=focus_set, cross_domain=cross_domain)
    return {"day": day, "window": window, "cross_domain": cross_domain,
            "pairs": pairs}


@router.get("/trends")
def get_trends(range: str = "this-week", metric: str = "energy"):
    try:
        rng = ranges.parse_range(range)
    except ValueError as e:
        raise HTTPException(400, str(e))
    today = db.day_key()
    days = [d for d in rng.days() if d <= today]  # no future points

    # Energy bypasses providers.get(), so envelope stamping lives here (else a failed Fitbit backfill degrades silently).
    degraded: list[str] = []
    if metric in BASE_METRICS:
        with offline.collect() as noted:
            data = _energy_series(rng, days)
        degraded = noted
    elif metric.startswith("habit:"):
        name = metric[len("habit:"):]
        with db.session() as con:
            h = con.execute(
                "SELECT id FROM habits WHERE name=? AND archived=0",
                (name,)).fetchone()
            if not h:
                raise HTTPException(400, f"unknown habit {name!r}")
            have = {r["day"]: r["value"] for r in con.execute(
                "SELECT day, value FROM habit_checkins"
                " WHERE habit_id=? AND day BETWEEN ? AND ?",
                (h["id"], rng.start_key, rng.end_key))}
        # no checkin row = genuinely not done = 0 (unlike energy's null)
        data = [{"day": d, "value": have.get(d, 0)} for d in days]
    else:
        raise HTTPException(
            400, f"unknown metric {metric!r}; use energy or habit:<name>")

    return {"source": "trends", "range": rng.token, "start": rng.start_key,
            "end": rng.end_key, "metric": metric, "data": data,
            **({"stale": True, "offline": degraded} if degraded else {})}
