"""Time-to-leave logic with calendar, fix, geocode and route mocked: leave_by math, origin choice, event filter, degradation."""

import http.client
from datetime import datetime, timedelta

import pytest

from app import leave
from app.config import HOME_LAT, HOME_LON


def _ev(summary, start_dt, location="Somewhere", all_day=False):
    return {"summary": summary, "start": start_dt.isoformat(),
            "location": location, "all_day": all_day}


@pytest.fixture(autouse=True)
def _clear_cache():
    leave._cache.clear()
    yield
    leave._cache.clear()


def _patch(monkeypatch, events, *, fix=None, geo=(34.0, -112.0),
           drive=40.0, capture=None):
    monkeypatch.setattr(leave.calendar, "get", lambda rng: {"data": events})
    monkeypatch.setattr(leave.location, "latest_fix", lambda: fix)
    monkeypatch.setattr(leave, "geocode", lambda loc: geo)

    def _drive(origin, dest):
        if capture is not None:
            capture.append(origin)
        return drive
    monkeypatch.setattr(leave, "_drive_minutes", _drive)


def test_leave_by_is_start_minus_drive_and_buffer(monkeypatch):
    start = datetime.now().astimezone() + timedelta(hours=3)
    _patch(monkeypatch, [_ev("Dentist", start)], drive=40.0)
    rows = leave.compute_leaves("2026-07-23")
    assert len(rows) == 1
    r = rows[0]
    assert r["drive_min"] == 40
    expected = start - timedelta(minutes=40 + leave.LEAVE_BUFFER_MIN)
    assert datetime.fromisoformat(r["leave_by"]) == expected


def test_home_origin_when_no_fix(monkeypatch):
    start = datetime.now().astimezone() + timedelta(hours=2)
    seen = []
    _patch(monkeypatch, [_ev("Game", start)], fix=None, capture=seen)
    rows = leave.compute_leaves()
    assert rows[0]["origin"] == "home"
    assert seen == [(HOME_LAT, HOME_LON)]


def test_fresh_fix_used_as_origin(monkeypatch):
    start = datetime.now().astimezone() + timedelta(hours=2)
    seen = []
    fix = {"lat": 34.05, "lon": -112.25, "age_min": 5.0}
    _patch(monkeypatch, [_ev("Game", start)], fix=fix, capture=seen)
    rows = leave.compute_leaves()
    assert rows[0]["origin"] == "fix"
    assert seen == [(34.05, -112.25)]


def test_stale_fix_falls_back_to_home(monkeypatch):
    start = datetime.now().astimezone() + timedelta(hours=2)
    fix = {"lat": 34.05, "lon": -112.25, "age_min": 45.0}  # > LEAVE_FIX_MAX_MIN
    _patch(monkeypatch, [_ev("Game", start)], fix=fix)
    assert leave.compute_leaves()[0]["origin"] == "home"


def test_route_failure_degrades_to_null_leave_by(monkeypatch):
    start = datetime.now().astimezone() + timedelta(hours=2)
    _patch(monkeypatch, [_ev("Meeting", start)], drive=None)
    r = leave.compute_leaves()[0]
    assert r["event"] == "Meeting"
    assert r["leave_by"] is None and r["drive_min"] is None


def test_filters_allday_unlocated_and_past(monkeypatch):
    now = datetime.now().astimezone()
    events = [
        _ev("Holiday", now + timedelta(hours=1), all_day=True),      # all-day
        _ev("Call", now + timedelta(hours=1), location=None),         # no location
        _ev("Past", now - timedelta(hours=1)),                        # already started
        _ev("Real", now + timedelta(hours=1)),                        # keep
    ]
    _patch(monkeypatch, events)
    rows = leave.compute_leaves()
    assert [r["event"] for r in rows] == ["Real"]


def test_no_api_key_returns_none_from_drive(monkeypatch):
    # _drive_minutes short-circuits to None with no key, never hitting HTTP.
    monkeypatch.setattr(leave, "GOOGLE_MAPS_API_KEY", "")
    assert leave._drive_minutes((0, 0), (1, 1)) is None


def test_the_day_argument_actually_selects_the_day(monkeypatch):
    """compute_leaves(day) answers for that day, not today."""
    seen = []

    def _get(rng):
        seen.append((rng.start.isoformat(), rng.end.isoformat()))
        return {"data": []}

    monkeypatch.setattr(leave.calendar, "get", _get)
    leave.compute_leaves("2026-07-23")
    assert seen == [("2026-07-23", "2026-07-23")]


