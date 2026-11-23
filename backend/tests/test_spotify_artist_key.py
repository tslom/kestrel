"""providers/spotify.artist_key — the identity that survives the import seam.

`plays.artist_id` holds Spotify's real id on rows the poller wrote and a
synthetic "sha:" hash on the ~266k rows the GDPR importer wrote, because
neither export carries an artist id. Grouping on it split every artist in two
at the seam, which pinned `novelty` at ~1.0 for twelve days — a six-year
favourite counted as brand-new the first time the poller saw it. These tests
pin the fix: one artist, two ids, one key.
"""

from datetime import date, timedelta

import pytest

from app import db
from app.providers import spotify
from app.providers.ranges import parse_range

NOW = "2026-07-22T10:00:00"


def _pin_today(monkeypatch, day):
    """Pin only the no-arg "today" call — day_key(dt) still has to compute the
    real key, because _day_key_of uses it to bucket each play's instant (same
    caveat as test_spotify.py)."""
    real = db.day_key
    monkeypatch.setattr(db, "day_key",
                        lambda *a, **k: day if not a and not k else real(*a, **k))


def _play(con, played_at, day, artist, artist_id, *, track="t", ms=180000,
          artist_key=None):
    """Insert deliberately without artist_key unless asked — that's the state
    every pre-v35 row is in, and what ensure_artist_keys has to repair."""
    with con:
        con.execute(
            "INSERT INTO plays (played_at, day, track_id, track, artist,"
            " artist_id, artist_key, album, ms, refreshed_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (played_at, day, track, track, artist, artist_id, artist_key,
             None, ms, NOW))


@pytest.mark.parametrize("raw,expected", [
    ("Radiohead", "radiohead"),
    ("RADIOHEAD", "radiohead"),
    ("  Radiohead  ", "radiohead"),
    ("Radio  head", "radio head"),        # internal whitespace collapses
    ("Vîęzë Ãsbæk", "vîęzë ãsbæk"),        # casefold handles non-ASCII...
    ("VÎĘZË ÃSBÆK", "vîęzë ãsbæk"),        # ...which SQLite's lower() would not
])
def test_artist_key_normalizes(raw, expected):
    assert spotify.artist_key(raw) == expected


def test_sqlite_lower_would_not_have_worked(dbfile):
    """Why the key is computed in Python. If SQLite's lower() could do this,
    artist_key could have been a plain SQL expression in the migration."""
    con = db.connect()
    row = con.execute("SELECT lower(?) AS l", ("VÎĘZË ÃSBÆK",)).fetchone()
    con.close()
    assert row["l"] != "vîęzë ãsbæk"        # ASCII-only: the accents keep their case
    assert spotify.artist_key("VÎĘZË ÃSBÆK") == "vîęzë ãsbæk"


def test_ensure_artist_keys_backfills_and_is_idempotent(dbfile):
    con = db.connect()
    _play(con, "2026-07-20T10:00:00Z", "2026-07-20", "Logic", "sha:abc")
    _play(con, "2026-07-21T10:00:00Z", "2026-07-21", "LOGIC", "real-id")

    assert spotify.ensure_artist_keys(con) == 2
    keys = {r["artist_key"] for r in con.execute("SELECT artist_key FROM plays")}
    assert keys == {"logic"}               # both spellings, both writers, one key

    assert spotify.ensure_artist_keys(con) == 0   # nothing left to do
    con.close()


def test_novelty_survives_the_import_seam(dbfile, monkeypatch):
    """The regression. An artist listened to for months under the importer's
    synthetic id must not read as novel when the poller first sees them under
    Spotify's real id."""
    today = "2026-07-22"
    _pin_today(monkeypatch, today)
    con = db.connect()

    # Six months of history, importer-style: synthetic artist_id.
    old = (date.fromisoformat(today) - timedelta(days=180)).isoformat()
    _play(con, f"{old}T10:00:00Z", old, "Logic", "sha:logic")
    # Today, poller-style: same artist, Spotify's real id.
    _play(con, f"{today}T10:00:00Z", today, "Logic", "7hJcb9fa4alzcOq3EaNPoG")
    con.close()

    out = spotify.get(parse_range(f"{today}..{today}"), "novelty")
    assert out["data"] == [{"day": today, "value": 0.0}]


def test_novelty_still_counts_a_genuinely_new_artist(dbfile, monkeypatch):
    """The fix must not flatten novelty to zero — an artist with no history is
    still novel."""
    today = "2026-07-22"
    _pin_today(monkeypatch, today)
    con = db.connect()
    old = (date.fromisoformat(today) - timedelta(days=180)).isoformat()
    _play(con, f"{old}T10:00:00Z", old, "Logic", "sha:logic")
    _play(con, f"{today}T10:00:00Z", today, "Logic", "real-logic")
    _play(con, f"{today}T11:00:00Z", today, "Somebody New", "real-new")
    con.close()

    out = spotify.get(parse_range(f"{today}..{today}"), "novelty")
    assert out["data"] == [{"day": today, "value": 0.5}]   # 1 of 2 plays


def test_summary_top_artist_unifies_across_the_seam(dbfile, monkeypatch):
    """Two ids for one artist also split the week's top-artist count, which is
    how an artist you played 30 times could lose to one you played 20 times."""
    today = "2026-07-22"
    _pin_today(monkeypatch, today)
    con = db.connect()
    with con:
        con.execute(
            "INSERT INTO tokens (provider, account, client_id, client_secret,"
            " refresh_token, token_uri, updated_at)"
            " VALUES ('spotify','personal','cid','','rt','uri',?)", (NOW,))
    # Logic: 3 plays split 2/1 across two ids. Drake: 2 plays under one id.
    _play(con, f"{today}T09:00:00Z", today, "Logic", "sha:logic")
    _play(con, f"{today}T09:30:00Z", today, "Logic", "sha:logic")
    _play(con, f"{today}T10:00:00Z", today, "Logic", "real-logic")
    _play(con, f"{today}T11:00:00Z", today, "Drake", "real-drake")
    _play(con, f"{today}T12:00:00Z", today, "Drake", "real-drake")
    con.close()

    top = spotify.summary()["week"]["top_artist"]
    assert top["artist"] == "Logic" and top["plays"] == 3
