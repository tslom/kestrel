"""Google Health provider (Fitbit Air, Health API v4). Google Health is truth;
`health_days`, `health_sync`, `health_hours` and `sleep_segments` are derived
cache. Days are the API's civil dates, not day_key; sleep belongs to the
wake-up date (endTime + endUtcOffset). A synced day with no row for a metric
means no data, not never fetched.
Design: DESIGN.md → §3 Health.
"""

import logging
import urllib.parse
from collections import defaultdict
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone

from .. import db, tz
from ..config import (
    EXERCISE_AZM_THRESHOLD,
    EXERCISE_HABIT_NAME,
    EXERCISE_HR_MAX_THRESHOLD,
    GOOGLE_HEALTH_ACCOUNT,
    SLEEP_HABIT_NAME,
    SLEEP_HOURS_THRESHOLD,
)
from . import http, metric_cache, oauth, offline
from .ranges import Range, days_between

log = logging.getLogger(__name__)

NAME = "health"
METRICS = ("steps", "resting_hr", "hr_avg", "hr_max", "hrv", "sleep_hours",
           "sleep_deep_min", "sleep_rem_min", "sleep_light_min",
           "sleep_awake_min", "active_zone_minutes",
           # nightly, sleep-derived (wake-up day): SpO2 %, breaths/min, skin temp °F
           "spo2", "respiratory_rate", "skin_temp")
DEFAULT_METRIC = "steps"
ALLOW_FUTURE = False    # your body has no next-week data

API_BASE = "https://health.googleapis.com/v4/users/me/dataTypes"

# dailyRollUp's per-request civil-range cap: 14 days for heart-rate, 90 otherwise.
ROLLUP_MAX_DAYS = 90
ROLLUP_MAX_DAYS_HEART_RATE = 14

# refresh() keeps [today-7 .. today] warm; colder days are fetched on demand by get().
REFRESH_PAST_DAYS = 7

# Intraday HR (feeds features.py's hr_var_day); the rollUp caps heart-rate at 14 days.
INTRADAY_HR_DAYS = 7
HR_HOURLY_METRIC = "hr_hourly"

# Intraday steps share the health_hours shelf. Not capped at 14 days, so cold
# days fill on demand in day_detail(), not by widening the poller's window.
STEPS_HOURLY_METRIC = "steps_hourly"

# API stage names -> the four keys the app and sheet CSS use.
STAGE_KEYS = {"DEEP": "deep", "REM": "rem", "LIGHT": "light", "AWAKE": "awake"}

# Desk card window: 28 days holds four of each weekday, so a weekday/weekend shape averages out.
SUMMARY_DAYS = 28

# What the card can lead with; deliberately not METRICS (stages and the nightly trio are sheet-only).
SUMMARY_METRICS = ("steps", "sleep_hours", "resting_hr", "active_zone_minutes")


def _api(url: str, token: str, body: dict | None = None) -> dict:
    # Every call is a read, POSTs included (rollUp takes its range in a body), so retry is safe.
    return http.json_request(url, label="health API", token=token,
                             method="GET" if body is None else "POST",
                             body=body, idempotent=True, reauth=lambda:
                             oauth.refresh("google", GOOGLE_HEALTH_ACCOUNT))


def _civil(d: date) -> dict:
    return {"date": {"year": d.year, "month": d.month, "day": d.day}}


def _day_of(civil: dict) -> str:
    d = civil["date"]
    return date(d["year"], d["month"], d["day"]).isoformat()


def _civil_end_day(interval: dict) -> str:
    """Local civil date a session ended: endTime (UTC) + endUtcOffset."""
    end = datetime.fromisoformat(interval["endTime"].replace("Z", "+00:00"))
    offset = int(interval["endUtcOffset"].removesuffix("s"))
    return (end + timedelta(seconds=offset)).date().isoformat()


def _rollup_points(token: str, dtype: str, start: date, end: date,
                   max_days: int = ROLLUP_MAX_DAYS):
    """Yield dailyRollUp points (windowSizeDays=1) for [start, end] inclusive,
    chunked to the per-type civil-range cap. Range is closed-open."""
    url = f"{API_BASE}/{dtype}/dataPoints:dailyRollUp"
    chunk = start
    while chunk <= end:
        chunk_end = min(chunk + timedelta(days=max_days - 1), end)
        body = {"range": {"start": _civil(chunk),
                          "end": _civil(chunk_end + timedelta(days=1))},
                "windowSizeDays": 1}
        while True:
            blob = _api(url, token, body)
            yield from blob.get("rollupDataPoints", [])
            if not blob.get("nextPageToken"):
                break
            body = {**body, "pageToken": blob["nextPageToken"]}
        chunk = chunk_end + timedelta(days=1)


