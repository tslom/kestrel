"""providers/reddit — the Arctic Shift cache and the live search.

Nothing here touches the network: _api (or urlopen under it) is stubbed.
What's worth pinning is the stuff that only bites in production — the 4am
fold deciding which day a post lands on, a quiet day not being mistaken for a
gap, and a missing score staying NULL rather than a real-looking zero.
"""

import io
import json
import urllib.error
from datetime import datetime, timedelta

import pytest

from app import config, db
from app.providers import reddit
from app.providers.ranges import parse_range

DAY = "2026-08-12"


@pytest.fixture
def today(monkeypatch):
    """Pin what 'today' is without breaking the fold itself: db.day_key() with
    no argument answers DAY, db.day_key(dt) still does the real 4am arithmetic
    (conftest's frozen_day flattens both, which would hide _day_of's whole job).
    """
    real = db.day_key
    monkeypatch.setattr(
        db, "day_key", lambda dt=None: DAY if dt is None else real(dt))
    return DAY


@pytest.fixture
def subs(monkeypatch):
    monkeypatch.setattr(reddit, "REDDIT_SUBREDDITS", ["onebag"])
    return ["onebag"]


def _epoch(iso: str) -> int:
    """Local wall-clock string -> epoch, the way a reader thinks about it."""
    return int(datetime.fromisoformat(iso).timestamp())


def _post(pid, when, *, score=10, comments=2, sub="onebag", title=None,
          selftext="", url=None):
    return {"id": pid, "created_utc": _epoch(when), "subreddit": sub,
            "title": title or f"post {pid}", "author": "someone",
            "selftext": selftext, "url": url,
            "permalink": f"/r/{sub}/comments/{pid}/x/",
            "score": score, "num_comments": comments}


def _arctic(monkeypatch, posts, *, calls=None):
    def fake(path, params):
        if calls is not None:
            calls.append((path, params))
        # search() sends no `before` — it's open-ended up to now.
        lo, hi = int(params["after"]), int(params.get("before", 1 << 62))
        return [p for p in posts if lo <= p["created_utc"] < hi]
    monkeypatch.setattr(reddit, "_api", fake)


class _Resp:
    """The bit of a urlopen response _api uses: a context manager that reads."""

    def __init__(self, payload):
        self._body = json.dumps(payload).encode()

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _http_error(code: int, body: str) -> urllib.error.HTTPError:
    return urllib.error.HTTPError("https://arctic-shift.example/api", code,
                                  "err", None, io.BytesIO(body.encode()))


def _stub_http(monkeypatch, script: list) -> dict:
    """urlopen playing a scripted list of payloads and HTTPErrors, in order.

    Stubs one layer lower than _arctic: these tests are about _api's own retry,
    which _arctic replaces wholesale. The returned dict counts the attempts —
    a retry that silently didn't happen is the failure worth catching.
    """
    state = {"n": 0}

    def fake(req, timeout=None):
        item = script[state["n"]]
        state["n"] += 1
        if isinstance(item, Exception):
            raise item
        return _Resp(item)

    monkeypatch.setattr(reddit.urllib.request, "urlopen", fake)
    return state


# ---------- day keys ----------

def test_post_after_midnight_belongs_to_the_night_before():
    """The rule the whole table rests on: 1am Wednesday is Tuesday's day_key."""
    assert reddit._day_of(_epoch("2026-08-12T01:30:00")) == "2026-08-11"
    assert reddit._day_of(_epoch("2026-08-12T04:30:00")) == "2026-08-12"


def test_day_bounds_span_four_to_four():
    lo, hi = reddit._day_bounds("2026-08-11", "2026-08-11")
    assert lo == _epoch("2026-08-11T04:00:00")
    assert hi == _epoch("2026-08-12T04:00:00")
    assert reddit._day_of(lo) == "2026-08-11"
    assert reddit._day_of(hi - 1) == "2026-08-11"


# ---------- sync ----------

def test_sync_buckets_posts_by_day_key(dbfile, today, subs, monkeypatch):
    _arctic(monkeypatch, [
        _post("a", "2026-08-11T12:00:00"),
        _post("b", "2026-08-12T02:00:00"),   # folds back onto the 11th
        _post("c", "2026-08-12T09:00:00"),
    ])
    con = db.connect()
    try:
        info = reddit._sync(con, "2026-08-11", "2026-08-12")
        assert info["posts"] == 3
        days = dict(con.execute(
            "SELECT id, day FROM reddit_posts").fetchall())
    finally:
        con.close()
    assert days == {"a": "2026-08-11", "b": "2026-08-11", "c": "2026-08-12"}


