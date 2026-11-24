"""One-time (rerunnable) backfill: a Spotify GDPR export -> the `plays` table.

Handles both exports Spotify hands out, auto-detected:

  * Extended Streaming History  (`Streaming_History_Audio_*.json`) — the good
    one: full history back to account creation, second-resolution `ts` (UTC),
    real `spotify_track_uri`, album names, ms_played. Preferred.
  * Account Data                (`StreamingHistory_music_*.json`) — ~1 year,
    minute resolution, {endTime, artistName, trackName, msPlayed}, no IDs/album.

Neither export carries an *artist* id (only a name), so artist_id is always
synthesized as "sha:"+sha1(artist)[:16] — stable per artist, but it can never
agree with the real Spotify ids the poller writes. Grouping on artist_id
therefore splits every artist at the seam; `artist_key` (the normalized name,
spotify.artist_key) is what the provider actually groups on, and it's written
here too so an import needs no follow-up backfill. track_id is the real Spotify
id for the Extended export and a synthetic "sh:"+sha1(artist\\ntrack)[:16] for
the Account Data one.

played_at is the table PRIMARY KEY. Spotify timestamps aren't unique — the
Extended export logs many rapid skips within the same second, the Account one
only has minute resolution — so we pack a deterministic hash of the record's
identity into the microseconds field. That keeps distinct plays that share a
timestamp (recovering ~22k skip rows), collapses true duplicates, stays
fromisoformat-parseable, and sorts chronologically against live "...Z" rows.

Reconciliation (why this is safe to run over a live `plays` table): an export
is authoritative over the span it covers, [min_ts, max_ts]. We DELETE existing
plays within that span — the previous synthetic `sh:` backfill and the ~1 day
where live polling overlaps the export's tail — then insert the export. Live
rows *after* max_ts (everything the poller has captured since the export was
generated) are untouched, so history stays continuous with no double-counting.
Re-runs are idempotent: the poller only ever adds rows newer than max_ts, so
the same DELETE+INSERT reproduces the same state.

Usage:
    python scripts/import_spotify_history.py                       # ~/Downloads/my_spotify_data (2).zip, else my_spotify_data.zip
    python scripts/import_spotify_history.py /path/to/export.zip   # or the unzipped folder
    python scripts/import_spotify_history.py --dry-run
"""

import argparse
import hashlib
import io
import json
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import db
from app.providers import spotify

DL = Path.home() / "Downloads"
DEFAULT_SOURCES = [DL / "my_spotify_data (2).zip", DL / "my_spotify_data.zip"]
EXTENDED_PREFIX = "Streaming_History_Audio_"   # Extended Streaming History
ACCOUNT_PREFIX = "StreamingHistory_music_"     # Account Data


def _sha16(s: str) -> str:
    return hashlib.sha1(s.encode()).hexdigest()[:16]


def _micro(*parts) -> str:
    """Deterministic 6-digit disambiguator for the microseconds field."""
    h = hashlib.sha1("\n".join(str(p) for p in parts).encode()).hexdigest()
    return f"{int(h, 16) % 1_000_000:06d}"


def _extended_row(rec: dict, now: str) -> dict | None:
    uri = rec.get("spotify_track_uri")
    if not uri:  # podcast / audiobook / unknown — not a music play
        return None
    artist = (rec.get("master_metadata_album_artist_name") or "").strip()
    track = (rec.get("master_metadata_track_name") or "").strip()
    ts = rec.get("ts")
    if not artist or not track or not ts:
        return None
    ms = int(rec.get("ms_played") or 0)
    played_at = (f"{ts[:-1]}.{_micro(uri, ms, rec.get('reason_start'), rec.get('reason_end'))}"
                 "+00:00")  # ts is "...ssZ"; swap Z for the hashed micro + offset
    return {
        "played_at": played_at,
        "day": spotify.day_key_of(played_at),
        "track_id": uri.rsplit(":", 1)[-1],
        "track": track,
        "artist": artist,
        "artist_id": "sha:" + _sha16(artist.lower()),
        "artist_key": spotify.artist_key(artist),
        "album": (rec.get("master_metadata_album_album_name") or "").strip() or None,
        "ms": ms,
        "refreshed_at": now,
    }


