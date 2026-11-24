"""/api/delegates: the providers page, a batch's variants, and their files.

A variant's file is something a model wrote, possibly HTML, shown in a frame
on a page that can reach every API here. So it is served with a CSP sandbox:
no origin, no scripts reaching the app, whatever the frame's own attributes
say.
"""

from __future__ import annotations

import asyncio
import mimetypes

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from . import accounts, boundary, registry, runs, store

router = APIRouter()

FRAME_HEADERS = {
    "Content-Security-Policy": "sandbox allow-scripts; default-src 'self' 'unsafe-inline' data: https:; connect-src 'none'",
    "X-Content-Type-Options": "nosniff",
    "Cache-Control": "no-store",
}
RUN_PUBLIC = ("variant", "provider", "model", "angle", "status", "error", "file",
              "chars", "tokens_in", "tokens_out", "duration_ms", "redactions", "kind")


@router.get("/api/delegates/providers")
async def providers(fresh: bool = False) -> dict:
    rows = await asyncio.to_thread(registry.status, fresh)
    used = {u["provider"]: u for u in await asyncio.to_thread(store.usage)}
    accts = await asyncio.to_thread(accounts.listing)
    return {"providers": [{**{k: v for k, v in r.items() if k != "path"},
                           "usage": used.get(r["id"]),
                           "accounts": accts.get(r["id"], []),
                           "signing_in": accounts.login_state(r["id"])} for r in rows],
            "purposes": boundary.PURPOSES, "tags": list(boundary.WORK_FOR)}


# ---------- accounts (accounts.py) ----------

class LoginIn(BaseModel):
    label: str | None = None
    allowed_for: list[str] | None = None
    way: str | None = None          # registry.LoginWay.id; omitted: the first


def _known(provider: str) -> registry.Provider:
    p = registry.PROVIDERS.get(provider)
    if p is None or not p.logins:
        raise HTTPException(404, "no provider with accounts by that name")
    return p


@router.post("/api/delegates/providers/{provider}/login")
async def login_start(provider: str, body: LoginIn) -> dict:
    _known(provider)
    try:
        return await accounts.start_login(provider, body.label, body.allowed_for, body.way)
    except accounts.AccountError as e:
        raise HTTPException(409, str(e)) from None


@router.post("/api/delegates/providers/{provider}/login/reopen")
async def login_reopen(provider: str) -> dict:
    _known(provider)
    try:
        return await accounts.reopen(provider)
    except accounts.AccountError as e:
        raise HTTPException(409, str(e)) from None


@router.get("/api/delegates/providers/{provider}/login")
async def login_get(provider: str) -> dict:
    _known(provider)
    return {"login": accounts.login_state(provider)}


@router.delete("/api/delegates/providers/{provider}/login")
async def login_cancel(provider: str) -> dict:
    _known(provider)
    accounts.cancel_login(provider)
    return {"login": None}


class AccountIn(BaseModel):
    label: str | None = None
    enabled: bool | None = None
    allowed_for: list[str] | None = None
    monthly_credits: float | None = None
    credit_cap: float | None = None
    window_cap: float | None = None


def _account(account_id: str) -> dict:
    acct = next((a for a in store.accounts() if a["id"] == account_id), None)
    if acct is None:
        raise HTTPException(404, "no such account")
    return acct


@router.patch("/api/delegates/accounts/{account_id}")
async def account_patch(account_id: str, body: AccountIn) -> dict:
    await asyncio.to_thread(_account, account_id)
    fields = body.model_dump(exclude_unset=True)
    if "label" in fields:
        fields["label"] = (fields["label"] or "").strip()[:60]
        if not fields["label"]:
            raise HTTPException(400, "A name can't be empty.")
    if "allowed_for" in fields:
        fields["allowed_for"] = ",".join(t for t in boundary.WORK_FOR
                                         if t in (fields["allowed_for"] or []))
    if fields.get("window_cap") is not None and not 1 <= fields["window_cap"] <= 100:
        raise HTTPException(400, "A ceiling is a percentage from 1 to 100.")
    for n in ("monthly_credits", "credit_cap"):
        if fields.get(n) is not None and fields[n] < 0:
            raise HTTPException(400, "Credits can't be negative.")
    if "enabled" in fields:
        fields["enabled"] = int(bool(fields["enabled"]))
    await asyncio.to_thread(store.update_account, account_id, **fields)
    return await providers()


class OrderIn(BaseModel):
    ids: list[str]


@router.post("/api/delegates/providers/{provider}/order")
async def account_order(provider: str, body: OrderIn) -> dict:
    have = [a["id"] for a in await asyncio.to_thread(store.accounts, provider)]
    if sorted(have) != sorted(body.ids):
        raise HTTPException(400, "The order has to name every account of this provider once.")
    for i, account_id in enumerate(body.ids):
        await asyncio.to_thread(store.update_account, account_id, priority=i)
    return await providers()


@router.delete("/api/delegates/accounts/{account_id}")
async def account_delete(account_id: str) -> dict:
    try:
        await asyncio.to_thread(accounts.forget, account_id)
    except accounts.AccountError as e:
        raise HTTPException(404, str(e)) from None
    return await providers()


class Switch(BaseModel):
    enabled: bool


@router.post("/api/delegates/providers/{provider}")
async def switch(provider: str, body: Switch) -> dict:
    p = registry.PROVIDERS.get(provider)
    if p is None:
        raise HTTPException(404, "no such provider")
    if not p.outside:
        raise HTTPException(400, "Claude is what Kestrel runs on; it has no switch")
    await asyncio.to_thread(store.set_enabled, provider, body.enabled)
    return await providers()


@router.get("/api/delegates/usage")
async def usage(days: int = 30) -> dict:
    return {"days": days, "providers": await asyncio.to_thread(store.usage, max(1, min(days, 365)))}


@router.get("/api/delegates/batches/{batch}")
async def batch(batch: str) -> dict:
    rows = await asyncio.to_thread(store.batch_runs, batch)
    if not rows:
        raise HTTPException(404, "no such batch")
    return {"batch": batch,
            "variants": [{k: r.get(k) for k in RUN_PUBLIC} for r in rows]}


@router.get("/api/delegates/batches/{batch}/{variant}/file")
async def variant_file(batch: str, variant: int):
    rows = await asyncio.to_thread(store.batch_runs, batch)
    row = next((r for r in rows if r["variant"] == variant and r["file"]), None)
    if row is None:
        raise HTTPException(404, "no such variant")
    try:
        folder = runs.variant_dir(batch, variant)
    except runs.DelegateError:
        raise HTTPException(404, "no such batch") from None
    path = (folder / row["file"]).resolve()
    if folder.resolve() not in path.parents or not path.is_file():
        raise HTTPException(404, "the file is gone")
    kind = mimetypes.guess_type(path.name)[0] or "text/plain"
    if not kind.startswith(("image/", "text/html")):
        kind = "text/plain; charset=utf-8"
    return FileResponse(path, media_type=kind, headers=FRAME_HEADERS)
