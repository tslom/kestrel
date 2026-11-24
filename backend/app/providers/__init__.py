"""Context providers: one module per external source, exposed three ways over the same code:
REST (providers/api.py), CLI (`python -m app.providers {source} --range this-week`), and
cache rows in app.db refreshed by providers/poller.py. The service is truth; rows are a
droppable cache. A provider contributes NAME, METRICS, DEFAULT_METRIC, ALLOW_FUTURE,
get(rng, metric) and optionally refresh(); register it in REGISTRY.
Design: DESIGN.md → Context providers (`app/providers/`).
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
    # after spotify: tags the artists in `plays` in the cycle they first appear.
    lastfm.NAME: lastfm,
    messages.NAME: messages,
    location.NAME: location,
    athletics.NAME: athletics,
    coursework.NAME: coursework,
    usc_dining.NAME: usc_dining,
    # after messages/email: reads their attachment rows.
    attachments.NAME: attachments,
    # features last: it recomputes from what the others just refreshed.
    features.NAME: features,
}


def get(source: str, range_token: str = "today", metric: str | None = None,
        query: str | None = None) -> dict:
    """The one entrypoint REST/CLI wrap; raises ValueError on bad input.
    `query` is a case-insensitive substring match on every string field of each item, after the cache read."""
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
    # An offline read answers from cache and says so rather than raising.
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
