"""Last.fm provider: genre tags for the artists in `plays` (Spotify no longer supplies them).

Name-keyed via `plays.artist_key` (spotify.artist_key: casefolded name). `artist_tags`
keeps Last.fm's untouched reply in `raw`, so `reclean` re-derives `tags` offline.
No LASTFM_API_KEY = state "off", refresh() no-ops. Reads never call Last.fm.
Design: DESIGN.md → §8 Last.fm tags.

CLI:
    python -m app.providers lastfm --range this-week          # top genres
    python -m app.providers lastfm --range this-week --metric artists
    python -m app.providers.lastfm backfill --limit 500       # bulk enrich now
"""

import json
import re
import sys
import time
import urllib.parse
import urllib.request  # tests patch lastfm.urllib.request.urlopen
from collections import defaultdict
from datetime import date, datetime, timedelta

from .. import db
from ..config import (LASTFM_API_KEY, LASTFM_ENRICH_PER_CYCLE, LASTFM_MAX_TAGS,
                      LASTFM_MIN_TAG_WEIGHT)
from . import http, spotify
from .ranges import Range

NAME = "lastfm"
METRICS = ("genres", "artists", "coverage")
DEFAULT_METRIC = "genres"
ALLOW_FUTURE = False

API_BASE = "https://ws.audioscrobbler.com/2.0/"
REQUEST_TIMEOUT = 15
ENRICH_PAUSE_SEC = 0.25   # Last.fm asks for <=5 req/s averaged; well under
RECENT_DAYS = 7           # what counts as "recently played"; see _untagged

# Listener chatter, matched casefolded and exactly ("favourite metal" stays).
TAG_BLOCKLIST = {
    "seen live", "favourites", "favorites", "favourite", "favorite",
    "favourite songs", "favorite songs", "favourite artists", "favorite artists",
    "awesome", "love", "loved", "best", "good", "great", "amazing", "cool",
    "my music", "music", "spotify", "check out", "under 2000 listeners",
    "albums i own", "vinyl", "male vocalists", "female vocalists",
}

# Origins aren't genres. Bare nationalities only: "uk garage", "french house", "j-pop" must survive.
TAG_BLOCKLIST |= {
    "usa", "us", "united states", "american", "america",
    "uk", "united kingdom", "british", "britain", "england", "english",
    "scottish", "irish", "welsh", "canadian", "canada", "australian",
    "australia", "new zealand", "japanese", "japan", "korean", "korea",
    "chinese", "china", "french", "france", "german", "germany", "swedish",
    "sweden", "norwegian", "norway", "danish", "denmark", "finnish", "finland",
    "icelandic", "dutch", "netherlands", "belgian", "italian", "italy",
    "spanish", "spain", "portuguese", "portugal", "brazilian", "brazil",
    "mexican", "mexico", "russian", "russia", "polish", "poland", "israeli",
    "indian", "india", "african", "south african", "nigerian", "jamaican",
    "european", "latin american", "asian",
}


class NotConfigured(Exception):
    """No LASTFM_API_KEY; not alarming."""


class NotFound(Exception):
    """Unknown artist: a settled answer, recorded as missing=1 (else re-requested forever)."""


ERR_NOT_FOUND = 6


def _api(method: str, params: dict) -> dict:
    if not LASTFM_API_KEY:
        raise NotConfigured("LASTFM_API_KEY not set")
    url = API_BASE + "?" + urllib.parse.urlencode(
        {**params, "method": method, "api_key": LASTFM_API_KEY, "format": "json"})
    blob = http.json_request(url, label=f"last.fm {method}",
                             headers={"User-Agent": "vault-dashboard/1.0"},
                             timeout=REQUEST_TIMEOUT, detail_limit=200)
    # Last.fm signals failure in a 200 body as often as in the status line.
    if isinstance(blob, dict) and "error" in blob:
        if blob["error"] == ERR_NOT_FOUND:
            raise NotFound(blob.get("message", "artist not found"))
        raise RuntimeError(f"last.fm {method} error {blob['error']}:"
                           f" {blob.get('message', '')}")
    return blob