def _account_row(rec: dict, now: str) -> dict | None:
    artist = (rec.get("artistName") or "").strip()
    track = (rec.get("trackName") or "").strip()
    end = rec.get("endTime")
    if not artist or not track or not end:
        return None
    try:
        dt = datetime.strptime(end, "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    ms = int(rec.get("msPlayed") or 0)
    played_at = f"{dt:%Y-%m-%dT%H:%M}:00.{_micro(artist, track, ms)}+00:00"
    return {
        "played_at": played_at,
        "day": spotify.day_key_of(played_at),
        "track_id": "sh:" + _sha16(f"{artist.lower()}\n{track.lower()}"),
        "track": track,
        "artist": artist,
        "artist_id": "sha:" + _sha16(artist.lower()),
        "artist_key": spotify.artist_key(artist),
        "album": None,
        "ms": ms,
        "refreshed_at": now,
    }


def _iter_json(source: Path, prefix: str):
    """Yield parsed JSON arrays from each file named `prefix*`, in a .zip or a folder."""
    if source.is_dir():
        for p in sorted(source.rglob("*.json")):
            if p.name.startswith(prefix):
                yield json.loads(p.read_text())
    elif source.suffix == ".zip":
        with zipfile.ZipFile(source) as zf:
            for n in sorted(zf.namelist()):
                if Path(n).name.startswith(prefix):
                    yield json.loads(io.BytesIO(zf.read(n)).read())
    else:
        sys.exit(f"source must be a .zip or a folder: {source}")


def _load(source: Path) -> tuple[str, list[dict]]:
    """Detect the export flavor and return (label, mapped rows), Extended first."""
    now = datetime.now().isoformat(timespec="seconds")
    for label, prefix, mapper in (
        ("extended", EXTENDED_PREFIX, _extended_row),
        ("account", ACCOUNT_PREFIX, _account_row),
    ):
        rows = [row for arr in _iter_json(source, prefix)
                for rec in arr if (row := mapper(rec, now))]
        if rows:
            # collapse any exact PK collisions the disambiguator didn't split
            return label, list({r["played_at"]: r for r in rows}.values())
    sys.exit(f"no Spotify streaming-history JSON found in {source}"
             f" (looked for {EXTENDED_PREFIX}* and {ACCOUNT_PREFIX}*)")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("source", nargs="?", type=Path, default=None,
                    help="Spotify export .zip or its unzipped folder"
                         " (default: newest my_spotify_data*.zip in ~/Downloads)")
    ap.add_argument("--dry-run", action="store_true",
                    help="report what would change without writing")
    args = ap.parse_args()

    source = args.source or next((p for p in DEFAULT_SOURCES if p.exists()), None)
    if source is None or not source.exists():
        sys.exit(f"no such export: {source or ' / '.join(map(str, DEFAULT_SOURCES))}")

    label, rows = _load(source)
    max_played = max(r["played_at"] for r in rows)
    span = (min(r["played_at"] for r in rows)[:10], max_played[:10])

    con = db.connect()
    try:
        doomed = con.execute(
            "SELECT COUNT(*) AS c FROM plays WHERE played_at <= ?", (max_played,)
        ).fetchone()["c"]
        if args.dry_run:
            print(f"[dry-run] {label} export: {len(rows)} plays ({span[0]} .. {span[1]});"
                  f" would delete {doomed} rows at/before {max_played[:19]}"
                  f" (old backfill + live overlap) and reinsert")
            return
        with con:
            con.execute("DELETE FROM plays WHERE played_at <= ?", (max_played,))
            con.executemany(
                "INSERT INTO plays (played_at, day, track_id, track, artist,"
                " artist_id, artist_key, album, ms, refreshed_at)"
                " VALUES (:played_at,:day,:track_id,:track,:artist,:artist_id,"
                ":artist_key,:album,:ms,:refreshed_at)", rows)
        total = con.execute("SELECT COUNT(*) AS c FROM plays").fetchone()["c"]
        print(f"imported {len(rows)} {label} plays ({span[0]} .. {span[1]});"
              f" replaced {doomed} rows in-span; plays now holds {total}")
    finally:
        con.close()


if __name__ == "__main__":
    main()
