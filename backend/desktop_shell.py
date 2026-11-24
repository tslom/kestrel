"""The macOS bits that make the window feel like an app: a real menu bar (built on
AppKit, since pywebview's MenuAction has no accelerator), window frame memory, microphone
permission, Tab reaching every control, and Kestrel-branded notifications.

macOS-only; the AppKit import is guarded so elsewhere the launcher runs without it.
Design: DESIGN.md → `backend/desktop_shell.py`: macOS window shell.
"""
from __future__ import annotations

import json
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Callable

MACOS = sys.platform == "darwin"

try:
    import objc
    from AppKit import (NSApplication, NSEventModifierFlagCommand,
                        NSEventModifierFlagControl, NSEventModifierFlagOption,
                        NSEventModifierFlagShift, NSMenu, NSMenuItem, NSScreen)
    from Foundation import NSObject
    from PyObjCTools import AppHelper
    HAVE_APPKIT = MACOS
except ImportError:                                   # pragma: no cover
    HAVE_APPKIT = False


# ------------------------------------------------------------- media capture

# WKPermissionDecision Grant: macOS's own per-app mic prompt is the real consent.
_GRANT = 1


def allow_media_capture() -> bool:
    """Make pywebview's WKWebView answer getUserMedia at all; without the delegate
    method the promise never settles. Returns False (never raises) if it can't."""
    if not HAVE_APPKIT:
        return False
    try:
        from webview.platforms import cocoa

        def _decide(self, webview, origin, frame, kind, handler):
            handler(_GRANT)

        objc.classAddMethods(cocoa.BrowserView.BrowserDelegate, [objc.selector(
            _decide,
            selector=b"webView:requestMediaCapturePermissionForOrigin:"
                     b"initiatedByFrame:type:decisionHandler:",
            signature=b"v@:@@@q@?")])
        return True
    except Exception as exc:                          # pragma: no cover
        print(f"[app] no microphone in this window: {exc}", file=sys.stderr)
        return False


def tab_to_every_control(window) -> None:
    """Let Tab reach buttons and links, not only text fields. Scheduled onto the
    main runloop; a no-op off macOS or if the view can't be found."""
    if not HAVE_APPKIT:
        return

    def now():
        try:
            from WebKit import WKWebView
            stack = [window.native.contentView()]
            while stack:
                view = stack.pop()
                if isinstance(view, WKWebView):
                    view.configuration().preferences().setTabFocusesLinks_(True)
                    return
                stack.extend(view.subviews() or [])
        except Exception as exc:                      # pragma: no cover
            print(f"[app] Tab stays on text fields: {exc}", file=sys.stderr)

    AppHelper.callAfter(now)


# ---------------------------------------------------------------- frame state

DEFAULT_FRAME = {"width": 1280, "height": 860, "x": None, "y": None}
MIN_W, MIN_H = 640, 480


def load_frame(path: Path) -> dict:
    """Last window frame; the centred default if it isn't reachable on a current screen."""
    try:
        saved = json.loads(path.read_text())
    except (OSError, ValueError):
        return dict(DEFAULT_FRAME)

    frame = dict(DEFAULT_FRAME)
    for k in frame:
        v = saved.get(k)
        if isinstance(v, (int, float)):
            frame[k] = int(v)
    frame["width"] = max(MIN_W, frame["width"])
    frame["height"] = max(MIN_H, frame["height"])

    if frame["x"] is None or frame["y"] is None:
        return frame
    if not _on_a_screen(frame):
        return dict(DEFAULT_FRAME)
    return frame


def _on_a_screen(frame: dict) -> bool:
    """Is the top-left (title bar) corner on some connected display? Not
    full containment: hanging off the right edge is fine."""
    if not HAVE_APPKIT:
        return True
    x, y = frame["x"], frame["y"]
    for screen in NSScreen.screens() or []:
        f = screen.frame()
        if (f.origin.x - 8 <= x <= f.origin.x + f.size.width - 40
                and f.origin.y - 8 <= y <= f.origin.y + f.size.height - 40):
            return True
    return False


def save_frame(path: Path, frame: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(frame))
    except OSError as e:                              # pragma: no cover
        print(f"[app] could not save window frame: {e}", file=sys.stderr)


def track_frame(window, path: Path) -> dict:
    """Mirror the live geometry from resize/move events and write it on close
    (at `closing` the window's own frame is already being torn down)."""
    frame = dict(DEFAULT_FRAME)

    def on_resize(w, h):
        frame["width"], frame["height"] = int(w), int(h)

    def on_move(x, y):
        frame["x"], frame["y"] = int(x), int(y)

    window.events.resized += on_resize
    window.events.moved += on_move
    window.events.closing += lambda: save_frame(path, frame)
    return frame


# ------------------------------------------------------------------ menu bar