def _hourly(token: str, dtype: str, start: date, end: date, pick,
            max_days: int = ROLLUP_MAX_DAYS) -> dict[tuple[str, int], float]:
    """Hourly buckets over local days [start, end] inclusive -> {(local_day, local_hour): value}.
    `pick` returns None for a bucket the watch didn't record. The span is framed
    in `tz.zone_on(day)`, never the machine's zone: a cold day spent elsewhere
    otherwise double-counts hours (10,610 hourly vs 8,765 daily steps)."""
    # Widen a day each side to cover the edges, filter by bucket key; the 2 days stay inside the API cap.
    out: dict[tuple[str, int], float] = {}
    url = f"{API_BASE}/{dtype}/dataPoints:rollUp"
    chunk = start
    while chunk <= end:
        chunk_end = min(chunk + timedelta(days=max(1, max_days - 2) - 1), end)
        zone = tz.zone_on(chunk)
        lo = datetime.combine(chunk - timedelta(days=1), time.min, zone)
        hi = datetime.combine(chunk_end + timedelta(days=2), time.min, zone)
        body = {"range": {"startTime": _instant(lo), "endTime": _instant(hi)},
                "windowSize": "3600s"}
        while True:
            blob = _api(url, token, body)
            for p in blob.get("rollupDataPoints", []):
                value = pick(p)
                if value is None:
                    continue
                key = _bucket(p["startTime"])
                if start.isoformat() <= key[0] <= end.isoformat():
                    out[key] = value
            if not blob.get("nextPageToken"):
                break
            body = {**body, "pageToken": blob["nextPageToken"]}
        chunk = chunk_end + timedelta(days=1)
    return out


def _bucket(ts: str) -> tuple[str, int]:
    """A UTC instant -> (local day, local hour). Zone depends on day and day on
    zone, so seed with the UTC date and re-read once; that settles it."""
    inst = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    seed = inst.astimezone(tz.zone_on(inst.date()))
    local = inst.astimezone(tz.zone_on(seed.date()))
    return local.date().isoformat(), local.hour


def _pick_hr(p: dict) -> float | None:
    hr = p.get("heartRate") or {}
    return None if "beatsPerMinuteAvg" not in hr else round(hr["beatsPerMinuteAvg"], 1)


def _pick_steps(p: dict) -> float | None:
    """0 steps is a real answer; only a missing `countSum` is a gap."""
    steps = p.get("steps") or {}
    return None if "countSum" not in steps else float(steps["countSum"])


def _hr_hourly(token: str, start: date, end: date) -> dict[tuple[str, int], float]:
    """Hourly mean bpm, capped at the 14-day heart-rate span."""
    return _hourly(token, "heart-rate", start, end, _pick_hr,
                   max_days=ROLLUP_MAX_DAYS_HEART_RATE)


def _steps_hourly(token: str, start: date, end: date) -> dict[tuple[str, int], float]:
    """Steps taken per hour."""
    return _hourly(token, "steps", start, end, _pick_steps)


def _instant(dt: datetime) -> str:
    """Local aware datetime -> RFC3339 UTC instant the API accepts."""
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _write_hours(con, metric: str, start: date, end: date,
                 buckets: dict[tuple[str, int], float], mark: bool = False) -> int:
    """Replace one metric's window in health_hours. The delete is scoped by metric
    too: the two series use different windows and must not evict each other."""
    now = datetime.now().isoformat(timespec="seconds")
    with con:
        con.execute("DELETE FROM health_hours WHERE metric=? AND day BETWEEN ? AND ?",
                    (metric, start.isoformat(), end.isoformat()))
        con.executemany(
            "INSERT INTO health_hours (day, hour, metric, value, refreshed_at)"
            " VALUES (?, ?, ?, ?, ?)",
            [(day, hour, metric, value, now)
             for (day, hour), value in buckets.items()])
        # An empty fetched day is still fetched (stops day_detail() re-asking); only steps sets `steps_at`.
        d = start
        while mark and d <= end:
            con.execute(
                "INSERT INTO health_sync (day, refreshed_at, steps_at)"
                " VALUES (?,?,?) ON CONFLICT(day) DO UPDATE SET steps_at=excluded.steps_at",
                (d.isoformat(), now, now))
            d += timedelta(days=1)
    return len(buckets)


