"""The health poller's failure modes: a transient blip is retried, and a
series that still fails costs only its own metrics.

Measured before this (2026-10): ~8% of refresh cycles died on one read
timeout / reset / 503 out of eleven sequential calls, and the whole window —
every metric — was thrown away with it.
"""

import io
import socket
import urllib.error
from datetime import date

import pytest

from app import db
from app.providers import health, http


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _script(monkeypatch, *outcomes):
    """urlopen playing `outcomes` in order: bytes answer, an exception raises."""
    calls = []

    def fake(req, timeout=None):
        calls.append(req.get_method())
        out = outcomes[len(calls) - 1]
        if isinstance(out, BaseException):
            raise out
        return _Resp(out)
    monkeypatch.setattr(http.urllib.request, "urlopen", fake)
    monkeypatch.setattr(http.time, "sleep", lambda s: None)
    return calls


def _http_error(code):
    return urllib.error.HTTPError("https://x", code, "err", {}, io.BytesIO(b"why"))


@pytest.mark.parametrize("blip", [
    TimeoutError("The read operation timed out"),
    urllib.error.URLError(ConnectionResetError(54, "Connection reset by peer")),
    urllib.error.URLError(TimeoutError("_ssl.c:993: The handshake operation timed out")),
    http.http.client.RemoteDisconnected("Remote end closed connection without response"),
    _http_error(503),
])
def test_a_get_survives_one_transient_failure(monkeypatch, blip):
    calls = _script(monkeypatch, blip, b'{"ok": 1}')
    assert http.json_request("https://x", label="t") == {"ok": 1}
    assert calls == ["GET", "GET"]


def test_a_second_blip_raises_the_last_failure(monkeypatch):
    calls = _script(monkeypatch, TimeoutError("one"), TimeoutError("two"))
    with pytest.raises(TimeoutError, match="two"):
        http.json_request("https://x", label="t")
    assert len(calls) == 2


@pytest.mark.parametrize("answer", [401, 404, 429])
def test_a_4xx_is_an_answer_not_a_blip(monkeypatch, answer):
    calls = _script(monkeypatch, _http_error(answer), b"{}")
    with pytest.raises(RuntimeError, match=f"t {answer}: why"):
        http.json_request("https://x", label="t")
    assert len(calls) == 1


def test_a_401_refreshes_the_token_once_and_resends(monkeypatch):
    """Google now and then rejects a token its expiry still vouches for. A
    401 never landed, so even a non-idempotent POST is safe to resend."""
    seen = []
    orig = http.urllib.request.Request

    def spy(*a, **kw):
        seen.append(kw["headers"]["Authorization"])
        return orig(*a, **kw)
    calls = _script(monkeypatch, _http_error(401), b'{"ok": 1}')
    monkeypatch.setattr(http.urllib.request, "Request", spy)
    assert http.json_request("https://x", label="t", token="old", method="POST",
                             body={"a": 1}, reauth=lambda: "new") == {"ok": 1}
    assert calls == ["POST", "POST"]
    assert seen == ["Bearer old", "Bearer new"]


def test_a_second_401_is_the_answer(monkeypatch):
    calls = _script(monkeypatch, _http_error(401), _http_error(401), b"{}")
    with pytest.raises(RuntimeError, match="t 401"):
        http.json_request("https://x", label="t", token="old", reauth=lambda: "new")
    assert len(calls) == 2


def test_the_resent_call_keeps_its_transient_retry(monkeypatch):
    calls = _script(monkeypatch, _http_error(401), TimeoutError("blip"), b'{"ok": 1}')
    assert http.json_request("https://x", label="t", token="old",
                             reauth=lambda: "new") == {"ok": 1}
    assert len(calls) == 3


def test_offline_is_not_retried(monkeypatch):
    """A dead resolver half a second later is still dead; offline.py serves
    the cache for it."""
    dns = urllib.error.URLError(socket.gaierror(8, "nodename nor servname"))
    calls = _script(monkeypatch, dns, b"{}")
    with pytest.raises(urllib.error.URLError):
        http.json_request("https://x", label="t")
    assert len(calls) == 1


