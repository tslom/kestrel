"""The plan view's brief line (POST /api/brief/line): one line over the asks
the page shows, cached per day and per set of asks. The model is stubbed."""

import pytest

from app import brief


@pytest.fixture
def client(router_client, monkeypatch):
    brief._lines.clear()
    calls = []

    async def fake_ask(prompt, *, model, system=None):
        calls.append(prompt)
        return "Confirm the MATH 229 change before  answering Martin.\nTomorrow: lecture 10:00."
    monkeypatch.setattr(brief.agent, "ask", fake_ask)
    monkeypatch.setattr(brief, "_tomorrow_first", lambda: {"summary": "Lecture", "start": "2026-09-26T10:00:00-07:00"})
    c = router_client(brief.router)
    c.calls = calls
    return c


ASKS = [{"title": "MATH 229 moved", "why": "from mail", "card": "courses"},
        {"title": "4 waiting on a reply", "why": "Martin and 3 more", "card": "people"}]


def test_a_line_is_one_line_and_cached_per_set_of_asks(client):
    r = client.post("/api/brief/line", json={"asks": ASKS}).json()
    assert r["line"] == "Confirm the MATH 229 change before answering Martin. Tomorrow: lecture 10:00."
    assert r["cached"] is False
    again = client.post("/api/brief/line", json={"asks": ASKS}).json()
    assert again["cached"] is True and len(client.calls) == 1
    client.post("/api/brief/line", json={"asks": ASKS[:1]})
    assert len(client.calls) == 2, "a changed list asks again"


def test_nothing_to_join_and_no_tomorrow_is_an_empty_line_without_a_model_call(client, monkeypatch):
    monkeypatch.setattr(brief, "_tomorrow_first", lambda: None)
    assert client.post("/api/brief/line", json={"asks": []}).json()["line"] == ""
    assert client.calls == []


def test_the_model_sees_the_asks_and_tomorrow(client):
    client.post("/api/brief/line", json={"asks": ASKS})
    assert "MATH 229 moved" in client.calls[0] and "Lecture" in client.calls[0]


def test_a_failure_is_reported_and_not_cached(client, monkeypatch):
    async def boom(prompt, *, model, system=None):
        raise RuntimeError("the API returned 401")
    monkeypatch.setattr(brief.agent, "ask", boom)
    r = client.post("/api/brief/line", json={"asks": ASKS}).json()
    assert r["line"] is None and "401" in r["error"]
    assert brief._lines == {}


def test_an_overlong_row_is_clipped_not_rejected(client):
    """A 422 here hid the whole bar: a row's `why` grows on its own (the
    Messages-can't-be-read suffix), so length is clipped, never refused."""
    long = [{"title": "t" * 500, "why": "w" * 900, "card": "c" * 80}] * 50
    r = client.post("/api/brief/line", json={"asks": long})
    assert r.status_code == 200 and r.json()["line"]
    sent = client.calls[-1]
    assert "t" * 301 not in sent and "w" * 301 not in sent and "c" * 41 not in sent
