"""Weather provider: Open-Meteo, keyless. `weather_days` + `geo_cache` are a droppable cache.

One row per (day, metric); `day` is Open-Meteo's civil date for the origin (not
day_key). Origin: latest location fix under 6h old, else home. No sync-marker
table: a day with no rows means never fetched or past the horizon.
Design: DESIGN.md → §6 Weather.
"""

import time
import urllib.parse
from datetime import UTC, date, datetime, timedelta

from .. import db
from . import http, metric_cache, offline
from .ranges import Range

NAME = "weather"
METRICS = ("tmax", "tmin", "precip_prob", "weather_code", "sunrise", "sunset",
           "uv_index", "wind_speed")
DEFAULT_METRIC = "tmax"
ALLOW_FUTURE = True    # it's a forecast

FORECAST_BASE = "https://api.open-meteo.com/v1/forecast"
GEOCODE_BASE = "https://geocoding-api.open-meteo.com/v1/search"

# Open-Meteo daily field -> our metric name.
FIELD_METRIC = {
    "temperature_2m_max": "tmax",
    "temperature_2m_min": "tmin",
    "precipitation_probability_max": "precip_prob",
    "weather_code": "weather_code",
    "sunrise": "sunrise",
    "sunset": "sunset",
    "uv_index_max": "uv_index",
    "wind_speed_10m_max": "wind_speed",
}

# Open-Meteo caps: forecast_days <= 16, past_days <= 92.
MAX_FORECAST_DAYS = 16
MAX_PAST_DAYS = 92

REFRESH_FUTURE_DAYS = 7

# WMO weather codes, short text for brief/widget use.
WMO_TEXT = {
    0: "clear", 1: "mostly clear", 2: "partly cloudy", 3: "overcast",
    45: "fog", 48: "rime fog",
    51: "light drizzle", 53: "drizzle", 55: "dense drizzle",
    56: "freezing drizzle", 57: "dense freezing drizzle",
    61: "light rain", 63: "rain", 65: "heavy rain",
    66: "freezing rain", 67: "heavy freezing rain",
    71: "light snow", 73: "snow", 75: "heavy snow", 77: "snow grains",
    80: "light showers", 81: "showers", 82: "violent showers",
    85: "light snow showers", 86: "snow showers",
    95: "thunderstorm", 96: "thunderstorm + hail", 99: "thunderstorm + hail",
}


def weather_text(code: float | int | None) -> str | None:
    return None if code is None else WMO_TEXT.get(int(code), f"code {int(code)}")


def _api(url: str) -> dict:
    return http.json_request(url, label="weather API", timeout=15)


def _minutes_since_midnight(iso_dt: str) -> float:
    hh, mm = iso_dt[11:16].split(":")
    return int(hh) * 60 + int(mm)


def _origin() -> tuple[float, float]:
    """Latest location fix < 6h old if there is one, else home."""
    try:
        from . import location  # guarded, so this module works standalone
    except ImportError:
        location = None
    if location is not None:
        fix = location.latest_fix()
        if fix and fix.get("age_min", 999) < 360:
            return fix["lat"], fix["lon"]
    # Today's home, not a constant: the owner has moved.
    from ..places import home_now
    home = home_now()
    return home.lat, home.lon


def _fetch(lat: float, lon: float, start: date, end: date) -> dict[str, dict[str, float]]:
    """Pull daily fields for [start, end] -> {day: {metric: value}}; one call spans past and future."""
    # Offsets into Open-Meteo's own calendar: anchor on the civil date, not day_key (wrong between midnight and 4am).
    today = date.today()  # noqa: TID251 — Open-Meteo's civil date, not a day key
    past_days = min(MAX_PAST_DAYS, max(0, (today - start).days))
    forecast_days = min(MAX_FORECAST_DAYS, max(1, (end - today).days + 1))
    params = {
        "latitude": lat, "longitude": lon,
        "daily": ",".join(FIELD_METRIC),
        "timezone": "auto",
        # Force °F/mph (default is °C, km/h); wipe weather_days after changing units.
        "temperature_unit": "fahrenheit",
        "wind_speed_unit": "mph",
        "past_days": past_days,
        "forecast_days": forecast_days,
    }
    blob = _api(f"{FORECAST_BASE}?{urllib.parse.urlencode(params)}")
    daily = blob["daily"]
    days: dict[str, dict[str, float]] = {}
    for i, day in enumerate(daily["time"]):
        vals: dict[str, float] = {}
        for field, metric in FIELD_METRIC.items():
            v = daily[field][i]
            if v is None:
                continue
            if metric in ("sunrise", "sunset"):
                v = _minutes_since_midnight(v)
            vals[metric] = v
        days[day] = vals
    return days