def test_quiet_day_is_marked_synced_and_not_refetched(dbfile, today, subs,
                                                      monkeypatch):
    """A day with no posts is an answer, not a gap. Without the reddit_sync
    marker every later read would re-fetch it forever."""
    calls: list = []
    _arctic(monkeypatch, [], calls=calls)
    con = db.connect()
    try:
        reddit._sync(con, "2026-08-10", "2026-08-10")
        marked = con.execute("SELECT day FROM reddit_sync").fetchall()
    finally:
        con.close()
    assert [r["day"] for r in marked] == ["2026-08-10"]

    before = len(calls)
    out = reddit.get(parse_range("2026-08-10"))
    assert out["data"] == []
    assert len(calls) == before, "a synced-but-empty day was fetched again"


def test_days_still_ahead_are_never_marked(dbfile, today, subs, monkeypatch):
    """A this-week read on a Wednesday used to mark Thu–Sun synced-and-empty,
    and a marked day is never revisited — so those days' posts never came."""
    calls: list = []
    _arctic(monkeypatch, [], calls=calls)
    reddit.get(parse_range("2026-08-11..2026-08-15"))
    with db.session() as con:
        marked = [r["day"] for r in con.execute(
            "SELECT day FROM reddit_sync ORDER BY day")]
    assert marked == ["2026-08-11", "2026-08-12"]
    calls.clear()
    reddit.get(parse_range("2026-08-13..2026-08-15"))
    assert calls == [], "an all-future range asked the archive anyway"


def test_get_backfills_only_never_synced_days(dbfile, today, subs, monkeypatch):
    calls: list = []
    _arctic(monkeypatch, [_post("a", "2026-08-10T12:00:00"),
                          _post("b", "2026-08-11T12:00:00")], calls=calls)
    reddit.get(parse_range("2026-08-10"))
    assert len(calls) == 1
    reddit.get(parse_range("2026-08-10"))
    assert len(calls) == 1, "a cached day was re-fetched on read"
    out = reddit.get(parse_range("2026-08-10..2026-08-11"))
    assert len(calls) == 2
    assert [it["title"] for it in out["data"]] == ["post a", "post b"]


def test_fetch_window_stops_when_a_page_adds_nothing(monkeypatch):
    """Pagination advances on created_utc; a full page sharing one timestamp
    can't advance the cursor, so the loop has to notice it learned nothing."""
    same = [_post(f"p{i}", "2026-08-11T12:00:00") for i in range(reddit.PAGE_LIMIT)]
    calls = []

    def fake(path, params):
        calls.append(params)
        return same
    monkeypatch.setattr(reddit, "_api", fake)

    got = reddit._fetch_window("onebag", 0, 9999999999)
    assert len(got) == reddit.PAGE_LIMIT
    assert len(calls) == 2, "should stop on the first page that adds no new id"


def test_one_bad_subreddit_does_not_kill_the_sync(dbfile, today, monkeypatch):
    monkeypatch.setattr(reddit, "REDDIT_SUBREDDITS", ["good", "bad"])

    def fake(path, params):
        if params["subreddit"] == "bad":
            raise RuntimeError("arctic-shift 429")
        return [_post("a", "2026-08-11T12:00:00", sub="good")]
    monkeypatch.setattr(reddit, "_api", fake)

    con = db.connect()
    try:
        info = reddit._sync(con, "2026-08-11", "2026-08-11")
        marked = con.execute("SELECT COUNT(*) c FROM reddit_sync").fetchone()["c"]
    finally:
        con.close()
    assert info["posts"] == 1
    assert "bad" in info["errors"]
    assert marked == 0, "a day the archive errored on must stay unsynced"