def _tag_id(name: str) -> str:
    """Canonical stored form: separators collapse to a space ("hip-hop" == "hip hop"), so one genre is one row."""
    return " ".join(re.split(r"[\s\-_/]+", name.casefold())).strip()


def _clean_tags(raw: list, artist: str = "") -> list[str]:
    """Weighted tag list -> ranked genre names, chatter removed.

    `count` is undocumented, so weights are optional: with none, keep the top N in order.
    `artist` drops an exact self-named tag (Drake tagged "drake"); stylised spellings survive.
    """
    def weight(t) -> int:
        try:
            return int(t.get("count") or 0)
        except (TypeError, ValueError):
            return 0

    weighted = any(weight(t) for t in raw)
    self_id = _tag_id(artist)
    out, seen = [], set()
    for t in raw:
        name = (t.get("name") or "").strip()
        if not name or name.casefold() in TAG_BLOCKLIST:
            continue
        if weighted and weight(t) < LASTFM_MIN_TAG_WEIGHT:
            continue
        tag_id = _tag_id(name)
        if not tag_id or tag_id in seen or tag_id in TAG_BLOCKLIST:
            continue
        if self_id and tag_id == self_id:
            continue
        seen.add(tag_id)
        out.append(tag_id)
        if len(out) >= LASTFM_MAX_TAGS:
            break
    return out


def _fetch_artist(key: str, display_name: str) -> dict:
    """One artist -> an `artist_tags` row; autocorrect=1 so we store Last.fm's settled name."""
    try:
        blob = _api("artist.gettoptags",
                    {"artist": display_name, "autocorrect": "1"})
    except NotFound:
        # Settled: record it so the trickle moves on permanently.
        return {"artist_key": key, "name": display_name, "tags": "[]",
                "raw": "[]", "mbid": None,
                "refreshed_at": datetime.now().isoformat(timespec="seconds"),
                "missing": 1}
    top = (blob.get("toptags") or {})
    raw = top.get("tag") or []
    if isinstance(raw, dict):      # single tag comes back unwrapped
        raw = [raw]
    name = (top.get("@attr") or {}).get("artist") or display_name
    tags = _clean_tags(raw, name)
    return {
        "artist_key": key,
        "name": name,
        "tags": json.dumps(tags),
        "raw": json.dumps(raw),
        "mbid": None,
        "refreshed_at": datetime.now().isoformat(timespec="seconds"),
        "missing": 0 if tags else 1,
    }


def _untagged(con, limit: int) -> list[tuple[str, str]]:
    """(artist_key, display name) for untagged artists: recently-played first, then most-played
    (play count alone leaves today's one-off behind the ~8k tail, so today's coverage reads 0%)."""
    spotify.ensure_artist_keys(con)
    # day_key(), not date.today(): `plays.day` uses the 4am rollover.
    recent = (date.fromisoformat(db.day_key())
              - timedelta(days=RECENT_DAYS)).isoformat()
    return [(r["artist_key"], r["artist"]) for r in con.execute(
        "SELECT p.artist_key, MIN(p.artist) AS artist, COUNT(*) AS c,"
        "       MAX(p.day) >= ? AS recent"
        " FROM plays p LEFT JOIN artist_tags t ON t.artist_key = p.artist_key"
        " WHERE t.artist_key IS NULL AND p.artist_key IS NOT NULL"
        "   AND p.artist_key != ''"
        " GROUP BY p.artist_key ORDER BY recent DESC, c DESC LIMIT ?",
        (recent, limit)).fetchall()]


