"""What keeps a localhost-only server localhost-only against the browser.

Binding 127.0.0.1 stops other machines; it doesn't stop a web page open in a
tab on this one. Two holes, closed here for every HTTP request and socket:

* DNS rebinding. A page on evil.example re-points its name at 127.0.0.1, and
  its requests to evil.example:8788 are then same-origin to the browser, so
  CORS lets it read every response. The Host header still says evil.example,
  so anything not addressed to a loopback name is refused.
* Simple requests. A body-less or form/multipart POST from any site needs no
  CORS preflight, so it runs (the page just can't read the answer). A browser
  always sends Origin on those, so a state-changing method from an Origin we
  didn't serve is refused. A missing Origin isn't a browser (curl, skills).
"""

from __future__ import annotations

from .config import BACKEND_PORT

LOOPBACK_HOSTS = {"localhost", "127.0.0.1"}
# Starlette's TestClient sends `Host: testserver`. It isn't a public name, so
# no page can be served from it.
_TEST_HOSTS = {"testserver"}
# The Vite dev server and the app's own pages (the .app loads the backend).
ALLOWED_ORIGINS = {f"http://{host}:{port}" for host in LOOPBACK_HOSTS
                   for port in (5173, BACKEND_PORT)}
_UNSAFE = {"POST", "PUT", "PATCH", "DELETE"}


def host_ok(host: str | None) -> bool:
    """Hostname of a Host header is a loopback name (any port: the Vite proxy
    forwards its own)."""
    if not host:
        return False
    name = host.rsplit(":", 1)[0] if not host.endswith("]") else host
    return name.lower() in LOOPBACK_HOSTS | _TEST_HOSTS


def origin_ok(origin: str | None, host: str | None = None) -> bool:
    """A missing Origin, one of ours, or a page same-origin with a loopback
    dev proxy. That last is a Vite dev server on any port (the verify rig runs
    one on :5199) forwarding with its own Host: a browser only sends
    Origin == http://Host when the page posts back to the server that served
    it, and a request straight to the backend carries Host :8788, already
    allowed. So it only ever fires through a local proxy, which a local
    process could skip by sending no Origin at all: no page off this machine
    gains anything. Requires host_ok(host) first (LocalGuard checks it)."""
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
            # Refuse the handshake before accept: the client sees a 403.
            return await send({"type": "websocket.close", "code": 1008})
        await send({"type": "http.response.start", "status": 403,
                    "headers": [(b"content-type", b"text/plain")]})
        await send({"type": "http.response.body", "body": bad.encode()})
