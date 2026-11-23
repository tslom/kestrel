"""The at-desk channel, for facts that are already true.

Steps 1–3 built the channel that reaches a shut laptop: a future-dated alarm on
a surface iCloud syncs, so the phone fires it hours after this machine went to
sleep. That shape only fits an alert with a *known future time*. The other half
of the problem is the fact that just became true and matters in the next few
minutes — a flight that slipped, a package out for delivery. There is no future
time to date those to. They are news, and news is only useful now.

So this channel is an **emitter**, not a reconciler, and the difference is not a
shortcut. alarms.py needs a reconciler because a prediction can be revised: the
flight moves, so the alarm must move with it, which means being able to take it
back. Nothing here is a prediction. A notification states something that already
happened, and the past does not get amended — so there is nothing to withdraw,
and the only bug an emitter can have is saying it twice. That makes dedupe the
entire discipline of this module: one row per *fact*, and the key is the fact.

Delivery is unreliable and the module says so out loud rather than pretending.
`display notification` succeeds whether or not a human is in front of the
screen; there is no delivery proof at any layer. Two consequences shaped
everything below:

  * **A presence gate, not an honest-looking send.** With the screen locked or
    nobody at the keyboard, a banner lands where nobody is looking and we would
    have recorded it delivered. A queued row waits instead.
  * **Staleness beats the queue.** An alarm carries its own fire time and is
    worth retrying all day. A notification means "now" — "leave in five
    minutes", delivered ninety minutes later, is worse than silence. A row that
    waited too long expires rather than firing late.

Senders enqueue a row and nothing more; `flush()` is the only thing that talks
to the OS. The same split as reconcile()/deliver() one module over, for the same
two reasons: an extraction loop must never block on a subprocess, and there
should be exactly one delivery path to test.
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

# Kinds this channel carries. Unlike alarms' KINDS there is no builder registry
# behind these — a notification is enqueued by whatever noticed the fact, at the
# place it noticed it, because "the row moved" is knowledge the writer has and a
# later re-derivation does not.
KINDS = ("flight", "package", "location", "console-turn", "console-ask")

_TIMEOUT = 10           # osascript posting a banner; nothing to wait on
_pipe_lock = threading.Lock()   # one line at a time onto the window's pipe
# The window's pipe, if Kestrel.app started this server (config.NOTIFY_FD).
WINDOW_FD: int | None = int(NOTIFY_FD) if NOTIFY_FD and NOTIFY_FD.isdigit() else None


def _is_pipe(fd: int) -> bool:
    """Only ever write to a pipe: an fd number that means something else in
    this process (a file, a socket) must never get a banner written into it."""
    try:
        return stat.S_ISFIFO(os.fstat(fd).st_mode)
    except OSError:
        return False


def _notify_key(kind: str, *parts: str) -> str:
    """Escapes every part, which is where this differs from alarms._alarm_key.

    There the ':' ban is a useful tripwire, because a part is always an id and a
    colon in one means a composite got passed by mistake. Here a part is the
    *value that changed* — an AeroAPI status is free text and "Delayed until
    15:30" is a perfectly ordinary one. Raising on that would sink a flight poll
    from inside its transaction over a punctuation mark. Escaping keeps the join
    injective, which is the property that actually matters.
    """
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
    """Seconds since the last keypress or mouse move, or None if unanswerable.

    HIDIdleTime is in nanoseconds and comes from the same input-idle clock
    aw-watcher-afk samples for the screen-time provider — this is the primary
    signal, and screen.py's afk buckets are a derived cache over ranges of it.
    """
    out = _probe(_IDLE_PROBE)
    m = _IDLE_RE.search(out) if out else None
    return int(m.group(1)) / 1e9 if m else None


def _screen_locked() -> bool:
    """True only on positive evidence. `CGSSessionScreenIsLocked` is absent from
    IOConsoleUsers entirely while unlocked, so a missing key means unlocked and
    an unreadable probe means unlocked too — both fall the same way as
    at_desk()'s fail-open."""
    out = _probe(_LOCK_PROBE)
    return bool(out and _LOCK_RE.search(out))


def at_desk() -> bool:
    """Is someone actually in front of this machine right now?

    **Fails open.** A probe that cannot answer lets the notification through.
    That direction is chosen, not incidental: this step exists because `_notify`
    sat in email_extract.py for months and never once fired, and a presence check
    that silently swallows every banner is that same bug in better clothes. A
    banner nobody sees costs one wasted line in Notification Center; a channel
    that is quietly always-off costs the feature.

    Input idle is the whole test, and the display power state deliberately isn't
    part of it: `pmset -g powerstate IODisplayWrangler` returns "Internal
    failure" on this machine and IODisplayWrangler is not in its IORegistry at
    all, so the obvious second probe is one that answers "no" for a reason that
    has nothing to do with the user. Idle time also covers the case it would
    have: a shut lid means no input, and a suspended laptop isn't running this
    code in the first place. The lock check earns its place only because typing
    a password *is* input — idle alone would read a lock screen as presence.
    """
    if _screen_locked():
        return False
    idle = _idle_seconds()
    return True if idle is None else idle <= NOTIFY_IDLE_MAX_SEC


