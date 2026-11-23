"""Reddit provider — Arctic Shift, the one anonymous route left.

Reddit closed the anonymous JSON door. Verified from this machine:
`www.reddit.com/*.json` and `api.reddit.com` answer 403 with *any* User-Agent
(default, custom, full browser), and `old.reddit.com/*.json` 302s to a login.
It is not a header problem and there is nothing to tune. Its `.rss` feeds
were a today-only fallback here until they closed too: every feed
(search, subreddit, /new, thread) answers 404, with any User-Agent. So the
fallback was removed rather than
left failing quietly into every sync's error list. What is left:

  * **Arctic Shift** (arctic-shift.photon-reddit.com) — Pushshift's successor,
    no key, full post objects, filterable by subreddit/query/author and an
    epoch window. This is the sync engine. Its one weakness is *fresh* scores:
    it records a post at creation and revisits it later, so anything younger
    than REDDIT_SCORE_MATURE_DAYS is re-synced on later refreshes and reported
    `provisional`.

Arctic Shift is truth; `reddit_posts` + `reddit_sync` are a derived cache, safe
to drop and re-pull — drop both together (see the v40 migration).

Days are day_keys (4am rollover), so a day's window is [04:00, 04:00) local,
converted to epoch seconds for the API. refresh() keeps the maturation window
warm; get() backfills any never-synced day in the range it was asked for.

search() is the odd one out: a live, uncached query into any one subreddit for
the research case ("what has r/onebag said about rain covers"), exposed as
`python -m app.providers reddit --search "rain cover" --sub onebag` and as the
console's `reddit_search`. It never writes. The subreddit is not optional —
Arctic Shift 400s a query that names neither a subreddit nor an author, so
searching Reddit at large is not on offer here.
"""

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta

from .. import db
from ..config import (DAY_ROLLOVER_HOUR, REDDIT_MAX_PAGES, REDDIT_REFRESH_MIN,
                      REDDIT_SCORE_MATURE_DAYS, REDDIT_SELFTEXT_CHARS,
                      REDDIT_SUBREDDITS, REDDIT_TOP_PER_DAY)
from . import metric_cache, offline
from .ranges import Range, days_between

NAME = "reddit"
METRICS = ("posts", "top")
DEFAULT_METRIC = "posts"
ALLOW_FUTURE = False

ARCTIC_BASE = "https://arctic-shift.photon-reddit.com/api"
_HEADERS = {"User-Agent": "vault-dashboard/1.0 (+localhost, personal use)"}

# Arctic Shift's own ceiling: limit=100 is fine, 1000 is a 400.
PAGE_LIMIT = 100

# One retry on the archive's busy answers. Two attempts, not five: a sync walks
# up to REDDIT_MAX_PAGES per subreddit, and a long backoff inside a page loop
# turns one slow moment into a poller that never returns.
_API_ATTEMPTS = 2
_API_RETRY_WAIT_S = 5
_API_RETRY_CODES = (422, 429, 500, 502, 503, 504)


class ArchiveBusy(RuntimeError):
    """The archive said "try later" and still said it after the retry.

    Separate from a plain RuntimeError so a caller can tell "come back in a
    minute" from "this query is wrong" — a 400 is never going to succeed and a
    422 usually is. The console tool turns it into advice; the poller doesn't
    care and lets either one through to the heartbeat.
    """


# ---------- http ----------

def _api(path: str, params: dict) -> list[dict]:
    """GET an Arctic Shift endpoint -> its `data` list.

    A 429 or a 5xx arrives as HTTPError and is deliberately *not* treated as
    "network down" by offline.best_effort: the archive answering "slow down" is
    a real answer, and swallowing it would leave the cache quietly empty.

    It is retried once, though, because the archive's busy answer is a *422*
    carrying "Timeout. Maybe slow down a bit" — measured, and it is
    load, not the query: `cover` in r/onebag failed twice a few seconds apart
    and succeeded on the same terms a minute later, while `rain` never failed.
    A 400 is the query being wrong and is not retried.
    """
    url = f"{ARCTIC_BASE}/{path}?{urllib.parse.urlencode(params)}"
    for attempt in range(_API_ATTEMPTS):
        req = urllib.request.Request(url, headers=_HEADERS)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                body = json.loads(resp.read())
            break
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")[:200]
            if e.code in _API_RETRY_CODES:
                if attempt < _API_ATTEMPTS - 1:
                    time.sleep(_API_RETRY_WAIT_S)
                    continue
                raise ArchiveBusy(
                    f"arctic-shift {e.code} on {path}: {detail}") from None
            raise RuntimeError(
                f"arctic-shift {e.code} on {path}: {detail}") from None
    data = body.get("data")
    return data if isinstance(data, list) else []