def _gather(jobs: dict[str, Callable[[], object]]) -> dict[str, object]:
    """Run independent API reads concurrently -> {name: result or the exception
    it raised}. Serial was ~12.6s for 11 calls; concurrent costs the slowest
    (heart-rate rollUp, 2-4s). Workers fetch only; the caller writes."""
    out: dict[str, object] = {}
    with ThreadPoolExecutor(max_workers=max(1, len(jobs)),
                            thread_name_prefix="health") as pool:
        futures = {name: pool.submit(fn) for name, fn in jobs.items()}
        for name, fut in futures.items():
            try:
                out[name] = fut.result()
            except Exception as e:  # handed back to the caller
                out[name] = e
    return out


def _fetch_hours(token: str, start: date, end: date) -> dict[str, object]:
    """Both intraday series for [start, end] -> {metric: buckets or error}."""
    return _gather({
        HR_HOURLY_METRIC: lambda: _hr_hourly(token, start, end),
        STEPS_HOURLY_METRIC: lambda: _steps_hourly(token, start, end),
    })


def _write_fetched_hours(con, start: date, end: date,
                         fetched: dict[str, object]) -> int:
    """Write each series that came back, then raise the first error (one series failing keeps the other)."""
    n, errors = 0, []
    for metric, got in fetched.items():
        if isinstance(got, BaseException):
            errors.append(got)
            continue
        n += _write_hours(con, metric, start, end, got,
                          mark=metric == STEPS_HOURLY_METRIC)
    if errors:
        raise errors[0]
    return n


def _list_metric(token, dtype, filter_field, obj_key, value_key, metric,
                 start, end, transform=lambda v: round(v, 1)
                 ) -> dict[str, dict[str, float]]:
    """A Daily-record list metric (resting HR, HRV, the nightly trio) ->
    {day: {metric: value}}."""
    filter_ = (f'{filter_field}.date >= "{start.isoformat()}"'
               f' AND {filter_field}.date < "{(end + timedelta(days=1)).isoformat()}"')
    days: dict[str, dict[str, float]] = defaultdict(dict)
    for p in _list_points(token, dtype, filter_):
        obj = p.get(obj_key)
        if obj and value_key in obj:
            days[_day_of(obj)][metric] = transform(obj[value_key])
    return days


def _list_points(token: str, dtype: str, filter_: str):
    """Yield dataPoints.list results across pages."""
    page_token = None
    while True:
        params = {"filter": filter_}
        if page_token:
            params["pageToken"] = page_token
        blob = _api(f"{API_BASE}/{dtype}/dataPoints"
                    f"?{urllib.parse.urlencode(params)}", token)
        yield from blob.get("dataPoints", [])
        page_token = blob.get("nextPageToken")
        if not page_token:
            break


def _steps_days(token, start, end):
    days: dict[str, dict[str, float]] = defaultdict(dict)
    for p in _rollup_points(token, "steps", start, end):
        if "steps" in p:
            days[_day_of(p["civilStartTime"])]["steps"] = int(p["steps"].get("countSum", 0))
    return days


def _azm_days(token, start, end):
    days: dict[str, dict[str, float]] = defaultdict(dict)
    for p in _rollup_points(token, "active-zone-minutes", start, end):
        if "activeZoneMinutes" in p:
            azm = p["activeZoneMinutes"]
            days[_day_of(p["civilStartTime"])]["active_zone_minutes"] = sum(
                int(azm.get(z, 0)) for z in ("sumInFatBurnHeartZone",
                                             "sumInCardioHeartZone",
                                             "sumInPeakHeartZone"))
    return days


def _hr_days(token, start, end):
    days: dict[str, dict[str, float]] = defaultdict(dict)
    for p in _rollup_points(token, "heart-rate", start, end,
                            max_days=ROLLUP_MAX_DAYS_HEART_RATE):
        if "heartRate" in p:
            hr, day = p["heartRate"], _day_of(p["civilStartTime"])
            if "beatsPerMinuteAvg" in hr:
                days[day]["hr_avg"] = round(hr["beatsPerMinuteAvg"], 1)
            if "beatsPerMinuteMax" in hr:
                days[day]["hr_max"] = hr["beatsPerMinuteMax"]
    return days


