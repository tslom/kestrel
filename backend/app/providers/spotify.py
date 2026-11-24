"""Spotify provider: authorization-code + PKCE against accounts.spotify.com (public client, no secret).

`plays` is NOT a purely derived cache: recently-played only returns the last 50, so the
table is the durable record, and the GDPR-export importer backfills the same table.
Auth: the redirect lands on the real backend (routes/spotify.py); `authorize` here is a thin
client of that route. Token row: `tokens` (provider='spotify', client_secret='').
Design: DESIGN.md → §8 Spotify.

CLI:
    python -m app.providers.spotify authorize --client-id ID   # first time
    python -m app.providers.spotify authorize                  # re-auth, reuses stored client_id
    python -m app.providers.spotify status
"""

import base64
import hashlib
import json
import secrets
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone

from .. import db
from . import http
from ..config import (BACKEND_PORT, DESK_BASELINE_DAYS, SPOTIFY_ACCOUNT,
                      SPOTIFY_REDIRECT_URI, SPOTIFY_SCOPES)
from .ranges import Range

NAME = "spotify"
METRICS = ("plays", "listen_min", "novelty", "bracket", "top_repeat", "shape")
DEFAULT_METRIC = "plays"
ALLOW_FUTURE = False  # no next-week listening history

AUTH_URI = "https://accounts.spotify.com/authorize"
TOKEN_URI = "https://accounts.spotify.com/api/token"
API_BASE = "https://api.spotify.com/v1"

EXPIRY_MARGIN = timedelta(seconds=60)
NOVELTY_WINDOW_DAYS = 7

# Silence this long ends a listening session: above any single track, below an errand.
LISTEN_GAP_MIN = 30
# Below this many plays a day has no shape: a gap, not zero.
SHAPE_MIN_PLAYS = 5


class NotAuthorized(Exception):
    """No usable Spotify token — run the authorize CLI."""


# ---------- token storage (reuses the shared `tokens` table) ----------

def _row() -> dict | None:
    with db.session() as con:
        row = con.execute("SELECT * FROM tokens WHERE provider='spotify' AND account=?",
                           (SPOTIFY_ACCOUNT,)).fetchone()
    return dict(row) if row else None


def stored_client_id() -> str | None:
    row = _row()
    return row["client_id"] if row else None


def _save(client_id: str, access_token: str, refresh_token: str | None,
          expires_at: str) -> None:
    with db.session() as con, con:
        con.execute(
            "INSERT INTO tokens (provider, account, client_id, client_secret,"
            " access_token, refresh_token, token_uri, scopes, expires_at, updated_at)"
            " VALUES ('spotify',?,?,'',?,?,?,?,?,?)"
            " ON CONFLICT(provider, account) DO UPDATE SET"
            " client_id=excluded.client_id, access_token=excluded.access_token,"
            # a refresh grant may omit refresh_token: keep the old one
            " refresh_token=COALESCE(excluded.refresh_token, tokens.refresh_token),"
            " token_uri=excluded.token_uri, scopes=excluded.scopes,"
            " expires_at=excluded.expires_at, updated_at=excluded.updated_at",
            (SPOTIFY_ACCOUNT, client_id, access_token, refresh_token,
             TOKEN_URI, SPOTIFY_SCOPES, expires_at,
             datetime.now(timezone.utc).isoformat(timespec="seconds")))


def status() -> dict:
    row = _row()
    if row is None:
        return {"connected": False}
    return {
        "connected": bool(row["refresh_token"]),
        "client_id": row["client_id"],
        "scopes": row["scopes"],
        "updated_at": row["updated_at"],
    }


# ---------- PKCE authorization-code flow ----------

def build_auth_url(client_id: str) -> tuple[str, str, str]:
    """(url, state, verifier) for one authorization attempt."""
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(16)
    url = AUTH_URI + "?" + urllib.parse.urlencode({
        "client_id": client_id,
        "response_type": "code",
        "redirect_uri": SPOTIFY_REDIRECT_URI,
        "scope": SPOTIFY_SCOPES,
        "state": state,
        "code_challenge_method": "S256",
        "code_challenge": challenge,
    })
    return url, state, verifier