# ---------- day <-> epoch ----------

def _day_bounds(start_day: str, end_day: str) -> tuple[int, int]:
    """Epoch seconds spanning [start_day .. end_day] as day_keys — i.e. from
    04:00 local on the first to 04:00 local on the day after the last."""
    start = (datetime.fromisoformat(start_day)
             + timedelta(hours=DAY_ROLLOVER_HOUR))
    end = (datetime.fromisoformat(end_day)
           + timedelta(days=1, hours=DAY_ROLLOVER_HOUR))
    return int(start.timestamp()), int(end.timestamp())


def _day_of(created_utc: int) -> str:
    """The day_key a post belongs to — db.day_key does the 4am fold, so a 1am
    post lands on the night before exactly like a 1am text does."""
    return db.day_key(datetime.fromtimestamp(created_utc))


# ---------- sync ----------

def _fetch_window(subreddit: str, after: int, before: int) -> list[dict]:
    """Every post in [after, before) for one subreddit, paginated.

    Paginates on created_utc rather than an offset (Arctic Shift has no cursor)
    and stops on a page that adds no id it hasn't already seen — a page whose
    posts all share one timestamp would otherwise re-request itself forever,
    since `after` can't be advanced past them without skipping them.
    """
    seen: dict[str, dict] = {}
    cursor = after
    for _ in range(REDDIT_MAX_PAGES):
        page = _api("posts/search", {
            "subreddit": subreddit, "after": cursor, "before": before,
            "limit": PAGE_LIMIT, "sort": "asc",
        })
        fresh = [p for p in page if p.get("id") and p["id"] not in seen]
        for p in fresh:
            seen[p["id"]] = p
        if len(page) < PAGE_LIMIT or not fresh:
            break
        cursor = max(int(p.get("created_utc") or 0) for p in page)
    return list(seen.values())


def _row(post: dict) -> tuple | None:
    """One archive post -> the reddit_posts tuple, or None if it's unusable."""
    created = int(post.get("created_utc") or 0)
    pid = post.get("id")
    if not pid or not created:
        return None
    permalink = post.get("permalink") or f"/comments/{pid}/"
    # A missing count stays NULL — the archive omits them on removed posts,
    # and 0 there would be a real-looking zero.
    score = post.get("score")
    comments = post.get("num_comments")
    return (
        _day_of(created), pid, post.get("subreddit") or "",
        post.get("title") or "", post.get("author") or "",
        post.get("selftext") or "", post.get("url"), permalink,
        int(score) if score is not None else None,
        int(comments) if comments is not None else None,
        created,
    )


def _write(con, rows: list[tuple], now: str) -> int:
    """Upsert post rows. The counts are overwritten on every pull — that is
    how a provisional score matures."""
    sql = (
        "INSERT INTO reddit_posts"
        " (day, id, subreddit, title, author, selftext, url, permalink,"
        "  score, num_comments, created_utc, refreshed_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)"
        " ON CONFLICT(day, id) DO UPDATE SET"
        " subreddit=excluded.subreddit, title=excluded.title,"
        " author=excluded.author, selftext=excluded.selftext,"
        " url=excluded.url, permalink=excluded.permalink,"
        " score=excluded.score, num_comments=excluded.num_comments,"
        " refreshed_at=excluded.refreshed_at")
    with con:
        con.executemany(sql, [r + (now,) for r in rows])
    return len(rows)


def _sync(con, start_day: str, end_day: str) -> dict:
    """Pull [start_day .. end_day] for every tracked subreddit into the cache.

    Marks every day in the span synced, including ones that produced no posts —
    a quiet Tuesday is an answer, and without the marker every later read would
    treat it as a gap and re-fetch it. But only if the archive actually
    answered: a 429 that got marked synced anyway would be cached as "that day
    has no posts" permanently, since nothing ever revisits a marked day. An
    empty *reply* is data; an error is not.
    """
    now = datetime.now().isoformat(timespec="seconds")
    after, before = _day_bounds(start_day, end_day)
    today = db.day_key()
    span = days_between(start_day, end_day)

    rows: list[tuple] = []
    errors: dict[str, str] = {}
    for sub in REDDIT_SUBREDDITS:
        try:
            posts = _fetch_window(sub, after, before)
        except Exception as e:  # one bad sub shouldn't kill the sync
            errors[sub] = str(e)
            continue
        rows += [r for r in (_row(p) for p in posts) if r]

    written = _write(con, rows, now) if rows else 0

    # No fallback for a today the archive hasn't ingested yet: Reddit's .rss
    # was one until its feeds started answering 404.
    if not errors:
        with con:
            con.executemany(
                "INSERT INTO reddit_sync (day, refreshed_at) VALUES (?,?)"
                " ON CONFLICT(day) DO UPDATE SET"
                " refreshed_at=excluded.refreshed_at",
                # Not days still ahead: a this-week read would otherwise mark
                # the rest of the week empty, and marked days aren't revisited.
                [(d, now) for d in span if d <= today])

    # Nothing got through at all — say so instead of returning an empty sync
    # that reads like a quiet week. This is what puts reddit on /api/status
    # when the archive is down: the poller records whatever refresh() raises,
    # and a provider that swallows its own outage always looks healthy.
    # A partial failure stays soft: the subs that answered are real data.
    if errors and len(errors) == len(REDDIT_SUBREDDITS):
        raise RuntimeError("; ".join(f"r/{s}: {e}" for s, e in errors.items()))

    return {"rows": written, "posts": written,
            "days": len(span) if not errors else 0, "refreshed_at": now,
            **({"errors": errors} if errors else {})}


