"""Hand a URL to the system browser, backend-side.

The dashboard runs in a pywebview window (launch_app.py) as often as it runs
in a browser tab, and WKWebView answers a JS `window.open()` with null:
pywebview's Cocoa delegate only forwards *link activations* to the browser, so
a tab the frontend opens for itself is dropped on the floor — and assigning to
the null it hands back is a TypeError. The consent page couldn't live in the
webview anyway; Google refuses to sign you in inside an embedded one.

So the hand-off happens here, the same call people.py's Messages hand-off
makes and for the same reason: one code path that works in both shells.
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
        # These URLs are ours (built by the OAuth providers), but they reach
        # an OS launcher — open_url keeps the URL an argv element.
        try:
            macos.open_url(url)
        except RuntimeError as e:
            raise HTTPException(502, f"couldn't open the browser: {e}") from e
    elif not webbrowser.open(url):
        raise HTTPException(502, "no browser available to open the page")
