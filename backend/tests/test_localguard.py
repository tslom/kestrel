"""app/localguard.py: a page in the browser can't reach the API by DNS
rebinding or by a cross-site simple request."""

import pytest
from fastapi import FastAPI, WebSocket
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app import localguard
from app.localguard import LocalGuard


@pytest.fixture
def client():
    app = FastAPI()
    app.add_middleware(LocalGuard)

    @app.get("/r")
    def read():
        return {"ok": True}

    @app.post("/w")
    def write():
        return {"ok": True}

    @app.websocket("/ws")
    async def sock(ws: WebSocket):
        await ws.accept()
        await ws.send_text("hi")
        await ws.close()

    return TestClient(app, base_url="http://127.0.0.1:8788")


def test_loopback_hosts_pass(client):
    assert client.get("/r").status_code == 200
    assert client.get("/r", headers={"host": "localhost:5173"}).status_code == 200


def test_a_rebound_name_is_refused(client):
    """DNS rebinding: same-origin to the browser, but the Host says so."""
    r = client.get("/r", headers={"host": "evil.example:8788"})
    assert r.status_code == 403
    assert client.post("/w", headers={"host": "evil.example:8788"}).status_code == 403


def test_a_cross_site_post_is_refused(client):
    """A no-cors POST from any page skips preflight; its Origin gives it away."""
    r = client.post("/w", headers={"origin": "https://evil.example"})
    assert r.status_code == 403
    assert client.post("/w", headers={"origin": "null"}).status_code == 403


def test_our_own_origins_and_non_browsers_still_write(client):
    for origin in localguard.ALLOWED_ORIGINS:
        assert client.post("/w", headers={"origin": origin}).status_code == 200
    assert client.post("/w").status_code == 200          # curl, skills


def test_a_page_on_a_loopback_dev_proxy_writes(client):
    """The verify rig's Vite on :5199 keeps its own Host (changeOrigin off): same-origin through the proxy."""
    rig = {"host": "localhost:5199", "origin": "http://localhost:5199"}
    assert client.post("/w", headers=rig).status_code == 200
    with client.websocket_connect("/ws", headers=rig) as ws:
        assert ws.receive_text() == "hi"


def test_same_origin_only_counts_on_a_loopback_host(client):
    # Straight to the backend from another local port: Host is :8788.
    assert client.post("/w", headers={"origin": "http://localhost:5199"}).status_code == 403
    # Through the rig's proxy from another site: Origin isn't the Host.
    assert client.post("/w", headers={"host": "localhost:5199",
                                      "origin": "https://evil.example"}).status_code == 403
    # A rebound name that matches its own Origin is still refused on Host.
    assert client.post("/w", headers={"host": "evil.example:5199",
                                      "origin": "http://evil.example:5199"}).status_code == 403
    assert not localguard.origin_ok("http://localhost:5199", "localhost:8788")
    assert not localguard.origin_ok("http://evil.example", "evil.example")


def test_a_cross_site_get_is_left_to_cors(client):
    """Reads aren't blocked here: CORS already keeps the body from the page."""
    assert client.get("/r", headers={"origin": "https://evil.example"}).status_code == 200


def test_sockets_check_origin_too(client):
    with client.websocket_connect("/ws", headers={"origin": "http://127.0.0.1:5173"}) as ws:
        assert ws.receive_text() == "hi"
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws", headers={"origin": "https://evil.example"}):
            pass