def _mature_window() -> tuple[str, str]:
    """The trailing days whose scores are still moving, oldest first."""
    end = datetime.fromisoformat(db.day_key()).date()
    start = end - timedelta(days=max(REDDIT_SCORE_MATURE_DAYS - 1, 0))
    return start.isoformat(), end.isoformat()


def refresh() -> dict:
    """Re-sync the score-maturation window, at most once per REDDIT_REFRESH_MIN.

    The poller comes round every five minutes; a free third-party archive does
    not need three days of history re-requested that often for numbers that
    move hourly. The interval is measured from the *stalest* day in the window,
    so a day that has never been synced always pulls immediately.
    """
    start, end = _mature_window()
    with db.session() as con:
        row = con.execute(
            "SELECT MIN(refreshed_at) AS oldest, COUNT(*) AS n FROM reddit_sync"
            " WHERE day BETWEEN ? AND ?", (start, end)).fetchone()
        want = len(days_between(start, end))
        if row["n"] == want and row["oldest"]:
            age = datetime.now() - datetime.fromisoformat(row["oldest"])
            if age < timedelta(minutes=REDDIT_REFRESH_MIN):
                return {"skipped": f"synced {int(age.total_seconds() // 60)}m ago",
                        "start": start, "end": end}
        return _sync(con, start, end)


# ---------- read ----------

def _item(r, today: str) -> dict:
    """A cache row as the payload shape. `provisional` says the score is still
    settling — either the archive hasn't revisited the post yet, or it has no
    score at all (NULL: removed posts, and rows the old .rss fallback wrote)."""
    age = (datetime.fromisoformat(today).date()
           - datetime.fromisoformat(r["day"]).date()).days
    selftext = r["selftext"] or ""
    return {
        "day": r["day"],
        "subreddit": r["subreddit"],
        "title": r["title"],
        "author": r["author"],
        "score": r["score"],
        "num_comments": r["num_comments"],
        "url": f"https://www.reddit.com{r['permalink']}",
        "link": r["url"],
        "created": datetime.fromtimestamp(r["created_utc"]).isoformat(timespec="minutes"),
        "selftext": selftext[:REDDIT_SELFTEXT_CHARS],
        "truncated": len(selftext) > REDDIT_SELFTEXT_CHARS,
        "provisional": r["score"] is None or age < REDDIT_SCORE_MATURE_DAYS,
    }


_SELECT = ("SELECT day, id, subreddit, title, author, selftext, url, permalink,"
           " score, num_comments, created_utc, refreshed_at FROM reddit_posts"
           " WHERE day BETWEEN ? AND ?"
           " ORDER BY day, score DESC NULLS LAST, num_comments DESC NULLS LAST")


def get(rng: Range, metric: str | None = None) -> dict:
    """Read the slice from cache, syncing any day in it that has never been
    fetched (one Arctic Shift window spanning them, per subreddit).

    Only never-synced days are backfilled here. Days whose scores are merely
    still maturing belong to refresh() — a read has no business re-fetching
    three days of history because someone opened a widget.
    """
    metric = metric or DEFAULT_METRIC
    today = db.day_key()
    params = (rng.start_key, rng.end_key)
    with db.session() as con:
        rows = con.execute(_SELECT, params).fetchall()
        missing = metric_cache.missing_days(con, "reddit_sync", rng, upto=today)
        if missing:
            with offline.best_effort("reddit backfill"):
                _sync(con, missing[0], missing[-1])
                rows = con.execute(_SELECT, params).fetchall()

    items = [_item(r, today) for r in rows]
    if metric == "top":
        per_day: dict[str, int] = {}
        data = []
        for it in items:                       # _SELECT already orders by score
            n = per_day.get(it["day"], 0) + 1
            per_day[it["day"]] = n
            if n <= REDDIT_TOP_PER_DAY:
                data.append(it)
    else:
        data = items

    return {
        "metric": metric,
        "subreddits": list(REDDIT_SUBREDDITS),
        "count": len(data),
        "data": data,
        "refreshed_at": max((r["refreshed_at"] for r in rows), default=None),
    }