def test_strict_lets_a_calendar_outage_through(monkeypatch):
    """strict=True raises on a calendar outage: [] would withdraw live alarms (the card wants [])."""
    def _boom(rng):
        raise RuntimeError("no calendar auth")

    monkeypatch.setattr(leave.calendar, "get", _boom)
    assert leave.compute_leaves() == []
    with pytest.raises(RuntimeError):
        leave.compute_leaves(strict=True)


def _patch_geocode(monkeypatch, google: dict, *, context="", meteo=None):
    """Mock both geocoders; `google` maps address to coords, absent is a miss. Returns addresses Google was asked."""
    asked = []

    def _g(address):
        asked.append(address)
        return google.get(address)

    monkeypatch.setattr(leave, "_google_geocode", _g)
    monkeypatch.setattr(leave.weather, "geocode", lambda loc: meteo)
    monkeypatch.setattr(leave, "LEAVE_GEOCODE_CONTEXT", context)
    return asked


@pytest.mark.parametrize("loc", [
    "https://usc.zoom.us/j/1234567890",
    "Zoom — https://usc.zoom.us/j/93590008552",
    "https://meet.google.com/ehk-jwbj-ghv",
    "TBD",
    "Online through Web Registration",
])
def test_virtual_locations_never_geocode(monkeypatch, loc):
    """Virtual locations are never looked up (Google geocodes them confidently and wrongly)."""
    asked = _patch_geocode(monkeypatch, {}, context="USC")
    assert leave.geocode(loc) is None
    assert asked == []


@pytest.mark.parametrize("loc", [
    "The Virtual Reality Lab",
    "Naples, FL",
    "Nonesuch Hall",
    "Remotely Green Cafe",
])
def test_real_places_containing_a_marker_still_geocode(monkeypatch, loc):
    """Virtual markers match as a prefix word, not a substring ("Na..." is not "n/a")."""
    _patch_geocode(monkeypatch, {loc: (1.0, 2.0)})
    assert leave.geocode(loc) == (1.0, 2.0)


def test_bare_room_code_retries_with_context(monkeypatch):
    """A bare room code resolves only with the campus appended."""
    asked = _patch_geocode(
        monkeypatch,
        {"KDC 240, University of Southern California": (34.02, -112.28)},
        context="University of Southern California")
    assert leave.geocode("KDC 240") == (34.02, -112.28)
    assert asked == ["KDC 240", "KDC 240, University of Southern California"]


def test_no_retry_when_the_location_already_resolves(monkeypatch):
    asked = _patch_geocode(monkeypatch, {"900 Exposition Blvd": (34.0, -112.3)},
                           context="USC")
    assert leave.geocode("900 Exposition Blvd") == (34.0, -112.3)
    assert asked == ["900 Exposition Blvd"]


def test_no_retry_when_context_is_already_in_the_location(monkeypatch):
    """The campus is not appended to a string that already names it."""
    asked = _patch_geocode(monkeypatch, {}, context="Los Angeles, CA")
    assert leave.geocode("Trousdale Pkwy, los angeles, ca") is None
    assert asked == ["Trousdale Pkwy, los angeles, ca"]


def test_no_retry_without_a_context_configured(monkeypatch):
    asked = _patch_geocode(monkeypatch, {}, context="")
    assert leave.geocode("KDC 240") is None
    assert asked == ["KDC 240"]


def test_open_meteo_still_answers_before_the_retry(monkeypatch):
    asked = _patch_geocode(monkeypatch, {}, context="USC", meteo=(9.0, 9.0))
    assert leave.geocode("Durham Bulls Athletic Park") == (9.0, 9.0)
    assert asked == ["Durham Bulls Athletic Park"]


@pytest.mark.parametrize("exc", [
    http.client.RemoteDisconnected(
        "Remote end closed connection without response"),
    http.client.IncompleteRead(b""),
    ConnectionResetError(54, "Connection reset by peer"),
])
def test_a_dropped_connection_degrades_instead_of_500ing(monkeypatch, exc):
    """Errors urlopen doesn't wrap in URLError are caught in _http_json."""
    def boom(req, timeout=None):
        raise exc
    monkeypatch.setattr(leave.urllib.request, "urlopen", boom)
    assert leave._http_json("https://x", headers={}) is None
