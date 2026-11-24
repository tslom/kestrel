"""Dashboard OAuth connect flow: the browser face of providers/oauth.py.

First auth per account stays on the CLI (needs the client JSON); the Connect button reuses
the stored client creds. POST /start returns the consent URL (open_browser: the frontend
can't, see browser.py), GET /callback exchanges the code, and the frontend polls /api/status.
Token values never leave the backend. Design: DESIGN.md → OAuth and Plaid credentials.
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

# state -> in-flight attempt (verifier, account, client creds); in-memory is fine (click Connect again).
_pending: dict[str, dict] = {}
PENDING_TTL = 600  # seconds


# Google address per linked surface, for `authuser=` on outbound links (else ↗ opens under the browser's
# first account). Resolved from Gmail profile / calendar primary id; cached for the process.
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
    # The desktop shell can't open the consent tab (browser.py), so the frontend asks us to; the CLI opens its own.
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
    # Last: the callback can land the moment the browser has the URL and needs `state` recorded.
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