def enrich(limit: int, *, pause: float = ENRICH_PAUSE_SEC) -> dict:
    """Look up `limit` untagged artists and return counts; one artist's failure is logged and skipped."""
    if not LASTFM_API_KEY:
        raise NotConfigured("LASTFM_API_KEY not set")
    with db.session() as con:
        todo = _untagged(con, limit)
        tagged = missing = failed = 0
        for key, name in todo:
            try:
                row = _fetch_artist(key, name)
            except NotConfigured:
                raise
            except Exception as e:  # one bad artist must not stop the trickle
                failed += 1
                from . import heartbeat
                heartbeat.log_event(NAME, "degraded", f"{name}: {e}")
                continue
            with con:
                con.execute(
                    "INSERT INTO artist_tags (artist_key, name, tags, raw, mbid,"
                    " refreshed_at, missing) VALUES"
                    " (:artist_key,:name,:tags,:raw,:mbid,:refreshed_at,:missing)"
                    " ON CONFLICT(artist_key) DO UPDATE SET name=excluded.name,"
                    " tags=excluded.tags, raw=excluded.raw, mbid=excluded.mbid,"
                    " refreshed_at=excluded.refreshed_at, missing=excluded.missing",
                    row)
            if row["missing"]:
                missing += 1
            else:
                tagged += 1
            if pause:
                time.sleep(pause)
        return {"looked_up": len(todo), "tagged": tagged,
                "unknown": missing, "failed": failed}


def reclean() -> dict:
    """Re-derive `tags` from stored raw replies, no network. Run after changing the blocklist,
    weight floor or cap. Rows without `raw` (pre-v36) are `skipped`."""
    with db.session() as con:
        rows = con.execute(
            "SELECT artist_key, name, tags, raw FROM artist_tags").fetchall()
        changed = skipped = 0
        for r in rows:
            if not r["raw"]:
                skipped += 1
                continue
            tags = _clean_tags(json.loads(r["raw"]), r["name"])
            new = json.dumps(tags)
            if new == r["tags"]:
                continue
            with con:
                con.execute(
                    "UPDATE artist_tags SET tags=?, missing=? WHERE artist_key=?",
                    (new, 0 if tags else 1, r["artist_key"]))
            changed += 1
        return {"rows": len(rows), "changed": changed, "skipped": skipped}


def refresh() -> dict:
    """Poller entrypoint — a bounded slice of the backfill each cycle."""
    if not LASTFM_API_KEY:
        return {"skipped": "no LASTFM_API_KEY"}
    return enrich(LASTFM_ENRICH_PER_CYCLE)


# ---------- reads ----------

def _tag_map(con) -> dict[str, list[str]]:
    return {r["artist_key"]: json.loads(r["tags"])
            for r in con.execute("SELECT artist_key, tags FROM artist_tags")}


def day_mix(con, day: str, limit: int = 3) -> dict | None:
    """Top genres for one day plus `coverage`, the share of plays they come from; None means no plays.
    For callers already holding a connection (spotify.summary())."""
    # Without this an un-backfilled DB matches no rows and reads as "no plays today".
    spotify.ensure_artist_keys(con)
    rows = con.execute(
        "SELECT p.artist_key, COUNT(*) AS plays, t.tags"
        " FROM plays p LEFT JOIN artist_tags t ON t.artist_key = p.artist_key"
        " WHERE p.day = ? AND p.artist_key IS NOT NULL"
        " GROUP BY p.artist_key", (day,)).fetchall()
    if not rows:
        return None
    counts: dict[str, int] = defaultdict(int)
    total = known = 0
    for r in rows:
        total += r["plays"]
        tags = json.loads(r["tags"]) if r["tags"] else []
        if tags:
            known += r["plays"]
        for g in tags:
            counts[g] += r["plays"]
    if not total:
        return None
    top = sorted(counts.items(), key=lambda kv: -kv[1])[:limit]
    return {"top": [{"genre": g, "plays": c} for g, c in top],
            "coverage": round(known / total, 2)}


def brief_line(it: dict) -> str:
    if "genre" in it:
        return f"{it['genre']}: {it['plays']} plays ({it['share']:.0%})"
    if "artist" in it:
        return f"{it['artist']} — {', '.join(it['tags']) or 'untagged'}"
    return f"{it['day']} {it['value']}"


