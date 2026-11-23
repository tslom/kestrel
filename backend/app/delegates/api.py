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

from . import registry, runs, store

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
    return {"providers": [{**{k: v for k, v in r.items() if k != "path"},
                           "usage": used.get(r["id"])} for r in rows]}


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