def _sleep_days(token, start, end):
    """Nightly totals plus the nights' segments; one response carries both."""
    stage_metric = {"DEEP": "sleep_deep_min", "REM": "sleep_rem_min",
                    "LIGHT": "sleep_light_min", "AWAKE": "sleep_awake_min"}
    days: dict[str, dict[str, float]] = defaultdict(dict)
    nights: dict[str, list[dict]] = defaultdict(list)
    for day, sleep in _sleep_sessions(token, start, end):
        summary = sleep.get("summary", {})
        if "minutesAsleep" in summary:
            days[day]["sleep_hours"] = round(
                days[day].get("sleep_hours", 0)
                + int(summary["minutesAsleep"]) / 60, 2)
        for s in summary.get("stagesSummary", []):
            metric = stage_metric.get(s.get("type"))
            if metric:
                days[day][metric] = days[day].get(metric, 0) + int(s.get("minutes", 0))
        nights[day].extend(_segments_of(sleep))
    return days, nights


# (name, health_days metrics it owns, optional, fetch(token, start, end) -> {day: {metric: value}}).
# Ownership lets a failed series keep its cached rows. `optional` types may be
# unavailable on a watch: an API refusal (RuntimeError) is "no data"; a network failure still counts.
_SLEEP_METRICS = ("sleep_hours", "sleep_deep_min", "sleep_rem_min",
                  "sleep_light_min", "sleep_awake_min")
SERIES = (
    ("steps", ("steps",), False, _steps_days),
    ("active-zone-minutes", ("active_zone_minutes",), False, _azm_days),
    ("heart-rate", ("hr_avg", "hr_max"), False, _hr_days),
    ("resting-hr", ("resting_hr",), False,
     lambda t, s, e: _list_metric(
         t, "daily-resting-heart-rate", "daily_resting_heart_rate",
         "dailyRestingHeartRate", "beatsPerMinute", "resting_hr", s, e,
         transform=int)),
    ("hrv", ("hrv",), False,
     lambda t, s, e: _list_metric(
         t, "daily-heart-rate-variability", "daily_heart_rate_variability",
         "dailyHeartRateVariability", "averageHeartRateVariabilityMilliseconds",
         "hrv", s, e)),
    ("spo2", ("spo2",), True,
     lambda t, s, e: _list_metric(
         t, "daily-oxygen-saturation", "daily_oxygen_saturation",
         "dailyOxygenSaturation", "averagePercentage", "spo2", s, e)),
    ("respiratory-rate", ("respiratory_rate",), True,
     lambda t, s, e: _list_metric(
         t, "daily-respiratory-rate", "daily_respiratory_rate",
         "dailyRespiratoryRate", "breathsPerMinute", "respiratory_rate", s, e)),
    # API gives °C; stored as °F.
    ("skin-temp", ("skin_temp",), True,
     lambda t, s, e: _list_metric(
         t, "daily-sleep-temperature-derivations",
         "daily_sleep_temperature_derivations",
         "dailySleepTemperatureDerivations", "nightlyTemperatureCelsius",
         "skin_temp", s, e, transform=lambda c: round(c * 9 / 5 + 32, 1))),
    ("sleep", _SLEEP_METRICS, False, _sleep_days),
)


@dataclass
class _Pull:
    """What one _fetch got back. `metrics`: those whose series answered (only these
    may be replaced in cache); `nights` is None unless sleep answered."""
    days: dict[str, dict[str, float]] = field(
        default_factory=lambda: defaultdict(dict))
    nights: dict[str, list[dict]] | None = None
    metrics: set[str] = field(default_factory=set)
    errors: list[BaseException] = field(default_factory=list)


def _fetch(start: date, end: date) -> _Pull:
    """Pull every series for [start, end] concurrently. A point with no value means
    no row. A failed series goes to `errors` and contributes nothing."""
    token = oauth.get_token("google", GOOGLE_HEALTH_ACCOUNT)
    got = _gather({name: (lambda fn=fn: fn(token, start, end))
                   for name, _, _, fn in SERIES})
    pull = _Pull()
    for name, metrics, optional, _ in SERIES:
        result = got[name]
        if isinstance(result, BaseException):
            if optional and isinstance(result, RuntimeError):
                log.debug("health: %s unavailable: %s", name, result)
            else:
                log.warning("health: %s fetch failed: %r", name, result)
                pull.errors.append(result)
            continue
        if name == "sleep":
            result, pull.nights = result
        for day, values in result.items():
            pull.days[day].update(values)
        pull.metrics.update(metrics)
    return pull


