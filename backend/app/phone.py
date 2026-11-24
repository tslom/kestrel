"""The phone door: answer a waiting card from the phone.

The one exception to localhost-only: a second server on 127.0.0.1:8791 (what
`tailscale serve` points at), never routes on :8788. Its whole route list is
`DOOR_ROUTES` (held by test_phone.py). Two keys: the owner's tailnet login and a
paired device token. Off until switched on. Design: DESIGN.md → The phone door.
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import json
import os
import secrets
import shutil
import socket
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from . import console_link, db, notify, sessions, webpush

HOST = "127.0.0.1"          # never anything else: Tailscale Serve reaches loopback
PORT = int(os.environ.get("PHONE_DOOR_PORT", "8791"))
SERVE_HTTPS_PORT = 8443     # the tailnet side: https://<machine>.<tailnet>.ts.net:8443
PAGE = Path(__file__).parent / "phone_page"

SCOPES = ("status", "approve")
PAIR_SECONDS = 600
PAIR_TRIES = 5
PUSH_AFTER_S = 20.0         # a card answered at the desk within this never pushes
WATCH_EVERY_S = 4.0
DETAIL_CHARS = 300
EVENTS_KEPT = 500
VAPID_SUBJECT = os.environ.get("PHONE_VAPID_SUBJECT", "mailto:kestrel@example.com")

_TAILSCALE_APP = "/Applications/Tailscale.app/Contents/MacOS/Tailscale"


# ---------- what's stored (app.db: phone_state, phone_devices, phone_events) ----------

def _get(key: str, con=None) -> str | None:
    with db.borrow(con) as con:
        r = con.execute("SELECT value FROM phone_state WHERE key = ?", (key,)).fetchone()
    return r["value"] if r else None


def _set(key: str, value: str | None) -> None:
    with db.session() as con, con:
        if value is None:
            con.execute("DELETE FROM phone_state WHERE key = ?", (key,))
        else:
            con.execute("INSERT INTO phone_state (key, value) VALUES (?, ?)"
                        " ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def log(kind: str, detail: str = "", device: int | None = None) -> None:
    """The door's record: pairings, answers, pushes, refusals. A phone that
    went quiet and a day with no cards look alike without it."""
    with db.session() as con, con:
        con.execute("INSERT INTO phone_events (at, kind, detail, device_id) VALUES (?,?,?,?)",
                    (_now(), kind, detail[:300], device))
        con.execute("DELETE FROM phone_events WHERE id <= (SELECT MAX(id) FROM phone_events) - ?",
                    (EVENTS_KEPT,))


def enabled() -> bool:
    return _get("enabled") == "1"


def owner() -> str:
    return (_get("owner_login") or "").strip().lower()


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def vapid_keys() -> tuple[str, str]:
    """The server's push key pair, made on first use. The private half stays
    in app.db and is never in a response."""
    private, public = _get("vapid_private"), _get("vapid_public")
    if not (private and public):
        private, public = webpush.new_keypair()
        _set("vapid_private", private)
        _set("vapid_public", public)
    return private, public


def devices() -> list[dict]:
    with db.session() as con:
        rows = con.execute(
            "SELECT id, name, scopes, created_at, last_seen_at, push IS NOT NULL AS push"
            " FROM phone_devices WHERE revoked_at IS NULL ORDER BY id").fetchall()
    return [{**dict(r), "push": bool(r["push"]), "scopes": r["scopes"].split(",")} for r in rows]


def revoke(device_id: int) -> None:
    with db.session() as con, con:
        con.execute("UPDATE phone_devices SET revoked_at = ?, push = NULL"
                    " WHERE id = ? AND revoked_at IS NULL", (_now(), device_id))
    log("revoked", device=device_id)


# ---------- pairing ----------

# One code at a time, in memory: a restart voids it, which is the safe way round.
_pair: dict[str, Any] = {}


def new_pair_code() -> str:
    code = f"{secrets.randbelow(10 ** 8):08d}"
    _pair.clear()
    _pair.update(hash=_hash(code), expires=time.monotonic() + PAIR_SECONDS, tries=0)
    return code


def redeem(code: str, name: str) -> str:
    """Trade a live pairing code for a device token. The code is spent either
    way: by use, by time, or by too many wrong guesses."""
    digits = "".join(c for c in str(code) if c.isdigit())
    if not _pair or time.monotonic() > _pair["expires"]:
        _pair.clear()
        raise HTTPException(403, "no pairing code is live; make one in Kestrel's Settings")
    if not hmac.compare_digest(_hash(digits), _pair["hash"]):
        _pair["tries"] += 1
        if _pair["tries"] >= PAIR_TRIES:
            _pair.clear()
            log("pair-voided", "too many wrong codes")
        raise HTTPException(403, "that code isn't right")
    _pair.clear()
    token = secrets.token_urlsafe(32)
    with db.session() as con, con:
        cur = con.execute(
            "INSERT INTO phone_devices (name, token_hash, scopes, created_at) VALUES (?,?,?,?)",
            (" ".join(str(name or "").split())[:40] or "Phone", _hash(token),
             ",".join(SCOPES), _now()))
    log("paired", device=cur.lastrowid)
    return token


# ---------- the cards ----------

def _tab_label(tab: str) -> str:
    with db.session() as con:
        r = con.execute("SELECT label, profile FROM console_sessions WHERE tab = ?"
                        " ORDER BY last_at DESC LIMIT 1", (tab,)).fetchone()
    if r and r["label"]:
        return " ".join(r["label"].split())[:60]
    return f"{(r['profile'] if r and r['profile'] else 'A')} tab".capitalize()


def _short(text: Any, n: int = DETAIL_CHARS) -> str:
    return " ".join(str(text or "").split())[:n]


def open_cards() -> list[dict]:
    """Every permission card still waiting, across the live tabs. The same
    one-line detail the desk's card leads with, capped; never the full input.
    Questions and plans aren't here: a plan is pages long, and a question's
    answer is typed, which this slice doesn't do."""
    out = []
    for tab, link in list(console_link.links.items()):
        for m in link.turn or []:
            rid = m.get("request_id")
            if m.get("type") != "permission_request" or not rid or m.get("plan"):
                continue
            if not link.open_request(rid):
                continue
            preview = (m.get("preview") or [{}])[0] or {}
            tool = str(m.get("tool") or "").removeprefix("mcp__vault__")
            out.append({
                "id": rid, "tab": tab, "title": _tab_label(tab), "tool": tool,
                "detail": _short(preview.get("lead") or preview.get("title")
                                 or sessions.tool_detail(m.get("input") or {})),
                "count": len(m.get("inputs") or []) or 1,
                "why": _short(m.get("why")),
                "warning": _short(m.get("guard") or m.get("danger"), 200),
            })
    return out


