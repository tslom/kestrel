"""Citation resolution — the read face of `app/citations.py`.

One endpoint, and it is a lookup, not a search: the console wrote
`[cite:e7a3f1]` into a sentence, the frontend wants to know what that is, and
the answer is either the row the ref was minted over or nothing.

The 404 is load-bearing and is not an error condition. A ref that was never
handed out is, with overwhelming likelihood, one the model invented, and the
frontend renders that as a dead marker rather than a link. Logging it as a
server fault would bury a real signal ("the console is citing things it did
not read") under noise, so it returns cleanly.

`found: false` is the third answer and the one worth keeping separate: the ref
is real, the row behind it is gone — a provider cache re-synced past it, or a
message deleted. The pill still says what it was, because "the email that said
this is no longer in the cache" and "the console made this up" should not look
alike on screen.

Bodies never come through here. An email citation resolves to its metadata and
its snippet; the body is `/api/email/{id}/body`, which fetches live from Gmail
and stores nothing (§5), and the pill only asks for it when it is opened.
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
