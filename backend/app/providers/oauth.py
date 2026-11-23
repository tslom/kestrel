"""OAuth helper — localhost redirect flow + token refresh (IDEAS §0).

One token row per (provider, account) in app.db `tokens`. Two Google
accounts are expected: 'school' (Workspace — calendar/gmail) and 'personal'
(health/fitbit); scope presets live in config.GOOGLE_ACCOUNT_SCOPES.

Security: tokens + client secrets live ONLY in app.db on this machine. The
app is localhost-only (backend binds 127.0.0.1) — that is the whole security
model. Never return token values from an API route or print them; status
output masks everything.

Authorize (interactive, run from a terminal — opens a browser):
    python -m app.providers.oauth authorize --account school \
        --client-json ~/Downloads/client_secret_*.json
Then anywhere in the app:  access_token = oauth.get_token("google", "school")
Re-auth after that (e.g. the Testing-mode ~7-day refresh-token expiry) is the
dashboard's Connect button — app/routes/oauth.py drives build_auth_url()/
exchange_code() through the backend's own callback route.

Google Cloud prerequisite: an OAuth client of type **Desktop app** (loopback
redirects to http://127.0.0.1:<any-port> are allowed implicitly — no redirect
URI registration needed).
"""

import base64
import hashlib
import json
import secrets
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from .. import db
from ..config import GOOGLE_ACCOUNT_SCOPES

GOOGLE_AUTH_URI = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URI = "https://oauth2.googleapis.com/token"

# refresh access tokens this close to expiry rather than risk a mid-call 401
EXPIRY_MARGIN = timedelta(seconds=60)


class NotAuthorized(Exception):
    """No usable token row — run the authorize CLI for this (provider, account)."""


