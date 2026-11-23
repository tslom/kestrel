"""Last.fm provider — genre/mood tags for the artists in `plays`.

Exists because Spotify stopped supplying them. The v16 `artist_genres` table
was fed by GET /artists?ids=, which 403s for this app (only single-resource
GETs are permitted), and the artist object no longer carries a `genres` field
at all — the response is down to id/name/images/urls. Verified live 2026-07-26;
the community reads it as a deliberate taxonomy change, not an outage. See the
v35 migration comment.

So genres come from Last.fm's artist.getTopTags, which is name-keyed — no
Spotify artist id is involved, and none is needed. The join key is
`plays.artist_key` (spotify.artist_key: the artist's name, casefolded), which
is also what makes the imported history and the live rows agree in the first
place.

Tags are crowd-sourced, so they're richer than a label taxonomy but noisier:
weights are 0-100 relative to the artist's own top tag, and the long tail is
listener chatter ("seen live", "favourites"). We keep tags at or above
LASTFM_MIN_TAG_WEIGHT, drop TAG_BLOCKLIST, and cap at LASTFM_MAX_TAGS.

Enrichment is a bounded trickle, not a batch: refresh() tags at most
LASTFM_ENRICH_PER_CYCLE artists per poll, **recently-played first, then
most-played**, so anything you put on today is tagged by the next cycle while
the 9k-artist tail fills in over the following days. An artist Last.fm has
never heard of is marked missing=1 rather than retried forever.

Tag cleaning is deliberately reversible: the untouched reply is kept in
`artist_tags.raw`, so revising the blocklist or the cap is a local `reclean`
rather than another 9.8k requests.

No key configured = provider reports "off" and refresh() no-ops. Get a free
one at last.fm/api/account/create, then set LASTFM_API_KEY in backend/.env.

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

# Listener chatter that rides along with the genre tags. Matched casefolded and
# exactly — "favourites" goes, "favourite metal" is left alone.
TAG_BLOCKLIST = {
    "seen live", "favourites", "favorites", "favourite", "favorite",
    "favourite songs", "favorite songs", "favourite artists", "favorite artists",
    "awesome", "love", "loved", "best", "good", "great", "amazing", "cool",
    "my music", "music", "spotify", "check out", "under 2000 listeners",
    "albums i own", "vinyl", "male vocalists", "female vocalists",
}

# Where an artist is from is not what they sound like. Last.fm's tag cloud is
# thick with these and they'd otherwise chart alongside real genres — "usa" and
# "united states" both showed up in the first 79 artists. Bare nationalities
# only: compounds like "uk garage", "french house" and "j-pop" are genuine
# genres and don't match. Cities are left alone for the same reason (Chicago
# house, Detroit techno), and are rare enough not to distort the chart.
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
    """No LASTFM_API_KEY — nothing to do, and not an error worth alarming on."""


class NotFound(Exception):
    """Last.fm has never heard of this artist. A settled answer, not a failure:
    it must be recorded as missing=1 like an empty tag list, or the artist is
    re-requested every cycle forever and logs a `degraded` event each time."""


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
    """Canonical form of a tag. Last.fm's tags are free text, so the same genre
    arrives punctuated several ways — Drake returns both "hip-hop" and
    "hip hop" — and each spelling would otherwise rank as its own genre,
    splitting one genre's play count between them.

    This is what gets *stored*, not just a de-dupe key: two artists can return
    different preferred spellings ("hip-hop" from one, "hip hop" from the
    other), so per-artist de-duping isn't enough to make them one genre in the
    chart. Separators collapse to a space, which costs a little prettiness
    ("lo-fi" -> "lo fi") and buys one row per genre.
    """
    return " ".join(re.split(r"[\s\-_/]+", name.casefold())).strip()


def _clean_tags(raw: list, artist: str = "") -> list[str]:
    """Weighted tag list -> ranked genre names, chatter removed.

    The docs' sample response carries only name+url — `count` is undocumented,
    even though live JSON returns it. So weights are treated as an optional
    refinement: if none of the tags carry one, the list is already ordered by
    popularity, and we keep the top N rather than dropping everything and
    recording the artist as unknown.

    `artist` drops self-referential tags: Last.fm users tag Drake "drake",
    which is true and useless — it would chart as a genre of its own. Only an
    exact name match goes; stylised spellings ("travis scott" on Travi$ Scott)
    survive, and aren't worth guessing at.
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
    """One artist -> a row for `artist_tags`. autocorrect=1 lets Last.fm fix
    spelling/casing, so we store the name it settled on."""
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
    """(artist_key, display name) for artists we haven't looked up yet.

    Recently-played first, then most-played. Play count alone looks right and
    isn't: a one-off artist you tried today sits behind the whole ~8k tail, so
    today's genre coverage reads 0% for days — zero exactly when you'd look at
    it. Recent listening is both the smallest bucket and the only one anyone
    reads, so it jumps the queue; play count still orders everything else, so
    the backfill keeps buying the most coverage per request.
    """
    spotify.ensure_artist_keys(con)
    # day_key(), not date.today() — `plays.day` is written with the 4am
    # rollover, so a 2am play belongs to the previous day on both sides.
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
    """Look up `limit` so-far-untagged artists. Returns counts; a single
    artist's failure is logged and skipped rather than sinking the batch."""
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
    """Re-derive `tags` from the stored raw replies. No network.

    Run this after changing TAG_BLOCKLIST, the weight floor or the tag cap —
    it applies the new rules to every artist already fetched, instead of
    spending another 9.8k requests re-asking Last.fm the same questions. Rows
    from before v36 have no raw reply and are left alone (`skipped`); they
    pick up the new rules whenever they're next fetched.
    """
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
    """Top genres for a single day, with the share of that day's plays they're
    computed from. One query, for callers that already hold a connection —
    spotify.summary() folds this into the MusicWidget payload rather than
    making the card fetch a second endpoint.

    `coverage` travels with the genres on purpose: a mix drawn from 20% of a
    day's plays is a different claim from one drawn from 95%, and the caller
    can't tell them apart from the genre list alone. None means no plays.
    """
    # Self-healing like every other artist_key reader: without this, a DB whose
    # backfill hasn't run matches no rows and reads as "no plays today".
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
    """Join the range's plays against the tag cache. Local-only: this never
    calls Last.fm (enrichment is refresh()'s job), so an unenriched artist
    simply doesn't contribute genres yet — `coverage` is how you tell."""
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
            # A play votes for each of its artist's tags. Tags are ranked, not
            # exclusive, so shares are of total votes and don't sum to 1.
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
            # Share of each day's plays whose artist has tags — how much to
            # trust that day's genre mix.
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
    """Coverage, not freshness: `artist_tags` has no day column, so there's
    nothing for heartbeat.FRESHNESS to check. What can actually go wrong is a
    backfill that stalls far short of your library."""
    if not LASTFM_API_KEY:
        return {"state": "off", "detail": "no LASTFM_API_KEY configured"}
    with db.session() as con:
        # Without this an unbackfilled `plays` has no keys to count, and the
        # panel would read "off" — the one state that renders as fine.
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
    # Enrichment is a deliberate trickle, so "incomplete" is the normal state
    # for a while — only a backfill that has barely started is worth flagging.
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