def test_a_failed_day_is_retried_rather_than_cached_as_empty(
        dbfile, today, subs, monkeypatch):
    """The trap this heads off: mark the day synced after a 429 and nothing
    ever looks at it again — the outage becomes a permanent 'no posts'."""
    fail = {"yes": True}

    def flaky(path, params):
        if fail["yes"]:
            raise RuntimeError("arctic-shift 429: slow down")
        return [_post("a", "2026-08-10T12:00:00")]
    monkeypatch.setattr(reddit, "_api", flaky)

    with pytest.raises(RuntimeError, match="429"):
        reddit.get(parse_range("2026-08-10"))   # loud, not a silent empty day
    fail["yes"] = False
    out = reddit.get(parse_range("2026-08-10"))
    assert [it["title"] for it in out["data"]] == ["post a"]


def test_a_partial_outage_still_serves_what_answered(dbfile, today, monkeypatch):
    """One sub down is not an outage — the subs that replied are real data."""
    monkeypatch.setattr(reddit, "REDDIT_SUBREDDITS", ["good", "bad"])

    def half(path, params):
        if params["subreddit"] == "bad":
            raise RuntimeError("arctic-shift 500")
        return [_post("a", "2026-08-10T12:00:00", sub="good")]
    monkeypatch.setattr(reddit, "_api", half)

    out = reddit.get(parse_range("2026-08-10"))
    assert [it["subreddit"] for it in out["data"]] == ["good"]


# ---------- no feed fallback ----------

def test_an_empty_today_makes_no_reddit_request(dbfile, today, subs,
                                                monkeypatch):
    """The .rss fallback for a today the archive hadn't ingested was removed
    on 11-22, when every Reddit feed was answering 404: it only ever added a
    failure to each sync's errors. An empty archive reply for today is now
    just that, marked synced and error-free, with nothing sent to reddit.com."""
    urls: list[str] = []

    def fake(req, timeout=None):
        urls.append(req.full_url)
        return _Resp({"data": []})
    monkeypatch.setattr(reddit.urllib.request, "urlopen", fake)
    con = db.connect()
    try:
        info = reddit._sync(con, DAY, DAY)
        marked = con.execute("SELECT day FROM reddit_sync").fetchall()
    finally:
        con.close()
    assert urls and all(u.startswith(reddit.ARCTIC_BASE) for u in urls)
    assert "errors" not in info and info["rows"] == 0
    assert [r["day"] for r in marked] == [DAY]
    assert not hasattr(reddit, "_rss")


# ---------- read ----------

def test_top_metric_caps_per_day_and_sorts_unknown_scores_last(
        dbfile, today, subs, monkeypatch):
    monkeypatch.setattr(reddit, "REDDIT_TOP_PER_DAY", 2)
    _arctic(monkeypatch, [
        _post("low", "2026-08-10T10:00:00", score=3),
        _post("high", "2026-08-10T11:00:00", score=99),
        _post("mid", "2026-08-10T12:00:00", score=40),
    ])
    reddit.get(parse_range("2026-08-10"))
    con = db.connect()
    try:                                    # an unscored row joins the day
        with con:
            con.execute("INSERT INTO reddit_posts (day, id, subreddit, title,"
                        " author, selftext, permalink, created_utc,"
                        " refreshed_at) VALUES ('2026-08-10','q','onebag',"
                        "'unknown','a','','/r/onebag/comments/q/', 0,"
                        " '2026-08-10T00:00:00')")
    finally:
        con.close()
    out = reddit.get(parse_range("2026-08-10"), "top")
    assert [it["title"] for it in out["data"]] == ["post high", "post mid"]
    assert out["count"] == 2


def test_recent_scores_are_flagged_provisional(dbfile, today, subs, monkeypatch):
    monkeypatch.setattr(reddit, "REDDIT_SCORE_MATURE_DAYS", 3)
    _arctic(monkeypatch, [_post("new", f"{DAY}T09:00:00", score=1),
                          _post("old", "2026-08-01T09:00:00", score=214)])
    out = reddit.get(parse_range(f"2026-08-01..{DAY}"))
    flags = {it["title"]: it["provisional"] for it in out["data"]}
    assert flags == {"post old": False, "post new": True}


def test_selftext_is_stored_whole_but_trimmed_on_the_way_out(
        dbfile, today, subs, monkeypatch):
    monkeypatch.setattr(reddit, "REDDIT_SELFTEXT_CHARS", 10)
    _arctic(monkeypatch, [_post("a", "2026-08-10T10:00:00",
                                selftext="x" * 50)])
    out = reddit.get(parse_range("2026-08-10"))
    (item,) = out["data"]
    assert item["selftext"] == "x" * 10 and item["truncated"]
    con = db.connect()
    try:
        stored = con.execute("SELECT selftext FROM reddit_posts").fetchone()
    finally:
        con.close()
    assert len(stored["selftext"]) == 50, "the column keeps the whole body"


