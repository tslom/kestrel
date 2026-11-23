"""Plaid client + item lifecycle (archive/PLAID_PLAN.md phase 1) — the Plaid analogue
of providers/oauth.py: raw HTTP, credential storage, and a small CLI. No
plaid-python dependency; every Plaid call is a JSON POST through api() here.

Secrets: the Plaid client_id/secret live in the `tokens` table (one row,
provider='plaid'); item access_tokens live in `plaid_items`. app.db only —
never in API responses, never logged. status() masks everything.

Environments: one configured env at a time (`setup` overwrites the row).
Items remember the env they were created in; sync/read only touches items
matching the configured env, so a sandbox→production switch just orphans the
sandbox items instead of mixing data.

CLI (run from backend/ with the venv active):
    python -m app.providers.plaid setup --client-id ID --secret S --env sandbox
    python -m app.providers.plaid status
    python -m app.providers.plaid sandbox-item      # test item, no browser
    python -m app.providers.plaid remove --item-id ITEM
"""

import json
import urllib.error
import urllib.request
from datetime import datetime

from .. import db
from ..config import PLAID_COUNTRY_CODES, PLAID_DAYS_REQUESTED

PLAID_HOSTS = {
    "sandbox": "https://sandbox.plaid.com",
    "production": "https://production.plaid.com",
}

# The one sandbox institution Plaid documents for scripted test items.
SANDBOX_INSTITUTION = ("ins_109508", "First Platypus Bank")


class NotConfigured(Exception):
    """No Plaid credentials stored — run the setup CLI."""


class PlaidError(Exception):
    """Non-2xx from Plaid, carrying its JSON error fields."""

    def __init__(self, error_type: str, error_code: str, message: str):
        super().__init__(f"{error_type}/{error_code}: {message}")
        self.error_type = error_type
        self.error_code = error_code


def creds() -> tuple[str, str, str, str]:
    """(client_id, secret, base_url, env) — raises NotConfigured."""
    with db.session() as con:
        row = con.execute("SELECT * FROM tokens WHERE provider='plaid'").fetchone()
    if row is None:
        raise NotConfigured(
            "Plaid not set up — run: python -m app.providers.plaid setup"
            " --client-id ... --secret ... --env sandbox")
    return row["client_id"], row["client_secret"], row["token_uri"], row["scopes"]