@contextmanager
def as_value_error() -> Iterator[None]:
    """Re-raise NotAuthorized as ValueError, same message.

    ValueError is the providers' "bad request" — providers.get() callers turn
    it into a 400 and the CLI into a one-line error — which is the right
    answer to a read that needs a token nobody has granted. Used inside
    offline.best_effort(), which re-raises anything that isn't the network."""
    try:
        yield
    except NotAuthorized as e:
        raise ValueError(str(e)) from None


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _post_form(url: str, data: dict) -> dict:
    body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(url, data=body, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")
        raise RuntimeError(f"token endpoint {e.code}: {detail}") from None


def _save(provider: str, account: str, *, client_id: str, client_secret: str,
          token_uri: str, scopes: str, access_token: str | None,
          refresh_token: str | None, expires_at: str | None) -> None:
    with db.session() as con, con:
        con.execute(
            "INSERT INTO tokens (provider, account, client_id, client_secret,"
            " access_token, refresh_token, token_uri, scopes, expires_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(provider, account) DO UPDATE SET"
            " client_id=excluded.client_id, client_secret=excluded.client_secret,"
            " access_token=excluded.access_token,"
            # a refresh grant may omit refresh_token — keep the one we have
            " refresh_token=COALESCE(excluded.refresh_token, tokens.refresh_token),"
            " token_uri=excluded.token_uri, scopes=excluded.scopes,"
            " expires_at=excluded.expires_at, updated_at=excluded.updated_at",
            (provider, account, client_id, client_secret, access_token,
             refresh_token, token_uri, scopes, expires_at,
             _utcnow().isoformat(timespec="seconds")))


def _row(provider: str, account: str) -> dict | None:
    with db.session() as con:
        row = con.execute("SELECT * FROM tokens WHERE provider=? AND account=?",
                          (provider, account)).fetchone()
    return dict(row) if row else None


# ---------- authorization-code + PKCE building blocks ----------
# Shared by the CLI flow below and the dashboard Connect flow (app/routes/oauth.py).

def build_auth_url(client_id: str, scopes: list[str],
                   redirect_uri: str) -> tuple[str, str, str]:
    """(url, state, verifier) for one authorization attempt."""
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(16)
    url = GOOGLE_AUTH_URI + "?" + urllib.parse.urlencode({
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(scopes),
        "access_type": "offline",   # ask for a refresh_token
        "prompt": "consent",        # re-issue refresh_token on re-auth
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    })
    return url, state, verifier


def exchange_code(account: str, code: str, verifier: str, redirect_uri: str, *,
                  client_id: str, client_secret: str, scopes: list[str],
                  provider: str = "google") -> dict:
    """code -> tokens, stored; returns the masked status row."""
    tok = _post_form(GOOGLE_TOKEN_URI, {
        "code": code,
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": redirect_uri,
        "grant_type": "authorization_code",
        "code_verifier": verifier,
    })
    expires_at = (_utcnow() + timedelta(seconds=tok.get("expires_in", 3600))
                  ).isoformat(timespec="seconds")
    _save(provider, account, client_id=client_id, client_secret=client_secret,
          token_uri=GOOGLE_TOKEN_URI, scopes=" ".join(scopes),
          access_token=tok["access_token"],
          refresh_token=tok.get("refresh_token"), expires_at=expires_at)
    return status(provider, account)


def stored_client(provider: str, account: str) -> tuple[str, str] | None:
    """(client_id, client_secret) kept from a previous authorize, if any —
    what lets the dashboard re-auth without the client_secret_*.json file."""
    row = _row(provider, account)
    if row and row["client_id"] and row["client_secret"]:
        return row["client_id"], row["client_secret"]
    return None


# ---------- localhost redirect flow ----------

class _RedirectCatcher(BaseHTTPRequestHandler):
    """Catches exactly one GET /?code=...&state=... on 127.0.0.1."""
    result: dict = {}

    def do_GET(self):
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        _RedirectCatcher.result = {k: v[0] for k, v in q.items()}
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(b"<h3>Authorized \xe2\x9c\x93 &mdash; you can close this tab.</h3>")

    def log_message(self, *_):  # keep the CLI output clean
        pass


def authorize(account: str, scopes: list[str], client_id: str, client_secret: str,
              provider: str = "google", timeout: int = 300) -> dict:
    """Interactive: open browser, catch the loopback redirect, store tokens."""
    if not scopes:
        raise ValueError(f"no scopes for account {account!r} — pass --scopes")
    server = HTTPServer(("127.0.0.1", 0), _RedirectCatcher)
    port = server.server_address[1]
    redirect_uri = f"http://127.0.0.1:{port}/"
    url, state, verifier = build_auth_url(client_id, scopes, redirect_uri)

    print(f"Opening browser for {provider}/{account} "
          f"(pick the {account} Google account!):\n\n  {url}\n")
    threading.Thread(target=webbrowser.open, args=(url,), daemon=True).start()
    server.timeout = timeout
    _RedirectCatcher.result = {}
    server.handle_request()  # blocks for one request or timeout
    server.server_close()

    result = _RedirectCatcher.result
    if "error" in result:
        raise RuntimeError(f"authorization denied: {result['error']}")
    if not result.get("code"):
        raise RuntimeError(f"no redirect received within {timeout}s")
    if result.get("state") != state:
        raise RuntimeError("state mismatch — possible CSRF, aborting")

    return exchange_code(account, result["code"], verifier, redirect_uri,
                         client_id=client_id, client_secret=client_secret,
                         scopes=scopes, provider=provider)


# ---------- token access + refresh ----------

def get_token(provider: str, account: str) -> str:
    """Valid access token for (provider, account), refreshing if near expiry."""
    row = _row(provider, account)
    if row is None or not row["refresh_token"]:
        raise NotAuthorized(
            f"no tokens for {provider}/{account} — use the dashboard Connect"
            f" button, or: python -m app.providers.oauth authorize"
            f" --account {account}")
    expires_at = row["expires_at"] and datetime.fromisoformat(row["expires_at"])
    if row["access_token"] and expires_at and _utcnow() + EXPIRY_MARGIN < expires_at:
        return row["access_token"]
    return refresh(provider, account)


def refresh(provider: str, account: str) -> str:
    """Force-refresh the access token via the stored refresh_token."""
    row = _row(provider, account)
    if row is None or not row["refresh_token"]:
        raise NotAuthorized(f"no refresh_token for {provider}/{account}")
    try:
        tok = _post_form(row["token_uri"], {
            "client_id": row["client_id"],
            "client_secret": row["client_secret"],
            "refresh_token": row["refresh_token"],
            "grant_type": "refresh_token",
        })
    except RuntimeError as e:
        if "invalid_grant" in str(e):
            # Dead refresh token (Testing-mode ~7-day expiry, or revoked).
            # Drop it so status()/get_token() say needs-auth instead of
            # retrying a corpse every poll; keep the client creds so the
            # dashboard Connect button can re-auth without the client JSON.
            with db.session() as con, con:
                con.execute(
                    "UPDATE tokens SET access_token=NULL, refresh_token=NULL,"
                    " updated_at=? WHERE provider=? AND account=?",
                    (_utcnow().isoformat(timespec="seconds"), provider, account))
            raise NotAuthorized(
                f"refresh token for {provider}/{account} expired or was revoked"
                " — reconnect via the dashboard Connect button") from None
        raise
    expires_at = (_utcnow() + timedelta(seconds=tok.get("expires_in", 3600))
                  ).isoformat(timespec="seconds")
    _save(provider, account, client_id=row["client_id"],
          client_secret=row["client_secret"], token_uri=row["token_uri"],
          scopes=row["scopes"], access_token=tok["access_token"],
          refresh_token=tok.get("refresh_token"), expires_at=expires_at)
    return tok["access_token"]


def status(provider: str | None = None, account: str | None = None) -> dict | list[dict]:
    """Masked view of stored tokens — safe to print / return, never the secrets."""
    with db.session() as con:
        rows = [dict(r) for r in con.execute(
            "SELECT provider, account, scopes, expires_at, updated_at,"
            " refresh_token IS NOT NULL AS has_refresh_token FROM tokens"
            " ORDER BY provider, account")]
    if provider and account:
        for r in rows:
            if r["provider"] == provider and r["account"] == account:
                return r
        return {}
    return rows


# ---------- CLI:  python -m app.providers.oauth ... ----------

def _load_client(args) -> tuple[str, str]:
    if args.client_json:
        blob = json.loads(Path(args.client_json).expanduser().read_text())
        if "web" in blob:
            # web clients need pre-registered redirect URIs -> the loopback
            # flow dies with redirect_uri_mismatch. Only Desktop-app works.
            sys.exit("this is a 'Web application' OAuth client — the loopback"
                     " flow needs type 'Desktop app'. Create one:"
                     " Cloud console > Credentials > Create credentials >"
                     " OAuth client ID > Desktop app, download its JSON.")
        installed = blob.get("installed") or blob
        return installed["client_id"], installed["client_secret"]
    if args.client_id and args.client_secret:
        return args.client_id, args.client_secret
    row = _row(args.provider, args.account)  # re-auth reuses stored client creds
    if row:
        return row["client_id"], row["client_secret"]
    sys.exit("need --client-json (Google's downloaded Desktop-app client file)"
             " or --client-id/--client-secret")


def _main() -> None:
    import argparse
    p = argparse.ArgumentParser(
        prog="python -m app.providers.oauth",
        description="Authorize / inspect / refresh OAuth tokens in app.db.")
    sub = p.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("authorize", help="interactive localhost redirect flow")
    a.add_argument("--provider", default="google")
    a.add_argument("--account", required=True,
                   help=f"one of {list(GOOGLE_ACCOUNT_SCOPES)} (or any name)")
    a.add_argument("--scopes", nargs="*", default=None,
                   help="override the config.GOOGLE_ACCOUNT_SCOPES preset")
    a.add_argument("--client-json", help="path to Google's client_secret_*.json")
    a.add_argument("--client-id")
    a.add_argument("--client-secret")

    sub.add_parser("status", help="list stored tokens (masked)")

    r = sub.add_parser("refresh", help="force an access-token refresh")
    r.add_argument("--provider", default="google")
    r.add_argument("--account", required=True)

    args = p.parse_args()
    if args.cmd == "authorize":
        scopes = args.scopes or GOOGLE_ACCOUNT_SCOPES.get(args.account, [])
        client_id, client_secret = _load_client(args)
        out = authorize(args.account, scopes, client_id, client_secret,
                        provider=args.provider)
    elif args.cmd == "refresh":
        refresh(args.provider, args.account)
        out = status(args.provider, args.account)
    else:
        out = status()
    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    _main()
