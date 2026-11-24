"""providers/lastfm: Last.fm artist.getTopTags joined on `plays.artist_key` (the name); no network, _api is stubbed.

Design: DESIGN.md → `tests/test_lastfm.py`.
"""

import json

import pytest

from app import config, db
from app.providers import lastfm
from app.providers.ranges import parse_range

NOW = "2026-07-22T10:00:00"
DAY = "2026-07-22"


def _play(con, played_at, artist, *, day=DAY):
    with con:
        con.execute(
            "INSERT INTO plays (played_at, day, track_id, track, artist,"
            " artist_id, album, ms, refreshed_at)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (played_at, day, "t", "t", artist, "id", None, 180000, NOW))


def _tags(con, artist_key, tags, *, name=None, missing=0, raw=None):
    with con:
        con.execute(
            "INSERT INTO artist_tags (artist_key, name, tags, raw, mbid,"
            " refreshed_at, missing) VALUES (?,?,?,?,NULL,?,?)",
            (artist_key, name or artist_key, json.dumps(tags),
             json.dumps(raw) if raw is not None else None, NOW, missing))


@pytest.fixture
def key(monkeypatch):
    """Pretend a LASTFM_API_KEY is configured (patch config in the provider too)."""
    monkeypatch.setattr(config, "LASTFM_API_KEY", "k", raising=False)
    monkeypatch.setattr(lastfm, "LASTFM_API_KEY", "k")


# ---------- tag cleaning ----------

def test_clean_tags_drops_weak_and_chattery():
    raw = [
        {"name": "Hip-Hop", "count": 100},
        {"name": "seen live", "count": 90},     # blocklisted chatter
        {"name": "Trap", "count": 40},
        {"name": "obscure-listener-note", "count": 3},   # under the weight floor
    ]
    assert lastfm._clean_tags(raw) == ["hip hop", "trap"]


def test_clean_tags_caps_and_preserves_rank(monkeypatch):
    monkeypatch.setattr(lastfm, "LASTFM_MAX_TAGS", 3)
    raw = [{"name": f"g{i}", "count": 100 - i} for i in range(10)]
    assert lastfm._clean_tags(raw) == ["g0", "g1", "g2"]


def test_clean_tags_survives_junk_weights():
    raw = [{"name": "rock", "count": None}, {"name": "pop", "count": "50"},
           {"name": "", "count": 99}]
    assert lastfm._clean_tags(raw) == ["pop"]


def test_clean_tags_falls_back_to_rank_when_unweighted():
    """The sample response has no `count`; missing weights must not drop every tag (the list is popularity-ordered)."""
    raw = [{"name": "Hip-Hop", "url": "u"}, {"name": "seen live", "url": "u"},
           {"name": "Trap", "url": "u"}]
    assert lastfm._clean_tags(raw) == ["hip hop", "trap"]


def test_clean_tags_dedupes_punctuation_variants():
    """Drake returns "hip-hop" and "hip hop"; normalise so they aren't two genres."""
    raw = [{"name": "Hip-Hop", "count": 100}, {"name": "rap", "count": 90},
           {"name": "hip hop", "count": 80}, {"name": "Hip_Hop", "count": 70}]
    assert lastfm._clean_tags(raw) == ["hip hop", "rap"]


def test_clean_tags_drops_the_artists_own_name():
    raw = [{"name": "hip-hop", "count": 100}, {"name": "Drake", "count": 95}]
    assert lastfm._clean_tags(raw, "Drake") == ["hip hop"]
    # ...but only that artist's name — it's a real genre for someone else.
    assert lastfm._clean_tags(raw, "Logic") == ["hip hop", "drake"]


def test_clean_tags_drops_bare_nationalities_but_keeps_compounds():
    raw = [{"name": "UK Garage", "count": 100}, {"name": "british", "count": 90},
           {"name": "United States", "count": 80}, {"name": "j-pop", "count": 70}]
    assert lastfm._clean_tags(raw) == ["uk garage", "j pop"]


# ---------- enrichment ----------

def test_enrich_tags_most_played_first(dbfile, key, monkeypatch):
    con = db.connect()
    _play(con, f"{DAY}T09:00:00Z", "Drake")
    _play(con, f"{DAY}T10:00:00Z", "Logic")
    _play(con, f"{DAY}T11:00:00Z", "Logic")      # Logic leads 2-1
    con.close()

    asked = []

    def fake_api(method, params):
        asked.append(params["artist"])
        return {"toptags": {"tag": [{"name": "Rap", "count": 100}],
                            "@attr": {"artist": params["artist"]}}}

    monkeypatch.setattr(lastfm, "_api", fake_api)
    out = lastfm.enrich(1, pause=0)

    assert asked == ["Logic"]                    # the artist that carries the listening
    assert out == {"looked_up": 1, "tagged": 1, "unknown": 0, "failed": 0}
    con = db.connect()
    row = con.execute("SELECT * FROM artist_tags").fetchone()
    con.close()
    assert row["artist_key"] == "logic" and json.loads(row["tags"]) == ["rap"]


def test_enrich_prioritises_todays_listening_over_the_backlog(dbfile, key, monkeypatch):
    """Play count alone starves today: a once-tried artist queues behind the backlog and today's coverage reads 0%."""
    con = db.connect()
    old = "2026-01-01"
    for i in range(50):                     # a heavyweight from the archive
        _play(con, f"{old}T{i // 60:02d}:{i % 60:02d}:00Z", "Old Favourite", day=old)
    _play(con, f"{DAY}T09:00:00Z", "Heard Once Today")
    con.close()

    monkeypatch.setattr(db, "day_key", lambda *a, **k: DAY)
    con = db.connect()
    assert [a for _, a in lastfm._untagged(con, 2)] == \
        ["Heard Once Today", "Old Favourite"]
    con.close()


def test_enrich_marks_unknown_artist_so_it_is_not_retried(dbfile, key, monkeypatch):
    con = db.connect()
    _play(con, f"{DAY}T09:00:00Z", "Nobody At All")
    con.close()
    monkeypatch.setattr(lastfm, "_api", lambda m, p: {"toptags": {"tag": []}})

    assert lastfm.enrich(5, pause=0)["unknown"] == 1
    con = db.connect()
    assert con.execute("SELECT missing FROM artist_tags").fetchone()["missing"] == 1
    # already looked up -> not offered again, so the trickle moves on
    assert lastfm._untagged(con, 5) == []
    con.close()


def test_unknown_artist_is_settled_not_retried_forever(dbfile, key, monkeypatch):
    """Last.fm error 6 is a settled answer, not a failure (it re-requested real artists every cycle with a `degraded` event each)."""
    con = db.connect()
    _play(con, f"{DAY}T09:00:00Z", "Cree+")
    con.close()

    def not_found(method, params):
        raise lastfm.NotFound("The artist you supplied could not be found")

    monkeypatch.setattr(lastfm, "_api", not_found)
    assert lastfm.enrich(5, pause=0) == {
        "looked_up": 1, "tagged": 0, "unknown": 1, "failed": 0}

    con = db.connect()
    assert con.execute("SELECT missing FROM artist_tags").fetchone()["missing"] == 1
    assert lastfm._untagged(con, 5) == []        # never offered again
    con.close()


def test_api_only_treats_error_6_as_not_found(dbfile, key, monkeypatch):
    """A rate-limit or outage stays retryable, never filed as 'unknown artist'."""
    monkeypatch.setattr(lastfm.urllib.request, "urlopen",
                        lambda *a, **k: pytest.fail("should not reach network"))
    for code, exc in ((6, lastfm.NotFound), (29, RuntimeError)):
        monkeypatch.setattr(
            lastfm.json, "loads", lambda _b, c=code: {"error": c, "message": "x"})

        class _Resp:
            def read(self): return b"{}"
            def __enter__(self): return self
            def __exit__(self, *a): return False

        monkeypatch.setattr(lastfm.urllib.request, "urlopen", lambda *a, **k: _Resp())
        with pytest.raises(exc):
            lastfm._api("artist.gettoptags", {"artist": "x"})


def test_enrich_survives_one_bad_artist(dbfile, key, monkeypatch):
    con = db.connect()
    _play(con, f"{DAY}T09:00:00Z", "Good")
    _play(con, f"{DAY}T10:00:00Z", "Bad")
    _play(con, f"{DAY}T11:00:00Z", "Bad")
    con.close()

    def fake_api(method, params):
        if params["artist"] == "Bad":
            raise RuntimeError("last.fm artist.gettoptags 500: boom")
        return {"toptags": {"tag": [{"name": "Rock", "count": 100}]}}

    monkeypatch.setattr(lastfm, "_api", fake_api)
    out = lastfm.enrich(5, pause=0)
    assert out["failed"] == 1 and out["tagged"] == 1   # the batch carried on


def test_reclean_reapplies_filters_without_the_network(dbfile, key, monkeypatch):
    """Raw is stored so revising the blocklist costs no re-lookup (9.8k)."""
    con = db.connect()
    _play(con, f"{DAY}T09:00:00Z", "Logic")
    con.close()
    monkeypatch.setattr(lastfm, "_api", lambda m, p: {"toptags": {"tag": [
        {"name": "hip-hop", "count": 100}, {"name": "biracial", "count": 90}]}})
    lastfm.enrich(1, pause=0)

    con = db.connect()
    assert json.loads(con.execute("SELECT tags FROM artist_tags").fetchone()["tags"]) \
        == ["hip hop", "biracial"]
    con.close()

    # Decide "biracial" isn't a genre, then re-derive offline.
    monkeypatch.setattr(lastfm, "TAG_BLOCKLIST", lastfm.TAG_BLOCKLIST | {"biracial"})
    monkeypatch.setattr(lastfm, "_api", lambda *a, **k: pytest.fail("called out"))
    assert lastfm.reclean() == {"rows": 1, "changed": 1, "skipped": 0}

    con = db.connect()
    assert json.loads(con.execute("SELECT tags FROM artist_tags").fetchone()["tags"]) \
        == ["hip hop"]
    con.close()


def test_reclean_leaves_pre_v36_rows_alone(dbfile):
    """Rows fetched before raw was stored can't be re-derived: skipped, not blanked."""
    con = db.connect()
    _tags(con, "logic", ["hip-hop"])          # no raw
    con.close()
    assert lastfm.reclean() == {"rows": 1, "changed": 0, "skipped": 1}
    con = db.connect()
    assert json.loads(con.execute("SELECT tags FROM artist_tags").fetchone()["tags"]) \
        == ["hip-hop"]
    con.close()


def test_refresh_noops_without_a_key(dbfile, monkeypatch):
    monkeypatch.setattr(lastfm, "LASTFM_API_KEY", None)
    monkeypatch.setattr(lastfm, "_api", lambda *a, **k: pytest.fail("called out"))
    assert lastfm.refresh() == {"skipped": "no LASTFM_API_KEY"}


# ---------- reads ----------

def test_genres_ranks_by_plays(dbfile):
    con = db.connect()
    _play(con, f"{DAY}T09:00:00Z", "Logic")
    _play(con, f"{DAY}T10:00:00Z", "Logic")
    _play(con, f"{DAY}T11:00:00Z", "Clara La San")
    _tags(con, "logic", ["hip-hop", "rap"])
    _tags(con, "clara la san", ["r&b"])
    con.close()

    data = lastfm.get(parse_range(f"{DAY}..{DAY}"), "genres")["data"]
    assert [d["genre"] for d in data[:2]] == ["hip-hop", "rap"]
    assert data[0]["plays"] == 2
    assert [d for d in data if d["genre"] == "r&b"][0]["plays"] == 1


def test_reads_join_across_the_import_seam(dbfile):
    """Keyed on name, one lookup covers imported history and live rows (different artist_ids)."""
    con = db.connect()
    with con:
        con.execute(
            "INSERT INTO plays (played_at, day, track_id, track, artist,"
            " artist_id, album, ms, refreshed_at)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (f"{DAY}T09:00:00Z", DAY, "t", "t", "Logic", "sha:logic",
             None, 180000, NOW))
        con.execute(
            "INSERT INTO plays (played_at, day, track_id, track, artist,"
            " artist_id, album, ms, refreshed_at)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (f"{DAY}T10:00:00Z", DAY, "t", "t", "LOGIC", "real-logic",
             None, 180000, NOW))
    _tags(con, "logic", ["hip-hop"])
    con.close()

    data = lastfm.get(parse_range(f"{DAY}..{DAY}"), "genres")["data"]
    assert data == [{"genre": "hip-hop", "plays": 2, "share": 1.0}]