# ------------------------------------------------------------------- sending

def _post(title: str, body: str, target: str | None = None) -> None:
    """Post one banner. Raises on failure so flush() can leave the row queued.

    Through the window when Kestrel.app started this server: the window process
    is the bundle, so the banner is Kestrel's (its name, icon and line in System
    Settings › Notifications), and a click brings Kestrel forward on `target`.
    `display notification` posts as Script Editor, so a click on one of those
    opened Script Editor (#80). A pipe that's gone (the window quit under a
    server still draining) falls through to osascript rather than failing.
    """
    fd = WINDOW_FD
    if fd is not None and _is_pipe(fd):
        data = (json.dumps({"title": title, "body": body, "target": target})
                + "\n").encode()
        try:
            # Locked: the poller thread and a route can flush at once, and a
            # write is only atomic up to PIPE_BUF (512 bytes on macOS).
            with _pipe_lock:
                while data:
                    data = data[os.write(fd, data):]
            return
        except OSError as e:
            print(f"[notify] window channel gone ({e!r}); posting via osascript")
    _osascript_post(title, body)


def _osascript_post(title: str, body: str) -> None:
    """The fallback, for a server Kestrel.app didn't start (a terminal uvicorn).

    Values go through `on run argv`, never interpolation — the same trap
    reminders.py documents, and the old `_notify` pasted `json.dumps(body)` into
    the script text instead. That looks safe, because AppleScript does
    understand \\" and \\n. It does not understand \\uXXXX, which json.dumps
    emits for every non-ASCII character, and the failure is not a mangled
    banner: osascript refuses to compile the script at all (checked — "syntax
    error: Expected “"” but found unknown token", -2741). So a merchant named
    "Nespresso Café" or a status carrying an en-dash would have posted nothing,
    under a `check=False` that made sure nobody found out.
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

    `target` is what a click on the banner opens — a console tab id for the
    console kinds. It isn't part of the fact, so it isn't in the key.

    `parts` name the fact and not the moment. A sender passes the value that
    *changed* as a part, which is what makes the two cases come out right: the
    same status re-observed on every poll cycle collapses onto one row and
    notifies once, while a status that flaps away and back is genuinely two
    facts and notifies twice.

    Enqueues only — nothing here touches the OS, so a sender inside an
    extraction loop pays one INSERT and never a subprocess timeout. Passing the
    caller's `con` puts the row in the caller's transaction, which is what you
    want: if the write that discovered the fact rolls back, so does the promise
    to announce it.
    """
    key = _notify_key(kind, *parts)
    stamp = (now or datetime.now().astimezone()).isoformat(timespec="seconds")
    owned = con is None
    with db.borrow(con) as con:
        cur = con.execute(
            "INSERT INTO notifications (key, kind, title, body, target, queued_at)"
            " VALUES (?,?,?,?,?,?) ON CONFLICT(key) DO NOTHING",
            (key, kind, title, body, target, stamp))
        # DO NOTHING, not an upsert on title/body: the row already standing for
        # this fact may already have been *sent*, and rewriting it would say the
        # same thing twice with a slightly different wording.
        #
        # commit() and not `with con:`, which is the point of the `owned` check
        # and not an oversight. A caller's con is usually already inside a
        # `with con:` — sqlite3's connection context manager doesn't nest, so an
        # inner one would commit the caller's half-finished transaction here.
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
    # Kept, not deleted: what you missed while away is worth being able to read
    # back, and it is the only record that the channel had something to say.
    return cur.rowcount


def flush(con=None, *, now: datetime | None = None) -> dict:
    """Deliver the queue. Returns a poller-style info dict.

    Oldest first, and the cycle cap defers rather than refuses — coming back to
    the desk after two hours should not produce a wall of banners, but the rows
    past the cap keep their turn next cycle (until staleness rules them out).
    The opposite call from ALARM_MAX_WITHDRAW_PER_CYCLE, which refuses its whole
    batch, because there half a job strands alarms on a phone while here half
    the news still beats none of it.

    A failed post leaves the row queued and retries next cycle, like the alarm
    writer: self-healing beats a half-state nothing revisits.
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
            # Not a failure and not a drop. Nobody is here to see it.
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
            # Said out loud: a silent cap reads exactly like "there was nothing
            # else to tell you".
            out["deferred"] = len(deferred)
            print(f"[notify] {len(deferred)} deferred past the per-cycle cap"
                  f" ({NOTIFY_MAX_PER_CYCLE})")
        return out


def recent(con, *, limit: int = 50) -> list[dict]:
    """Everything this channel has had to say, newest first — the inspection
    view, and the only place an expired notification is still readable."""
    rows = con.execute(
        "SELECT * FROM notifications ORDER BY queued_at DESC LIMIT ?",
        (limit,)).fetchall()
    return [dict(r) for r in rows]