def _post_form(data: dict) -> dict:
    req = urllib.request.Request(
        TOKEN_URI, data=urllib.parse.urlencode(data).encode(), method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")
        raise RuntimeError(f"spotify token endpoint {e.code}: {detail}") from None


def exchange_code(code: str, verifier: str, client_id: str) -> None:
    """Authorization code -> tokens, stored. Called by spotify_routes.py's
    callback route when Spotify redirects back."""
    tok = _post_form({
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": SPOTIFY_REDIRECT_URI,
        "client_id": client_id,
        "code_verifier": verifier,
    })
    expires_at = (datetime.now(timezone.utc)
                  + timedelta(seconds=tok.get("expires_in", 3600))
                  ).isoformat(timespec="seconds")
    _save(client_id, tok["access_token"], tok.get("refresh_token"), expires_at)


def _refresh_access_token() -> str:
    row = _row()
    if row is None or not row["refresh_token"]:
        raise NotAuthorized("no Spotify refresh token — run: python -m"
                             " app.providers.spotify authorize --client-id ID")
    try:
        tok = _post_form({
            "grant_type": "refresh_token",
            "refresh_token": row["refresh_token"],
            "client_id": row["client_id"],
        })
    except RuntimeError as e:
        if "invalid_grant" in str(e):
            # Dead token: drop it so status() says needs-auth instead of retrying every poll.
            with db.session() as con, con:
                con.execute(
                    "UPDATE tokens SET access_token=NULL, refresh_token=NULL,"
                    " updated_at=? WHERE provider='spotify' AND account=?",
                    (datetime.now(timezone.utc).isoformat(timespec="seconds"),
                     SPOTIFY_ACCOUNT))
            raise NotAuthorized(
                "Spotify refresh token revoked/expired — re-run: python -m"
                " app.providers.spotify authorize") from None
        raise
    expires_at = (datetime.now(timezone.utc)
                  + timedelta(seconds=tok.get("expires_in", 3600))
                  ).isoformat(timespec="seconds")
    _save(row["client_id"], tok["access_token"], tok.get("refresh_token"), expires_at)
    return tok["access_token"]


def get_access_token() -> str:
    row = _row()
    if row is None or not row["access_token"]:
        return _refresh_access_token()
    expires_at = datetime.fromisoformat(row["expires_at"])
    if datetime.now(timezone.utc) + EXPIRY_MARGIN < expires_at:
        return row["access_token"]
    return _refresh_access_token()


# ---------- Spotify Web API ----------

def _api(path: str, params: dict | None = None) -> dict:
    token = get_access_token()
    url = API_BASE + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    return http.json_request(url, label="spotify API", token=token)


def day_key_of(ts_utc: str) -> str:
    dt = datetime.fromisoformat(ts_utc.replace("Z", "+00:00"))
    return db.day_key(dt.astimezone())


def artist_key(name: str) -> str:
    """The grouping key for an artist: name, whitespace-collapsed and casefolded.
    Never group on `artist_id` (poller and importer write different ids); casefold in Python, not SQL lower()."""
    return " ".join(name.split()).casefold()


def ensure_artist_keys(con) -> int:
    """Fill plays.artist_key wherever missing (v35 backfill and self-heal); one index probe once filled."""
    rows = con.execute(
        "SELECT played_at, artist FROM plays WHERE artist_key IS NULL").fetchall()
    if not rows:
        return 0
    with con:
        con.executemany(
            "UPDATE plays SET artist_key=? WHERE played_at=?",
            [(artist_key(r["artist"]), r["played_at"]) for r in rows])
    return len(rows)


def _sync_recent(con) -> int:
    """Pull the last 50 plays and upsert into `plays` (dedupe on played_at)."""
    blob = _api("/me/player/recently-played", {"limit": 50})
    now = datetime.now().isoformat(timespec="seconds")
    n = 0
    with con:
        for item in blob.get("items", []):
            track = item.get("track") or {}
            artists = track.get("artists") or []
            artist = artists[0] if artists else None
            artist_id = artist["id"] if artist else ""
            artist_name = artist["name"] if artist else "Unknown"
            day = day_key_of(item["played_at"])
            cur = con.execute(
                "INSERT OR IGNORE INTO plays (played_at, day, track_id, track,"
                " artist, artist_id, artist_key, album, ms, refreshed_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                (item["played_at"], day, track.get("id"), track.get("name"),
                 artist_name, artist_id, artist_key(artist_name),
                 (track.get("album") or {}).get("name"),
                 track.get("duration_ms", 0), now))
            if cur.rowcount:
                n += 1
    return n