def _sleep_sessions(token: str, start: date, end: date):
    """Yield (wake-up day, sleep object) for every session ending in the window;
    the day is the civil date the session ENDED, not its start."""
    sleep_filter = (f'sleep.interval.civil_end_time >= "{start.isoformat()}"'
                    f' AND sleep.interval.civil_end_time < '
                    f'"{(end + timedelta(days=1)).isoformat()}"')
    for p in _list_points(token, "sleep", sleep_filter):
        sleep = p.get("sleep")
        if sleep:
            yield _civil_end_day(sleep["interval"]), sleep


def _local(ts: str, offset: str) -> datetime:
    """A UTC instant plus its recorded offset -> the wall clock you slept by (never today's zone)."""
    inst = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    return inst.astimezone(timezone(timedelta(seconds=int(offset.removesuffix("s")))))


def _segments_of(sleep: dict) -> list[dict]:
    """The night's stage segments, in clock order."""
    out = []
    for s in sleep.get("stages", []):
        stage = STAGE_KEYS.get(s.get("type"))
        if not stage or "startTime" not in s or "endTime" not in s:
            continue
        lo = _local(s["startTime"], s.get("startUtcOffset", "0s"))
        hi = _local(s["endTime"], s.get("endUtcOffset", "0s"))
        out.append({"stage": stage, "start": lo.isoformat(), "end": hi.isoformat(),
                    "minutes": round((hi - lo).total_seconds() / 60, 2)})
    return sorted(out, key=lambda s: s["start"])


def _sync(con, start: date, end: date) -> int:
    """API -> cache for the window: replace metric rows, mark days synced.

    A failed series keeps its cached rows; the first failure is raised after the
    rest land, and the window is not marked synced (so get() asks again). The
    raised error prefers a non-network one so best_effort() can't hide a dead token."""
    pull = _fetch(start, end)
    now = datetime.now().isoformat(timespec="seconds")
    with con:
        if pull.metrics:
            marks = ",".join("?" * len(pull.metrics))
            con.execute(
                f"DELETE FROM health_days WHERE day BETWEEN ? AND ?"
                f" AND metric IN ({marks})",
                (start.isoformat(), end.isoformat(), *sorted(pull.metrics)))
        metric_cache.upsert_days(con, "health_days", pull.days, now)
        if pull.nights is not None:
            _write_segments(con, start, end, pull.nights, now)
        d = start
        while not pull.errors and d <= end:
            con.execute(
                "INSERT INTO health_sync (day, refreshed_at, segments_at)"
                " VALUES (?,?,?) ON CONFLICT(day) DO UPDATE SET"
                " refreshed_at=excluded.refreshed_at, segments_at=excluded.segments_at",
                (d.isoformat(), now, now))
            d += timedelta(days=1)
    if pull.errors:
        raise next((e for e in pull.errors if not offline.is_network_error(e)),
                   pull.errors[0])
    return sum(len(m) for m in pull.days.values())


def _write_segments(con, start: date, end: date,
                    nights: dict[str, list[dict]], now: str) -> None:
    """Replace the window's segments. Caller owns the transaction so totals and shape land together."""
    con.execute("DELETE FROM sleep_segments WHERE day BETWEEN ? AND ?",
                (start.isoformat(), end.isoformat()))
    con.executemany(
        "INSERT INTO sleep_segments (day, seq, stage, start_ts, end_ts, minutes,"
        " refreshed_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        [(day, seq, s["stage"], s["start"], s["end"], s["minutes"], now)
         for day, segs in nights.items()
         # naps share the wake-up day: sequence across the day
         for seq, s in enumerate(sorted(segs, key=lambda s: s["start"]))])


