"""News provider — Kagi Kite, no key/auth required.

Kite (kite.kagi.com/kite.json -> per-category JSON, e.g. world.json) is
truth; `news_items` in app.db is a derived cache (safe to drop and
re-pull). Kite has no per-day historical API — it republishes one edition
per category, refreshed roughly once a day (~12:00 UTC). So unlike the
civil-date providers (weather/health) or the day_key behavioral providers
(screen), there's nothing to backfill: day here is the **day_key of the
fetch**, not an article date. A fetch before Kite's daily refresh just
tags whatever it's currently serving under today's day_key rather than
erroring — content is a day stale, not missing.

Kite content is CC BY-NC (community-curated, clustered from 25+ outlets so
single-source bias washes out) — personal dashboard use only; `sources`
keeps the contributing domains for attribution.
"""

import json
import urllib.error
import urllib.request
from datetime import datetime

from .. import db
from ..config import NEWS_CATEGORIES, NEWS_HEADLINES_PER_CATEGORY
from . import offline
from .ranges import Range, parse_range

NAME = "news"
METRICS = ("headlines", "stories")
DEFAULT_METRIC = "headlines"
ALLOW_FUTURE = False

KITE_INDEX_URL = "https://kite.kagi.com/kite.json"
_HEADERS = {"User-Agent": "vault-dashboard/1.0 (+localhost, personal use)"}


def _api(url: str) -> tuple[dict, str]:
    """GET url -> (parsed JSON, final URL after redirects) — Kite's index
    lives behind a redirect (kite.kagi.com -> news.kagi.com); category
    files are fetched relative to wherever that lands."""
    req = urllib.request.Request(url, headers=_HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read()), resp.geturl()
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"news API {e.code}: {e.read().decode(errors='replace')}") from None


def _sync(con, day: str) -> dict:
    now = datetime.now().isoformat(timespec="seconds")
    index, final_url = _api(KITE_INDEX_URL)
    base = final_url.rsplit("/", 1)[0] + "/"
    file_by_slug = {c["file"].removesuffix(".json"): c["file"]
                    for c in index.get("categories", [])}
    rows = 0
    errors: dict[str, str] = {}
    with con:
        con.execute("DELETE FROM news_items WHERE day=?", (day,))
        for cat in NEWS_CATEGORIES:
            file = file_by_slug.get(cat)
            if file is None:
                errors[cat] = "not in Kite's category index"
                continue
            try:
                blob, _ = _api(base + file)
            except Exception as e:  # one bad category shouldn't kill the sync
                errors[cat] = str(e)
                continue
            for cluster in blob.get("clusters", []):
                sources = sorted({d["name"] for d in cluster.get("domains", []) if d.get("name")})
                con.execute(
                    "INSERT INTO news_items"
                    " (day, category, cluster_i, title, summary, sources, refreshed_at)"
                    " VALUES (?,?,?,?,?,?,?)"
                    " ON CONFLICT(day, category, cluster_i) DO UPDATE SET"
                    " title=excluded.title, summary=excluded.summary,"
                    " sources=excluded.sources, refreshed_at=excluded.refreshed_at",
                    (day, cat, cluster.get("cluster_number", 0),
                     cluster.get("title", ""), cluster.get("short_summary", ""),
                     json.dumps(sources[:8]), now))
                rows += 1
    return {"rows": rows, "day": day, "refreshed_at": now, **({"errors": errors} if errors else {})}


def refresh() -> dict:
    """Once per day_key — Kite itself only republishes ~daily, so re-fetching
    within the same day would just re-download identical content."""
    today = db.day_key()
    with db.session() as con:
        have = con.execute(
            "SELECT 1 FROM news_items WHERE day=? LIMIT 1", (today,)).fetchone()
        if have:
            return {"skipped": "already fetched today", "day": today}
        return _sync(con, today)


def get(rng: Range, metric: str | None = None) -> dict:
    """Slice the cache; if today is in range and not yet cached, fetch it
    on demand (mirrors other providers' backfill-on-read — but only for
    today, since Kite has nothing to backfill for past days)."""
    metric = metric or DEFAULT_METRIC
    today = db.day_key()
    with db.session() as con:
        query = ("SELECT day, category, cluster_i, title, summary, sources, refreshed_at"
                  " FROM news_items WHERE day BETWEEN ? AND ?"
                  " ORDER BY day, category, cluster_i")
        params = (rng.start_key, rng.end_key)
        rows = con.execute(query, params).fetchall()
        if today in rng.days() and not any(r["day"] == today for r in rows):
            with offline.best_effort("news backfill"):
                _sync(con, today)
                rows = con.execute(query, params).fetchall()

    items = [{
        "day": r["day"],
        "category": r["category"],
        "title": r["title"],
        "summary": r["summary"],
        "sources": json.loads(r["sources"]),
    } for r in rows]

    if metric == "headlines":
        seen: dict[tuple[str, str], int] = {}
        data = []
        for it in items:
            key = (it["day"], it["category"])
            seen[key] = seen.get(key, 0) + 1
            if seen[key] <= NEWS_HEADLINES_PER_CATEGORY:
                data.append(it)
    else:
        data = items

    return {
        "metric": metric,
        "data": data,
        "refreshed_at": max((r["refreshed_at"] for r in rows), default=None),
    }


def top_headlines(day: str | None = None, count: int = 5) -> list[dict]:
    """Merged top clusters across NEWS_CATEGORIES for the brief's "5 things
    to know" — round-robins categories so one category can't crowd the rest,
    truncated to `count`. Never raises: brief._gather already wraps every
    source in try/except, but an empty list degrades cleanly either way."""
    day = day or db.day_key()
    payload = get(parse_range(day), "headlines")
    by_cat: dict[str, list[dict]] = {}
    for it in payload["data"]:
        by_cat.setdefault(it["category"], []).append(it)
    out = []
    i = 0
    while len(out) < count and any(by_cat.values()):
        for cat in NEWS_CATEGORIES:
            bucket = by_cat.get(cat) or []
            if i < len(bucket):
                out.append(bucket[i])
                if len(out) >= count:
                    break
        i += 1
        if i > NEWS_HEADLINES_PER_CATEGORY:
            break
    return out


def brief_line(it: dict) -> str:
    return f"[{it['category']}] {it['title']}"