def refresh() -> dict:
    """Poller entrypoint: pull the last 50 plays each cycle; quiet no-op until authorized."""
    try:
        get_access_token()
    except NotAuthorized as e:
        return {"skipped": str(e)}
    with db.session() as con:
        n = _sync_recent(con)
        keyed = ensure_artist_keys(con)
    return {"new_plays": n, **({"artist_keys_filled": keyed} if keyed else {})}


def listen_shape_by_day(con, start_key: str, end_key: str) -> dict[str, dict[str, float]]:
    """`{day: {repeat_ratio, listen_sessions, longest_listen_min}}`; shared with features.py (one copy of the math).
    Sessions span play markers only, never marker plus `ms` (the two writers disagree what
    `played_at` means), so a one-play session is 0.0. Days under SHAPE_MIN_PLAYS are absent, not zero."""
    rows = con.execute(
        "SELECT day, played_at, track_id FROM plays"
        " WHERE day BETWEEN ? AND ? ORDER BY played_at",
        (start_key, end_key)).fetchall()

    by_day: dict[str, list] = defaultdict(list)
    for r in rows:
        by_day[r["day"]].append(r)

    out: dict[str, dict[str, float]] = {}
    for day, plays in by_day.items():
        if len(plays) < SHAPE_MIN_PLAYS:
            continue
        counts: dict[str, int] = defaultdict(int)
        spans: list[list[datetime]] = []
        prev: datetime | None = None
        for r in plays:
            counts[r["track_id"]] += 1
            at = datetime.fromisoformat(r["played_at"])
            if prev is None or (at - prev).total_seconds() / 60 >= LISTEN_GAP_MIN:
                spans.append([at, at])
            else:
                spans[-1][1] = at
            prev = at
        out[day] = {
            "repeat_ratio": round(max(counts.values()) / len(plays), 3),
            "listen_sessions": float(len(spans)),
            "longest_listen_min": round(
                max((b - a).total_seconds() / 60 for a, b in spans), 1),
        }
    return out


def brief_line(it: dict) -> str:
    if "repeat_ratio" in it:  # shape
        return (f"{it['day']} repeat={it['repeat_ratio']}"
                f" sessions={it['listen_sessions']:.0f}"
                f" longest={it['longest_listen_min']}m")
    if "first" in it:  # bracket
        return f"{it['day']} {it['first']}..{it['last']}"
    if "count" in it:  # top_repeat
        return f"{it['day']} {it['track']} ({it['artist']}) x{it['count']}"
    if "track" in it:  # plays
        return f"{it['played_at']} {it['artist']} - {it['track']}"
    return f"{it['day']} {it['value']}"  # listen_min, novelty


