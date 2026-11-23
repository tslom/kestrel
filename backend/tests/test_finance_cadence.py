"""finance.refresh()'s once-a-day legs.

The transactions cursor is incremental and cheap, so it runs every poller
cycle. `/transactions/recurring/get` and `/liabilities/get` are not: they
re-fetch the whole set, and neither dataset moves faster than daily, so on a
300s cycle they were spending ~570 identical calls a day. A sync_state marker
keyed per (leg, item) holds them to one run per day_key.

What must not regress: the marker is set only when a fetch actually landed.
Marking on failure would park a broken leg until tomorrow, and would break the
reconnect path — POST /api/plaid/sync resets a login_required/needs_consent
item and expects the very next refresh to re-pull it, not to wait out the day.
"""

import pytest

from app.providers import finance, plaid


@pytest.fixture
def calls(dbfile, monkeypatch):
    """refresh() reduced to its Plaid calls: one sandbox item, the transaction
    cursor and net-worth snapshot stubbed out, every /endpoint hit appended to
    the returned list so a test can count them across cycles."""
    seen: list[str] = []
    item = {"item_id": "itm_1", "access_token": "tok", "status": "ok"}

    def api(path, _payload):
        seen.append(path)
        if path == "/transactions/recurring/get":
            return {"outflow_streams": [], "inflow_streams": []}
        return {"liabilities": {}}

    monkeypatch.setattr(plaid, "creds", lambda: ("id", "secret", "url", "sandbox"))
    monkeypatch.setattr(plaid, "list_items", lambda env: [dict(item)])
    monkeypatch.setattr(plaid, "api", api)
    monkeypatch.setattr(finance, "_sync_item", lambda con, it: 0)
    monkeypatch.setattr(finance, "_snapshot_net_worth", lambda con, env, now: None)
    return seen, item


def test_first_refresh_of_the_day_runs_both_legs(calls):
    seen, _ = calls
    out = finance.refresh()
    assert sorted(seen) == ["/liabilities/get", "/transactions/recurring/get"]
    assert out["daily_legs"] == 2


def test_second_refresh_in_the_same_day_skips_both(calls):
    """The whole point: the 5-minute cycle stops re-fetching identical rows."""
    seen, _ = calls
    finance.refresh()
    seen.clear()
    out = finance.refresh()
    assert seen == []
    assert out["daily_legs"] == 0


def test_a_new_day_key_makes_the_legs_due_again(calls, freeze_day):
    seen, _ = calls
    finance.refresh()
    seen.clear()
    freeze_day("2099-01-01")
    finance.refresh()
    assert sorted(seen) == ["/liabilities/get", "/transactions/recurring/get"]


def test_the_marker_is_per_item(calls, monkeypatch):
    """Two banks are two markers — one item's successful leg must not satisfy
    the other's, or a second-linked account never pulls liabilities."""
    seen, item = calls
    finance.refresh()
    seen.clear()
    monkeypatch.setattr(plaid, "list_items", lambda env: [
        dict(item), {"item_id": "itm_2", "access_token": "tok2", "status": "ok"}])
    out = finance.refresh()
    assert sorted(seen) == ["/liabilities/get", "/transactions/recurring/get"]
    assert out["daily_legs"] == 2   # itm_2's two, not itm_1's


def test_a_failed_leg_is_not_marked_and_retries_next_cycle(calls, monkeypatch):
    """A 500 from Plaid must not cost the day's data. The failing leg stays
    due; the leg that worked stays done."""
    seen, _ = calls

    def api(path, _payload):
        seen.append(path)
        if path == "/liabilities/get":
            raise plaid.PlaidError("API_ERROR", "INTERNAL_SERVER_ERROR", "boom")
        return {"outflow_streams": [], "inflow_streams": []}

    monkeypatch.setattr(plaid, "api", api)
    assert finance.refresh()["daily_legs"] == 1     # recurring only
    seen.clear()
    finance.refresh()
    assert seen == ["/liabilities/get"]             # recurring stays marked


def test_needs_consent_item_leaves_liabilities_due(calls, monkeypatch):
    """_sync_liabilities short-circuits on needs_consent without calling out.
    That's a skip, not a success, so the marker stays unset and POST
    /api/plaid/sync's status reset takes effect on the next refresh."""
    seen, item = calls
    monkeypatch.setattr(plaid, "list_items", lambda env: [
        {**item, "status": "needs_consent"}])
    finance.refresh()
    assert seen == ["/transactions/recurring/get"]
    seen.clear()
    monkeypatch.setattr(plaid, "list_items", lambda env: [dict(item)])  # reset
    finance.refresh()
    assert seen == ["/liabilities/get"]


def test_no_syncable_items_touches_nothing(calls, monkeypatch):
    seen, _ = calls
    monkeypatch.setattr(plaid, "list_items", lambda env: [
        {"item_id": "itm_1", "access_token": "tok", "status": "login_required"}])
    assert "skipped" in finance.refresh()
    assert seen == []


def test_reconnect_sync_answers_after_the_sync_ran(calls, monkeypatch):
    """POST /api/plaid/sync runs the refresh before it answers, so the widget
    loads what it wrote. It used to be a background task the widget polled
    for, and a reconnect's item already had last_synced_at, so the poll gave
    up before the sync had run."""
    from app.routes import plaid as routes
    seen, _ = calls
    out = routes.sync()
    assert seen and out["sync"]["items"] == 1

    def broken():
        raise plaid.PlaidError("API_ERROR", "DOWN", "bank down")
    monkeypatch.setattr(finance, "refresh", broken)
    assert routes.sync() == {"sync": {"error": "API_ERROR/DOWN: bank down"}}
