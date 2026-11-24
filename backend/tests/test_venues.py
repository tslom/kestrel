"""Venues: Google Places search + Routes travel times for "where should I go"."""

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from app import console_tools
from app.providers import venues

HOME = {"label": "home", "lat": 34.0256, "lon": -112.2850, "kind": "home"}


def _place(pid, name, lat, lon, **kw):
    return {"id": pid, "displayName": {"text": name},
            "formattedAddress": f"{pid} St, Los Angeles, CA",
            "location": {"latitude": lat, "longitude": lon}, **kw}


def _fake(monkeypatch, places, *, resolve_to=None, walk=None, drive=None,
          matrix_error=None, calls=None):
    """Route _request by URL: searchText (resolve vs search by field mask), the matrix per mode, details."""
    calls = [] if calls is None else calls

    def request(url, *, body, field_mask):
        calls.append((url, body, field_mask))
        if url == venues.SEARCH_URL:
            if field_mask.startswith("places.displayName,places.formatted"):
                return {"places": [resolve_to]} if resolve_to else {}
            return {"places": places}
        if url == venues.MATRIX_URL:
            if matrix_error:
                raise RuntimeError(matrix_error)
            mins = walk if body["travelMode"] == "WALK" else drive
            return [{"destinationIndex": i, "condition": "ROUTE_EXISTS",
                     "duration": f"{m * 60}s", "distanceMeters": m * 500}
                    for i, m in enumerate(mins)]
        raise AssertionError(url)

    monkeypatch.setattr(venues, "api_request", request)
    monkeypatch.setattr(venues, "_default_origin", lambda: dict(HOME))
    monkeypatch.setattr(venues, "GOOGLE_MAPS_API_KEY", "k")
    return calls


def test_search_carries_address_status_hours_and_real_travel(monkeypatch):
    places = [
        _place("a", "Open Cafe", 34.03, -112.28, businessStatus="OPERATIONAL",
               rating=4.6, userRatingCount=1203, priceLevel="PRICE_LEVEL_MODERATE",
               currentOpeningHours={"openNow": True}, reservable=True,
               goodForGroups=False),
        _place("b", "Gone BBQ", 34.05, -112.30,
               businessStatus="CLOSED_PERMANENTLY"),
    ]
    _fake(monkeypatch, places, walk=[12, 40], drive=[5, 11])
    hits = venues.search("cafe")
    a, b = hits["data"]
    assert (a["walk_min"], a["drive_min"], a["drive_km"]) == (12, 5, 2.5)
    line = venues.brief_line(a)
    assert "a St, Los Angeles, CA" in line and "★4.6 (1,203) $$" in line
    assert "open" in line and "walk 12 min · drive 5 min (2.5 km)" in line
    # A walk past WALK_MAX_MIN is dropped rather than shown as "walk 892 min".
    assert "walk" not in venues.brief_line({**a, "walk_min": 892})
    assert "reservable, not for groups" in line and line.endswith("[a]")
    # A closed place says so and nothing else that could read as an endorsement.
    assert "PERMANENTLY CLOSED" in venues.brief_line(b)
    assert "walk" not in venues.brief_line(b)
    assert "near home" in venues.search_brief(hits)


def test_near_resolves_through_places_and_biases_the_search(monkeypatch):
    webb = {"displayName": {"text": "Maple Hall"},
            "formattedAddress": "100 Example St, Los Angeles, CA 90007",
            "location": {"latitude": 34.0245, "longitude": -112.2880}}
    calls = _fake(monkeypatch, [], resolve_to=webb)
    hits = venues.search("kbbq", "Maple Hall")
    # The resolved address is shown, so a wrong resolution is visible.
    assert hits["origin"]["label"] == "Maple Hall, 100 Example St, Los Angeles, CA 90007"
    resolve_body, search_body = calls[0][1], calls[1][1]
    # The resolve leans toward where the user is, so "Maple Hall" is the local one.
    assert resolve_body["locationBias"]["circle"]["center"]["latitude"] == HOME["lat"]
    assert search_body["locationBias"]["circle"]["center"] == {
        "latitude": 34.0245, "longitude": -112.2880}


