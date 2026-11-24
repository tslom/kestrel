"""Listening shape (repeat_ratio, listen_sessions, longest_listen_min): sessions split on silence not track length,
repeat keys on track, thin days are a gap, features.py calls the provider. Design: DESIGN.md → Spotify."""


from app.providers import features, spotify
from app.providers.ranges import parse_range

DAY = "2026-05-12"
NOW = "2026-05-13T10:00:00"


def _seed(con, plays, day=DAY):
    """plays: (hh, mm, track_id); played_at in UTC like the writers, day column passed through."""
    with con:
        for hh, mm, track in plays:
            at = f"{day}T{hh:02d}:{mm:02d}:00+00:00"
            con.execute(
                "INSERT INTO plays (played_at, day, track_id, track, artist,"
                " artist_id, artist_key, album, ms, refreshed_at)"
                " VALUES (?,?,?,?,?,?,?,NULL,180000,?)",
                (at, day, track, track, "A", "sha:a", "a", NOW))


def _run(n, start_h=9, track="t"):
    """n plays three minutes apart — one uninterrupted session."""
    return [(start_h + (i * 3) // 60, (i * 3) % 60, f"{track}{i}") for i in range(n)]


def test_sessions_split_on_silence(con):
    """A gap over LISTEN_GAP_MIN splits sittings; the longer span is reported."""
    _seed(con, _run(4, start_h=9)                       # 09:00..09:09
               + _run(4, start_h=13, track="u"))        # 13:00..13:09
    out = spotify.listen_shape_by_day(con, DAY, DAY)[DAY]
    assert out["listen_sessions"] == 2.0
    assert out["longest_listen_min"] == 9.0


def test_a_normal_album_is_one_session(con):
    """Back-to-back plays never split."""
    _seed(con, _run(12))
    out = spotify.listen_shape_by_day(con, DAY, DAY)[DAY]
    assert out["listen_sessions"] == 1.0
    assert out["longest_listen_min"] == 33.0


def test_gap_boundary_is_inclusive(con):
    """Exactly LISTEN_GAP_MIN of silence starts a new session (>=)."""
    base = _run(5)
    later = [(9 + (12 + spotify.LISTEN_GAP_MIN) // 60,
              (12 + spotify.LISTEN_GAP_MIN) % 60, "z")]
    _seed(con, base + later)                            # last base play 09:12
    assert spotify.listen_shape_by_day(con, DAY, DAY)[DAY]["listen_sessions"] == 2.0


def test_repeat_ratio_is_per_track_not_per_artist(con):
    """One song on loop differs from an album by one artist (repeat keys on track)."""
    _seed(con, [(9, i * 3, "loop") for i in range(6)] + [(9, 21, "other")])
    assert spotify.listen_shape_by_day(con, DAY, DAY)[DAY]["repeat_ratio"] == \
        round(6 / 7, 3)


def test_shuffle_day_reads_low(con):
    """Eight distinct tracks is 0.125."""
    _seed(con, _run(8))
    assert spotify.listen_shape_by_day(con, DAY, DAY)[DAY]["repeat_ratio"] == 0.125


def test_thin_days_stay_a_gap(con):
    """Under SHAPE_MIN_PLAYS the day is absent, not zero."""
    _seed(con, _run(spotify.SHAPE_MIN_PLAYS - 1))
    assert spotify.listen_shape_by_day(con, DAY, DAY) == {}


def test_single_play_session_spans_zero(con):
    """A lone play is a session of length 0.0, not the track's duration (the writers disagree on played_at)."""
    _seed(con, _run(5) + [(15, 0, "lonely")])
    out = spotify.listen_shape_by_day(con, DAY, DAY)[DAY]
    assert out["listen_sessions"] == 2.0
    assert out["longest_listen_min"] == 12.0          # the first run, not the loner


def test_days_are_independent(con):
    """Sessions group by the stored day column, so the 4am rollover splits them."""
    _seed(con, _run(6), day=DAY)
    _seed(con, _run(6), day="2026-05-13")
    out = spotify.listen_shape_by_day(con, DAY, "2026-05-13")
    assert set(out) == {DAY, "2026-05-13"}
    assert all(v["listen_sessions"] == 1.0 for v in out.values())


def test_provider_and_features_never_drift(con):
    """features.py calls the provider rather than re-expressing the math."""
    _seed(con, _run(4) + _run(4, start_h=13, track="u"))
    from_metric = spotify.get(parse_range(f"{DAY}..{DAY}"), "shape")["data"]
    from_features = {f: v for d, f, v in features._listen_shape_rows(con, [DAY])
                     if d == DAY}
    assert from_features == {k: v for k, v in from_metric[0].items() if k != "day"}


def test_features_only_yields_days_it_was_asked_for(con):
    """Only rows for the requested days are returned (the caller's DELETE covers just those)."""
    _seed(con, _run(6), day=DAY)
    _seed(con, _run(6), day="2026-05-13")
    days = {d for d, _, _ in features._listen_shape_rows(con, [DAY])}
    assert days == {DAY}