def test_coverage_reports_how_much_is_tagged(dbfile):
    con = db.connect()
    _play(con, f"{DAY}T09:00:00Z", "Tagged")
    _play(con, f"{DAY}T10:00:00Z", "Tagged")
    _play(con, f"{DAY}T11:00:00Z", "Untagged")
    _play(con, f"{DAY}T12:00:00Z", "Known But Blank")
    _tags(con, "tagged", ["rock"])
    _tags(con, "known but blank", [], missing=1)   # looked up, Last.fm knew nothing
    con.close()

    data = lastfm.get(parse_range(f"{DAY}..{DAY}"), "coverage")["data"]
    assert data == [{"day": DAY, "value": 0.5}]    # 2 of 4 plays


def test_day_mix_carries_its_own_coverage(dbfile):
    """The mix and the share it's drawn from travel together; three genres off 25% of a day differ from three off all of it."""
    con = db.connect()
    for i in range(3):
        _play(con, f"{DAY}T09:0{i}:00Z", "Tagged")
    _play(con, f"{DAY}T10:00:00Z", "Untagged")
    _tags(con, "tagged", ["hip hop", "rap"])
    mix = lastfm.day_mix(con, DAY)
    con.close()

    assert mix["coverage"] == 0.75          # 3 of 4 plays
    assert mix["top"] == [{"genre": "hip hop", "plays": 3},
                          {"genre": "rap", "plays": 3}]


