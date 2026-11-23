"""The file itself, for the chips that name one (ui/Files.jsx).

  GET /api/attachments/{id}/file   the bytes, on demand

Bytes pass through and are dropped: an email's come from Gmail on each open,
and `attachments` keeps its promise that sync never stores them.

What the browser may render here is the danger, not what's read: a sender
picks the filename and the mime, and this origin holds every other /api route.
So a raster image or a PDF goes inline, nosniff keeps the browser to the type
we declared, and anything else (SVG and HTML above all) downloads instead.
CORP keeps another site from embedding these while the app is running.
"""

from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Response

from ..providers import attachments

router = APIRouter(prefix="/api/attachments")


@router.get("/{attachment_id}/file")
def get_file(attachment_id: int):
    try:
        name, mime, data, inline = attachments.viewable(attachment_id)
    except ValueError as e:
        raise HTTPException(404, str(e))
    disposition = "inline" if inline else "attachment"
    return Response(data, media_type=mime, headers={
        "Content-Disposition": f"{disposition}; filename*=UTF-8''{quote(name)}",
        "X-Content-Type-Options": "nosniff",
        "Cross-Origin-Resource-Policy": "same-origin",
        "Cache-Control": "private, max-age=3600",
    })
