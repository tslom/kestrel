"""Stands in for `tailscale serve` in the rig: strips any client-sent
Tailscale-User-Login and stamps the owner's, then proxies to the door."""
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import httpx
DOOR, LOGIN = sys.argv[1], sys.argv[2]
class H(BaseHTTPRequestHandler):
    def _go(self):
        body = self.rfile.read(int(self.headers.get("content-length") or 0)) or None
        hs = {k: v for k, v in self.headers.items() if k.lower() not in ("tailscale-user-login", "connection", "accept-encoding")}
        hs["Tailscale-User-Login"] = LOGIN
        r = httpx.request(self.command, DOOR + self.path, headers=hs, content=body, timeout=20)
        self.send_response(r.status_code)
        for k, v in r.headers.items():
            if k.lower() not in ("transfer-encoding", "connection", "content-encoding", "content-length"): self.send_header(k, v)
        self.send_header("content-length", str(len(r.content))); self.end_headers(); self.wfile.write(r.content)
    do_GET = do_POST = _go
    def log_message(self, *a): pass
ThreadingHTTPServer(("127.0.0.1", int(sys.argv[3])), H).serve_forever()
