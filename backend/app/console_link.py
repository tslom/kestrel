"""A console session's line to the page, outliving any one socket.

A reload (⌘R) used to end every turn in flight: the session lived inside the
websocket handler, and the handler's `finally` denied the cards waiting and
disconnected the CLI the moment the socket closed. Now the session talks to a
`Link` instead, registered by its tab's id. The socket of the moment attaches
to it; when that socket goes, the session keeps working with nobody listening
and the next page to open the same tab attaches and carries on.

What a page that just arrived needs, and why a Link records it:

  * **The current turn so far** (`turn`). Everything before the turn is on
    disk in the session's transcript, so the Link notes how long that file
    was when the turn began (`cut`) and keeps the turn's own events; the page
    reads the transcript up to the cut and replays the events after it. No
    overlap, nothing missing. Consecutive text deltas are joined as they're
    recorded, so a long reply is one entry, not thousands.
  * **The state messages a fresh start would have sent** (`sticky`): the
    session id, the mode, the worktree, the command list, tasks, the agents
    strip, the context size — the last of each.
  * **Only the requests still open.** A card (or an open_tab / tabs request)
    already answered stays answered; one still waiting is replayed and its
    answer goes to the future that's been waiting all along.

A detached session is ended once it's been idle (`busy()` false) for
IDLE_GRACE_S, the way closing a socket used to end it at once — long enough
for a reload, short enough that closing a browser tab doesn't leave a CLI
running all evening. A busy one is left to finish, up to DETACHED_MAX_S.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Callable

from fastapi import WebSocket, WebSocketDisconnect

IDLE_GRACE_S = 20.0
DETACHED_MAX_S = 3 * 3600.0
REAP_EVERY_S = 2.0

# The last of each of these is what a fresh page is told on attach.
STICKY = frozenset({"session", "session_info", "mode", "model", "worktree", "commands", "held",
                    "tasks", "agents", "context", "mcp", "shells"})
# Never replayed: audio for words already in the log, and nudges that only
# mean something the moment they're sent.
UNRECORDED = frozenset({"speech", "phase", "refresh"})

# What changes whether a tab has work in flight (console_work).
WORK_EVENTS = frozenset({"turn_start", "result", "idle", "agents", "session"})

_CLOSED = object()


class Link:
    def __init__(self, key: str | None):
        self.key = key
        self.ws: WebSocket | None = None
        self.inbox: asyncio.Queue = asyncio.Queue()
        self.sticky: dict[str, dict] = {}
        self.turn: list[dict] | None = None   # None between turns
        self.cut: int | None = None           # transcript bytes when it began
        self.cut_session: str | None = None
        self.detached_at: float | None = time.monotonic()
        self.closed = False
        # Set by the session once it has them.
        self.busy: Callable[[], bool] = lambda: False
        self.open_request: Callable[[str], bool] = lambda _id: False
        self.transcript_at: Callable[[], tuple[str | None, int | None]] = lambda: (None, None)
        self.queued_text: dict[str, str] = {}
        self.matches: dict[str, Any] = {}     # profile / project it was started with
        # Called when what the tab has in flight may have changed: a turn
        # starting or ending, the agents strip moving (console_work.mark).
        self.on_work: Callable[[], None] = lambda: None

    # ------------------------------------------------------------ the session's side

    async def send_json(self, msg: dict) -> None:
        self._record(msg)
        ws = self.ws
        if ws is None:
            return
        try:
            await ws.send_json(msg)
        except Exception:   # a socket mid-close; the pump notices
            pass

    async def receive_text(self) -> str:
        item = await self.inbox.get()
        if item is _CLOSED:
            raise WebSocketDisconnect(1000)
        return item

    def begin_turn(self, user: dict | None) -> None:
        """A turn starts: note where the transcript ends, start its log. `user`
        is your message, if you sent it (the stream doesn't echo it back)."""
        self.cut_session, self.cut = self.transcript_at()
        self.turn = [user] if user else []
        self._work()

    def _work(self) -> None:
        try:
            self.on_work()
        except Exception as e:   # bookkeeping never breaks a turn
            print(f"[console] couldn't record the tab's work: {e!r}")

    def queue_text(self, qid: str, text: str) -> None:
        self.queued_text[qid] = text

    def _record(self, msg: dict) -> None:
        self._record_only(msg)
        if msg.get("type") in WORK_EVENTS:
            self._work()

    def _record_only(self, msg: dict) -> None:
        mtype = msg.get("type")
        if mtype in STICKY:
            self.sticky[mtype] = msg
            # A new session is named during its first turn: before that turn
            # there was no transcript at all, so the whole of it is replay.
            if (mtype == "session" and self.turn is not None
                    and self.cut_session is None and msg.get("id")):
                self.cut_session, self.cut = msg["id"], 0
            return
        if mtype in UNRECORDED:
            return
        if mtype == "turn_start" and self.turn is None:
            self.begin_turn(None)
        if self.turn is None:
            return
        last = self.turn[-1] if self.turn else None
        if (mtype == "text_delta" and last is not None
                and last.get("type") == "text_delta"):
            self.turn[-1] = {**last, "text": last["text"] + msg.get("text", "")}
        else:
            self.turn.append(dict(msg))
        if mtype in ("result", "idle"):
            # Over, and on disk: a page arriving now reads it all from there.
            self.turn, self.cut, self.cut_session = None, None, None

    # ------------------------------------------------------------- the sockets' side

    def replay(self) -> list[dict]:
        out = [m for t, m in self.sticky.items()]
        for m in self.turn or []:
            rid = m.get("request_id")
            if rid and not self.open_request(rid):
                continue
            if m.get("type") == "queued_in":
                # The page that queued it is gone, and with it the text.
                text = self.queued_text.get(m.get("id") or "")
                if text:
                    out.append({"type": "replay_user", "text": text, "queued": True})
                continue
            out.append(m)
        return out

    async def attach(self, ws: WebSocket, *, resumed: bool) -> None:
        """Make `ws` the socket this session talks through. `resumed`: a page
        coming back to a live session, which is told so and caught up."""
        old, self.ws, self.detached_at = self.ws, ws, None
        if old is not None and old is not ws:
            # Another page took this tab over. 4001 tells it not to retry.
            try:
                await old.close(code=4001)
            except Exception:   # already gone
                pass
        if resumed:
            await ws.send_json({"type": "attached", "cut": self.cut,
                                "cut_session": self.cut_session,
                                "turn": self.turn is not None,
                                "replay": self.replay()})

    def detach(self, ws: WebSocket) -> None:
        if self.ws is ws:
            self.ws = None
            self.detached_at = time.monotonic()

    def close(self) -> None:
        if not self.closed:
            self.closed = True
            self.inbox.put_nowait(_CLOSED)

    def expired(self, now: float) -> bool:
        """Detached long enough to end: idle past the grace, or past the cap.
        A session nobody can come back to (no key) ends as soon as it's alone."""
        if self.ws is not None or self.detached_at is None:
            return False
        alone = now - self.detached_at
        if self.key is None:
            return True
        if alone >= DETACHED_MAX_S:
            return True
        return alone >= IDLE_GRACE_S and not self.busy()


# The live links, by tab id.
links: dict[str, Link] = {}
_reaper: asyncio.Task | None = None


def ensure_reaper() -> None:
    global _reaper
    if _reaper is None or _reaper.done():
        _reaper = asyncio.get_running_loop().create_task(_reap())


async def _reap() -> None:
    while links:
        await asyncio.sleep(REAP_EVERY_S)
        reap_once(time.monotonic())


def reap_once(now: float) -> None:
    for key, link in list(links.items()):
        if link.closed:
            links.pop(key, None)
        elif link.expired(now):
            links.pop(key, None)
            link.close()


def register(link: Link) -> None:
    """A new session for its tab. One already registered there is ended:
    a start that didn't ask to attach is a deliberate new session (New
    chat, another session, a model change), which used to replace the old
    socket and its CLI with it."""
    if link.key is None:
        return
    old = links.get(link.key)
    if old is not None and old is not link:
        old.close()
    links[link.key] = link
    ensure_reaper()


def unregister(link: Link) -> None:
    if link.key is not None and links.get(link.key) is link:
        links.pop(link.key, None)
