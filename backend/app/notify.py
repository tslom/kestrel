"""The at-desk channel: an emitter for facts that are already true (nothing to revise or withdraw).

One `notifications` row per *fact* (the key is the fact): dedupe is the whole discipline.
`send()` only enqueues; `flush()` is the one place that talks to the OS. A presence gate
holds rows while nobody is at the desk; a row older than NOTIFY_STALE_MIN expires, not fires late.
Design: DESIGN.md → Reaching the user (§21).
"""

from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import threading
from datetime import datetime, timedelta

from . import db, keys, macos
from .config import (NOTIFY_ENABLED, NOTIFY_FD, NOTIFY_IDLE_MAX_SEC,
                     NOTIFY_MAX_PER_CYCLE, NOTIFY_STALE_MIN)

# Kinds this channel carries; enqueued by whatever noticed the fact, no builder registry.
KINDS = ("flight", "package", "location", "console-turn", "console-ask")

_TIMEOUT = 10           # osascript posting a banner; nothing to wait on
_pipe_lock = threading.Lock()   # one line at a time onto the window's pipe
# The window's pipe, if Kestrel.app started this server (config.NOTIFY_FD).
WINDOW_FD: int | None = int(NOTIFY_FD) if NOTIFY_FD and NOTIFY_FD.isdigit() else None


def _is_pipe(fd: int) -> bool:
    """Only write to a pipe; the fd number might mean a file or socket in this process."""
    try:
        return stat.S_ISFIFO(os.fstat(fd).st_mode)
    except OSError:
        return False


def _notify_key(kind: str, *parts: str) -> str:
    """Escapes every part (alarms reject ':' instead): a part can be a free-text status."""
    return keys.join(kind, *(keys.escape_part(p) for p in parts),
                     allowed=KINDS, label="notification")


# ----------------------------------------------------------------- presence

_IDLE_PROBE = ["ioreg", "-n", "IOHIDSystem", "-r", "-d", "1"]
_IDLE_RE = re.compile(r'"HIDIdleTime"\s*=\s*(\d+)')

_LOCK_PROBE = ["ioreg", "-n", "Root", "-r", "-d", "1", "-k", "IOConsoleUsers"]
_LOCK_RE = re.compile(r'"CGSSessionScreenIsLocked"\s*=\s*Yes')


def _probe(cmd: list[str]) -> str | None:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=_TIMEOUT)
    except (OSError, subprocess.SubprocessError):
        return None
    return proc.stdout if proc.returncode == 0 else None


def _idle_seconds() -> float | None:
    """Seconds since the last keypress or mouse move, or None if unanswerable (HIDIdleTime, ns)."""
    out = _probe(_IDLE_PROBE)
    m = _IDLE_RE.search(out) if out else None
    return int(m.group(1)) / 1e9 if m else None


def _screen_locked() -> bool:
    """True only on positive evidence; a missing key or unreadable probe means unlocked."""
    out = _probe(_LOCK_PROBE)
    return bool(out and _LOCK_RE.search(out))


def at_desk() -> bool:
    """Is someone in front of this machine? **Fails open**: an unanswerable probe lets it through.

    Input idle only: `pmset -g powerstate IODisplayWrangler` fails on this machine. The lock
    check is needed because typing a password counts as input.
    """
    if _screen_locked():
        return False
    idle = _idle_seconds()
    return True if idle is None else idle <= NOTIFY_IDLE_MAX_SEC


# ------------------------------------------------------------------- sending

def _post(title: str, body: str, target: str | None = None) -> None:
    """Post one banner via the window pipe (Kestrel's own banner; click opens `target`), else osascript.

    Raises on failure so flush() can leave the row queued.
    """
    fd = WINDOW_FD
    if fd is not None and _is_pipe(fd):
        data = (json.dumps({"title": title, "body": body, "target": target})
                + "\n").encode()
        try:
            # Locked: writes are atomic only up to PIPE_BUF (512 bytes on macOS).
            with _pipe_lock:
                while data:
                    data = data[os.write(fd, data):]
            return
        except OSError as e:
            print(f"[notify] window channel gone ({e!r}); posting via osascript")
    _osascript_post(title, body)