def _auto_habit(con, start: date, end: date, metric: str, threshold: float,
                habit_name: str, note_fn, gate: tuple[str, float] | None = None
                ) -> int:
    """Auto-check `habit_name` for each just-synced day whose `metric` reaches
    `threshold`. Fills only untouched days (a manual check or un-check wins).
    `note_fn(value, gate_value)` renders the note. Returns new check-ins; off
    when `threshold` <= 0.

    `gate` is an optional second `(metric, threshold)` the day must also clear;
    a gated day with no gate metric cached does not check in. Gate <= 0 is dropped."""
    if threshold <= 0:
        return 0
    if gate and gate[1] <= 0:
        gate = None
    sql = ("SELECT day, value, NULL AS gate_value FROM health_days"
           " WHERE metric=? AND day BETWEEN ? AND ? AND value >= ?")
    args = [metric, start.isoformat(), end.isoformat(), threshold]
    if gate:
        sql = ("SELECT m.day, m.value, g.value AS gate_value"
               " FROM health_days m JOIN health_days g"
               " ON g.day = m.day AND g.metric = ? AND g.value >= ?"
               " WHERE m.metric=? AND m.day BETWEEN ? AND ? AND m.value >= ?")
        args = [gate[0], gate[1]] + args
    n = 0
    for r in con.execute(sql, args).fetchall():
        note = note_fn(r["value"], r["gate_value"])
        if db.auto_checkin(habit_name, r["day"], note=note):
            n += 1
    return n


def _auto_exercise(con, start: date, end: date) -> int:
    """Active Zone Minutes >= EXERCISE_AZM_THRESHOLD *and* peak HR >=
    EXERCISE_HR_MAX_THRESHOLD -> the Exercise habit (civil date = the day the
    minutes were earned). AZM alone can't tell a workout from any other reason
    the heart rate sat high all evening; the peak-HR gate is what makes it a
    workout signal. See config for the numbers and why."""
    return _auto_habit(
        con, start, end, "active_zone_minutes", EXERCISE_AZM_THRESHOLD,
        EXERCISE_HABIT_NAME,
        # hr is None only when the peak gate is switched off in config.
        lambda v, hr: f"auto: {int(v)} active zone min" + (
            f", {int(hr)} bpm peak" if hr is not None else ""),
        gate=("hr_max", EXERCISE_HR_MAX_THRESHOLD))


def _auto_sleep(con, start: date, end: date) -> int:
    """sleep_hours >= SLEEP_HOURS_THRESHOLD -> the Sleep habit (keyed on the
    wake-up day, exactly as the sleep metric itself is stored)."""
    return _auto_habit(
        con, start, end, "sleep_hours", SLEEP_HOURS_THRESHOLD,
        SLEEP_HABIT_NAME, lambda v, _: f"auto: {v:.1f}h sleep")


def refresh() -> dict:
    """Poller entrypoint: keep the trailing window warm. Quiet no-op until
    the personal account is authorized (the poller runs from first boot)."""
    try:
        token = oauth.get_token("google", GOOGLE_HEALTH_ACCOUNT)
    except oauth.NotAuthorized as e:
        return {"skipped": str(e)}
    end = date.fromisoformat(db.day_key())
    start = end - timedelta(days=REFRESH_PAST_DAYS)
    hr_start = end - timedelta(days=INTRADAY_HR_DAYS)
    # The intraday pull runs beside the daily one rather than after it: the
    # hourly heart-rate rollUp is the single slowest call (~3-4s for the
    # week), so waiting for it last was most of a refresh's wall time.
    with db.session() as con, ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="health-hours") as pool:
        hours_job = pool.submit(_fetch_hours, token, hr_start, end)
        try:
            n = _sync(con, start, end)
        finally:
            # Intraday HR is best-effort: never let it sink the daily sync
            # above — and written even when that sync failed, since it's a
            # separate fetch that may well have answered.
            try:
                hours = _write_fetched_hours(con, hr_start, end,
                                             hours_job.result())
            except Exception as e:  # best-effort, see above
                log.warning("health: intraday HR sync failed: %r", e)
                hours = None
        # Auto exercise check-in is best-effort too: an app.db hiccup here must
        # never sink the sync. Runs over the just-synced trailing window.
        try:
            exercise = _auto_exercise(con, start, end)
        except Exception as e:  # never let a check-in sink sync
            log.warning("health: auto exercise check-in failed: %s", e)
            exercise = None
        try:
            sleep = _auto_sleep(con, start, end)
        except Exception as e:  # never let a check-in sink sync
            log.warning("health: auto sleep check-in failed: %s", e)
            sleep = None
    return {"rows": n, "hr_hours": hours, "exercise_checkins": exercise,
            "sleep_checkins": sleep, "window": f"{start}..{end}"}


def brief_line(pt: dict) -> str:
    """One-line data point for --format brief."""
    return f"{pt['day']} {pt['value']}"