def _horizon() -> tuple[str, str]:
    """First and last day _fetch() can return, on _fetch()'s civil-date anchor (forecast days count today)."""
    today = date.today()  # noqa: TID251 — the same civil anchor as _fetch
    return ((today - timedelta(days=MAX_PAST_DAYS)).isoformat(),
            (today + timedelta(days=MAX_FORECAST_DAYS - 1)).isoformat())


def _sync(con, lat: float, lon: float, start: date, end: date) -> int:
    days = _fetch(lat, lon, start, end)
    now = datetime.now().isoformat(timespec="seconds")
    with con:
        return metric_cache.upsert_days(con, "weather_days", days, now)


def refresh() -> dict:
    """Poller entrypoint: keep [today .. today+7] warm and refresh current()'s reading.
    Taking the reading here is what fills current()'s stale fallback; its own failure isn't ours."""
    lat, lon = _origin()
    start = date.fromisoformat(db.day_key())
    end = start + timedelta(days=REFRESH_FUTURE_DAYS)
    with db.session() as con:
        n = _sync(con, lat, lon, start, end)
    cur = current()
    return {"rows": n, "window": f"{start}..{end}",
            "current": None if cur is None
                       else "stale" if cur.get("stale") else "live"}


def geocode(name: str) -> tuple[float, float] | None:
    """name -> (lat, lon), cached in geo_cache; None on no match or API failure."""
    with db.session() as con:
        row = con.execute("SELECT lat, lon FROM geo_cache WHERE name=?",
                          (name,)).fetchone()
        if row:
            return row["lat"], row["lon"]
        try:
            blob = _api(f"{GEOCODE_BASE}?"
                       f"{urllib.parse.urlencode({'name': name, 'count': 1})}")
        except Exception:
            return None
        results = blob.get("results")
        if not results:
            return None
        lat, lon = results[0]["latitude"], results[0]["longitude"]
        with con:
            con.execute(
                "INSERT OR REPLACE INTO geo_cache (name, lat, lon) VALUES (?,?,?)",
                (name, lat, lon))
        return lat, lon


def forecast_at(lat: float, lon: float, day: str) -> dict | None:
    """Uncached single-day forecast for any (lat, lon); weather_days is the single-origin series."""
    d = date.fromisoformat(day)
    try:
        days = _fetch(lat, lon, d, d)
    except Exception:
        return None
    vals = days.get(day)
    if not vals:
        return None
    return {**vals, "weather_text": weather_text(vals.get("weather_code"))}


CURRENT_FIELDS = "temperature_2m,weather_code,is_day"


# Last good current() payload, in memory on purpose (a restart should ask, not show yesterday's weather).
# _CURRENT_STALE_S: past it the chip goes blank rather than show an old temperature as current.
_current_cache: dict | None = None
_current_origin: tuple[float, float] | None = None   # where _current_cache was read
_CURRENT_STALE_S = 3600
_CURRENT_RETRY_WAIT_S = 0.8
# Page loads reuse a reading this young: the call costs ~0.8s and upstream moves in 15-minute steps.
CURRENT_FRESH_S = 600