async def answer(card_id: str, allow: bool, device: int) -> None:
    """Put the phone's answer where the desk's would go: the tab's inbox, as
    the same `permission` frame. An id that isn't open any more is refused,
    so a tap that arrives late can't land on a later card."""
    card = next((c for c in open_cards() if c["id"] == card_id), None)
    link = console_link.links.get(card["tab"]) if card else None
    if card is None or link is None:
        raise HTTPException(409, "that card isn't waiting any more")
    link.inbox.put_nowait(json.dumps({
        "type": "permission", "request_id": card_id, "allow": bool(allow), "always": False,
        "reason": None if allow else "Denied from the phone."}))
    # The page at the desk still shows the card; tell it who answered.
    await link.send_json({"type": "permission_answered", "request_id": card_id,
                          "allow": bool(allow), "by": "phone"})
    log("allowed" if allow else "denied", f"{card['tool']} in {card['title']}", device)


# ---------- the push ----------

def push_all(payload: dict) -> dict:
    """Send one notification to every paired device that subscribed."""
    private, public = vapid_keys()
    with db.session() as con:
        rows = con.execute("SELECT id, push FROM phone_devices"
                           " WHERE revoked_at IS NULL AND push IS NOT NULL").fetchall()
    out = {"sent": 0, "failed": 0, "errors": []}
    for r in rows:
        try:
            webpush.send(json.loads(r["push"]), payload, private_b64=private,
                         public_b64=public, subject=VAPID_SUBJECT)
            out["sent"] += 1
        except webpush.PushError as e:
            out["failed"] += 1
            out["errors"].append(str(e))
            if e.gone:
                with db.session() as con, con:
                    con.execute("UPDATE phone_devices SET push = NULL WHERE id = ?", (r["id"],))
            log("push-failed", str(e), r["id"])
    return out


