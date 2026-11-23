"""Dashboard OAuth connect flow — the browser face of providers/oauth.py.

Testing-mode Google apps expire refresh tokens after ~7 days (DESIGN.md), so
re-auth is routine; this turns it into a Connect button instead of a terminal
session. First-ever auth for an account still goes through the CLI (it needs
the downloaded client_secret_*.json); the button reuses the client creds the
CLI stored in the token row.

Flow: POST /start hands back a Google consent URL bound to our own callback
route (Desktop-app clients allow any 127.0.0.1 port/path implicitly) and, for
UI callers, opens it in the system browser (open_browser — the frontend can't,
see browser.py); Google redirects to GET /callback on the backend origin,
which exchanges the code and stores tokens; the frontend polls GET
/api/status (routes/status.py) until the account flips to authorized.

Security: same model as the rest of the app — localhost-only. Token values
never leave the backend, and the callback renders plain confirmation HTML.
"""

import time
from html import escape

from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from ..browser import open_in_browser
from ..config import (
    BACKEND_PORT, GMAIL_ACCOUNT, GOOGLE_ACCOUNT_SCOPES, GOOGLE_CALENDAR_ACCOUNT,
)
from ..providers import calendar as calendar_provider
from ..providers import email as email_provider
from ..providers import oauth

router = APIRouter(prefix="/api/oauth")

REDIRECT_URI = f"http://127.0.0.1:{BACKEND_PORT}/api/oauth/callback"

# state -> in-flight attempt (verifier, account, client creds). In-memory is
# fine: single user, and a dead entry just means clicking Connect again.
_pending: dict[str, dict] = {}
PENDING_TTL = 600  # seconds


# Which Google address each linked surface belongs to. Google's web apps key
# off `authuser=<address>`, so without this every ↗ opens under whichever
# account happens to be first in the browser's session — usually the wrong one.
# Resolved from the APIs we already hold scopes for (Gmail profile, calendar
# primary id), cached for the process: it only changes if you re-authorize.
_addresses: dict[str, str | None] = {}


def _google_address(kind: str) -> str | None:
    if kind in _addresses:
        return _addresses[kind]
    addr = None
    try:
        if kind == "gmail":
            token = oauth.get_token("google", GMAIL_ACCOUNT)
            blob = email_provider.api_request(
                "https://gmail.googleapis.com/gmail/v1/users/me/profile", token)
            addr = blob.get("emailAddress")
        elif kind == "calendar":
            token = oauth.get_token("google", GOOGLE_CALENDAR_ACCOUNT)
            blob = calendar_provider.api_request(
                "https://www.googleapis.com/calendar/v3/calendars/primary", token)
            addr = blob.get("id")
    except (oauth.NotAuthorized, RuntimeError, OSError):
        addr = None  # unauthorized/offline: links fall back to plain URLs
    _addresses[kind] = addr
    return addr


@router.get("/addresses")
def oauth_addresses():
    """{gmail, calendar} Google addresses, for authuser= on outbound links."""
    return {k: _google_address(k) for k in ("gmail", "calendar")}


class StartIn(BaseModel):
    account: str
    provider: str = "google"
    # The frontend can't open the consent tab itself in the desktop shell
    # (browser.py), so it asks us to. Off by default: the CLI opens its own.
    open_browser: bool = False


@router.post("/start")
def oauth_start(body: StartIn):
    scopes = GOOGLE_ACCOUNT_SCOPES.get(body.account)
    if not scopes:
        raise HTTPException(400, f"unknown account {body.account!r} — add it"
                                 " to config.GOOGLE_ACCOUNT_SCOPES")
    creds = oauth.stored_client(body.provider, body.account)
    if creds is None:
        raise HTTPException(
            409, f"no stored client credentials for {body.provider}/"
                 f"{body.account} — bootstrap once from a terminal:"
                 f" python -m app.providers.oauth authorize --account"
                 f" {body.account} --client-json <client_secret_*.json>")
    client_id, client_secret = creds
    url, state, verifier = oauth.build_auth_url(client_id, scopes, REDIRECT_URI)
    now = time.monotonic()
    for k in [k for k, v in _pending.items() if now - v["at"] > PENDING_TTL]:
        del _pending[k]
    _pending[state] = {"provider": body.provider, "account": body.account,
                       "client_id": client_id, "client_secret": client_secret,
                       "scopes": scopes, "verifier": verifier, "at": now}
    # Last, and only once the attempt is recorded: the callback can land the
    # moment the browser has the URL, and it needs to find `state` waiting.
    if body.open_browser:
        open_in_browser(url)
    return {"url": url, "account": body.account}


def _page(title: str, detail: str = "", status_code: int = 200) -> HTMLResponse:
    return HTMLResponse(
        f"<body style='font-family:system-ui;margin:3em'><h3>{escape(title)}</h3>"
        f"<p>{escape(detail)}</p></body>", status_code=status_code)


@router.get("/callback")
def oauth_callback(state: str = "", code: str = "", error: str = ""):
    ctx = _pending.pop(state, None) if state else None
    if ctx is None:
        return _page("Unknown or expired sign-in attempt",
                     "Go back to the dashboard and click Connect again.", 400)
    if error or not code:
        return _page("Authorization failed", error or "no code returned", 400)
    try:
        oauth.exchange_code(
            ctx["account"], code, ctx["verifier"], REDIRECT_URI,
            client_id=ctx["client_id"], client_secret=ctx["client_secret"],
            scopes=ctx["scopes"], provider=ctx["provider"])
    except RuntimeError as e:
        return _page("Token exchange failed", str(e), 502)
    return _page(f"{ctx['provider']}/{ctx['account']} connected ✓",
                 "You can close this tab — the dashboard picks it up on its own.")