# ---------- refresh ----------

def test_refresh_syncs_the_maturation_window_then_backs_off(
        dbfile, today, subs, monkeypatch):
    monkeypatch.setattr(reddit, "REDDIT_SCORE_MATURE_DAYS", 3)
    calls: list = []
    _arctic(monkeypatch, [_post("a", f"{DAY}T09:00:00")], calls=calls)

    info = reddit.refresh()
    assert info["days"] == 3                      # 08-10 .. 08-12
    assert len(calls) == 1
    again = reddit.refresh()
    assert "skipped" in again
    assert len(calls) == 1, "the poller re-fetched inside the backoff window"


def test_refresh_pulls_again_once_the_window_is_stale(dbfile, today, subs,
                                                      monkeypatch):
    monkeypatch.setattr(reddit, "REDDIT_REFRESH_MIN", 60)
    _arctic(monkeypatch, [])
    reddit.refresh()
    stale = (datetime.now() - timedelta(hours=2)).isoformat(timespec="seconds")
    con = db.connect()
    try:
        with con:
            con.execute("UPDATE reddit_sync SET refreshed_at=?", (stale,))
    finally:
        con.close()
    assert "skipped" not in reddit.refresh()


# ---------- search ----------

def test_search_is_live_and_writes_nothing(dbfile, today, monkeypatch):
    seen: list = []
    _arctic(monkeypatch, [_post("a", "2026-08-01T10:00:00",
                                title="rain cover thoughts")], calls=seen)
    out = reddit.search("rain cover", "r/onebag", days=30, limit=5)
    assert out["count"] == 1
    assert out["data"][0]["title"] == "rain cover thoughts"
    assert seen[0][1]["subreddit"] == "onebag"   # r/ prefix stripped
    assert seen[0][1]["query"] == "rain cover"
    con = db.connect()
    try:
        assert con.execute("SELECT COUNT(*) c FROM reddit_posts").fetchone()["c"] == 0
        assert con.execute("SELECT COUNT(*) c FROM reddit_sync").fetchone()["c"] == 0
    finally:
        con.close()


def _down_search(monkeypatch, posts, calls):
    """The archive as it was on 11-17/18: every `query=` search refused, the
    newest-first listing (paged on `before`) still answering."""
    def fake(path, params):
        calls.append(params)
        if "query" in params:
            raise reddit.ArchiveBusy("arctic-shift 422 on posts/search: Timeout.")
        hi = int(params.get("before", 1 << 62))
        older = sorted((p for p in posts if p["created_utc"] < hi),
                       key=lambda p: -p["created_utc"])
        return older[:int(params["limit"])]
    monkeypatch.setattr(reddit, "_api", fake)


def test_a_refused_search_matches_the_phrase_in_the_newest_posts(today, monkeypatch):
    """30 of 54 console searches 422'd on 11-17. The listing still answered,
    so the phrase is matched here instead; a word-by-word match would take
    "rain ... cover" too (55 false hits for "code mode" in r/ClaudeCode)."""
    calls: list = []
    _down_search(monkeypatch, [
        _post("a", "2026-08-11T10:00:00", title="Rain cover for a 40L?"),
        _post("b", "2026-08-10T10:00:00", selftext="the rain\ncover held up"),
        _post("c", "2026-08-09T10:00:00", title="rain jacket", selftext="no cover"),
        _post("d", "2026-06-01T10:00:00", title="rain cover, older than the window"),
    ], calls)
    out = reddit.search('"rain cover"', "onebag", days=30)
    assert [it["day"] for it in out["data"]] == ["2026-08-11", "2026-08-10"]
    assert out["scan"] == {"posts": 3, "complete": True, "back_to": "2026-08-09"}
    # No `after` on the listing: with a two-year one it 422'd as well.
    assert "after" not in calls[1] and "query" not in calls[1]


