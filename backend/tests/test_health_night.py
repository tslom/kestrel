"""The two drill-downs: one night's stage segments, one day's hourly steps (what gets stored).

Design: DESIGN.md → `app/health.py`: night and hourly drill-downs, test-pinned.
"""

from datetime import date

import pytest

from app import db
from app.providers import health


def _sleep_point(day_end: str, stages, offset="-25200s"):
    """The shape health.py reads: interval, summary and the stages array."""
    return {
        "interval": {"endTime": f"{day_end}T15:00:00Z", "endUtcOffset": offset},
        "summary": {"minutesAsleep": sum(m for t, _, _, m in stages if t != "AWAKE"),
                    "stagesSummary": []},
        "stages": [{"startTime": s, "endTime": e, "type": t,
                    "startUtcOffset": offset, "endUtcOffset": offset}
                   for t, s, e, _ in stages],
    }


# (type, startTime, endTime, minutes) — 23:38 PT to 00:38 PT, one wake
NIGHT = [
    ("AWAKE", "2026-09-14T06:38:00Z", "2026-09-14T06:43:00Z", 5),
    ("LIGHT", "2026-09-14T06:43:00Z", "2026-09-14T07:13:00Z", 30),
    ("DEEP", "2026-09-14T07:13:00Z", "2026-09-14T07:33:00Z", 20),
    ("REM", "2026-09-14T07:33:00Z", "2026-09-14T07:43:00Z", 10),
]


def test_segments_keep_the_clock_they_were_recorded_against(monkeypatch):
    """A segment keeps the UTC offset it was recorded with."""
    segs = health._segments_of(_sleep_point("2026-09-14", NIGHT))

    assert [s["stage"] for s in segs] == ["awake", "light", "deep", "rem"]
    assert segs[0]["start"] == "2026-09-13T23:38:00-07:00"
    assert segs[0]["minutes"] == 5.0
    # ordered by clock, not by the order the API happened to list them
    assert segs == sorted(segs, key=lambda s: s["start"])


def test_a_night_reports_time_asleep_and_time_in_bed_apart(dbfile, monkeypatch):
    """The night reports time in bed as well as minutes asleep."""
    monkeypatch.setattr(health.oauth, "get_token", lambda *a, **k: "tok")
    monkeypatch.setattr(health, "_sleep_sessions",
                        lambda tok, s, e: [("2026-09-14", _sleep_point("2026-09-14", NIGHT))])

    got = health.night("2026-09-14")

    assert len(got["segments"]) == 4
    assert got["asleep_min"] == 60.0        # light 30 + deep 20 + rem 10
    assert got["awake_min"] == 5.0
    assert got["in_bed_min"] == 65.0
    assert got["start"] == "2026-09-13T23:38:00-07:00"
    assert got["end"] == "2026-09-14T00:43:00-07:00"


def test_a_night_with_no_sleep_is_asked_about_once(dbfile, monkeypatch):
    """An empty night is recorded so it isn't re-fetched on every open."""
    calls = []
    monkeypatch.setattr(health.oauth, "get_token", lambda *a, **k: "tok")
    monkeypatch.setattr(health, "_sleep_sessions",
                        lambda tok, s, e: calls.append(s) or [])

    assert health.night("2026-09-14")["segments"] == []
    assert health.night("2026-09-14")["segments"] == []

    assert calls == [date(2026, 9, 14)]
    with db.session() as con:
        assert con.execute("SELECT segments_at FROM health_sync WHERE day=?",
                           ("2026-09-14",)).fetchone()["segments_at"]


def test_a_resynced_night_replaces_rather_than_doubles(dbfile, monkeypatch):
    """A re-sync replaces a night whole; a night that gained a nap doesn't keep both readings."""
    monkeypatch.setattr(health.oauth, "get_token", lambda *a, **k: "tok")
    monkeypatch.setattr(health, "_sleep_sessions",
                        lambda tok, s, e: [("2026-09-14", _sleep_point("2026-09-14", NIGHT))])
    health.night("2026-09-14")

    con = db.connect()
    health._sync_segments(con, date(2026, 9, 14))
    n = con.execute("SELECT COUNT(*) AS n FROM sleep_segments WHERE day=?",
                    ("2026-09-14",)).fetchone()["n"]
    con.close()
    assert n == 4


