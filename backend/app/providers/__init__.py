"""Context providers — one module per external source, one shared
interface, exposed three ways over the same code:

  1. REST  GET /api/{source}?range=this-week&metric=bpm   (providers/api.py)
  2. CLI   python -m app.providers {source} --range this-week --metric bpm
  3. Cache rows in app.db, refreshed by the background poller (providers/poller.py)

The external service is truth; app.db rows are a derived cache, safe to drop
and re-pull. Never write back except through explicit actions.

A provider module contributes: NAME, METRICS (tuple or None), DEFAULT_METRIC,
ALLOW_FUTURE, get(rng: Range, metric) -> dict payload, and optionally
refresh() -> dict for the poller. Register it in REGISTRY below.
"""

from . import (athletics, attachments, calendar, coursework, email, features,
               finance, health, lastfm, location, messages, news, offline,
               ranges, reddit, spotify, usc_dining, weather)

REGISTRY = {
    calendar.NAME: calendar,
    health.NAME: health,
    email.NAME: email,
    finance.NAME: finance,
    weather.NAME: weather,
    news.NAME: news,
    reddit.NAME: reddit,
    spotify.NAME: spotify,
    # after spotify: enriches the artists in `plays`, so a just-synced artist
    # can be tagged in the same cycle it first appears.
    lastfm.NAME: lastfm,
    messages.NAME: messages,
    location.NAME: location,
    athletics.NAME: athletics,
    coursework.NAME: coursework,
    usc_dining.NAME: usc_dining,
    # after messages/email: reads the attachment rows those two record.
    attachments.NAME: attachments,
    # features last: a denormalization of every source above, so it recomputes
    # from whatever the others just refreshed (worst case one cycle stale).
    features.NAME: features,
}


def get(source: str, range_token: str = "today", metric: str | None = None,
        query: str | None = None) -> dict:
    """The one entrypoint REST/CLI wrap. Raises ValueError on bad input.

    `query` is a case-insensitive substring match against every string field
    of each data item (e.g. summary/location for calendar, from/subject for
    email) — filters within the range, after the cache read.
    """
    mod = REGISTRY.get(source)
    if mod is None:
        raise ValueError(f"unknown source {source!r}; expected one of: {', '.join(REGISTRY)}")
    rng = ranges.parse_range(range_token)
    if rng.future and not mod.ALLOW_FUTURE:
        raise ValueError(f"{source!r} has no future data; {range_token!r} not allowed")
    if metric is not None and mod.METRICS is not None and metric not in mod.METRICS:
        raise ValueError(f"unknown metric {metric!r} for {source!r};"
                         f" expected one of: {', '.join(mod.METRICS)}")
    with offline.collect() as degraded:
        payload = mod.get(rng, metric)
    envelope = {}
    # A read that couldn't reach the source still answers, from cache. Say so
    # in the payload rather than raising: the numbers are real, just not new.
    if degraded:
        envelope["stale"] = True
        envelope["offline"] = degraded
    if query:
        q = query.lower()
        data = [it for it in payload.get("data", [])
                if any(isinstance(v, str) and q in v.lower() for v in it.values())]
        payload = {**payload, "data": data}
        if "count" in payload:
            payload["count"] = len(data)
        if "unread" in payload:
            payload["unread"] = sum(1 for it in data if it.get("unread"))
        envelope["query"] = query
    return {
        "source": source,
        "range": rng.token,
        "start": rng.start_key,
        "end": rng.end_key,
        **envelope,
        **payload,
    }


def describe() -> dict:
    """Registry metadata for /api/providers and the CLI --list."""
    return {
        name: {
            "metrics": list(mod.METRICS) if mod.METRICS else None,
            "default_metric": getattr(mod, "DEFAULT_METRIC", None),
            "ranges": list(ranges.ALL_RANGES if mod.ALLOW_FUTURE else ranges.PAST_RANGES),
            "custom": ranges.CUSTOM_GRAMMAR,
            "refreshable": hasattr(mod, "refresh"),
        }
        for name, mod in REGISTRY.items()
    }
