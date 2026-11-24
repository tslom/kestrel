"""The file itself, for the chips that name one (ui/Files.jsx).

  GET /api/attachments/{id}/file   the bytes, on demand

Bytes pass through and are dropped (sync never stores them). The danger is what
the browser may render: raster images and PDFs go inline under nosniff; anything
else (SVG, HTML) downloads. CORP blocks cross-site embedding.
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
