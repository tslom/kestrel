"""Listening *shape* (§8's "richer derived signals") — repeat_ratio,
listen_sessions, longest_listen_min.

`listen_min` says how much you listened; none of it says how. The contract
worth pinning: sessions split on silence and never on track length, the
obsession measure keys on track (an album front-to-back is not one song
eleven times), thin days are a gap rather than a fake 0.0, and features.py
calls the provider instead of re-expressing any of it.
"""


from app.providers import features, spotify
from app.providers.ranges import parse_range

DAY = "2026-05-12"
NOW = "2026-05-13T10:00:00"


def _seed(con, plays, day=DAY):
    """plays: (hh, mm, track_id). played_at is UTC like both real writers;
    the day column is passed straight through, as the poller does."""
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
    """Two runs either side of a gap longer than LISTEN_GAP_MIN are two
    sittings; the span reported is the longer one."""
    _seed(con, _run(4, start_h=9)                       # 09:00..09:09
               + _run(4, start_h=13, track="u"))        # 13:00..13:09
    out = spotify.listen_shape_by_day(con, DAY, DAY)[DAY]
    assert out["listen_sessions"] == 2.0
    assert out["longest_listen_min"] == 9.0


def test_a_normal_album_is_one_session(con):
    """The gap threshold sits above any single track, so plays that follow
    each other never split — only real silence does."""
    _seed(con, _run(12))
    out = spotify.listen_shape_by_day(con, DAY, DAY)[DAY]
    assert out["listen_sessions"] == 1.0
    assert out["longest_listen_min"] == 33.0


def test_gap_boundary_is_inclusive(con):
    """Exactly LISTEN_GAP_MIN of silence starts a new session — the rule is
    ">= gap", and a threshold nobody has pinned drifts."""
    base = _run(5)
    later = [(9 + (12 + spotify.LISTEN_GAP_MIN) // 60,
              (12 + spotify.LISTEN_GAP_MIN) % 60, "z")]
    _seed(con, base + later)                            # last base play 09:12
    assert spotify.listen_shape_by_day(con, DAY, DAY)[DAY]["listen_sessions"] == 2.0


def test_repeat_ratio_is_per_track_not_per_artist(con):
    """One song on loop and an album by the same artist are different
    behaviours; grouping by artist can't tell them apart."""
    _seed(con, [(9, i * 3, "loop") for i in range(6)] + [(9, 21, "other")])
    assert spotify.listen_shape_by_day(con, DAY, DAY)[DAY]["repeat_ratio"] == \
        round(6 / 7, 3)


def test_shuffle_day_reads_low(con):
    """The other end of the same measure: eight distinct tracks is 0.125,
    not a number that only says 'you listened'."""
    _seed(con, _run(8))
    assert spotify.listen_shape_by_day(con, DAY, DAY)[DAY]["repeat_ratio"] == 0.125


def test_thin_days_stay_a_gap(con):
    """Under SHAPE_MIN_PLAYS there's no shape to measure: repeat_ratio over
    two plays can only be 0.5 or 1.0. Absent, not zero — the GENRE_MIN_PLAYS
    precedent."""
    _seed(con, _run(spotify.SHAPE_MIN_PLAYS - 1))
    assert spotify.listen_shape_by_day(con, DAY, DAY) == {}


def test_single_play_session_spans_zero(con):
    """A lone play after a long silence is a session of length 0.0, which is
    honest — no sustained block happened. It must not inherit the ms of the
    track: the two writers disagree about what played_at means."""
    _seed(con, _run(5) + [(15, 0, "lonely")])
    out = spotify.listen_shape_by_day(con, DAY, DAY)[DAY]
    assert out["listen_sessions"] == 2.0
    assert out["longest_listen_min"] == 12.0          # the first run, not the loner


def test_days_are_independent(con):
    """Plays are grouped by the stored day column, so a run either side of
    the 4am rollover never merges into one session."""
    _seed(con, _run(6), day=DAY)
    _seed(con, _run(6), day="2026-05-13")
    out = spotify.listen_shape_by_day(con, DAY, "2026-05-13")
    assert set(out) == {DAY, "2026-05-13"}
    assert all(v["listen_sessions"] == 1.0 for v in out.values())


def test_provider_and_features_never_drift(con):
    """features.py calls the provider rather than re-expressing the math —
    the mistake that let `novelty` drift for twelve days (v35)."""
    _seed(con, _run(4) + _run(4, start_h=13, track="u"))
    from_metric = spotify.get(parse_range(f"{DAY}..{DAY}"), "shape")["data"]
    from_features = {f: v for d, f, v in features._listen_shape_rows(con, [DAY])
                     if d == DAY}
    assert from_features == {k: v for k, v in from_metric[0].items() if k != "day"}


def test_features_only_yields_days_it_was_asked_for(con):
    """The caller's DELETE only covers the requested days, so a row outside
    them would never be recomputed."""
    _seed(con, _run(6), day=DAY)
    _seed(con, _run(6), day="2026-05-13")
    days = {d for d, _, _ in features._listen_shape_rows(con, [DAY])}
    assert days == {DAY}