_first_seen: dict[str, float] = {}
_pushed: set[str] = set()


async def watch_once(now: float | None = None) -> int:
    """Push for each card that has waited PUSH_AFTER_S with nobody at the
    desk. Once per card. The text names the tab, never what the card says:
    Apple relays it, encrypted or not."""
    now = time.monotonic() if now is None else now
    cards = open_cards()
    live = {c["id"] for c in cards}
    for gone in set(_first_seen) - live:
        _first_seen.pop(gone, None)
        _pushed.discard(gone)
    due = [c for c in cards
           if c["id"] not in _pushed and now - _first_seen.setdefault(c["id"], now) >= PUSH_AFTER_S]
    if not due or await asyncio.to_thread(notify.at_desk):
        return 0
    sent = 0
    for c in due:
        _pushed.add(c["id"])
        got = await asyncio.to_thread(push_all, {
            "title": "Kestrel", "body": f"{c['title']} needs an answer",
            "url": f"/#card={c['id']}", "tag": c["id"]})
        if got["sent"]:
            sent += 1
            log("pushed", c["title"])
    return sent


# ---------- the door ----------

door = FastAPI(title="Kestrel phone door", docs_url=None, redoc_url=None, openapi_url=None)

# Everything a phone can ask for. test_phone.py holds the app to this list,
# so a route added here by habit fails a test instead of opening something.
DOOR_ROUTES = {
    ("GET", "/"), ("GET", "/app.js"), ("GET", "/app.css"), ("GET", "/sw.js"),
    ("GET", "/manifest.webmanifest"), ("GET", "/icon.png"),
    ("POST", "/api/pair"), ("GET", "/api/status"), ("POST", "/api/cards/{card_id}"),
    ("GET", "/api/push/key"), ("POST", "/api/push/subscribe"),
}
_STATIC = {"/": ("index.html", "text/html; charset=utf-8"),
           "/app.js": ("app.js", "text/javascript; charset=utf-8"),
           "/app.css": ("app.css", "text/css; charset=utf-8"),
           "/sw.js": ("sw.js", "text/javascript; charset=utf-8"),
           "/manifest.webmanifest": ("manifest.webmanifest", "application/manifest+json"),
           "/icon.png": ("icon.png", "image/png")}
_HEADERS = [
    (b"content-security-policy",
     b"default-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"),
    (b"x-content-type-options", b"nosniff"),
    (b"referrer-policy", b"no-referrer"),
    (b"cache-control", b"no-store"),
]


def host_ok(host: str | None) -> bool:
    """Addressed to this machine's tailnet name, or to loopback (Serve's own
    hop, and tests). Anything else is a rebinding page."""
    name = (host or "").rsplit(":", 1)[0].lower()
    return name in ("127.0.0.1", "localhost", "testserver") or name.endswith(".ts.net")


