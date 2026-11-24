"""Plaid item lifecycle: the browser face of providers/plaid.py (connect/reconnect/remove;
finance data is the registry's GET /api/finance). /exchange and /sync run the sync before
answering. The /setup secret is stored backend-side and never echoed; tokens never leave app.db.
Design: DESIGN.md → OAuth and Plaid credentials.
"""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from .. import db
from ..providers import finance, plaid

router = APIRouter(prefix="/api/plaid")


@router.get("/status")
def plaid_status():
    """Masked: configured?, env, items (institution, status, sync times)."""
    return plaid.status()


class SetupIn(BaseModel):
    client_id: str
    secret: str
    env: str = "sandbox"


@router.post("/setup")
def setup(body: SetupIn):
    """Store (or replace) the Plaid credentials — the widget form's face of
    the setup CLI. Responds with the masked status, never the secret."""
    if not body.client_id.strip() or not body.secret.strip():
        raise HTTPException(400, "client_id and secret are required")
    try:
        plaid.setup(body.client_id.strip(), body.secret.strip(), body.env)
    except ValueError as e:   # bad env
        raise HTTPException(400, str(e))
    return plaid.status()


class LinkTokenIn(BaseModel):
    item_id: str | None = None   # set = Link update mode (reconnect)


@router.post("/link_token")
def link_token(body: LinkTokenIn):
    try:
        return {"link_token": plaid.create_link_token(body.item_id)}
    except plaid.NotConfigured as e:
        raise HTTPException(409, str(e))
    except ValueError as e:       # unknown item_id
        raise HTTPException(404, str(e))
    except plaid.PlaidError as e:
        raise HTTPException(502, str(e))


class ExchangeIn(BaseModel):
    public_token: str
    institution_name: str | None = None
    institution_id: str | None = None


def _sync_now() -> dict:
    """finance.refresh, answered not raised: the item is stored and a failed sync's error is on its row."""
    try:
        return finance.refresh()
    except plaid.PlaidError as e:
        return {"error": str(e)}


@router.post("/exchange")
def exchange(body: ExchangeIn):
    try:
        item_id = plaid.exchange(body.public_token, body.institution_name,
                                 body.institution_id)
    except plaid.NotConfigured as e:
        raise HTTPException(409, str(e))
    except plaid.PlaidError as e:
        raise HTTPException(502, str(e))
    return {"item_id": item_id, "sync": _sync_now()}


@router.post("/sync")
def sync():
    """Force a finance refresh now (vs the 300s poller) — the widget calls
    this after a Link update-mode reconnect. Parked login_required and
    needs_consent items get their status reset first, since refresh()
    deliberately skips/short-circuits them; a still-broken item just parks
    itself again on the failing sync."""
    with db.session() as con, con:
        con.execute("UPDATE plaid_items SET status='ok', error=NULL"
                    " WHERE status IN ('login_required', 'needs_consent')")
    return {"sync": _sync_now()}


@router.delete("/items/{item_id}")
def remove_item(item_id: str):
    try:
        plaid.remove_item(item_id)
    except ValueError as e:
        raise HTTPException(404, str(e))
    except plaid.PlaidError as e:
        raise HTTPException(502, str(e))
    return {"removed": item_id}