def get(rng: Range, metric: str | None = None) -> dict:
    """Join the range's plays against the tag cache; local-only, `coverage` shows what's untagged."""
    metric = metric or DEFAULT_METRIC
    with db.session() as con:
        spotify.ensure_artist_keys(con)
        rows = con.execute(
            "SELECT day, artist_key, MIN(artist) AS artist, COUNT(*) AS plays"
            " FROM plays WHERE day BETWEEN ? AND ? AND artist_key IS NOT NULL"
            " GROUP BY day, artist_key", (rng.start_key, rng.end_key)).fetchall()
        tags = _tag_map(con)
        refreshed = con.execute(
            "SELECT MAX(refreshed_at) AS m FROM artist_tags").fetchone()["m"]

        if metric == "genres":
            # A play votes for each tag, so shares are of total votes and don't sum to 1.
            counts: dict[str, int] = defaultdict(int)
            votes = 0
            for r in rows:
                for g in tags.get(r["artist_key"], []):
                    counts[g] += r["plays"]
                    votes += r["plays"]
            data = [{"genre": g, "plays": c, "share": round(c / votes, 3) if votes else 0.0}
                    for g, c in sorted(counts.items(), key=lambda kv: -kv[1])]

        elif metric == "artists":
            by_artist: dict[str, dict] = {}
            for r in rows:
                a = by_artist.setdefault(r["artist_key"], {
                    "artist": r["artist"], "artist_key": r["artist_key"],
                    "plays": 0, "tags": tags.get(r["artist_key"], [])})
                a["plays"] += r["plays"]
            data = sorted(by_artist.values(), key=lambda a: -a["plays"])

        elif metric == "coverage":
            total, known = defaultdict(int), defaultdict(int)
            for r in rows:
                total[r["day"]] += r["plays"]
                if tags.get(r["artist_key"]):
                    known[r["day"]] += r["plays"]
            data = [{"day": d, "value": round(known[d] / total[d], 2)}
                    for d in sorted(total) if total[d]]
        else:
            raise ValueError(f"unknown metric {metric!r} for lastfm;"
                             f" expected one of: {', '.join(METRICS)}")

        return {"metric": metric, "data": data, "refreshed_at": refreshed}


def health() -> dict:
    """Coverage, not freshness: `artist_tags` has no day column for heartbeat.FRESHNESS."""
    if not LASTFM_API_KEY:
        return {"state": "off", "detail": "no LASTFM_API_KEY configured"}
    with db.session() as con:
        # Without this an unbackfilled `plays` has no keys and the panel reads "off", which renders as fine.
        spotify.ensure_artist_keys(con)
        row = con.execute(
            "SELECT COUNT(DISTINCT p.artist_key) AS artists,"
            "       COUNT(DISTINCT t.artist_key) AS tagged"
            " FROM plays p LEFT JOIN artist_tags t ON t.artist_key = p.artist_key"
            " WHERE p.artist_key IS NOT NULL").fetchone()
    artists, tagged = row["artists"] or 0, row["tagged"] or 0
    if not artists:
        return {"state": "off", "detail": "no plays to tag yet"}
    pct = tagged / artists
    detail = f"{tagged}/{artists} artists tagged ({pct:.0%})"
    # The trickle is slow, so incomplete is normal; only an unstarted backfill is flagged.
    return {"state": "ok" if tagged else "stale", "detail": detail,
            "artists": artists, "tagged": tagged}


# ---------- CLI ----------

def _main() -> None:
    import argparse
    p = argparse.ArgumentParser(
        prog="python -m app.providers.lastfm",
        description="Backfill Last.fm genre tags for the artists in `plays`.")
    sub = p.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("backfill", help="enrich untagged artists, most-played first")
    b.add_argument("--limit", type=int, default=200,
                   help="how many artists to look up this run (default 200)")
    b.add_argument("--pause", type=float, default=ENRICH_PAUSE_SEC,
                   help=f"seconds between calls (default {ENRICH_PAUSE_SEC})")
    sub.add_parser("status", help="tag coverage")
    sub.add_parser("reclean", help="re-apply the tag filters to cached replies"
                                   " (no network)")

    args = p.parse_args()
    if args.cmd == "backfill":
        try:
            print(json.dumps(enrich(args.limit, pause=args.pause), indent=2))
        except NotConfigured as e:
            sys.exit(f"{e} — get a free key at https://www.last.fm/api/account/create"
                     " and add it to backend/.env")
    elif args.cmd == "status":
        print(json.dumps(health(), indent=2))
    elif args.cmd == "reclean":
        print(json.dumps(reclean(), indent=2))


if __name__ == "__main__":
    _main()