def test_a_scan_that_stops_short_of_the_window_says_how_far_it_got(today, monkeypatch):
    calls: list = []
    monkeypatch.setattr(reddit, "PAGE_LIMIT", 2)
    monkeypatch.setattr(reddit, "SCAN_PAGES", 2)
    _down_search(monkeypatch, [_post(str(i), f"2026-08-{11 - i:02d}T10:00:00",
                                     title=f"rain cover {i}") for i in range(6)],
                 calls)
    out = reddit.search("rain cover", "onebag", days=30, limit=10)
    assert out["scan"] == {"posts": 4, "complete": False, "back_to": "2026-08-08"}
    assert out["count"] == 2                     # PAGE_LIMIT caps the limit too
    assert calls[2]["before"] == _epoch("2026-08-10T10:00:00")


def test_the_cli_brief_says_when_it_only_scanned(today, monkeypatch):
    """Skills read `--search` through the CLI, whose head named the window."""
    from app.providers.__main__ import _search_brief
    calls: list = []
    _down_search(monkeypatch, [_post("a", "2026-08-11T10:00:00",
                                     title="rain cover?")], calls)
    out = reddit.search("rain cover", "onebag", days=30)
    out["scan"]["complete"] = False
    head = _search_brief(out, reddit).splitlines()[0]
    assert head == ("reddit search='rain cover' in onebag newest 1 posts only,"
                    " back to 2026-08-11 (search down) (1 items)")
    out["scan"]["complete"] = True
    assert "last 30d (1 items)" in _search_brief(out, reddit)


def test_a_refused_listing_is_still_archive_busy(today, monkeypatch):
    def fake(path, params):
        raise reddit.ArchiveBusy("arctic-shift 422 on posts/search: Timeout.")
    monkeypatch.setattr(reddit, "_api", fake)
    with pytest.raises(reddit.ArchiveBusy):
        reddit.search("rain cover", "onebag")


def test_search_without_a_subreddit_is_refused_before_the_call(monkeypatch):
    """Arctic Shift 400s a query that scopes to neither a subreddit nor an
    author, so an unscoped search is not a thing this route can do. It shipped
    with the parameter optional, which made every such call a wasted round
    trip and a misleading "search all of Reddit" in the tool's description."""
    called = []
    monkeypatch.setattr(reddit, "_api", lambda p, q: called.append(p) or [])
    for missing in (None, "", "   ", "r/"):
        with pytest.raises(ValueError, match="subreddit"):
            reddit.search("rain cover", missing)
    assert called == []


def test_a_busy_archive_is_retried_once_then_named(monkeypatch):
    """The archive's busy answer is a 422 reading "Timeout. Maybe slow down a
    bit" — load, not a bad query: the same terms fail and then succeed seconds
    apart. One retry turns most of those into an answer; what survives it is
    ArchiveBusy, so a caller can say "later" rather than "wrong"."""
    monkeypatch.setattr(reddit.time, "sleep", lambda s: None)
    attempts = _stub_http(monkeypatch, [_http_error(422, "Timeout."),
                                        {"data": [_post("a", f"{DAY}T09:00:00")]}])
    assert len(reddit._api("posts/search", {"query": "cover"})) == 1
    assert attempts["n"] == 2

    attempts = _stub_http(monkeypatch, [_http_error(422, "Timeout."),
                                        _http_error(422, "Timeout.")])
    with pytest.raises(reddit.ArchiveBusy):
        reddit._api("posts/search", {"query": "cover"})
    assert attempts["n"] == reddit._API_ATTEMPTS


def test_a_bad_query_is_not_retried(monkeypatch):
    """A 400 is the request being wrong and will be wrong again — retrying it
    only doubles the wait before the caller hears why."""
    monkeypatch.setattr(reddit.time, "sleep", lambda s: None)
    attempts = _stub_http(monkeypatch, [_http_error(400, "'limit' must be…")])
    with pytest.raises(RuntimeError) as e:
        reddit._api("posts/search", {"query": "x", "limit": 99999})
    assert not isinstance(e.value, reddit.ArchiveBusy)
    assert attempts["n"] == 1


def test_registered_and_described():
    from app.providers import REGISTRY, describe
    assert REGISTRY["reddit"] is reddit
    entry = describe()["reddit"]
    assert entry["metrics"] == ["posts", "top"]
    assert entry["refreshable"] is True


def test_config_defaults_are_sane():
    assert config.REDDIT_SCORE_MATURE_DAYS >= 1
    assert config.REDDIT_REFRESH_MIN > 0
    assert config.REDDIT_MAX_PAGES > 0
