"""REST face of the providers: GET /api/{source}?range=&metric=.

Routes are generated from the registry — adding a provider module and
registering it is all it takes to get its endpoint. Registered names must not
collide with the hand-written routes in widgets.py/main.py (the health
provider owns /api/health; the liveness probe moved to /api/healthz).
"""

import json

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from . import (REGISTRY, describe, get, health, location, offline, poller,
               ranges, weather)
from .. import place_suggest

router = APIRouter(prefix="/api")


def _serve(fn, *args):
    """Run a hand-written read the way get() runs a registry one: inside
    offline.collect(), so a live fetch that fell back to cache stamps the
    payload `stale`/`offline` instead of vanishing — best_effort() with no
    collect() around it has nowhere to write its note, which is how
    /health/night served cache-only nights as if fresh — and with ValueError
    (bad day, bad range, not authorized) as a 400. Pass a lambda when parsing
    an argument can itself raise, so that lands inside too."""
    try:
        with offline.collect() as degraded:
            payload = fn(*args)
    except ValueError as e:
        raise HTTPException(400, str(e))
    if degraded and isinstance(payload, dict):
        # Envelope first, payload over it — the same precedence get() uses.
        payload = {"stale": True, "offline": degraded, **payload}
    return payload


@router.get("/providers")
def list_providers():
    return describe()


@router.post("/sync")
async def sync_now():
    """Force one full refresh cycle right now — the same work the poller does
    on its PROVIDER_REFRESH_SECONDS timer, so a just-changed source (a new
    calendar event, a fresh transaction) lands immediately instead of waiting
    for the next cycle. Returns per-provider results/errors, plus the alarm
    reconcile that follows them under an `alarms` key."""
    return await poller.sync_all()


@router.get("/sync/stream")
async def sync_stream():
    """Same cycle as POST /api/sync, streamed as SSE so the UI can name the
    source it's waiting on. GET because EventSource only speaks GET; it's a
    localhost-only app and the work is idempotent (a refresh)."""
    async def events():
        async for evt in poller.refresh_stream():
            yield f"data: {json.dumps(evt)}\n\n"

    return StreamingResponse(events(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-store",
                                      "X-Accel-Buffering": "no"})


@router.get("/weather/current")
def weather_current():
    """Right-now conditions at the origin — the glance strip's small chip;
    not a range/metric slice, so hand-written rather than registry-generated.

    The 502 is now the genuinely-nothing case only. weather.current() retries
    and then falls back to its last good reading (flagged `stale`), so an
    upstream blip costs the chip a staleness marker instead of a failed
    request, which is what it used to cost."""
    cur = weather.current(fresh_s=weather.CURRENT_FRESH_S)
    if cur is None:
        raise HTTPException(502, "weather API unavailable")
    return cur




@router.get("/health/summary")
def health_summary():
    """The desk card's slice — today's four headline metrics, each with the
    28-day series the card reads it against. Hand-written for the same reason
    the two below are: get() is one metric per call, and four metrics plus a
    baseline apiece would be eight."""
    return _serve(health.summary)


@router.get("/health/night/{day}")
def health_night(day: str):
    """One night's stage segments — the hypnogram behind the sleep bar. Keyed
    by wake-up date, which is how the sleep metrics are keyed too."""
    return _serve(health.night, day)


@router.get("/health/day/{day}")
def health_day(day: str):
    """One day's intraday curves — steps per hour, plus cached hourly HR."""
    return _serve(health.day_detail, day)


@router.get("/location/summary")
def location_summary():
    """The desk card's slice — newest day with real coverage, the day's shape,
    a 14-day distance trend. Hand-written for the same reason /screen/summary
    is: not a range/metric slice, and six metrics would be six calls."""
    return _serve(location.summary)


@router.get("/location/trace")
def location_trace(range: str = "this-week"):
    """The map's slice: every fix in the range grouped by day, plus the places
    they dwell into. Hand-written because it is neither a metric series nor a
    summary — one range, two shapes."""
    return _serve(lambda: location.trace(ranges.parse_range(range)))


@router.get("/location/place/suggest")
def location_place_suggest(lat: float, lon: float, range: str = "this-week"):
    """Ranked name candidates for the unnamed cluster at (lat, lon).

    A read, so it lives beside the other location reads — but the slow one:
    it may call out to a geocoder, which is why it is asked for per place on
    demand and never folded into /location/trace."""
    try:
        return _serve(lambda: place_suggest.suggest(
            lat, lon, ranges.parse_range(range)))
    except LookupError as e:
        raise HTTPException(404, str(e))


def _endpoint_for(source: str):
    def endpoint(range: str = "today", metric: str | None = None,
                 query: str | None = None):
        # get() stamps its own envelope; _serve adds only the 400 here.
        return _serve(get, source, range, metric, query)
    endpoint.__name__ = f"provider_{source}"
    return endpoint


for _name in REGISTRY:
    router.add_api_route(f"/{_name}", _endpoint_for(_name), methods=["GET"])