def test_day_mix_is_none_on_a_silent_day(dbfile):
    con = db.connect()
    assert lastfm.day_mix(con, DAY) is None
    con.close()


def test_day_mix_reports_zero_coverage_rather_than_no_genres(dbfile):
    """An all-untagged day must not look like no plays: 'nothing listened' vs 'nothing tagged yet'."""
    con = db.connect()
    _play(con, f"{DAY}T09:00:00Z", "Untagged")
    mix = lastfm.day_mix(con, DAY)
    con.close()
    assert mix == {"top": [], "coverage": 0.0}


def test_get_rejects_unknown_metric(dbfile):
    with pytest.raises(ValueError, match="unknown metric"):
        lastfm.get(parse_range(f"{DAY}..{DAY}"), "nope")


def test_health_is_off_without_a_key(dbfile, monkeypatch):
    monkeypatch.setattr(lastfm, "LASTFM_API_KEY", None)
    assert lastfm.health()["state"] == "off"


def test_health_reports_coverage(dbfile, key):
    con = db.connect()
    _play(con, f"{DAY}T09:00:00Z", "Logic")
    _play(con, f"{DAY}T10:00:00Z", "Drake")
    _tags(con, "logic", ["hip-hop"])
    con.close()
    h = lastfm.health()
    assert h["state"] == "ok" and h["tagged"] == 1 and h["artists"] == 2