class DoorGuard:
    """Outermost, pure ASGI. Before any route runs: the request is addressed
    to us, carries the owner's tailnet login, and (if it changes anything)
    comes from our own page as JSON. Sockets are refused outright."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "lifespan":
            return await self.app(scope, receive, send)
        if scope["type"] != "http":
            return await send({"type": "websocket.close", "code": 1008})
        headers = {k.decode("latin-1"): v.decode("latin-1") for k, v in scope["headers"]}
        host, who = headers.get("host"), owner()
        bad = None
        if not host_ok(host):
            bad = "host not allowed"
        elif not who:
            bad = "no owner set: finish Settings > Phone on the Mac"
        elif headers.get("tailscale-user-login", "").strip().lower() != who:
            bad = "not the owner's tailnet login"
        elif scope["method"] not in ("GET", "HEAD"):
            origin = headers.get("origin")
            if origin is not None and origin not in (f"https://{host}", f"http://{host}"):
                bad = "origin not allowed"
            elif not headers.get("content-type", "").startswith("application/json"):
                bad = "json only"
        if bad:
            await send({"type": "http.response.start", "status": 403,
                        "headers": [(b"content-type", b"text/plain"), *_HEADERS]})
            return await send({"type": "http.response.body", "body": bad.encode()})

        async def with_headers(message):
            if message["type"] == "http.response.start":
                have = {k for k, _ in message.get("headers", [])}
                message["headers"] = [*message.get("headers", []),
                                      *(h for h in _HEADERS if h[0] not in have)]
            await send(message)
        await self.app(scope, receive, with_headers)


door.add_middleware(DoorGuard)


def device(request: Request) -> dict:
    """The paired device a request's bearer token belongs to."""
    kind, _, token = request.headers.get("authorization", "").partition(" ")
    if kind.lower() != "bearer" or not token:
        raise HTTPException(401, "pair this phone first")
    with db.session() as con, con:
        r = con.execute("SELECT id, scopes FROM phone_devices"
                        " WHERE token_hash = ? AND revoked_at IS NULL", (_hash(token),)).fetchone()
        if r is None:
            raise HTTPException(401, "this phone isn't paired any more")
        con.execute("UPDATE phone_devices SET last_seen_at = ? WHERE id = ?", (_now(), r["id"]))
    return {"id": r["id"], "scopes": set(r["scopes"].split(","))}


def scoped(scope: str):
    def check(dev: dict = Depends(device)) -> dict:
        if scope not in dev["scopes"]:
            raise HTTPException(403, f"this phone can't {scope}")
        return dev
    return check


def _static(path: str):
    name, media = _STATIC[path]

    def serve() -> FileResponse:
        # The service worker and its shell must revalidate, or a fixed page
        # never reaches a phone that cached the broken one.
        return FileResponse(PAGE / name, media_type=media, headers={"cache-control": "no-cache"})
    return serve


for _path in _STATIC:
    door.get(_path)(_static(_path))


class PairIn(BaseModel):
    code: str = Field(min_length=8, max_length=12)
    name: str = Field(default="", max_length=60)


class AnswerIn(BaseModel):
    allow: bool


class SubscribeIn(BaseModel):
    subscription: dict


@door.post("/api/pair")
def pair(body: PairIn):
    return {"token": redeem(body.code, body.name), "scopes": list(SCOPES)}


@door.get("/api/status")
def status(dev: dict = Depends(scoped("status"))):
    cards = open_cards()
    working = sum(1 for link in console_link.links.values() if link.turn is not None)
    return {"at": _now(), "cards": cards, "working": working,
            "can_answer": "approve" in dev["scopes"]}


@door.post("/api/cards/{card_id}")
async def answer_card(card_id: str, body: AnswerIn, dev: dict = Depends(scoped("approve"))):
    await answer(card_id, body.allow, dev["id"])
    return {"ok": True}


@door.get("/api/push/key")
def push_key(_dev: dict = Depends(scoped("status"))):
    return {"key": vapid_keys()[1]}


