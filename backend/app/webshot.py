"""A WebKit screenshot of one of Kestrel's own pages, taken off screen.

    python -m app.webshot <url> <out.png> [--width 1200] [--height 900]
                          [--scheme dark|light] [--ready <js expression>]

Runs as its own subprocess (AppKit wants the main thread; use `shoot()`).
Loopback URLs only: it is a real browser engine with the user's network access.
Design: DESIGN.md → Themes Kestrel makes.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit

SHOT_SECONDS = 25          # the whole subprocess, page load included
_POLL_SECONDS = 0.15
_LOOPBACK = {"127.0.0.1", "localhost"}


class ShotError(RuntimeError):
    """No picture, and why."""


def loopback(url: str) -> bool:
    parts = urlsplit(url)
    return parts.scheme == "http" and parts.hostname in _LOOPBACK


def shoot(url: str, out: Path, *, width: int = 1200, height: int = 900,
          scheme: str = "dark", ready: str | None = None) -> Path:
    """Render `url` in an off-screen WKWebView and write a PNG to `out`."""
    if not loopback(url):
        raise ShotError(f"not a loopback http URL: {url}")
    cmd = [sys.executable, "-m", "app.webshot", url, str(out),
           "--width", str(width), "--height", str(height), "--scheme", scheme]
    if ready:
        cmd += ["--ready", ready]
    try:
        r = subprocess.run(cmd, cwd=Path(__file__).resolve().parents[1],
                           capture_output=True, text=True, timeout=SHOT_SECONDS + 5)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise ShotError(f"the screenshot process didn't finish: {e}") from e
    if r.returncode != 0 or not out.is_file() or out.stat().st_size == 0:
        said = (r.stderr or r.stdout or "").strip().splitlines()
        raise ShotError(said[-1] if said else f"exit {r.returncode}, no picture")
    return out


def _run(url: str, out: Path, width: int, height: int, scheme: str,
         ready: str | None) -> int:   # pragma: no cover - needs a window server
    from AppKit import (NSAppearance, NSApplication,
                        NSApplicationActivationPolicyProhibited,
                        NSBackingStoreBuffered, NSBitmapImageRep, NSPNGFileType,
                        NSWindow, NSWindowStyleMaskBorderless)
    from Foundation import NSURL, NSMakeRect, NSObject, NSTimer, NSURLRequest
    from WebKit import (WKSnapshotConfiguration, WKWebView,
                        WKWebViewConfiguration, WKWebsiteDataStore)

    app = NSApplication.sharedApplication()
    # No Dock icon, never frontmost: must not take focus from the user's window.
    app.setActivationPolicy_(NSApplicationActivationPolicyProhibited)
    state = {"code": 1, "loaded": False, "waited": 0.0}

    def fail(why: str) -> None:
        print(why, file=sys.stderr)
        app.terminate_(None)

    config = WKWebViewConfiguration.alloc().init()
    # Its own empty store: no cookies or localStorage shared with anything.
    config.setWebsiteDataStore_(WKWebsiteDataStore.nonPersistentDataStore())
    frame = NSMakeRect(0, 0, width, height)
    view = WKWebView.alloc().initWithFrame_configuration_(frame, config)
    # prefers-color-scheme follows the view's appearance, not the system's.
    name = "NSAppearanceNameDarkAqua" if scheme == "dark" else "NSAppearanceNameAqua"
    view.setAppearance_(NSAppearance.appearanceNamed_(name))
    # A view outside any window snapshots blank; the window is never shown.
    window = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
        NSMakeRect(-20000, -20000, width, height), NSWindowStyleMaskBorderless,
        NSBackingStoreBuffered, False)
    window.setContentView_(view)

    def snap() -> None:
        conf = WKSnapshotConfiguration.alloc().init()
        # One pixel per point: Retina default doubles bytes for no gain to a model.
        conf.setSnapshotWidth_(width / (window.backingScaleFactor() or 1))

        def done(image, error):
            if image is None:
                return fail(f"WebKit gave no snapshot: {error}")
            rep = NSBitmapImageRep.imageRepWithData_(image.TIFFRepresentation())
            png = rep.representationUsingType_properties_(NSPNGFileType, None)
            if png is None or not png.writeToFile_atomically_(str(out), True):
                return fail(f"couldn't write {out}")
            state["code"] = 0
            app.terminate_(None)
        view.takeSnapshotWithConfiguration_completionHandler_(conf, done)

    def poll() -> None:
        state["waited"] += _POLL_SECONDS
        if state["waited"] > SHOT_SECONDS:
            return fail("the page never said it was ready"
                        if state["loaded"] else "the page didn't load")
        if not state["loaded"]:
            return again()

        def answered(value, error):
            if value:
                # One more beat, so the frame that set the flag is painted.
                NSTimer.scheduledTimerWithTimeInterval_repeats_block_(
                    0.3, False, lambda _t: snap())
            else:
                again()
        view.evaluateJavaScript_completionHandler_(ready or "true", answered)

    def again() -> None:
        NSTimer.scheduledTimerWithTimeInterval_repeats_block_(
            _POLL_SECONDS, False, lambda _t: poll())

    class Nav(NSObject):
        def webView_didFinishNavigation_(self, _view, _nav):
            state["loaded"] = True

        def webView_didFailProvisionalNavigation_withError_(self, _view, _nav, error):
            fail(f"the page didn't load: {error.localizedDescription()}")

        def webView_didFailNavigation_withError_(self, _view, _nav, error):
            fail(f"the page didn't load: {error.localizedDescription()}")

    nav = Nav.alloc().init()
    view.setNavigationDelegate_(nav)
    view.loadRequest_(NSURLRequest.requestWithURL_(NSURL.URLWithString_(url)))
    again()
    app.run()
    return state["code"]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("url")
    ap.add_argument("out", type=Path)
    ap.add_argument("--width", type=int, default=1200)
    ap.add_argument("--height", type=int, default=900)
    ap.add_argument("--scheme", choices=("dark", "light"), default="dark")
    ap.add_argument("--ready")
    args = ap.parse_args()
    if not loopback(args.url):
        raise SystemExit(f"not a loopback http URL: {args.url}")
    raise SystemExit(_run(args.url, args.out, args.width, args.height,
                          args.scheme, args.ready))


if __name__ == "__main__":
    main()
