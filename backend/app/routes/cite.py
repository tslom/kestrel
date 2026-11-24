"""Citation resolution, the read face of `app/citations.py`: a lookup, not a search.

404 = never handed out (likely invented; the frontend renders a dead marker,
and it is not logged as a fault). `found: false` = real ref, row gone from the
cache. Bodies never come through here (email bodies: `/api/email/{id}/body`).
Design: DESIGN.md → Citations — what a sentence was read off.
"""
from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from .. import citations, docs

router = APIRouter(prefix="/api/cite", tags=["cite"])


@router.get("/docs/recent")
def recent_docs(limit: int = 20):
    """Documents kept lately (dropped, or opened with doc_pages), newest first,
    for the console's document pane: you can open any of them yourself, not
    only what the newest reply cited. Declared before /{ref}, which it would
    otherwise match."""
    return {"docs": docs.recent(max(1, min(limit, 50)))}


@router.get("/{ref}")
def resolve(ref: str):
    out = citations.resolve(ref)
    if out is None:
        raise HTTPException(404, "unknown citation")
    return out


@router.get("/{ref}/file")
def doc_file(ref: str):
    """A document citation's bytes, for pdf.js to render the cited page.

    By ref only, never by path or hash: the ref is the one handle the console
    was given, and a request can only reach files someone dropped. The bytes
    are untrusted (it is whatever PDF was dragged in) and are decoded by the
    browser, never here. Immutable for a given ref — the file is named by its
    own sha256 — so the browser may keep it."""
    path = docs.file_for(ref)
    if path is None:
        raise HTTPException(404, "no such document")
    return FileResponse(path, media_type="application/pdf", headers={
        "Cache-Control": "private, max-age=31536000, immutable",
        "X-Content-Type-Options": "nosniff",
        "Content-Disposition": "inline",
    })