def get(rng: Range, metric: str | None = None) -> dict:
    """Slice the `plays` cache; no on-demand backfill (the endpoint only answers "the last 50")."""
    metric = metric or DEFAULT_METRIC
    with db.session() as con:
        ensure_artist_keys(con)
        rows = [dict(r) for r in con.execute(
            "SELECT played_at, day, track_id, track, artist, artist_id,"
            " artist_key, album, ms, refreshed_at FROM plays"
            " WHERE day BETWEEN ? AND ?"
            " ORDER BY played_at DESC", (rng.start_key, rng.end_key)).fetchall()]
        refreshed = max((r["refreshed_at"] for r in rows), default=None)

        if metric == "plays":
            data = rows

        elif metric == "listen_min":
            by_day = defaultdict(int)
            for r in rows:
                by_day[r["day"]] += r["ms"]
            data = [{"day": d, "value": round(by_day[d] / 60000, 1)}
                    for d in sorted(by_day)]

        elif metric == "novelty":
            first_seen = {r["artist_key"]: r["played_at"] for r in con.execute(
                "SELECT artist_key, MIN(played_at) AS played_at FROM plays"
                " GROUP BY artist_key").fetchall()}
            total = defaultdict(int)
            novel = defaultdict(int)
            for r in rows:
                total[r["day"]] += 1
                first = first_seen.get(r["artist_key"])
                if first and (date.fromisoformat(r["day"])
                              - date.fromisoformat(day_key_of(first))
                              ).days <= NOVELTY_WINDOW_DAYS:
                    novel[r["day"]] += 1
            data = [{"day": d, "value": round(novel[d] / total[d], 2)}
                    for d in sorted(total)]

        elif metric == "bracket":
            by_day = defaultdict(list)
            for r in rows:
                by_day[r["day"]].append(r["played_at"])
            data = [{"day": d, "first": min(ts), "last": max(ts)}
                    for d, ts in sorted(by_day.items())]

        elif metric == "top_repeat":
            counts: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
            names = {}
            for r in rows:
                counts[r["day"]][r["track_id"]] += 1
                names[r["track_id"]] = (r["track"], r["artist"])
            data = []
            for d, tc in sorted(counts.items()):
                track_id, count = max(tc.items(), key=lambda kv: kv[1])
                track, artist = names[track_id]
                data.append({"day": d, "track": track, "artist": artist, "count": count})

        elif metric == "shape":
            shape = listen_shape_by_day(con, rng.start_key, rng.end_key)
            data = [{"day": d, **shape[d]} for d in sorted(shape)]

        else:
            raise ValueError(f"unknown metric {metric!r} for spotify;"
                             f" expected one of: {', '.join(METRICS)}")

        return {"metric": metric, "data": data, "refreshed_at": refreshed}


# ---------- compact widget summary ----------

def _top_track(rows) -> dict | None:
    """Most-repeated track among `rows` (today's plays): {track, artist, count}."""
    counts: dict = defaultdict(int)
    names: dict = {}
    for r in rows:
        counts[r["track_id"]] += 1
        names[r["track_id"]] = (r["track"], r["artist"])
    if not counts:
        return None
    tid, count = max(counts.items(), key=lambda kv: kv[1])
    return {"track": names[tid][0], "artist": names[tid][1], "count": count}


def _today_novelty(con, today: str, rows) -> float | None:
    """The `novelty` metric for today: share of plays whose artist's first play is within NOVELTY_WINDOW_DAYS."""
    if not rows:
        return None
    artist_keys = {r["artist_key"] for r in rows if r["artist_key"]}
    if not artist_keys:
        return 0.0
    marks = ",".join("?" * len(artist_keys))
    first_seen = {r["artist_key"]: r["played_at"] for r in con.execute(
        f"SELECT artist_key, MIN(played_at) AS played_at FROM plays"
        f" WHERE artist_key IN ({marks}) GROUP BY artist_key", tuple(artist_keys))}
    today_d = date.fromisoformat(today)
    novel = sum(
        1 for r in rows
        if (fs := first_seen.get(r["artist_key"]))
        and (today_d - date.fromisoformat(day_key_of(fs))).days <= NOVELTY_WINDOW_DAYS)
    return round(novel / len(rows), 2)


def _lastfm():
    from . import lastfm
    return lastfm


