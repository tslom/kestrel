"""The macOS bits that make the window feel like an app rather than a viewport.

Two things pywebview doesn't give us:

- **A real menu bar.** pywebview has a `Menu`/`MenuAction` API, but `MenuAction`
  takes no accelerator, so everything built with it is click-only. ⌘R and ⌘Q
  are most of the point, so the menu is built straight against AppKit instead.
  The Edit items deliberately carry a nil target: that sends them down the
  responder chain to the web view, which is what makes ⌘C/⌘V work on the
  selection rather than on whatever Python thinks is focused.
- **Window frame memory.** Reopening at 1280×860 in the middle of the screen
  every time is the single most "this is a script" thing about the window.
- **A microphone.** WKWebView routes every `getUserMedia` call through a
  `WKUIDelegate` method that pywebview doesn't implement, and an unimplemented
  one is not a denial — the promise never settles at all. In the browser the
  voice console works; in the app it hung with the mic button lit and no error
  anywhere. See `allow_media_capture()`.

macOS-only by construction. Import guarded — on any other platform, or if
PyObjC isn't there, the launcher runs exactly as it did before.
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

# WKPermissionDecision. Grant rather than Prompt: WebKit's own prompt is not
# the gate that matters — macOS still asks for microphone access the first
# time, per app, and that consent is the real one. Prompting twice for one
# click on a mic button is worse, and the page asking is already a deliberate
# act (holding talk, or arming the wake word).
_GRANT = 1


def allow_media_capture() -> bool:
    """Teach pywebview's WKWebView to answer a getUserMedia request at all.

    WebKit asks the `WKUIDelegate` for permission, and pywebview's delegate
    (6.2.1) has no such method. An unimplemented delegate method is not a
    denial — WebKit drops the request on the floor, so the JS promise never
    resolves *or* rejects and the caller waits forever with no error to show.
    That is the difference between the voice console working in a browser tab
    and hanging silently in the app.

    Adding the selector to pywebview's delegate class is reaching into someone
    else's object, which is why it's guarded and reports what happened rather
    than throwing: a mute app is a degraded one, a crashed launcher is not.
    """
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
    """Let Tab reach buttons and links, not only text fields.

    WKWebView defaults to Safari's "Tab highlights text fields only", so in
    the app no button in the page could be reached from the keyboard at all
    (a browser tab, or Playwright's Chrome, hides that). WKPreferences is
    shared between the configuration pywebview built and the live web view,
    so setting it once the window exists takes effect, the same way
    pywebview's own developerExtrasEnabled does. Scheduled onto the main
    runloop; a no-op off macOS or if the view can't be found.
    """
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
    """Last window frame, clamped to something actually visible.

    A frame saved on an external display is off-screen once it's unplugged,
    and macOS will happily open a window you can't reach. Anything that
    doesn't fit a current screen falls back to the centred default.
    """
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
    """Is the window's title bar reachable on some connected display?

    Corner-in-bounds, not fully-contained: a window hanging off the right edge
    is normal and fine to restore. What isn't fine is a title bar you can't
    grab, so the check is on the top-left.
    """
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
    """Mirror the live window geometry, and write it out as the window closes.

    Read from the resize/move events rather than polling `window.x` on the way
    out: by the time `closing` fires the frame is already being torn down, and
    what you get back is the last thing macOS wrote, not where the user left
    it.
    """
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
        """ObjC target for the menu items that call back into Python.

        Kept alive by the module-level `_actions` reference — a menu item's
        target is a weak reference, and a collected target silently greys the
        item out.
        """

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
    """Build and install the app's main menu. No-op off macOS.

    Scheduled onto the main runloop: pywebview runs `start(func=…)` on a
    worker thread, and AppKit is main-thread-only.
    """
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
    # Config lives in backend/.env — vault path, port, model choices. Opening
    # the real file beats a settings pane that would have to round-trip to it.
    _item(app_menu, "Settings (.env)…", "doSettings:", ",", CMD, _actions)
    _item(app_menu, "Open Log", "doLogs:", "l", CMD | SHIFT, _actions)
    app_menu.addItem_(NSMenuItem.separatorItem())
    _item(app_menu, "Hide Kestrel", "hide:", "h", CMD)
    _item(app_menu, "Hide Others", "hideOtherApplications:", "h", CMD | ALT)
    _item(app_menu, "Show All", "unhideAllApplications:")
    app_menu.addItem_(NSMenuItem.separatorItem())
    # Not terminate: — that tears the process down where it stands, skipping
    # the uvicorn drain and the process-group sweep. Route ⌘Q through the same
    # close path as the red button so shutdown stays one code path.
    _item(app_menu, "Quit Kestrel", "doQuit:", "q", CMD, _actions)

    # nil target on purpose: these travel the responder chain to the web view,
    # so they act on the selection instead of on the Python side.
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

# UNUserNotificationCenter, so banners are Kestrel's own: its name and icon,
# its own row in System Settings › Notifications, and a click that comes back
# here. app/notify.py used to post with `display notification`, which macOS
# attributes to Script Editor — a click opened Script Editor and the
# permissions lived under its name (#80).
#
# What the center demands, found by trial on macOS 26 (11-20): the process
# must run from a bundle registered with LaunchServices *and* code-signed —
# ad-hoc is enough, unsigned gets UNErrorDomain 1 "Notifications are not
# allowed for this application" (scripts/make-app.sh signs it). A bundle under
# /tmp is refused even when signed. Outside a bundle (a terminal launch) the
# center throws, so it's never touched there and osascript carries on.

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

        # Kestrel in front still shows the banner: the page only asks for one
        # when it isn't looking, and by delivery that may have changed.
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

    `post()` is callable from any thread. Falls back to osascript whenever
    the center can't be used — not a bundle, not signed, or permission
    denied in System Settings — so a misbuilt app still says something.
    """

    # Banners with sound. A test bundle swaps in Provisional (quiet delivery
    # to Notification Center, no prompt on anyone's screen).
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
        # The first launch of a newly signed bundle shows macOS's one-time
        # "Kestrel would like to send notifications" prompt; after that it's
        # the System Settings switch.
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
        # Grouped per tab in Notification Center, so one chatty tab doesn't
        # bury the rest.
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
