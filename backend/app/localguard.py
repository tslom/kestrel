"""Keeps a localhost-only server localhost-only against the browser.

Refuses non-loopback Host (DNS rebinding) and state-changing requests whose
Origin we didn't serve (CORS-free simple POSTs), on HTTP and WebSocket.
Design: DESIGN.md → `app/localguard.py`.
"""

from __future__ import annotations

from .config import BACKEND_PORT

LOOPBACK_HOSTS = {"localhost", "127.0.0.1"}
# TestClient's Host; not a public name.
_TEST_HOSTS = {"testserver"}
ALLOWED_ORIGINS = {f"http://{host}:{port}" for host in LOOPBACK_HOSTS
                   for port in (5173, BACKEND_PORT)}
_UNSAFE = {"POST", "PUT", "PATCH", "DELETE"}


def host_ok(host: str | None) -> bool:
    """The Host header's name is loopback (any port: the Vite proxy forwards its own)."""
    if not host:
        return False
    name = host.rsplit(":", 1)[0] if not host.endswith("]") else host
    return name.lower() in LOOPBACK_HOSTS | _TEST_HOSTS


def origin_ok(origin: str | None, host: str | None = None) -> bool:
    """A missing Origin, one of ours, or Origin == http://Host through a loopback
    dev proxy (the verify rig's :5199). Requires host_ok(host) first."""
    if origin is None or origin in ALLOWED_ORIGINS:
        return True
    return bool(host) and host_ok(host) and origin == f"http://{host}"


class LocalGuard:
    """Pure ASGI, so it covers WebSockets too (BaseHTTPMiddleware doesn't)."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            return await self.app(scope, receive, send)
        headers = {k.decode("latin-1"): v.decode("latin-1")
                   for k, v in scope.get("headers", [])}
        bad = None
        if not host_ok(headers.get("host")):
            bad = "host not allowed"
        elif scope["type"] == "websocket" or scope.get("method") in _UNSAFE:
            if not origin_ok(headers.get("origin"), headers.get("host")):
                bad = "origin not allowed"
        if bad is None:
            return await self.app(scope, receive, send)
        if scope["type"] == "websocket":
            return await send({"type": "websocket.close", "code": 1008})
        await send({"type": "http.response.start", "status": 403,
                    "headers": [(b"content-type", b"text/plain")]})
        await send({"type": "http.response.body", "body": bad.encode()})