def api(path: str, body: dict | None = None) -> dict:
    """POST {base}{path} with client_id/secret injected. On non-2xx raises
    PlaidError from the response's error fields — never echoes the request
    body (it holds secrets)."""
    client_id, secret, base, _ = creds()
    payload = {"client_id": client_id, "secret": secret, **(body or {})}
    req = urllib.request.Request(
        base + path, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")
        try:
            err = json.loads(detail)
        except json.JSONDecodeError:
            err = {}
        raise PlaidError(err.get("error_type", "HTTP"),
                         err.get("error_code", str(e.code)),
                         err.get("error_message", detail[:300])) from None


def setup(client_id: str, secret: str, env: str) -> None:
    """Store (or replace) the Plaid credentials row. Reuses the tokens table:
    token_uri = API base URL, scopes = env, oauth token columns stay NULL."""
    if env not in PLAID_HOSTS:
        raise ValueError(f"env must be one of: {', '.join(PLAID_HOSTS)}")
    with db.session() as con, con:
        con.execute(
            "INSERT INTO tokens (provider, account, client_id, client_secret,"
            " token_uri, scopes, updated_at) VALUES ('plaid','api',?,?,?,?,?)"
            " ON CONFLICT(provider, account) DO UPDATE SET"
            " client_id=excluded.client_id, client_secret=excluded.client_secret,"
            " token_uri=excluded.token_uri, scopes=excluded.scopes,"
            " updated_at=excluded.updated_at",
            (client_id, secret, PLAID_HOSTS[env], env,
             datetime.now().isoformat(timespec="seconds")))


def get_item(item_id: str) -> dict:
    with db.session() as con:
        row = con.execute("SELECT * FROM plaid_items WHERE item_id=?",
                          (item_id,)).fetchone()
    if row is None:
        raise ValueError(f"unknown Plaid item {item_id!r}")
    return dict(row)


def list_items(env: str) -> list[dict]:
    """Full item rows (access tokens included) for one env — internal use."""
    with db.session() as con:
        rows = [dict(r) for r in con.execute(
            "SELECT * FROM plaid_items WHERE env=? ORDER BY created_at", (env,))]
    return rows


def create_link_token(item_id: str | None = None) -> str:
    """Link token for connecting a new bank, or — with item_id — for Link
    update mode (re-auth after ITEM_LOGIN_REQUIRED, or granting additional
    consent after `needs_consent`: pass the access_token, omit products).

    `additional_consented_products: ["liabilities"]` on every token (§18) —
    optional, so it never blocks Link finishing for an institution that
    doesn't support liabilities, but grants it up front where supported
    instead of requiring a second reconnect later."""
    body = {
        "client_name": "vault-dashboard",
        "language": "en",
        "country_codes": PLAID_COUNTRY_CODES,
        "user": {"client_user_id": "solo"},
        "additional_consented_products": ["liabilities"],
    }
    if item_id:
        body["access_token"] = get_item(item_id)["access_token"]
    else:
        body["products"] = ["transactions"]
        body["transactions"] = {"days_requested": PLAID_DAYS_REQUESTED}
    return api("/link/token/create", body)["link_token"]


def exchange(public_token: str, institution_name: str | None = None,
             institution_id: str | None = None) -> str:
    """public_token -> access_token, stored as a new plaid_items row.
    Returns the item_id. Cursor starts NULL = full history on first sync."""
    out = api("/item/public_token/exchange", {"public_token": public_token})
    env = creds()[3]
    with db.session() as con, con:
        con.execute(
            "INSERT INTO plaid_items (item_id, access_token, env,"
            " institution_id, institution_name, status, created_at)"
            " VALUES (?,?,?,?,?,'ok',?)",
            (out["item_id"], out["access_token"], env, institution_id,
             institution_name, datetime.now().isoformat(timespec="seconds")))
    return out["item_id"]


def remove_item(item_id: str) -> None:
    """/item/remove at Plaid, then drop the item and its cached rows."""
    item = get_item(item_id)
    api("/item/remove", {"access_token": item["access_token"]})
    with db.session() as con, con:
        con.execute("DELETE FROM fin_transactions WHERE item_id=?", (item_id,))
        con.execute("DELETE FROM fin_accounts WHERE item_id=?", (item_id,))
        con.execute("DELETE FROM plaid_items WHERE item_id=?", (item_id,))


def status() -> dict:
    """Masked view for the CLI and GET /api/plaid/status: env, institution
    names, sync state and account counts — never tokens or error internals."""
    try:
        env = creds()[3]
    except NotConfigured:
        return {"configured": False, "env": None, "items": []}
    with db.session() as con:
        rows = con.execute(
            "SELECT i.item_id, i.institution_name, i.status, i.created_at,"
            " i.last_synced_at, COUNT(a.account_id) AS accounts"
            " FROM plaid_items i LEFT JOIN fin_accounts a ON a.item_id=i.item_id"
            " WHERE i.env=? GROUP BY i.item_id ORDER BY i.created_at",
            (env,)).fetchall()
    return {
        "configured": True,
        "env": env,
        "items": [{
            "item_id": r["item_id"],
            "institution": r["institution_name"],
            "status": r["status"],
            "accounts": r["accounts"],
            "created_at": r["created_at"],
            "last_synced_at": r["last_synced_at"],
        } for r in rows],
    }


def _sandbox_item() -> str:
    """Sandbox only: mint a test item without a browser —
    /sandbox/public_token/create then the normal exchange()."""
    env = creds()[3]
    if env != "sandbox":
        raise ValueError(f"sandbox-item needs env=sandbox (configured: {env})")
    inst_id, inst_name = SANDBOX_INSTITUTION
    out = api("/sandbox/public_token/create", {
        "institution_id": inst_id,
        "initial_products": ["transactions"],
    })
    return exchange(out["public_token"], inst_name, inst_id)


# ---------- CLI:  python -m app.providers.plaid ... ----------

def _main() -> None:
    import argparse
    import sys
    p = argparse.ArgumentParser(
        prog="python -m app.providers.plaid",
        description="Set up / inspect / manage Plaid credentials and items.")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("setup", help="store Plaid credentials (overwrites)")
    s.add_argument("--client-id", required=True)
    s.add_argument("--secret", required=True)
    s.add_argument("--env", default="sandbox", choices=sorted(PLAID_HOSTS))

    sub.add_parser("status", help="env + items, masked — no tokens")

    sub.add_parser("sandbox-item",
                   help="sandbox only: create a test item without a browser")

    r = sub.add_parser("remove", help="remove an item at Plaid + locally")
    r.add_argument("--item-id", required=True)

    args = p.parse_args()
    try:
        if args.cmd == "setup":
            setup(args.client_id, args.secret, args.env)
            out = status()
        elif args.cmd == "sandbox-item":
            out = {"item_id": _sandbox_item(), **status()}
        elif args.cmd == "remove":
            remove_item(args.item_id)
            out = status()
        else:
            out = status()
    except (NotConfigured, PlaidError, ValueError) as e:
        sys.exit(f"error: {e}")
    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    _main()