def summary() -> dict:
    """One payload for the desk card: today's headline metrics, each with the
    trailing window it should be read against.

    Hand-written for the same reason /screen/summary and /location/summary are
    — get() is one metric per call, so a card wanting four of them plus a
    baseline for each would cost eight round trips. And **cache-only**, the
    rule those two settled: rendering a widget must never reach out to Google,
    so a day that was never synced reads as a gap here and the poller (or an
    explicit get()) is what fills it.

    What this returns is series, not judgement. Deciding which of these four
    is worth leading the card with belongs to `lib/notable.js` on the desk,
    for the reason DESIGN.md gives about salience generally: a second copy of
    that rule living back here would drift out of agreement with the card
    that renders it, and the card is the thing you actually read.
    """
    today = db.day_key()
    start = (date.fromisoformat(today) - timedelta(days=SUMMARY_DAYS - 1)).isoformat()
    marks = ",".join("?" * len(SUMMARY_METRICS))
    with db.session() as con:
        rows = con.execute(
            f"SELECT day, metric, value FROM health_days WHERE metric IN ({marks})"
            " AND day BETWEEN ? AND ? ORDER BY day",
            (*SUMMARY_METRICS, start, today)).fetchall()
        refreshed = con.execute(
            "SELECT MIN(refreshed_at) AS r FROM health_sync WHERE day BETWEEN ? AND ?",
            (start, today)).fetchone()["r"]

    by_metric: dict[str, dict[str, float]] = defaultdict(dict)
    for r in rows:
        by_metric[r["metric"]][r["day"]] = r["value"]

    span = days_between(start, today)
    # Absent day = absent point, never 0 — the same gap-not-zero rule the
    # features table and the screen sparkline rest on. A watch left on the
    # charger is not a day you took zero steps, and a baseline that counted it
    # as one would call every ordinary day afterwards a spike.
    metrics = {
        m: {"value": by_metric[m].get(today),
            "series": [{"day": d, "value": by_metric[m].get(d)} for d in span]}
        for m in SUMMARY_METRICS
    }
    return {"day": today, "days": SUMMARY_DAYS, "metrics": metrics,
            "refreshed_at": refreshed}


def get(rng: Range, metric: str | None = None) -> dict:
    """Read the slice from cache; sync any never-fetched days first (one API
    window spanning them). Days after today (this-week's tail) are skipped."""
    metric = metric or DEFAULT_METRIC
    today = db.day_key()
    with db.session() as con:
        missing = metric_cache.missing_days(con, "health_sync", rng, upto=today)
        if missing:
            with offline.best_effort("health backfill"), oauth.as_value_error():
                _sync(con, date.fromisoformat(missing[0]),
                      date.fromisoformat(missing[-1]))
        rows = con.execute(
            "SELECT day, value FROM health_days WHERE metric=?"
            " AND day BETWEEN ? AND ? ORDER BY day",
            (metric, rng.start_key, rng.end_key)).fetchall()
        refreshed = metric_cache.oldest_refresh(con, "health_sync", rng)
    return {
        "metric": metric,
        "data": [{"day": r["day"], "value": r["value"]} for r in rows],
        "refreshed_at": refreshed,
    }


# ---------------------------------------------------------------- drill-downs
#
# One night and one day, at the resolution the daily rows threw away. Both fill
# on demand and then cache, which is get()'s bargain rather than summary()'s:
# a card renders without being asked and must never reach out to Google, but
# these are a click on a specific bar, and a July night that predates the
# segments table has to be fetchable or the drill-down is a feature that only
# works on this week.


def _fetched(con, day: str, column: str) -> bool:
    row = con.execute(f"SELECT {column} AS at FROM health_sync WHERE day=?",
                      (day,)).fetchone()
    return bool(row and row["at"])


def _sync_segments(con, day: date) -> int:
    """Fill one night's segments from the API and mark it fetched."""
    token = oauth.get_token("google", GOOGLE_HEALTH_ACCOUNT)
    nights: dict[str, list[dict]] = defaultdict(list)
    for d, sleep in _sleep_sessions(token, day, day):
        nights[d].extend(_segments_of(sleep))
    now = datetime.now().isoformat(timespec="seconds")
    with con:
        _write_segments(con, day, day, nights, now)
        con.execute(
            "INSERT INTO health_sync (day, refreshed_at, segments_at) VALUES (?,?,?)"
            " ON CONFLICT(day) DO UPDATE SET segments_at=excluded.segments_at",
            (day.isoformat(), now, now))
    return sum(len(v) for v in nights.values())