def search(query: str, subreddit: str, days: int = 365,
           limit: int = 25) -> dict:
    """Live search of one subreddit — uncached, never written to app.db.

    This is the research path, not a dashboard read: it answers "what has been
    said about X" in subreddits nobody tracks. Scores come back as the archive
    has them, so recent hits read low for the same reason get()'s do.

    The subreddit is required, and that is the API's rule rather than a choice
    here: `posts/search` with a bare `query` answers 400 ("'query' query
    parameter requires one of: author, subreddit"), so a search across all of
    Reddit is not something this route can do at all. It shipped optional and
    every call that omitted it failed.
    """
    subreddit = (subreddit or "").strip().removeprefix("r/")
    if not subreddit:
        raise ValueError("reddit search needs a subreddit: the archive refuses "
                         "a query that isn't scoped to one")
    after, _ = _day_bounds(
        (datetime.fromisoformat(db.day_key()).date()
         - timedelta(days=max(days, 1))).isoformat(), db.day_key())
    limit = min(max(limit, 1), PAGE_LIMIT)
    params = {"query": query, "after": after, "limit": limit,
              "sort": "desc", "subreddit": subreddit}
    today = db.day_key()
    scan = None
    try:
        posts = _api("posts/search", params)
    except ArchiveBusy:
        posts, scan = _scan(subreddit, query, after, limit)
    rows = [r for r in (_row(p) for p in posts) if r]
    cols = ("day", "id", "subreddit", "title", "author", "selftext", "url",
            "permalink", "score", "num_comments", "created_utc")
    data = [_item(dict(zip(cols, r)), today) for r in rows]
    data.sort(key=lambda it: it["created"], reverse=True)
    return {
        "source": NAME, "search": query,
        "subreddit": subreddit, "days": days,
        "count": len(data), "data": data,
        **({"scan": scan} if scan else {}),
    }


# Pages of newest posts _scan reads when the archive's search refuses: 300
# posts, three requests of about 2s each.
SCAN_PAGES = 3


def _scan(subreddit: str, query: str, after: int,
          limit: int) -> tuple[list[dict], dict]:
    """search()'s fallback: the newest posts, with the phrase matched here.

    The archive's full-text search goes down on its own. One evening 30 of
    the console's 54 searches came back 422 after the retry; the next morning
    every `query=` call did, in under a second, while a newest-first listing of the same subreddits
    answered in 2s. That listing is what this reads, so it covers the newest
    SCAN_PAGES pages rather than the window asked for: weeks of a small
    subreddit, a day or two of a big one. `scan` says how far back it got, so
    the caller can't pass it off as a search of the window. No `after` on
    the request: the listing itself 422'd with a two-year one.

    A phrase, not every word: "code mode" matched 55 of r/ClaudeCode's newest
    300 posts word by word, nearly all of them for "Claude Code".

    The first page failing raises, as the search did. A later one ends the
    scan with what it has. `complete` is the scan reaching the start of the
    window (or the subreddit's first post), when it is as good as a search.
    """
    phrase = " ".join(query.replace('"', " ").lower().split())
    hits: list[dict] = []
    scanned, oldest, before, complete = 0, None, None, False
    for n in range(SCAN_PAGES):
        try:
            page = _api("posts/search", {"subreddit": subreddit,
                                         "limit": PAGE_LIMIT, "sort": "desc",
                                         **({"before": before} if before else {})})
        except Exception:
            if n == 0:
                raise
            break
        stamps = [int(p.get("created_utc") or 0) for p in page]
        fresh = [p for p, at in zip(page, stamps) if at >= after]
        scanned += len(fresh)
        hits += [p for p in fresh if phrase in " ".join(
            f"{p.get('title') or ''} {p.get('selftext') or ''}".lower().split())]
        if fresh:
            oldest = min(at for at in stamps if at >= after)
        if len(page) < PAGE_LIMIT or len(fresh) < len(page):
            complete = True
            break
        before = min(stamps)
    return hits[:limit], {"posts": scanned, "complete": complete,
                          "back_to": _day_of(oldest) if oldest else None}


def brief_line(it: dict) -> str:
    score = "?" if it["score"] is None else f"{it['score']}"
    star = "~" if it["provisional"] else ""
    return (f"{it['day']} r/{it['subreddit']} [{star}{score}pt"
            f" {it['num_comments'] if it['num_comments'] is not None else '?'}c]"
            f" {it['title']}")