@door.post("/api/push/subscribe")
def push_subscribe(body: SubscribeIn, dev: dict = Depends(scoped("status"))):
    if not webpush.subscription_ok(body.subscription):
        raise HTTPException(422, "not a push subscription from a known push service")
    keep = {"endpoint": body.subscription["endpoint"],
            "keys": {k: body.subscription["keys"][k] for k in ("p256dh", "auth")}}
    with db.session() as con, con:
        con.execute("UPDATE phone_devices SET push = ? WHERE id = ?", (json.dumps(keep), dev["id"]))
    log("subscribed", device=dev["id"])
    return {"ok": True}


# ---------- the listener ----------

class _DoorServer(uvicorn.Server):
    """A second server in the backend's loop. It must not take the process's
    signals: the main server owns shutdown."""

    @contextlib.contextmanager
    def capture_signals(self):
        yield


_server: _DoorServer | None = None
_serving: asyncio.Task | None = None     # held so the loop doesn't drop it
_watcher: asyncio.Task | None = None
_bind_error: str | None = None


def listening() -> bool:
    return _server is not None and not _server.should_exit


async def _watch() -> None:
    while True:
        await asyncio.sleep(WATCH_EVERY_S)
        try:
            await watch_once()
        except Exception as e:   # a failed push never ends the watch
            print(f"[phone] watch failed: {e!r}")


BIND_RETRIES = 6            # a restart's old process may hold the port a moment longer
BIND_RETRY_S = 5.0


async def sync(retries: int = BIND_RETRIES) -> None:
    """Open the door if Settings says on, close it if off. Called at startup
    and whenever the switch moves."""
    global _server, _serving, _watcher, _bind_error
    if not enabled():
        return await stop()
    if listening():
        return
    # Bound here, not by uvicorn: its answer to a taken port is sys.exit,
    # which would take the whole backend down with it.
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind((HOST, PORT))
    except OSError as e:
        sock.close()
        _bind_error = f"port {PORT} is taken ({e.strerror})"
        print(f"[phone] door not opened: {_bind_error}")
        if retries > 0:
            asyncio.get_running_loop().call_later(
                BIND_RETRY_S, lambda: asyncio.ensure_future(sync(retries - 1)))
        return
    _bind_error = None
    _server = _DoorServer(uvicorn.Config(door, log_level="warning", lifespan="off",
                                         access_log=False, server_header=False))
    _serving = asyncio.create_task(_server.serve(sockets=[sock]))
    _watcher = asyncio.create_task(_watch())
    print(f"[phone] door open on {HOST}:{PORT}")


async def stop() -> None:
    global _server, _watcher
    if _server is not None:
        _server.should_exit = True      # its task ends by itself and frees the port
        _server = None
    if _watcher is not None:
        _watcher.cancel()
        _watcher = None


# ---------- Settings (the main app, this machine only) ----------

router = APIRouter(prefix="/api/phone")


def _tailscale_exe() -> str | None:
    return shutil.which("tailscale") or (_TAILSCALE_APP if Path(_TAILSCALE_APP).exists() else None)


def tailscale() -> dict:
    """What the Tailscale CLI says, best effort: is it up, this machine's
    tailnet name, the login, and whether Serve points at the door."""
    exe = _tailscale_exe()
    if not exe:
        return {"installed": False}
    out: dict[str, Any] = {"installed": True, "cli": exe, "online": False, "served": False}
    try:
        st = json.loads(subprocess.run([exe, "status", "--json"], capture_output=True,
                                       text=True, timeout=5).stdout or "{}")
        me = st.get("Self") or {}
        out["online"] = st.get("BackendState") == "Running" and bool(me.get("Online"))
        out["state"] = st.get("BackendState")
        out["name"] = str(me.get("DNSName") or "").rstrip(".") or None
        out["https"] = bool(st.get("CertDomains"))
        user = (st.get("User") or {}).get(str(me.get("UserID"))) or {}
        out["login"] = user.get("LoginName")
        serve = subprocess.run([exe, "serve", "status", "--json"], capture_output=True,
                               text=True, timeout=5).stdout
        out["served"] = f"{HOST}:{PORT}" in serve
    except (OSError, subprocess.TimeoutExpired, ValueError) as e:
        out["error"] = str(e)[:200]
    return out


