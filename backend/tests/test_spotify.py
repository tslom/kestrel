"""providers/spotify.summary — the MusicWidget's compact one-call payload.
It reads only the `plays` cache: today's minutes/plays/top-track/last-play +
novelty, plus a rolling-7-day sparkline and week top artist. Tests seed `plays`
directly and pin `day_key` so "today" is deterministic; novelty is checked to
match the provider's own `novelty` metric so the two never drift."""

from datetime import date, timedelta

from app import db
from app.providers import spotify
from app.providers.ranges import parse_range

TODAY = "2026-07-22"
NOW = "2026-07-22T10:00:00"


def _connect_spotify(con):
    """A token row with a refresh_token, so status().connected is True."""
    with con:
        con.execute(
            "INSERT INTO tokens (provider, account, client_id, client_secret,"
            " refresh_token, token_uri, updated_at)"
            " VALUES ('spotify', 'personal', 'cid', '', 'rt', 'uri', ?)", (NOW,))


def _play(con, played_at, day, track_id, track, artist, artist_id, ms):
    con.execute(
        "INSERT INTO plays (played_at, day, track_id, track, artist, artist_id,"
        " album, ms, refreshed_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (played_at, day, track_id, track, artist, artist_id, None, ms, NOW))


def _pin_today(monkeypatch, day=TODAY):
    # Only the no-arg "today" call is pinned; day_key(dt) (used by _day_key_of
    # to bucket a play's instant) must keep computing the real key.
    real = db.day_key
    monkeypatch.setattr(db, "day_key",
                        lambda *a, **k: day if not a and not k else real(*a, **k))


def test_summary_not_connected(dbfile):
    assert spotify.summary() == {"connected": False}


def test_summary_today_and_week(dbfile, monkeypatch):
    _pin_today(monkeypatch)
    con = db.connect()
    _connect_spotify(con)
    with con:
        # today: 3 plays, "Loop" twice (top track), last play at 15:00.
        _play(con, f"{TODAY}T09:00:00Z", TODAY, "t1", "Loop", "A", "aid", 180000)
        _play(con, f"{TODAY}T12:00:00Z", TODAY, "t1", "Loop", "A", "aid", 180000)
        _play(con, f"{TODAY}T15:00:00Z", TODAY, "t2", "Solo", "B", "bid", 60000)
        # earlier in the rolling week: one play 3 days ago.
        older = (date.fromisoformat(TODAY) - timedelta(days=3)).isoformat()
        _play(con, f"{older}T10:00:00Z", older, "t3", "Old", "A", "aid", 120000)
    con.close()

    s = spotify.summary()
    assert s["connected"] is True
    t = s["today"]
    assert t["day"] == TODAY
    assert t["plays"] == 3
    assert t["listen_min"] == 7.0                     # (180+180+60)s*1000 / 60000
    assert t["top_track"] == {"track": "Loop", "artist": "A", "count": 2}
    assert t["last_played"]["track"] == "Solo"        # latest played_at
    w = s["week"]
    assert w["listen_min"] == 9.0                      # 7 today + 2 (older 120s)
    assert len(w["days"]) == 7                          # rolling 7-day sparkline
    assert w["days"][-1] == {"day": TODAY, "value": 7.0}
    # artist A appears 3x across the week (2 today + 1 older) vs B once.
    assert w["top_artist"] == {"artist": "A", "plays": 3}


def test_summary_empty_today_is_no_plays(dbfile, monkeypatch):
    _pin_today(monkeypatch)
    con = db.connect()
    _connect_spotify(con)
    con.close()
    t = spotify.summary()["today"]
    assert t["plays"] == 0 and t["listen_min"] == 0.0
    assert t["top_track"] is None and t["last_played"] is None
    assert t["novelty"] is None                         # no plays -> gap, not 0


def test_summary_novelty_matches_get_metric(dbfile, monkeypatch):
    _pin_today(monkeypatch)
    con = db.connect()
    _connect_spotify(con)
    with con:
        # A first seen today (novel); B first seen 30 days ago (not novel).
        old = (date.fromisoformat(TODAY) - timedelta(days=30)).isoformat()
        _play(con, f"{old}T10:00:00Z", old, "t9", "B-old", "B", "bid", 60000)
        _play(con, f"{TODAY}T09:00:00Z", TODAY, "t1", "Loop", "A", "aid", 60000)
        _play(con, f"{TODAY}T12:00:00Z", TODAY, "t2", "Solo", "B", "bid", 60000)
    con.close()

    summary_nov = spotify.summary()["today"]["novelty"]
    get_nov = spotify.get(parse_range(f"{TODAY}..{TODAY}"), "novelty")["data"]
    assert get_nov == [{"day": TODAY, "value": summary_nov}]
    assert summary_nov == 0.5                            # 1 of 2 today's plays novel
