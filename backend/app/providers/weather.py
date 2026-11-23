"""Weather provider — Open-Meteo, no key/auth required. Open-Meteo
is truth; `weather_days` + `geo_cache` in app.db are derived cache (safe to
drop and re-pull). One row per (day, metric); day is Open-Meteo's own civil
date for the queried lat/lon (timezone=auto) — same camp as calendar/health,
not day_key.

Origin: the latest location fix < 6h old, else HOME_LAT/HOME_LON. The
location import is guarded so this module works standalone.

This is a forecast (ALLOW_FUTURE=True) — unlike calendar/health there's no
separate sync marker table: every fetched day carries at least its core
metrics (a null one, e.g. precip_prob for a past day, is simply skipped), so
a day with NO rows in weather_days means "never fetched or past the
forecast/history horizon" (_fetched_days). get() retries the first kind on the next read and
leaves the second alone (_horizon()) — no call can fill it.
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

# refresh() keeps [today .. today+7] warm; colder days (this-month, explicit
# dates) are fetched on demand by get() and backfill the cache.
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
    # Today's home, not a fixed constant — this vault's owner has moved once
    # already, and forecasting the old city is a silent wrong answer.
    from ..places import home_now
    home = home_now()
    return home.lat, home.lon


def _fetch(lat: float, lon: float, start: date, end: date) -> dict[str, dict[str, float]]:
    """Pull daily fields for [start, end] -> {day: {metric: value}}. A single
    call covers both directions via past_days/forecast_days."""
    # past_days/forecast_days are offsets into Open-Meteo's OWN calendar, which
    # is anchored on the real local date — NOT db.day_key(). Between midnight
    # and the 4am rollover day_key still reads yesterday, so using it here asks
    # for a window one day in the future and the requested day never lands in
    # the response. Anchor on the actual civil date; the stored `day` keys still
    # come from the API's own dates, so this isn't defining an app day-key.
    today = date.today()  # noqa: TID251 — Open-Meteo's civil date, not a day key
    past_days = min(MAX_PAST_DAYS, max(0, (today - start).days))
    forecast_days = min(MAX_FORECAST_DAYS, max(1, (end - today).days + 1))
    params = {
        "latitude": lat, "longitude": lon,
        "daily": ",".join(FIELD_METRIC),
        "timezone": "auto",
        # US dashboard: Open-Meteo defaults to °C / km/h — force imperial so
        # tmax/tmin are always °F and wind is mph. (precip_prob is a %,
        # unit-agnostic.) weather_days is derived cache; wipe it after changing
        # units so stale metric rows don't mix scales.
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
    """First and last day _fetch() can return: MAX_PAST_DAYS back through the
    last of MAX_FORECAST_DAYS forecast days (which count today), on the same
    civil-date anchor _fetch() uses."""
    today = date.today()  # noqa: TID251 — the same civil anchor as _fetch
    return ((today - timedelta(days=MAX_PAST_DAYS)).isoformat(),
            (today + timedelta(days=MAX_FORECAST_DAYS - 1)).isoformat())


def _sync(con, lat: float, lon: float, start: date, end: date) -> int:
    days = _fetch(lat, lon, start, end)
    now = datetime.now().isoformat(timespec="seconds")
    with con:
        return metric_cache.upsert_days(con, "weather_days", days, now)


def refresh() -> dict:
    """Poller entrypoint: keep [today .. today+7] warm, and the right-now
    reading with it.

    The second half is what makes current()'s stale fallback real. That cache
    was filled only by current() itself, which runs only on a page load, and a
    served-stale reading may be at most _CURRENT_STALE_S old — so the fallback
    could only ever cover a blip that landed within an hour of a *previous*
    successful desk load, and never the first load of the day, which is
    exactly when the chip was observed blanking (502s mid-desk-burst, even
    with the retry-and-fall-back in place).
    Refreshing here puts the last good reading at most one poller cycle behind
    instead, which is well inside the window.

    Its failure is not this function's failure: an unreachable API already
    costs `_sync` above, and a current() that can't fetch has its own answer
    (serve stale, or None) that nothing here improves on.
    """
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
    """name -> (lat, lon), cached in geo_cache. None on no match/API failure —
    callers degrade (skip), never fail."""
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
    """Ad hoc single-day forecast for an arbitrary (lat, lon) — used for
    per-event location forecasts in the brief. Not cached in weather_days
    (that table is the single-origin series); geocode() already caches the
    name->coords lookup, and this is one cheap API call per event."""
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


# Last successful current() payload, kept in memory so a flaky upstream costs
# a stale reading rather than an empty chip. Open-Meteo has returned a 502 on
# four separate days in one week (the `failures` table's longest-running
# row), each one blanking the glance strip for a page load.
#
# In memory and not app.db on purpose: this is a right-now reading, so its
# value decays in minutes and surviving a backend restart would be a
# liability, not a feature — a fresh process should go ask rather than open
# with yesterday's weather. _CURRENT_STALE_S is how long a cached reading may
# still be shown at all; past it the chip goes back to saying nothing, because
# a two-hour-old temperature presented as current is worse than a blank.
_current_cache: dict | None = None
_current_origin: tuple[float, float] | None = None   # where _current_cache was read
_CURRENT_STALE_S = 3600
_CURRENT_RETRY_WAIT_S = 0.8
# How young a reading the page-load route takes instead of asking. The call
# is ~0.8s of pure upstream latency (measured: every time, no failures)
# and Open-Meteo's `current` block only moves in 15-minute steps, so asking
# again on every page load paid that for an identical answer — and the poller
# cycle's refresh() has usually just taken the reading anyway.
CURRENT_FRESH_S = 600


def current(fresh_s: float = 0) -> dict | None:
    """Right-now conditions at the origin, plus today's high/low — not a
    daily metric. Retried once on failure and then served from the last good
    reading (marked `stale`) if there is a recent one. None only when the API
    is down AND nothing recent is cached — caller degrades, never fails.

    `fresh_s` > 0 hands back the cached reading without asking when it is
    younger than that and was taken at the same origin — still `stale: False`
    with its real `as_of`, because it is as current as upstream's own
    15-minute step. Default 0 always asks (refresh() depends on that).

    A served-stale payload carries `stale: True` and `as_of`, and callers are
    expected to say so rather than pass it off as live: the whole point of the
    fallback is to avoid a blank chip, which is not a licence to show an old
    temperature as if it were current.

    Everything here is already °F: the live reading via temperature_unit,
    and today's high/low straight out of `weather_days`, which _fetch()
    stores in °F too. One unit end to end — do NOT re-convert (that's what
    made the glance strip read 150°+)."""
    global _current_cache, _current_origin

    lat, lon = _origin()
    if (fresh_s > 0 and _current_cache is not None
            and _current_origin == (lat, lon)
            and _age_s(_current_cache) < fresh_s):
        return dict(_current_cache)
    params ={"latitude": lat, "longitude": lon, "current": CURRENT_FIELDS,
              "timezone": "auto", "temperature_unit": "fahrenheit"}
    url = f"{FORECAST_BASE}?{urllib.parse.urlencode(params)}"

    # Two attempts, a beat apart. The observed failure is a transient upstream
    # 502, and the wait stays under a second because this call sits in a page
    # load — a retry that makes the strip feel broken has cured nothing.
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
    """The last good reading, if it's recent enough to be worth showing.

    Returns a *copy* with `stale` flipped, so the cached payload keeps its own
    honest `as_of` and can be served again on the next failure — mutating it
    in place would relabel the original and lose when it was actually taken."""
    if _current_cache is None or _age_s(_current_cache) > _CURRENT_STALE_S:
        return None
    return {**_current_cache, "stale": True}


def brief_line(pt: dict) -> str:
    return f"{pt['day']} {pt['value']}"


def _fetched_days(con, start: str, end: str) -> set[str]:
    """Days in [start, end] that some fetch has already answered for.

    Keyed on the day, not the (day, metric): _fetch() skips a metric
    Open-Meteo returns as null (gap, never 0), so a fetched day can lack
    precip_prob or uv_index — and asking again changes nothing, while a
    per-metric check made every read of such a day a live call. Any row
    means the day was fetched; the core fields (tmax/tmin/code/sunrise/
    sunset) are always present inside the horizon.

    The cost: adding a METRIC won't backfill already-cached days. The table
    is derived — wipe weather_days after adding one, as after a unit change."""
    return {r["day"] for r in con.execute(
        "SELECT DISTINCT day FROM weather_days WHERE day BETWEEN ? AND ?",
        (start, end))}


def get(rng: Range, metric: str | None = None) -> dict:
    """Read the slice from cache; sync any never-fetched days first (one API
    window spanning them, in either time direction)."""
    metric = metric or DEFAULT_METRIC
    lat, lon = _origin()
    with db.session() as con:
        rows = con.execute(
            "SELECT day, value, refreshed_at FROM weather_days WHERE metric=?"
            " AND day BETWEEN ? AND ? ORDER BY day",
            (metric, rng.start_key, rng.end_key)).fetchall()
        # Only days Open-Meteo can actually answer for. A gap past its horizon
        # stays a gap, and counting it as missing made every read of a long
        # range (this-year, next-month) a live call that could never fill it.
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
    """All metrics for one civil day (default today) — convenience for the
    brief, which wants a single weather_today object, not a metric series."""
    day = day or db.day_key()
    with db.session() as con:
        rows = con.execute(
            "SELECT metric, value FROM weather_days WHERE day=?", (day,)).fetchall()
        # Any row means fetched (see _fetched_days): a null metric stays a
        # gap rather than a live call on every brief/current() read.
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