def _osascript_post(title: str, body: str) -> None:
    """Fallback for a server Kestrel.app didn't start.

    Values go through `on run argv`, never interpolation: json.dumps emits \\uXXXX
    for non-ASCII, which AppleScript can't compile (-2741), so nothing would post.
    """
    macos.osascript(
        'on run argv\n'
        '  display notification (item 2 of argv) with title (item 1 of argv)\n'
        'end run',
        title, body, timeout=_TIMEOUT)


def send(kind: str, *parts: str, title: str, body: str = "",
         target: str | None = None, con=None,
         now: datetime | None = None) -> bool:
    """Queue one notification about one fact. True if it was newly queued.

    `target` is what a click opens; it isn't part of the key. `parts` name the fact, not
    the moment: pass the value that changed, so a re-observed status collapses to one row
    and a flap away and back is two. Pass the caller's `con` so the row shares its transaction.
    """
    key = _notify_key(kind, *parts)
    stamp = (now or datetime.now().astimezone()).isoformat(timespec="seconds")
    owned = con is None
    with db.borrow(con) as con:
        cur = con.execute(
            "INSERT INTO notifications (key, kind, title, body, target, queued_at)"
            " VALUES (?,?,?,?,?,?) ON CONFLICT(key) DO NOTHING",
            (key, kind, title, body, target, stamp))
        # DO NOTHING: the standing row may already be sent. commit(), not `with con:`:
        # a caller's con is usually mid-`with`, which doesn't nest and would commit its half-transaction.
        if owned:
            con.commit()
        return cur.rowcount > 0


def _queued(con) -> list[dict]:
    rows = con.execute(
        "SELECT * FROM notifications WHERE sent_at IS NULL AND expired_at IS NULL"
        " ORDER BY queued_at").fetchall()
    return [dict(r) for r in rows]


def _expire(con, *, now: datetime, now_iso: str) -> int:
    cutoff = (now - timedelta(minutes=NOTIFY_STALE_MIN)).isoformat(
        timespec="seconds")
    with con:
        cur = con.execute(
            "UPDATE notifications SET expired_at=? WHERE sent_at IS NULL"
            " AND expired_at IS NULL AND queued_at < ?", (now_iso, cutoff))
    # Kept, not deleted: the record of what you missed while away.
    return cur.rowcount


def flush(con=None, *, now: datetime | None = None) -> dict:
    """Deliver the queue, oldest first; returns a poller-style info dict.

    The cycle cap defers (unlike the alarm withdraw cap, which refuses); a failed post
    stays queued for the next cycle.
    """
    now = now or datetime.now().astimezone()
    now_iso = now.isoformat(timespec="seconds")
    with db.borrow(con) as con:
        expired = _expire(con, now=now, now_iso=now_iso)
        pending = _queued(con)
        out = {"sent": 0, "failed": 0, "expired": expired,
               "queued": len(pending)}
        if not pending:
            return out
        if not NOTIFY_ENABLED:
            out["disabled"] = True
            return out
        if not at_desk():
            # Held, not failed or dropped.
            out["held"] = len(pending)
            return out

        batch, deferred = pending[:NOTIFY_MAX_PER_CYCLE], \
            pending[NOTIFY_MAX_PER_CYCLE:]
        for row in batch:
            try:
                _post(row["title"], row["body"] or "", row.get("target"))
                with con:
                    con.execute("UPDATE notifications SET sent_at=? WHERE key=?",
                                (now_iso, row["key"]))
                out["sent"] += 1
            except Exception as e:  # retried next cycle
                print(f"[notify] post failed {row['key']}: {e!r}")
                out["failed"] += 1
        if deferred:
            # Logged: a silent cap reads like "nothing else to tell you".
            out["deferred"] = len(deferred)
            print(f"[notify] {len(deferred)} deferred past the per-cycle cap"
                  f" ({NOTIFY_MAX_PER_CYCLE})")
        return out


def recent(con, *, limit: int = 50) -> list[dict]:
    """Everything the channel has said, newest first; the only place expired rows are readable."""
    rows = con.execute(
        "SELECT * FROM notifications ORDER BY queued_at DESC LIMIT ?",
        (limit,)).fetchall()
    return [dict(r) for r in rows]
