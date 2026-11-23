"""Spotify OAuth callback — the browser face of providers/spotify.py.

Spotify requires the redirect_uri to be registered *exactly*
(SPOTIFY_REDIRECT_URI: 127.0.0.1, not 'localhost', banned Nov 2025), so
unlike the Google Connect flow (routes/oauth.py, which owns its own
ephemeral-port loopback catcher inside the CLI) the redirect always lands
on this route, in the already-running backend. The `authorize` CLI in
providers/spotify.py is a thin client of the three routes below: POST
/start (client_id, first time only) to get a consent URL, open it in the
browser, then poll /status until this callback has done the code exchange.

Security: same as the rest of the app — localhost-only, token values never
leave the backend.
"""

import time
from html import escape

from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from ..browser import open_in_browser
from ..providers import spotify

router = APIRouter(prefix="/api/oauth/spotify")

# Listening data lives under /api/spotify (DESIGN.md), separate from the OAuth
# callback router above. The range/metric slice is registry-generated
# (providers_api.py); this is the MusicWidget's compact one-call summary.
data_router = APIRouter(prefix="/api/spotify")


@data_router.get("/summary")
def spotify_summary():
    return spotify.summary()


# state -> in-flight attempt. In-memory is fine: single user, one attempt
# at a time; a dead entry just means running `authorize` again.
_pending: dict[str, dict] = {}
PENDING_TTL = 600  # seconds


@router.get("/status")
def spotify_status():
    return spotify.status()


class StartIn(BaseModel):
    client_id: str | None = None
    # See routes/oauth.StartIn — the desktop shell can't open its own tab.
    open_browser: bool = False


@router.post("/start")
def spotify_start(body: StartIn):
    client_id = body.client_id or spotify.stored_client_id()
    if not client_id:
        raise HTTPException(
            409, "no Spotify client_id stored yet — pass --client-id the"
                 " first time (developer.spotify.com app's Client ID)")
    url, state, verifier = spotify.build_auth_url(client_id)
    now = time.monotonic()
    for k in [k for k, v in _pending.items() if now - v["at"] > PENDING_TTL]:
        del _pending[k]
    _pending[state] = {"client_id": client_id, "verifier": verifier, "at": now}
    if body.open_browser:  # after the pending row exists — see oauth_routes
        open_in_browser(url)
    return {"url": url}


def _page(title: str, detail: str = "", status_code: int = 200) -> HTMLResponse:
    return HTMLResponse(
        f"<body style='font-family:system-ui;margin:3em'><h3>{escape(title)}</h3>"
        f"<p>{escape(detail)}</p></body>", status_code=status_code)


@router.get("")
def spotify_callback(state: str = "", code: str = "", error: str = ""):
    ctx = _pending.pop(state, None) if state else None
    if ctx is None:
        return _page("Unknown or expired sign-in attempt",
                     "Run the authorize CLI again.", 400)
    if error or not code:
        return _page("Authorization failed", error or "no code returned", 400)
    try:
        spotify.exchange_code(code, ctx["verifier"], ctx["client_id"])
    except RuntimeError as e:
        return _page("Token exchange failed", str(e), 502)
    return _page("Spotify connected ✓",
                 "You can close this tab and return to the terminal.")