if HAVE_APPKIT:
    class _Actions(NSObject):
        """ObjC target for the menu items that call back into Python; held by
        the module-level `_actions` (a collected target greys the item out)."""

        def initWithHandlers_(self, handlers):
            self = objc.super(_Actions, self).init()
            if self is None:
                return None
            self._handlers = handlers
            return self

        def doReload_(self, sender):
            self._handlers["reload"]()

        def doQuit_(self, sender):
            self._handlers["quit"]()

        def doSettings_(self, sender):
            self._handlers["settings"]()

        def doLogs_(self, sender):
            self._handlers["logs"]()


_actions = None      # module-level: see _Actions docstring


def _item(menu, title, action, key="", mods=None, target=None):
    it = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
        title, action, key)
    if mods is not None:
        it.setKeyEquivalentModifierMask_(mods)
    if target is not None:
        it.setTarget_(target)
    menu.addItem_(it)
    return it


def _submenu(main, title):
    holder = NSMenuItem.alloc().init()
    main.addItem_(holder)
    sub = NSMenu.alloc().initWithTitle_(title)
    holder.setSubmenu_(sub)
    return sub


def install_menu(handlers: dict[str, Callable[[], None]]) -> None:
    """Build and install the main menu on the main runloop (AppKit is main-thread-only). No-op off macOS."""
    if not HAVE_APPKIT:
        return
    AppHelper.callAfter(_install_menu_now, handlers)


def _install_menu_now(handlers: dict[str, Callable[[], None]]) -> None:
    global _actions
    CMD = NSEventModifierFlagCommand
    SHIFT = NSEventModifierFlagShift
    ALT = NSEventModifierFlagOption
    CTRL = NSEventModifierFlagControl

    _actions = _Actions.alloc().initWithHandlers_(handlers)
    main = NSMenu.alloc().init()

    app_menu = _submenu(main, "Kestrel")
    _item(app_menu, "About Kestrel", "orderFrontStandardAboutPanel:")
    app_menu.addItem_(NSMenuItem.separatorItem())
    _item(app_menu, "Settings (.env)…", "doSettings:", ",", CMD, _actions)
    _item(app_menu, "Open Log", "doLogs:", "l", CMD | SHIFT, _actions)
    app_menu.addItem_(NSMenuItem.separatorItem())
    _item(app_menu, "Hide Kestrel", "hide:", "h", CMD)
    _item(app_menu, "Hide Others", "hideOtherApplications:", "h", CMD | ALT)
    _item(app_menu, "Show All", "unhideAllApplications:")
    app_menu.addItem_(NSMenuItem.separatorItem())
    # Not terminate: (skips the uvicorn drain and the group sweep); same path as the red button.
    _item(app_menu, "Quit Kestrel", "doQuit:", "q", CMD, _actions)

    # nil target on purpose: the responder chain carries these to the web view's selection.
    edit = _submenu(main, "Edit")
    _item(edit, "Undo", "undo:", "z", CMD)
    _item(edit, "Redo", "redo:", "z", CMD | SHIFT)
    edit.addItem_(NSMenuItem.separatorItem())
    _item(edit, "Cut", "cut:", "x", CMD)
    _item(edit, "Copy", "copy:", "c", CMD)
    _item(edit, "Paste", "paste:", "v", CMD)
    _item(edit, "Select All", "selectAll:", "a", CMD)

    view = _submenu(main, "View")
    _item(view, "Reload", "doReload:", "r", CMD, _actions)
    _item(view, "Enter Full Screen", "toggleFullScreen:", "f", CMD | CTRL)

    window = _submenu(main, "Window")
    _item(window, "Minimize", "performMiniaturize:", "m", CMD)
    _item(window, "Zoom", "performZoom:")
    window.addItem_(NSMenuItem.separatorItem())
    _item(window, "Close", "performClose:", "w", CMD)

    app = NSApplication.sharedApplication()
    app.setMainMenu_(main)
    app.setWindowsMenu_(window)


# -------------------------------------------------------------- notifications

# UNUserNotificationCenter needs a LaunchServices-registered, code-signed bundle (not under
# /tmp); elsewhere it throws or errors, so it is never touched and osascript carries on.

try:
    import UserNotifications as UN
    from Foundation import NSBundle
    HAVE_UN = HAVE_APPKIT
except ImportError:                                   # pragma: no cover
    HAVE_UN = False


def _osascript(title: str, body: str) -> None:
    try:
        subprocess.run(["osascript", "-e",
                        "on run argv\n display notification (item 2 of argv)"
                        " with title (item 1 of argv)\nend run",
                        "--", title, body], timeout=10, check=False,
                       capture_output=True)
    except (OSError, subprocess.TimeoutExpired) as e:  # pragma: no cover
        print(f"[notify] osascript failed: {e}", file=sys.stderr)


