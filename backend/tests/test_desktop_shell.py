"""Window frame memory and the macOS menu bar.

Checks the two silent breaks: an item losing its key equivalent, and an Edit item
gaining a Python target (which takes ⌘C off the responder chain).
Design: DESIGN.md → `backend/desktop_shell.py`: macOS window shell.
"""
import json

import pytest

import desktop_shell as ds


def test_missing_file_gives_the_default(tmp_path):
    assert ds.load_frame(tmp_path / "nope.json") == ds.DEFAULT_FRAME


def test_corrupt_file_gives_the_default(tmp_path):
    p = tmp_path / "w.json"
    p.write_text("{ this is not json")
    assert ds.load_frame(p) == ds.DEFAULT_FRAME


def test_roundtrip(tmp_path):
    p = tmp_path / "w.json"
    ds.save_frame(p, {"width": 1500, "height": 900, "x": 40, "y": 60})
    assert ds.load_frame(p) == {"width": 1500, "height": 900, "x": 40, "y": 60}


def test_offscreen_frame_falls_back(tmp_path, monkeypatch):
    """A frame saved on an external display must not open unreachable."""
    p = tmp_path / "w.json"
    ds.save_frame(p, {"width": 1200, "height": 800, "x": 99999, "y": 99999})
    assert ds.load_frame(p) == ds.DEFAULT_FRAME


def test_tiny_frame_is_clamped(tmp_path):
    p = tmp_path / "w.json"
    ds.save_frame(p, {"width": 10, "height": 10, "x": 40, "y": 60})
    got = ds.load_frame(p)
    assert (got["width"], got["height"]) == (ds.MIN_W, ds.MIN_H)


def test_garbage_types_are_ignored(tmp_path):
    p = tmp_path / "w.json"
    p.write_text(json.dumps({"width": "wide", "height": None, "x": [1]}))
    assert ds.load_frame(p) == ds.DEFAULT_FRAME


def test_unwritable_path_does_not_raise(tmp_path):
    ds.save_frame(tmp_path / "not-a-dir" / "x" / "w.json", ds.DEFAULT_FRAME)


def test_track_frame_writes_what_the_events_reported(tmp_path):
    """Geometry is mirrored from resize/move events, not read at `closing`."""
    p = tmp_path / "w.json"

    class _Ev:
        def __init__(self): self.subs = []
        def __iadd__(self, fn): self.subs.append(fn); return self
        def fire(self, *a): [f(*a) for f in self.subs]

    class _W:
        class events:
            resized, moved, closing = _Ev(), _Ev(), _Ev()

    w = _W()
    ds.track_frame(w, p)
    w.events.resized.fire(1440, 900)
    w.events.moved.fire(12, 34)
    w.events.closing.fire()
    assert ds.load_frame(p) == {"width": 1440, "height": 900, "x": 12, "y": 34}


pytestmark_appkit = pytest.mark.skipif(
    not ds.HAVE_APPKIT, reason="macOS + PyObjC only")


@pytestmark_appkit
def test_menu_has_the_shortcuts_that_matter():
    fired = []
    ds._install_menu_now(
        {k: (lambda k=k: fired.append(k))
         for k in ("reload", "quit", "settings", "logs")})

    from AppKit import NSApplication
    main = NSApplication.sharedApplication().mainMenu()
    found = {}
    for i in range(main.numberOfItems()):
        sub = main.itemAtIndex_(i).submenu()
        for j in range(sub.numberOfItems()):
            it = sub.itemAtIndex_(j)
            if not it.isSeparatorItem():
                found[it.title()] = it

    for title, key in [("Reload", "r"), ("Quit Kestrel", "q"),
                       ("Close", "w"), ("Copy", "c"), ("Paste", "v"),
                       ("Select All", "a"), ("Minimize", "m")]:
        assert title in found, f"{title} missing from the menu"
        assert found[title].keyEquivalent() == key, f"{title} lost its ⌘{key}"


@pytestmark_appkit
def test_edit_items_stay_on_the_responder_chain():
    """A Python target here would stop ⌘C copying the web view's selection."""
    ds._install_menu_now({k: (lambda: None)
                          for k in ("reload", "quit", "settings", "logs")})
    from AppKit import NSApplication
    main = NSApplication.sharedApplication().mainMenu()
    edit = next(main.itemAtIndex_(i).submenu()
                for i in range(main.numberOfItems())
                if main.itemAtIndex_(i).submenu().title() == "Edit")
    for j in range(edit.numberOfItems()):
        it = edit.itemAtIndex_(j)
        if not it.isSeparatorItem():
            assert it.target() is None, f"{it.title()} must have a nil target"


@pytestmark_appkit
def test_quit_does_not_use_terminate():
    """terminate: skips the uvicorn drain and the process-group sweep."""
    ds._install_menu_now({k: (lambda: None)
                          for k in ("reload", "quit", "settings", "logs")})
    from AppKit import NSApplication
    main = NSApplication.sharedApplication().mainMenu()
    app_menu = main.itemAtIndex_(0).submenu()
    quit_item = next(
        app_menu.itemAtIndex_(j) for j in range(app_menu.numberOfItems())
        if app_menu.itemAtIndex_(j).title().startswith("Quit"))
    assert str(quit_item.action()) == "doQuit:"


@pytestmark_appkit
def test_menu_callbacks_reach_python():
    fired = []
    ds._install_menu_now(
        {k: (lambda k=k: fired.append(k))
         for k in ("reload", "quit", "settings", "logs")})
    ds._actions.doReload_(None)
    ds._actions.doSettings_(None)
    assert fired == ["reload", "settings"]