@pytest.mark.parametrize("instant, day, hour", [
    # 2026-07-20 04:00Z: midnight ET on the 20th (desk on the east coast), 21:00 on the 19th in Pacific.
    ("2026-07-20T04:00:00Z", "2026-07-20", 0),
    # and the last hour of that ET day, which Pacific would push onto the 20th
    ("2026-07-21T03:00:00Z", "2026-07-20", 23),
    # a recent instant, where the two agree
    ("2026-09-13T17:00:00Z", "2026-09-13", 10),
])
def test_an_hour_lands_on_the_day_the_desk_was_living_in(instant, day, hour):
    assert health._bucket(instant) == (day, hour)


def test_writing_one_intraday_metric_leaves_the_other_alone(dbfile):
    """Steps and heart rate share health_hours, so a sync deletes only its own metric."""
    con = db.connect()
    health._write_hours(con, health.HR_HOURLY_METRIC, date(2026, 9, 13),
                        date(2026, 9, 13), {("2026-09-13", 9): 72.0})
    health._write_hours(con, health.STEPS_HOURLY_METRIC, date(2026, 9, 13),
                        date(2026, 9, 13), {("2026-09-13", 9): 400.0}, mark=True)
    rows = {r["metric"]: r["value"] for r in con.execute(
        "SELECT metric, value FROM health_hours WHERE day='2026-09-13'")}
    con.close()

    assert rows == {health.HR_HOURLY_METRIC: 72.0, health.STEPS_HOURLY_METRIC: 400.0}


def test_hourly_steps_are_filled_on_demand_then_read_from_cache(dbfile, monkeypatch):
    calls = []
    monkeypatch.setattr(health.oauth, "get_token", lambda *a, **k: "tok")
    monkeypatch.setattr(health, "_steps_hourly",
                        lambda tok, s, e: calls.append(s) or {("2026-07-20", 8): 900.0})

    first = health.day_detail("2026-07-20")
    second = health.day_detail("2026-07-20")

    assert calls == [date(2026, 7, 20)]
    assert len(first["hours"]) == 24                      # every hour has a slot
    assert first["hours"][8] == {"hour": 8, "steps": 900.0}
    assert first["hours"][9] == {"hour": 9}               # a gap, not a zero
    assert second["hours"] == first["hours"]


@pytest.fixture
def api_client():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.providers import api as providers_api

    app = FastAPI()
    app.include_router(providers_api.router)
    return TestClient(app)


def test_the_drill_down_routes_say_when_they_served_cache(dbfile, monkeypatch,
                                                         api_client):
    """These routes open offline.collect() themselves, so a cached night reports stale."""
    monkeypatch.setattr(health.oauth, "get_token", lambda *a, **k: "tok")

    def offline(*a):
        raise ConnectionError("no route to host")

    monkeypatch.setattr(health, "_sleep_sessions", offline)
    monkeypatch.setattr(health, "_steps_hourly", offline)

    night = api_client.get("/api/health/night/2026-09-14")
    assert night.status_code == 200
    assert night.json()["stale"] is True
    assert night.json()["offline"][0].startswith("sleep segments:")
    assert night.json()["segments"] == []                 # cache still answers

    day = api_client.get("/api/health/day/2026-09-14")
    assert day.status_code == 200
    assert day.json()["offline"][0].startswith("intraday steps:")


def test_the_drill_down_routes_map_bad_input_to_400(dbfile, monkeypatch,
                                                    api_client):
    """ValueError (malformed day or range token) is answered as the caller's fault."""
    assert api_client.get("/api/health/night/not-a-day").status_code == 400
    assert api_client.get("/api/location/trace",
                          params={"range": "whenever"}).status_code == 400
    monkeypatch.setattr(health.oauth, "get_token", lambda *a, **k: "tok")
    monkeypatch.setattr(health, "_sleep_sessions", lambda tok, s, e: [])
    ok = api_client.get("/api/health/night/2026-09-14")
    assert ok.status_code == 200
    assert "stale" not in ok.json()          # nothing degraded, no envelope
