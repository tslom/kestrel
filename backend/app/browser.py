"""Hand a URL to the system browser, backend-side: WKWebView (pywebview) answers
JS `window.open()` with null, and Google refuses sign-in in an embedded webview.
Same hand-off people.py's Messages link makes; one path for both shells.
"""

import sys
import webbrowser

from fastapi import HTTPException

from . import macos


def open_in_browser(url: str) -> None:
    """Open `url` in the default browser, or raise the reason it didn't."""
    if not url.startswith(("http://", "https://")):
        raise HTTPException(400, f"refusing to open non-http URL {url[:40]!r}")
    if sys.platform == "darwin":
        # open_url keeps the URL an argv element (these reach an OS launcher).
        try:
            macos.open_url(url)
        except RuntimeError as e:
            raise HTTPException(502, f"couldn't open the browser: {e}") from e
    elif not webbrowser.open(url):
        raise HTTPException(502, "no browser available to open the page")