def summary() -> dict:
    """Today + rolling-7-day snapshot for the MusicWidget, from the `plays` cache.
    {"connected": False} when unlinked. Zero-listening days are real zeros, not gaps."""
    if not status().get("connected"):
        return {"connected": False}
    today = db.day_key()
    week_start = (date.fromisoformat(today) - timedelta(days=6)).isoformat()
    with db.session() as con:
        ensure_artist_keys(con)
        today_rows = con.execute(
            "SELECT played_at, day, track_id, track, artist, artist_key, ms"
            " FROM plays WHERE day=? ORDER BY played_at", (today,)).fetchall()
        last = today_rows[-1] if today_rows else None

        week_min = {r["day"]: round(r["ms"] / 60000, 1) for r in con.execute(
            "SELECT day, SUM(ms) AS ms FROM plays WHERE day BETWEEN ? AND ?"
            " GROUP BY day", (week_start, today))}
        days = []
        d, end = date.fromisoformat(week_start), date.fromisoformat(today)
        while d <= end:
            k = d.isoformat()
            days.append({"day": k, "value": week_min.get(k, 0.0)})
            d += timedelta(days=1)
        # Baseline daily minutes; zero-fill is right (no plays = didn't listen, not a missed sensor).
        base_start = (date.fromisoformat(today)
                      - timedelta(days=DESK_BASELINE_DAYS - 1)).isoformat()
        base_min = {r["day"]: round(r["ms"] / 60000, 1) for r in con.execute(
            "SELECT day, SUM(ms) AS ms FROM plays WHERE day BETWEEN ? AND ?"
            " GROUP BY day", (base_start, today))}
        baseline = []
        d2, end2 = date.fromisoformat(base_start), date.fromisoformat(today)
        while d2 <= end2:
            k2 = d2.isoformat()
            baseline.append({"day": k2, "value": base_min.get(k2, 0.0)})
            d2 += timedelta(days=1)

        top_artist = con.execute(
            "SELECT artist, COUNT(*) AS c FROM plays"
            " WHERE day BETWEEN ? AND ? AND artist_key IS NOT NULL AND artist_key != ''"
            " GROUP BY artist_key ORDER BY c DESC LIMIT 1",
            (week_start, today)).fetchone()

        return {
            "connected": True,
            "today": {
                "day": today,
                "listen_min": round(sum(r["ms"] for r in today_rows) / 60000, 1),
                "plays": len(today_rows),
                "novelty": _today_novelty(con, today, today_rows),
                "top_track": _top_track(today_rows),
                # Lazy import: lastfm imports spotify, so a top-level import is a cycle.
                "genres": _lastfm().day_mix(con, today),
                "last_played": {
                    "track": last["track"], "artist": last["artist"],
                    "played_at": last["played_at"],
                } if last else None,
            },
            "baseline": baseline,
            "week": {
                "listen_min": round(sum(week_min.values()), 1),
                "days": days,
                "top_artist": {"artist": top_artist["artist"], "plays": top_artist["c"]}
                if top_artist else None,
            },
        }


# ---------- CLI: python -m app.providers.spotify ... ----------

def _authorize(client_id: str | None) -> None:
    base = f"http://127.0.0.1:{BACKEND_PORT}"
    try:
        req = urllib.request.Request(
            base + "/api/oauth/spotify/start",
            data=json.dumps({"client_id": client_id}).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=10) as resp:
            out = json.loads(resp.read())
    except urllib.error.URLError:
        sys.exit(f"can't reach the backend at {base} — start it first"
                  " (CLAUDE.md's Run section) before authorizing")
    except urllib.error.HTTPError as e:
        sys.exit(f"error starting Spotify auth: {e.read().decode(errors='replace')}")
    print(f"Opening {out['url']}")
    webbrowser.open(out["url"])
    deadline = time.monotonic() + 300
    while time.monotonic() < deadline:
        with urllib.request.urlopen(base + "/api/oauth/spotify/status", timeout=10) as resp:
            st = json.loads(resp.read())
        if st.get("connected"):
            print(json.dumps(st, indent=2))
            return
        time.sleep(2)
    sys.exit("timed out waiting for authorization in the browser")


def _main() -> None:
    import argparse
    p = argparse.ArgumentParser(
        prog="python -m app.providers.spotify",
        description="Authorize / inspect the Spotify token (app.db).")
    sub = p.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("authorize", help="interactive PKCE flow via the running backend")
    a.add_argument("--client-id", default=None,
                   help="from developer.spotify.com; omit to reuse the stored one")

    sub.add_parser("status", help="masked token status")

    args = p.parse_args()
    if args.cmd == "authorize":
        _authorize(args.client_id)
    elif args.cmd == "status":
        print(json.dumps(status(), indent=2))


if __name__ == "__main__":
    _main()