def test_a_post_is_retried_only_when_the_caller_says_it_is_a_read(monkeypatch):
    calls = _script(monkeypatch, _http_error(503), b"{}")
    with pytest.raises(RuntimeError, match="503"):
        http.json_request("https://x", label="t", method="POST", body={"a": 1})
    assert calls == ["POST"]

    calls = _script(monkeypatch, _http_error(503), b'{"ok": 1}')
    assert http.json_request("https://x", label="t", method="POST", body={"a": 1},
                             idempotent=True) == {"ok": 1}
    assert calls == ["POST", "POST"]


def test_health_marks_its_rollup_posts_idempotent(monkeypatch):
    seen = {}
    monkeypatch.setattr(health.http, "json_request",
                        lambda url, **kw: seen.update(kw) or {})
    health._api("https://x", "tok", body={"range": {}})
    assert seen["method"] == "POST" and seen["idempotent"] is True


DAY = "2026-09-14"
CIVIL = {"date": {"year": 2026, "month": 9, "day": 14}}


def _fake_api(fail=()):
    """A Google Health stand-in: one value per series on DAY. A dataType in
    `fail` raises what it's mapped to."""
    fail = dict(fail)

    def api(url, token, body=None):
        dtype = url.split("/dataTypes/")[1].split("/")[0]
        if dtype in fail:
            raise fail[dtype]
        if url.endswith(":dailyRollUp"):
            point = {"steps": {"steps": {"countSum": 9000}},
                     "active-zone-minutes": {"activeZoneMinutes":
                                             {"sumInCardioHeartZone": 40}},
                     "heart-rate": {"heartRate": {"beatsPerMinuteAvg": 70.0,
                                                  "beatsPerMinuteMax": 150}},
                     }[dtype]
            return {"rollupDataPoints": [{"civilStartTime": CIVIL, **point}]}
        if url.endswith(":rollUp"):
            point = ({"steps": {"countSum": 300}} if dtype == "steps"
                     else {"heartRate": {"beatsPerMinuteAvg": 80.0}})
            return {"rollupDataPoints": [{"startTime": f"{DAY}T17:00:00Z", **point}]}
        if dtype == "daily-resting-heart-rate":
            return {"dataPoints": [{"dailyRestingHeartRate":
                                    {**CIVIL, "beatsPerMinute": 55}}]}
        if dtype == "daily-oxygen-saturation":
            return {"dataPoints": [{"dailyOxygenSaturation":
                                    {**CIVIL, "averagePercentage": 96.0}}]}
        return {"dataPoints": []}
    return api


def _seed(con):
    """Yesterday's sync of DAY: a full set of rows and the mark."""
    with con:
        for metric, value in (("steps", 100), ("hr_avg", 60.0), ("hr_max", 120),
                              ("spo2", 94.0)):
            con.execute("INSERT INTO health_days (day, metric, value, refreshed_at)"
                        " VALUES (?,?,?,'old')", (DAY, metric, value))
        con.execute("INSERT INTO health_sync (day, refreshed_at) VALUES (?, 'old')",
                    (DAY,))


def _rows(con):
    return {r["metric"]: r["value"] for r in con.execute(
        "SELECT metric, value FROM health_days WHERE day=?", (DAY,))}


def _mark(con):
    return con.execute("SELECT refreshed_at FROM health_sync WHERE day=?",
                       (DAY,)).fetchone()["refreshed_at"]


@pytest.fixture
def token(monkeypatch):
    monkeypatch.setattr(health.oauth, "get_token", lambda *a, **k: "tok")


def test_series_cover_every_metric_exactly_once():
    owned = [m for _, metrics, _, _ in health.SERIES for m in metrics]
    assert sorted(owned) == sorted(health.METRICS)