if HAVE_UN:
    class _NotifyDelegate(NSObject, protocols=[
            objc.protocolNamed("UNUserNotificationCenterDelegate")]):
        """Kept alive by Notifier — the center holds its delegate weakly."""

        def initWithClick_(self, on_click):
            self = objc.super(_NotifyDelegate, self).init()
            if self is None:
                return None
            self._on_click = on_click
            return self

        # Show the banner even with Kestrel in front.
        def userNotificationCenter_willPresentNotification_withCompletionHandler_(
                self, center, notification, handler):
            handler(UN.UNNotificationPresentationOptionBanner
                    | UN.UNNotificationPresentationOptionList
                    | UN.UNNotificationPresentationOptionSound)

        def userNotificationCenter_didReceiveNotificationResponse_withCompletionHandler_(
                self, center, response, handler):
            try:
                info = response.notification().request().content().userInfo()
                target = info.get("target") if info else None
                self._on_click(str(target) if target else None)
            finally:
                handler()


def bring_forward() -> None:
    """Kestrel to the front with its window up, even if minimised or hidden
    (⌘H). Any thread; AppKit work is queued onto the main one."""
    if not HAVE_APPKIT:
        return

    def now():
        app = NSApplication.sharedApplication()
        app.unhide_(None)
        for w in app.windows():
            if w.isMiniaturized():
                w.deminiaturize_(None)
        app.activateIgnoringOtherApps_(True)
    AppHelper.callAfter(now)


class Notifier:
    """Posts banners as Kestrel and routes a click to `on_click(target)`.

    `post()` is callable from any thread. Falls back to osascript whenever the
    center can't be used (not a bundle, not signed, permission denied).
    """

    # A test bundle swaps in Provisional (quiet delivery, no prompt).
    AUTH_OPTIONS = (UN.UNAuthorizationOptionAlert | UN.UNAuthorizationOptionSound
                    if HAVE_UN else 0)

    def __init__(self, on_click: Callable[[str | None], None]):
        self.center = None
        self.allowed = False
        self._delegate = None
        if not HAVE_UN:
            return
        bundle = NSBundle.mainBundle()
        if not (bundle.bundleIdentifier() and str(bundle.bundlePath()).endswith(".app")):
            print("[notify] not running as Kestrel.app — banners via osascript")
            return
        try:
            self.center = UN.UNUserNotificationCenter.currentNotificationCenter()
            self._delegate = _NotifyDelegate.alloc().initWithClick_(on_click)
            self.center.setDelegate_(self._delegate)
        except Exception as e:                        # pragma: no cover
            print(f"[notify] no notification center ({e}) — banners via osascript")
            self.center = None
            return

        def answered(granted, error):
            self.allowed = bool(granted)
            if error is not None or not granted:
                # Said once at launch, where the log is read for "why no banners".
                print(f"[notify] Kestrel may not post notifications "
                      f"(granted={bool(granted)}, {error}); using osascript. "
                      "Allow it in System Settings › Notifications › Kestrel, "
                      "or re-run scripts/make-app.sh if the error says "
                      "'not allowed for this application'.")
        self.center.requestAuthorizationWithOptions_completionHandler_(
            self.AUTH_OPTIONS, answered)

    def post(self, title: str, body: str, target: str | None = None) -> None:
        if self.center is None or not self.allowed:
            _osascript(title, body)
            return
        content = UN.UNMutableNotificationContent.alloc().init()
        content.setTitle_(title)
        content.setBody_(body)
        content.setSound_(UN.UNNotificationSound.defaultSound())
        if target:
            content.setUserInfo_({"target": target})
        # Grouped per tab in Notification Center.
        content.setThreadIdentifier_(target or "kestrel")
        req = UN.UNNotificationRequest.requestWithIdentifier_content_trigger_(
            uuid.uuid4().hex, content, None)

        def added(error):
            if error is not None:
                print(f"[notify] post failed ({error}); using osascript")
                _osascript(title, body)
        self.center.addNotificationRequest_withCompletionHandler_(req, added)


# ------------------------------------------------------------------- helpers

def open_in_editor(path: Path) -> None:
    """Hand a file to whatever the user has set for it (`open -t` = editor)."""
    try:
        subprocess.run(["open", "-t", str(path)], timeout=10, check=False)
    except (OSError, subprocess.TimeoutExpired) as e:  # pragma: no cover
        print(f"[app] could not open {path}: {e}", file=sys.stderr)


def reveal(path: Path) -> None:
    try:
        subprocess.run(["open", str(path)], timeout=10, check=False)
    except (OSError, subprocess.TimeoutExpired) as e:  # pragma: no cover
        print(f"[app] could not open {path}: {e}", file=sys.stderr)