def test_unresolvable_near_is_an_error_not_a_guess(monkeypatch):
    _fake(monkeypatch, [])
    from app import leave
    monkeypatch.setattr(leave, "geocode", lambda loc: None)
    with pytest.raises(ValueError, match="couldn't find"):
        venues.search("things to do", "250 Nowhere Ave")


def test_by_distance_orders_by_walking_time(monkeypatch):
    places = [_place("far", "Far", 34.1, -112.3), _place("near", "Near", 34.02, -112.28)]
    calls = _fake(monkeypatch, places, walk=[50, 6], drive=[15, 3])
    hits = venues.search("ramen", by_distance=True)
    assert [it["id"] for it in hits["data"]] == ["near", "far"]
    assert calls[0][1]["rankPreference"] == "DISTANCE"


def test_matrix_failure_still_returns_the_list(monkeypatch):
    _fake(monkeypatch, [_place("a", "A", 34.03, -112.28)],
          matrix_error="google places 403: quota")
    hits = venues.search("tacos")
    assert hits["count"] == 1 and "quota" in hits["travel_error"]
    # Falls back to an honestly-labelled straight line, never a guess.
    assert "km straight-line" in venues.brief_line(hits["data"][0])


def test_disabled_api_reads_as_which_switch(monkeypatch):
    def refuse(*a, **kw):
        raise RuntimeError('google places 403: {"error": {"status": '
                           '"PERMISSION_DENIED", "reason": "SERVICE_DISABLED"}}')
    monkeypatch.setattr(venues, "json_request", refuse)
    monkeypatch.setattr(venues, "GOOGLE_MAPS_API_KEY", "k")
    with pytest.raises(RuntimeError, match=r"Places API \(New\) is not enabled"):
        venues.api_request(venues.SEARCH_URL, body={}, field_mask="places.id")


def test_missing_key_refuses(monkeypatch):
    monkeypatch.setattr(venues, "GOOGLE_MAPS_API_KEY", "")
    monkeypatch.setattr(venues, "_default_origin", lambda: dict(HOME))
    with pytest.raises(RuntimeError, match="GOOGLE_MAPS_API_KEY"):
        venues.search("coffee")


def test_closing_time_is_in_the_places_own_zone():
    # 05:00Z is 10pm in LA (-420) whatever zone this machine is in.
    # Tomorrow's 05:00Z, not "an hour from now": that landed on LA midnight ("midnight") nightly.
    now = datetime.now(timezone.utc)
    close = (now + timedelta(days=1)).replace(hour=5, minute=0, second=0, microsecond=0)
    la = close.astimezone(timezone(timedelta(minutes=-420)))
    got = venues._local_hm(close.strftime("%Y-%m-%dT%H:%M:%SZ"), -420)
    assert got.startswith(la.strftime("%-I%p").lower())


def test_closing_at_midnight_reads_as_midnight():
    tomorrow = datetime.now(timezone(timedelta(minutes=-420))) + timedelta(days=1)
    midnight = tomorrow.replace(hour=0, minute=0, second=0, microsecond=0)
    stamp = midnight.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert venues._local_hm(stamp, -420) == "midnight"


def test_details_carries_reviews_and_hours(monkeypatch):
    def request(url, *, body, field_mask):
        assert url == venues.DETAILS_URL + "abc" and body is None
        assert "reviews" in field_mask
        return {**_place("abc", "Spot", 34.0, -112.0),
                "websiteUri": "https://spot.example",
                "regularOpeningHours": {"weekdayDescriptions": ["Monday: 9 AM – 5 PM"]},
                "reviews": [{"rating": 5, "relativePublishTimeDescription": "a week ago",
                             "text": {"text": "Great  for\ngroups"}}]}
    monkeypatch.setattr(venues, "api_request", request)
    d = venues.details("places/abc")
    text = venues.details_brief(d)
    assert "website: https://spot.example" in text
    assert "Monday: 9 AM – 5 PM" in text
    assert "★5 a week ago: Great for groups" in text


def test_console_tool_reports_failure_and_the_fallback_rule(monkeypatch):
    def boom(*a, **kw):
        raise RuntimeError("Places API (New) is not enabled")
    monkeypatch.setattr(venues, "search", boom)
    out = asyncio.run(console_tools.venue_search.handler({"query": "pho"}))
    text = out["content"][0]["text"]
    assert out.get("is_error") and "not enabled" in text and "unverified" in text