def test_a_failed_series_keeps_its_rows_and_the_rest_still_land(con, token, monkeypatch):
    _seed(con)
    monkeypatch.setattr(health, "_api", _fake_api(
        {"heart-rate": TimeoutError("The read operation timed out")}))
    d = date.fromisoformat(DAY)

    with pytest.raises(TimeoutError):
        health._sync(con, d, d)

    rows = _rows(con)
    assert rows["steps"] == 9000            # replaced
    assert rows["resting_hr"] == 55         # new
    assert (rows["hr_avg"], rows["hr_max"]) == (60.0, 120)   # kept, not wiped
    # not marked: the mark would claim heart rate was fetched
    assert _mark(con) == "old"


def test_a_cold_day_with_a_failed_series_still_reads_as_missing(con, token, monkeypatch):
    """Backfill marks nothing on partial failure, so the next get() asks
    again instead of trusting a day with no heart rate in it."""
    monkeypatch.setattr(health, "_api", _fake_api(
        {"sleep": urllib.error.URLError(ConnectionResetError(54, "reset"))}))
    d = date.fromisoformat(DAY)
    with pytest.raises(urllib.error.URLError):
        health._sync(con, d, d)
    assert con.execute("SELECT 1 FROM health_sync WHERE day=?", (DAY,)).fetchone() is None
    assert _rows(con)["steps"] == 9000


def test_a_dead_token_outranks_a_timeout(con, token, monkeypatch):
    """best_effort() swallows the network; a 401 beside a timeout must be the
    one raised, or the expired credential hides behind stale data."""
    monkeypatch.setattr(health, "_api", _fake_api(
        {"heart-rate": TimeoutError("slow"), "steps": RuntimeError("health API 401: x")}))
    d = date.fromisoformat(DAY)
    with pytest.raises(RuntimeError, match="401"):
        health._sync(con, d, d)


def test_an_unavailable_optional_type_is_not_a_failure(con, token, monkeypatch):
    """The nightly trio may not exist for this watch: the API refusing one is
    "no data", so the sync completes — and it no longer wipes that metric's
    rows on the way."""
    _seed(con)
    monkeypatch.setattr(health, "_api", _fake_api(
        {"daily-oxygen-saturation": RuntimeError("health API 400: unsupported")}))
    d = date.fromisoformat(DAY)

    assert health._sync(con, d, d) > 0
    rows = _rows(con)
    assert rows["spo2"] == 94.0
    assert rows["hr_avg"] == 70.0
    assert _mark(con) != "old"


def test_a_clean_sync_still_replaces_the_window(con, token, monkeypatch):
    """A metric the source no longer reports for a day goes away, as before —
    the scoped delete covers every series that answered."""
    _seed(con)
    monkeypatch.setattr(health, "_api", _fake_api())
    d = date.fromisoformat(DAY)
    health._sync(con, d, d)
    rows = _rows(con)
    assert "spo2" in rows and rows["spo2"] == 96.0
    assert rows["steps"] == 9000 and rows["hr_max"] == 150


def test_refresh_keeps_the_intraday_curves_when_the_daily_sync_fails(
        dbfile, token, freeze_day, monkeypatch):
    freeze_day(DAY)
    monkeypatch.setattr(health, "_api", _fake_api(
        {"active-zone-minutes": TimeoutError("slow")}))
    with pytest.raises(TimeoutError):
        health.refresh()

    con = db.connect()
    hours = {r["metric"] for r in con.execute(
        "SELECT metric FROM health_hours WHERE day=?", (DAY,))}
    con.close()
    assert hours == {health.HR_HOURLY_METRIC, health.STEPS_HOURLY_METRIC}


def test_refresh_reports_the_same_shape_when_clean(dbfile, token, freeze_day, monkeypatch):
    freeze_day(DAY)
    monkeypatch.setattr(health, "_api", _fake_api())
    out = health.refresh()
    assert out["rows"] >= 6 and out["hr_hours"] == 2
    assert out["window"] == "2026-09-07..2026-09-14"