def current(fresh_s: float = 0) -> dict | None:
    """Right-now conditions plus today's high/low. Retried once, then the last good reading
    (`stale: True`, original `as_of`; callers must say so); None only if the API is down and nothing recent is cached.

    `fresh_s` > 0 returns a same-origin cached reading younger than that, still `stale: False`.
    Default 0 always asks (refresh() depends on that).

    Everything is already °F, live and from weather_days: do NOT re-convert (it once read 150°+)."""
    global _current_cache, _current_origin

    lat, lon = _origin()
    if (fresh_s > 0 and _current_cache is not None
            and _current_origin == (lat, lon)
            and _age_s(_current_cache) < fresh_s):
        return dict(_current_cache)
    params ={"latitude": lat, "longitude": lon, "current": CURRENT_FIELDS,
              "timezone": "auto", "temperature_unit": "fahrenheit"}
    url = f"{FORECAST_BASE}?{urllib.parse.urlencode(params)}"

    # Two attempts for transient upstream 502s; the wait stays under 1s (page-load path).
    cur = None
    for attempt in range(2):
        try:
            cur = _api(url).get("current")
            if cur:
                break
        except Exception:  # any failure means "try the cache"
            pass
        if attempt == 0:
            time.sleep(_CURRENT_RETRY_WAIT_S)

    if not cur:
        return _stale_current()

    today = day_summary()
    out = {
        "temp": cur.get("temperature_2m"),
        "weather_code": cur.get("weather_code"),
        "weather_text": weather_text(cur.get("weather_code")),
        "is_day": bool(cur.get("is_day")),
        "time": cur.get("time"),
        "high": today.get("tmax") if today else None,
        "low": today.get("tmin") if today else None,
        "stale": False,
        "as_of": datetime.now(UTC).isoformat(),
    }
    _current_cache, _current_origin = out, (lat, lon)
    return out


def _age_s(reading: dict) -> float:
    """Seconds since `reading` was taken; infinite when it can't say."""
    try:
        taken = datetime.fromisoformat(reading["as_of"])
    except (KeyError, TypeError, ValueError):
        return float("inf")
    return (datetime.now(UTC) - taken).total_seconds()


def _stale_current() -> dict | None:
    """The last good reading if recent enough, as a copy (mutating it would relabel the original)."""
    if _current_cache is None or _age_s(_current_cache) > _CURRENT_STALE_S:
        return None
    return {**_current_cache, "stale": True}


def brief_line(pt: dict) -> str:
    return f"{pt['day']} {pt['value']}"


def _fetched_days(con, start: str, end: str) -> set[str]:
    """Days in [start, end] with any row. Keyed on the day, not (day, metric): null metrics are
    skipped, and a per-metric check would make every read of such a day a live call.
    Adding a METRIC won't backfill cached days; wipe weather_days."""
    return {r["day"] for r in con.execute(
        "SELECT DISTINCT day FROM weather_days WHERE day BETWEEN ? AND ?",
        (start, end))}


def get(rng: Range, metric: str | None = None) -> dict:
    """Read the slice from cache, first syncing never-fetched days in one window."""
    metric = metric or DEFAULT_METRIC
    lat, lon = _origin()
    with db.session() as con:
        rows = con.execute(
            "SELECT day, value, refreshed_at FROM weather_days WHERE metric=?"
            " AND day BETWEEN ? AND ? ORDER BY day",
            (metric, rng.start_key, rng.end_key)).fetchall()
        # Only days inside the horizon count as missing, else long ranges call live forever.
        lo, hi = _horizon()
        missing = sorted({d for d in rng.days() if lo <= d <= hi}
                         - _fetched_days(con, rng.start_key, rng.end_key))
        if missing:
            with offline.best_effort("weather backfill"):
                _sync(con, lat, lon, date.fromisoformat(missing[0]),
                      date.fromisoformat(missing[-1]))
                rows = con.execute(
                    "SELECT day, value, refreshed_at FROM weather_days WHERE metric=?"
                    " AND day BETWEEN ? AND ? ORDER BY day",
                    (metric, rng.start_key, rng.end_key)).fetchall()
    return {
        "metric": metric,
        "data": [{"day": r["day"], "value": r["value"]} for r in rows],
        "refreshed_at": max((r["refreshed_at"] for r in rows), default=None),
    }


def day_summary(day: str | None = None) -> dict | None:
    """All metrics for one civil day (default today), for the brief's weather_today."""
    day = day or db.day_key()
    with db.session() as con:
        rows = con.execute(
            "SELECT metric, value FROM weather_days WHERE day=?", (day,)).fetchall()
        if not rows:
            lat, lon = _origin()
            with offline.best_effort("weather backfill"):
                _sync(con, lat, lon, date.fromisoformat(day), date.fromisoformat(day))
                rows = con.execute(
                    "SELECT metric, value FROM weather_days WHERE day=?", (day,)).fetchall()
    if not rows:
        return None
    out = {"day": day, **{r["metric"]: r["value"] for r in rows}}
    out["weather_text"] = weather_text(out.get("weather_code"))
    return out