def night(day: str) -> dict:
    """One night drawn instead of summed: every stage segment in clock order,
    plus the two durations the desk has never shown apart.

    `asleep_min` and `in_bed_min` are the answer to the question this was asked
    as. The nightly `sleep_hours` metric is Fitbit's `minutesAsleep` — time in
    bed *minus* awake — so a 5.2h night and a 6.3h lie-in have always rendered
    as the same bar height, and the difference between them was in a field the
    card doesn't print. Both are summed from the segments here, so the number
    and the picture can't disagree.
    """
    with db.session() as con:
        if not _fetched(con, day, "segments_at"):
            with offline.best_effort("sleep segments"), oauth.as_value_error():
                _sync_segments(con, date.fromisoformat(day))
        rows = con.execute(
            "SELECT stage, start_ts, end_ts, minutes FROM sleep_segments"
            " WHERE day=? ORDER BY seq", (day,)).fetchall()
        totals = {r["metric"]: r["value"] for r in con.execute(
            "SELECT metric, value FROM health_days WHERE day=? AND metric LIKE 'sleep%'",
            (day,))}
        refreshed = con.execute(
            "SELECT segments_at FROM health_sync WHERE day=?", (day,)).fetchone()

    segs = [{"stage": r["stage"], "start": r["start_ts"], "end": r["end_ts"],
             "minutes": r["minutes"]} for r in rows]
    awake = sum(s["minutes"] for s in segs if s["stage"] == "awake")
    asleep = sum(s["minutes"] for s in segs if s["stage"] != "awake")
    return {
        "day": day,
        "segments": segs,
        # Span is first-asleep to last-awake across the whole day, so a nap
        # widens it; the two sums below don't count the gap between sessions.
        "start": segs[0]["start"] if segs else None,
        "end": segs[-1]["end"] if segs else None,
        "asleep_min": round(asleep, 1),
        "awake_min": round(awake, 1),
        "in_bed_min": round(asleep + awake, 1),
        "totals": totals,
        "refreshed_at": refreshed["segments_at"] if refreshed else None,
    }


def day_detail(day: str) -> dict:
    """One day's intraday curves — steps per hour, and the heart rate already
    cached beside them.

    Steps fill on demand for any day. Heart rate doesn't: the rollUp endpoint
    caps heart-rate spans at ROLLUP_MAX_DAYS_HEART_RATE, so an old day's HR is
    simply gone and the payload says so by omitting the hours rather than by
    drawing a flat line at zero."""
    with db.session() as con:
        if not _fetched(con, day, "steps_at"):
            with offline.best_effort("intraday steps"), oauth.as_value_error():
                d = date.fromisoformat(day)
                _write_hours(con, STEPS_HOURLY_METRIC, d, d,
                             _steps_hourly(oauth.get_token(
                                 "google", GOOGLE_HEALTH_ACCOUNT), d, d),
                             mark=True)
        rows = con.execute(
            "SELECT hour, metric, value FROM health_hours WHERE day=?"
            " AND metric IN (?, ?) ORDER BY hour",
            (day, STEPS_HOURLY_METRIC, HR_HOURLY_METRIC)).fetchall()
        totals = {r["metric"]: r["value"] for r in con.execute(
            "SELECT metric, value FROM health_days WHERE day=?"
            " AND metric IN ('steps', 'active_zone_minutes', 'hr_avg', 'hr_max')",
            (day,))}
        refreshed = con.execute(
            "SELECT steps_at FROM health_sync WHERE day=?", (day,)).fetchone()

    by_hour: dict[int, dict] = {}
    for r in rows:
        slot = by_hour.setdefault(r["hour"], {"hour": r["hour"]})
        slot["steps" if r["metric"] == STEPS_HOURLY_METRIC else "hr"] = r["value"]
    return {
        "day": day,
        # Every hour of the day, so the curve has an x-axis even where the
        # watch recorded nothing — an absent hour carries no keys rather than
        # a zero, the same gap-not-zero rule the daily series follow.
        "hours": [by_hour.get(h, {"hour": h}) for h in range(24)],
        "totals": totals,
        "refreshed_at": refreshed["steps_at"] if refreshed else None,
    }
