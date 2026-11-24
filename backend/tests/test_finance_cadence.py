"""finance.refresh()'s once-a-day legs (recurring, liabilities): a sync_state marker per (leg, item) holds each to one run per day_key.

The marker is set only when a fetch landed: marking on failure would park a broken leg and break the reconnect path.
Design: DESIGN.md → §18 Finance (Plaid).
"""

import pytest

from app.providers import finance, plaid


@pytest.fixture
def calls(dbfile, monkeypatch):
    """refresh() reduced to its Plaid calls: one sandbox item; returns the list of /endpoints hit."""
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
    """One marker per item: a second-linked bank still pulls liabilities."""
    seen, item = calls
    finance.refresh()
    seen.clear()
    monkeypatch.setattr(plaid, "list_items", lambda env: [
        dict(item), {"item_id": "itm_2", "access_token": "tok2", "status": "ok"}])
    out = finance.refresh()
    assert sorted(seen) == ["/liabilities/get", "/transactions/recurring/get"]
    assert out["daily_legs"] == 2   # itm_2's two, not itm_1's


def test_a_failed_leg_is_not_marked_and_retries_next_cycle(calls, monkeypatch):
    """A 500 leaves the failing leg due and the successful one done."""
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
    """A needs_consent skip is not a success: the marker stays unset so POST /api/plaid/sync's reset re-pulls."""
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
    """POST /api/plaid/sync runs the refresh before answering, so the widget loads what it wrote."""
    from app.routes import plaid as routes
    seen, _ = calls
    out = routes.sync()
    assert seen and out["sync"]["items"] == 1

    def broken():
        raise plaid.PlaidError("API_ERROR", "DOWN", "bank down")
    monkeypatch.setattr(finance, "refresh", broken)
    assert routes.sync() == {"sync": {"error": "API_ERROR/DOWN: bank down"}}