def phone_url(ts: dict | None = None) -> str | None:
    """Where the phone goes: what was typed in Settings, else this machine's
    tailnet name on the Serve port."""
    typed = (_get("url") or "").strip().rstrip("/")
    if typed:
        return typed
    name = (ts if ts is not None else tailscale()).get("name")
    return f"https://{name}:{SERVE_HTTPS_PORT}" if name else None


def serve_command(ts: dict) -> str:
    exe = "tailscale" if shutil.which("tailscale") else ts.get("cli") or "tailscale"
    return f"{exe} serve --bg --https={SERVE_HTTPS_PORT} http://{HOST}:{PORT}"


def state() -> dict:
    ts = tailscale()
    with db.session() as con:
        last = con.execute("SELECT MAX(last_seen_at) FROM phone_devices"
                           " WHERE revoked_at IS NULL").fetchone()[0]
        events = [dict(r) for r in con.execute(
            "SELECT at, kind, detail FROM phone_events ORDER BY id DESC LIMIT 8")]
    return {"enabled": enabled(), "listening": listening(), "bind_error": _bind_error,
            "port": PORT, "owner_login": owner() or None, "suggested_login": ts.get("login"),
            "url": phone_url(ts), "url_typed": bool((_get("url") or "").strip()),
            "tailscale": {k: ts.get(k) for k in ("installed", "online", "state", "https",
                                                 "served", "name")},
            "serve_command": serve_command(ts), "devices": devices(),
            "last_seen_at": last, "events": events}


class SettingsIn(BaseModel):
    enabled: bool | None = None
    owner_login: str | None = Field(default=None, max_length=120)
    url: str | None = Field(default=None, max_length=200, pattern=r"^(https://[A-Za-z0-9.\-]+(:\d+)?/?)?$")


@router.get("")
async def get_state():
    return await asyncio.to_thread(state)


@router.put("")
async def put_settings(body: SettingsIn):
    if body.owner_login is not None:
        _set("owner_login", body.owner_login.strip().lower() or None)
    if body.url is not None:
        _set("url", body.url.strip() or None)
    if body.enabled is not None:
        if body.enabled and not owner():
            raise HTTPException(422, "set your Tailscale login first: it is one of the door's two keys")
        _set("enabled", "1" if body.enabled else None)
        log("switched-on" if body.enabled else "switched-off")
    await sync()
    return await asyncio.to_thread(state)


@router.post("/pair")
async def start_pairing():
    """A fresh code, and a QR of the phone's URL with the code in its
    fragment (a fragment never reaches a server or a log)."""
    if not enabled():
        raise HTTPException(409, "turn phone access on first")
    code = new_pair_code()
    url = await asyncio.to_thread(phone_url)
    qr = None
    if url:
        import io

        import segno
        buf = io.BytesIO()
        segno.make(f"{url}/#pair={code}", error="m").save(
            buf, kind="svg", scale=5, border=2, xmldecl=False, svgns=True, nl=False)
        qr = buf.getvalue().decode()
    return {"code": f"{code[:4]}-{code[4:]}", "expires_in": PAIR_SECONDS, "url": url, "qr": qr}


@router.delete("/devices/{device_id}")
async def delete_device(device_id: int):
    await asyncio.to_thread(revoke, device_id)
    return {"ok": True}


@router.post("/test-push")
async def test_push():
    """Send a notification now, to check the road end to end."""
    return await asyncio.to_thread(push_all, {
        "title": "Kestrel", "body": "Test: notifications reach this phone.", "url": "/", "tag": "test"})
