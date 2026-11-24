"""weather.get() only backfills days Open-Meteo can answer for.

A weather_days gap past the API horizon (MAX_PAST_DAYS / MAX_FORECAST_DAYS) can never fill and must not count as missing.
"""

from datetime import date, timedelta

from app.providers import ranges, weather


def _rng(start: date, end: date):
    return ranges.parse_range(f"{start.isoformat()}..{end.isoformat()}")


def _record_syncs(monkeypatch):
    calls = []
    monkeypatch.setattr(weather, "_origin", lambda: (34.02, -112.28))
    monkeypatch.setattr(weather, "_sync",
                        lambda con, lat, lon, s, e: calls.append((s, e)) or 0)
    return calls


def test_a_range_wholly_past_the_history_horizon_makes_no_call(dbfile, monkeypatch):
    calls = _record_syncs(monkeypatch)
    today = date.today()
    weather.get(_rng(today - timedelta(days=200), today - timedelta(days=100)))
    assert calls == []


def test_a_range_wholly_past_the_forecast_horizon_makes_no_call(dbfile, monkeypatch):
    calls = _record_syncs(monkeypatch)
    today = date.today()
    weather.get(_rng(today + timedelta(days=20), today + timedelta(days=40)))
    assert calls == []


def test_a_straddling_range_asks_only_for_the_fillable_part(dbfile, monkeypatch):
    calls = _record_syncs(monkeypatch)
    today = date.today()
    weather.get(_rng(today - timedelta(days=120), today + timedelta(days=30)))
    assert calls == [(today - timedelta(days=weather.MAX_PAST_DAYS),
                      today + timedelta(days=weather.MAX_FORECAST_DAYS - 1))]


# ---- a fetched day with a null metric is not refetched on every read ----

def _fake_fetch(monkeypatch):
    """_fetch stand-in: tmax present, precip_prob null."""
    calls = []

    def fetch(lat, lon, s, e):
        calls.append((s, e))
        out, d = {}, s
        while d <= e:
            out[d.isoformat()] = {"tmax": 70.0}
            d += timedelta(days=1)
        return out
    monkeypatch.setattr(weather, "_origin", lambda: (34.02, -112.28))
    monkeypatch.setattr(weather, "_fetch", fetch)
    return calls


def test_a_null_metric_on_a_fetched_day_is_a_gap_not_a_refetch(dbfile, monkeypatch):
    calls = _fake_fetch(monkeypatch)
    today = date.today()
    rng = _rng(today, today + timedelta(days=2))
    first = weather.get(rng, "precip_prob")
    assert len(calls) == 1
    assert first["data"] == []            # a gap — never a 0
    again = weather.get(rng, "precip_prob")
    assert len(calls) == 1                # fetched once; the null stays a gap
    assert again["data"] == []
    assert [p["value"] for p in weather.get(rng, "tmax")["data"]] == [70.0] * 3
    assert len(calls) == 1


def test_a_never_fetched_day_still_triggers_a_fetch(dbfile, monkeypatch):
    calls = _fake_fetch(monkeypatch)
    today = date.today()
    weather.get(_rng(today, today), "tmax")
    weather.get(_rng(today, today + timedelta(days=1)), "tmax")
    assert calls == [(today, today),
                     (today + timedelta(days=1), today + timedelta(days=1))]


def test_day_summary_does_not_refetch_for_a_null_metric(dbfile, monkeypatch):
    calls = _fake_fetch(monkeypatch)
    today = date.today().isoformat()
    s = weather.day_summary(today)
    assert s["tmax"] == 70.0 and "precip_prob" not in s
    weather.day_summary(today)
    assert len(calls) == 1
