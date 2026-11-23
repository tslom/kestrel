"""Gmail provider (IDEAS §5) — the read face of the email review queue.

Gmail is truth; `emails` + `email_days` in app.db are derived cache (metadata
only — bodies are fetched transiently for summarization, never stored).
refresh() syncs the inbox plus a recent-days window and hands new inbox
messages to the summarizer (email_triage.py); get() serves messages by
received date, so /daily-brief and the §1 expansion work like any provider.

Write actions (archive / trash / label / unsubscribe) are explicit and live
in app/routes/email.py — the provider itself never mutates the mailbox.

Auth: shared oauth helper, token row (google, GMAIL_ACCOUNT), gmail.modify.
Day mapping: plain local date of internalDate (same rule as calendar — a 1am
email belongs to its own date, no day_key rollover).
"""

import base64
import threading
import urllib.parse
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date, datetime, time, timedelta

from .. import agent, citations, db  # agent is light; the SDK it wraps loads lazily
from ..config import EMAIL_BODY_FULL_CHARS, GMAIL_ACCOUNT
from . import http, metric_cache, oauth, offline
from .ranges import Range

NAME = "email"
METRICS = None            # no metric axis — message list, like calendar
DEFAULT_METRIC = None
ALLOW_FUTURE = False

API_BASE = "https://gmail.googleapis.com/gmail/v1/users/me"

# refresh() keeps [today-7 .. today] day-synced (covers "today"/"this-week");
# colder days are fetched on demand in get() and backfill the cache.
REFRESH_PAST_DAYS = 7

METADATA_HEADERS = ("From", "To", "Subject", "List-Unsubscribe", "List-Unsubscribe-Post")


def api_request(url: str, token: str, method: str = "GET", body: dict | None = None,
             idempotent: bool | None = None) -> dict:
    return http.json_request(url, label="gmail API", token=token,
                             method=method, body=body, idempotent=idempotent,
                             reauth=lambda: oauth.refresh("google", GMAIL_ACCOUNT))


def access_token() -> str:
    return oauth.get_token("google", GMAIL_ACCOUNT)


# One refresh() reads the same message whole up to three times — attachment
# metadata, the triage excerpt, the package/trip extraction excerpt. Inside
# message_memo() each format=full answer is kept for the rest of the block,
# so a message costs one Gmail call per refresh. Module-level rather than a
# contextvar because the extractor fetches from a thread pool, which a
# contextvar doesn't reach; a lock covers that pool. Outside a refresh there
# is no memo and every read is live (a body() that lands mid-refresh may get
# that refresh's copy — seconds old, and a sent message doesn't change). Only
# successes are kept — a failed fetch is retried by the next caller, as before.
_memo: dict[str, dict] | None = None
_memo_lock = threading.Lock()


@contextmanager
def message_memo() -> Iterator[None]:
    """Memoize full-message fetches for this block. Nests: only the outermost
    block owns (and drops) the memo."""
    global _memo
    with _memo_lock:
        owner = _memo is None
        if owner:
            _memo = {}
    try:
        yield
    finally:
        if owner:
            with _memo_lock:
                _memo = None


def full_message(token: str, msg_id: str) -> dict:
    """GET messages/{id}?format=full — memoized inside message_memo(). Raises
    on failure (RuntimeError for an HTTP answer, URLError for no network)."""
    with _memo_lock:
        memo = _memo
        if memo is not None and msg_id in memo:
            return memo[msg_id]
    msg = api_request(f"{API_BASE}/messages/{msg_id}?format=full", token)
    if memo is not None:
        with _memo_lock:
            memo[msg_id] = msg
    return msg


def list_ids(token: str, query: str, max_ids: int | None = None) -> list[str]:
    """Message ids matching a Gmail search query (paginated), in Gmail's
    order — all of them, or the first `max_ids` (a free-text search can
    match the whole mailbox)."""
    out, page_token = [], None
    while True:
        params = {"q": query, "maxResults": str(min(max_ids or 100, 100))}
        if page_token:
            params["pageToken"] = page_token
        blob = api_request(f"{API_BASE}/messages?{urllib.parse.urlencode(params)}", token)
        out += [m["id"] for m in blob.get("messages", [])]
        page_token = blob.get("nextPageToken")
        if not page_token or (max_ids and len(out) >= max_ids):
            break
    return out[:max_ids] if max_ids else out


def header(payload: dict, name: str) -> str | None:
    for h in payload.get("headers", []):
        if h.get("name", "").lower() == name.lower():
            return h.get("value")
    return None


def split_from(raw: str | None) -> tuple[str | None, str | None]:
    """'Display Name <a@b.c>' -> (name, addr); bare address -> (None, addr)."""
    if not raw:
        return None, None
    if "<" in raw and raw.rstrip().endswith(">"):
        name, _, addr = raw.rpartition("<")
        return name.strip().strip('"') or None, addr.rstrip(">").strip()
    return None, raw.strip()


def _first_addr(raw: str | None) -> str | None:
    """Just the first recipient of a (possibly multi-address) To header,
    lowercase — enough to match a sent message back to a people.person_key;
    not a full address-list parse."""
    if not raw:
        return None
    _, addr = split_from(raw.split(",")[0])
    return addr.lower() if addr else None


def _fetch_metadata(token: str, msg_id: str) -> dict:
    """One message's cache row, from format=metadata (no body)."""
    params = urllib.parse.urlencode(
        [("format", "metadata")] + [("metadataHeaders", h) for h in METADATA_HEADERS])
    m = api_request(f"{API_BASE}/messages/{msg_id}?{params}", token)
    payload = m.get("payload", {})
    received = datetime.fromtimestamp(int(m["internalDate"]) / 1000).astimezone()
    labels = set(m.get("labelIds", []))
    name, addr = split_from(header(payload, "From"))
    unsub_post = header(payload, "List-Unsubscribe-Post") or ""
    return {
        "msg_id": m["id"],
        "thread_id": m.get("threadId"),
        "day": received.date().isoformat(),
        "received_at": received.isoformat(timespec="seconds"),
        "from_name": name,
        "from_addr": addr,
        "to_addr": _first_addr(header(payload, "To")),
        "subject": header(payload, "Subject"),
        "snippet": m.get("snippet"),
        "in_inbox": int("INBOX" in labels),
        "unread": int("UNREAD" in labels),
        "is_sent": int("SENT" in labels),
        "list_unsubscribe": header(payload, "List-Unsubscribe"),
        "list_unsubscribe_post": int("one-click" in unsub_post.lower()),
    }


def upsert(con, row: dict, now: str) -> None:
    con.execute(
        "INSERT INTO emails (msg_id, thread_id, day, received_at, from_name,"
        " from_addr, to_addr, subject, snippet, in_inbox, unread, is_sent,"
        " list_unsubscribe, list_unsubscribe_post, refreshed_at)"
        " VALUES (:msg_id,:thread_id,:day,:received_at,:from_name,:from_addr,"
        ":to_addr,:subject,:snippet,:in_inbox,:unread,:is_sent,"
        ":list_unsubscribe,:list_unsubscribe_post,:now)"
        " ON CONFLICT(msg_id) DO UPDATE SET"
        " in_inbox=excluded.in_inbox, unread=excluded.unread, is_sent=excluded.is_sent,"
        " to_addr=excluded.to_addr,"
        " snippet=excluded.snippet, refreshed_at=excluded.refreshed_at",
        {**row, "now": now})


def _chunks(seq: list, size: int = 500):
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


def fetch_new(con, token: str, ids: list[str]) -> list[dict]:
    """Metadata rows for ids not yet cached (one API call each). Network
    only — the caller writes them under its own transaction. Never call
    this while holding a write transaction: N sequential Gmail round-trips
    inside one would hold the app.db write lock past every other
    connection's busy_timeout ("database is locked" 500s elsewhere)."""
    known: set[str] = set()
    for chunk in _chunks(ids):
        known |= {r["msg_id"] for r in con.execute(
            "SELECT msg_id FROM emails WHERE msg_id IN (%s)"
            % ",".join("?" * len(chunk)), chunk)}
    return [_fetch_metadata(token, msg_id) for msg_id in ids
            if msg_id not in known]


def _set_flag(con, column: str, ids: list[str], scope: str = "",
              params: tuple = ()) -> None:
    """Make `column` true for exactly `ids` within the scoped rows (false on
    the rest of the scope; rows outside the scope keep their value)."""
    con.execute(f"UPDATE emails SET {column}=0 WHERE {column}=1"
                + (f" AND {scope}" if scope else ""), params)
    for chunk in _chunks(ids):
        con.execute("UPDATE emails SET %s=1 WHERE msg_id IN (%s)"
                    % (column, ",".join("?" * len(chunk))), chunk)


def _sync_inbox(con, token: str, now: str) -> dict:
    """Reconcile inbox membership: fetch new inbox messages, flip in_inbox/
    unread on cached rows to match Gmail (archives done elsewhere show up).
    The unread reconcile here only speaks for inbox rows — archived mail's
    read state is reconciled per day-window in _sync_days."""
    inbox_ids = list_ids(token, "in:inbox")
    unread_ids = list_ids(token, "in:inbox is:unread")
    new = fetch_new(con, token, inbox_ids)
    with con:
        for row in new:
            upsert(con, row, now)
        _set_flag(con, "in_inbox", inbox_ids)
        _set_flag(con, "unread", unread_ids, "in_inbox=1")
    return {"inbox": len(inbox_ids), "new": len(new)}


def _today() -> date:
    """The last day mail can have arrived on: the civil date, or the day key if
    that's later. See refresh() for why email doesn't take the 4am rollover."""
    return max(date.fromisoformat(db.day_key()), datetime.now().date())


def _sync_days(con, token: str, start: date, end: date, now: str) -> int:
    """Pull message metadata received in [start, end] (inclusive local days)
    and mark those days synced. Gmail after:/before: take epoch seconds.
    Also reconciles `unread` for every cached row in the window, so read
    state on archived mail tracks Gmail too, not just the inbox."""
    tz = datetime.now().astimezone().tzinfo
    after = int(datetime.combine(start, time.min, tz).timestamp())
    before = int(datetime.combine(end + timedelta(days=1), time.min, tz).timestamp())
    ids = list_ids(token, f"after:{after} before:{before}")
    unread_ids = list_ids(token, f"is:unread after:{after} before:{before}")
    new = fetch_new(con, token, ids)
    with con:
        for row in new:
            upsert(con, row, now)
        _set_flag(con, "unread", unread_ids, "day BETWEEN ? AND ?",
                  (start.isoformat(), end.isoformat()))
        # Never mark a day that hasn't happened: its (empty) result is not an
        # answer, and a marked day is never backfilled. A this-year read on
        # 08-13 once marked Sep–Dec, so a day the recent-days refresh then
        # missed (app closed) would have stayed empty for good.
        d, last = start, min(end, _today())
        while d <= last:
            con.execute(
                "INSERT INTO email_days (day, refreshed_at) VALUES (?,?)"
                " ON CONFLICT(day) DO UPDATE SET refreshed_at=excluded.refreshed_at",
                (d.isoformat(), now))
            d += timedelta(days=1)
    return len(new)


def _attachment_parts(payload: dict) -> list[dict]:
    """Attachment parts of a format=full payload, outermost first.

    A part is an attachment when it carries a filename; body.attachmentId is
    the handle you exchange for bytes later. Inline images qualify too and
    are kept — a pasted screenshot is still a file that arrived."""
    out, stack = [], [payload]
    while stack:
        part = stack.pop(0)
        stack += part.get("parts", [])
        if part.get("filename"):
            out.append(part)
    return out


def _sync_attachments(con, token: str, start: date, end: date,
                      now: str) -> int:
    """Record what arrived attached, without downloading any of it.

    Targeted rather than universal: the sync's own fetches use
    format=metadata, which omits the parts tree entirely, so learning an
    email's attachments costs a second format=full round-trip. Asking Gmail
    `has:attachment` first keeps that cost to the handful of messages that
    actually have one, and the already-recorded check keeps it to once per
    message ever.

    What lands here is metadata only — filename, mime, size, and the opaque
    attachmentId. Bytes are fetched on demand (see fetch_bytes), so a mailbox
    full of hostile PDFs never writes a single file to disk on its own."""
    tz = datetime.now().astimezone().tzinfo
    after = int(datetime.combine(start, time.min, tz).timestamp())
    before = int(datetime.combine(end + timedelta(days=1), time.min, tz).timestamp())
    ids = list_ids(token, f"has:attachment after:{after} before:{before}")
    if not ids:
        return 0
    known: set[str] = set()
    days: dict[str, str] = {}
    for chunk in _chunks(ids):
        marks = ",".join("?" * len(chunk))
        known |= {r["source_ref"] for r in con.execute(
            "SELECT DISTINCT source_ref FROM attachments"
            f" WHERE source='email' AND source_ref IN ({marks})", chunk)}
        days |= {r["msg_id"]: r["day"] for r in con.execute(
            f"SELECT msg_id, day FROM emails WHERE msg_id IN ({marks})", chunk)}

    # Network first, transaction after — same rule as fetch_new: never hold
    # the app.db write lock across a sequence of Gmail round-trips.
    found: list[tuple] = []
    for msg_id in ids:
        if msg_id in known or msg_id not in days:
            continue
        try:
            m = full_message(token, msg_id)
        except Exception:
            continue  # transient; the next refresh retries this message
        for ord_, part in enumerate(_attachment_parts(m.get("payload", {}))):
            body = part.get("body", {})
            found.append((msg_id, ord_, days[msg_id], part.get("filename"),
                          part.get("mimeType"), body.get("size"),
                          body.get("attachmentId"), now))
    with con:
        con.executemany(
            "INSERT OR IGNORE INTO attachments (source, source_ref, ord, day,"
            " filename, mime, size_bytes, locator, seen_at)"
            " VALUES ('email',?,?,?,?,?,?,?,?)", found)
    return len(found)


def fetch_attachment(msg_id: str, attachment_id: str) -> bytes:
    """Exchange a Gmail attachmentId for the actual bytes — on demand only.

    Deliberately not called from any sync path. Attachment metadata is
    recorded without this, so the inbox stays browsable while hostile content
    stays on Google's servers until something specifically asks for it. The
    caller gets bytes in memory and is expected to drop them; nothing here
    writes to disk."""
    data = api_request(
        f"{API_BASE}/messages/{msg_id}/attachments/{attachment_id}", access_token())
    raw = data.get("data")
    return base64.urlsafe_b64decode(raw + "===") if raw else b""


def refresh() -> dict:
    """Poller entrypoint: inbox + recent-days window, then summarize new
    inbox messages (cached by msg_id — nothing is summarized twice)."""
    token = access_token()
    now = datetime.now().isoformat(timespec="seconds")
    # The window ends on the civil date, not the day key. Everything email files
    # by the date it arrived (see the module docstring — no 4am rollover here),
    # so between midnight and 4am a day_key end bound stops at a midnight that
    # has already passed: mail from the small hours falls outside the window and
    # only gets its days marked and its attachments recorded on some later
    # refresh. A pass forwarded at 12:41am was invisible until dawn.
    today = _today()
    start = today - timedelta(days=REFRESH_PAST_DAYS)
    with message_memo():             # one full fetch per message per refresh
        with db.session() as con:
            # _sync_inbox/_sync_days each open their own short write transaction
            # after their Gmail calls return — no lock held across the network.
            info = _sync_inbox(con, token, now)
            info["fetched_days"] = _sync_days(con, token, start, today, now)
            info["attachments"] = _sync_attachments(con, token, start, today, now)
        from . import email_extract, email_triage  # late import — the SDK is heavy, keep get() light
        # The model-side half. Mail has synced by here, so a spent usage
        # window must not paint the whole source failed: the work it owes is
        # keyed on what's still unprocessed and simply catches up next cycle.
        # Anything that isn't the model saying no still fails the refresh.
        try:
            info["summarized"] = email_triage.summarize_pending(token)
        except agent.AgentError as e:
            info["summarized"] = {"skipped": str(e)}
        info["extracted"] = email_extract.refresh(token)
    return info


def body(msg_id: str, limit: int | None = None, html: bool = False) -> dict:
    """One message read whole — headers from the cache, body from Gmail and
    thrown away after (the `emails` table stays metadata-only, §5).

    Over `limit` the quoted reply chain is dropped before truncating: a
    forwarded invite keeps its link at the bottom, so a blind cut is the one
    way to lose the thing you opened the message for. `quoted_trimmed` and
    `truncated` say which happened.

    `html=True` adds the defanged HTML part as `html` (None when there isn't
    one) for the reader's formatted view; it is never capped by `limit`,
    which is a text budget.
    """
    limit = limit or EMAIL_BODY_FULL_CHARS
    with db.session() as con:
        row = con.execute(
            "SELECT msg_id, day, received_at, from_name, from_addr, subject,"
            " in_inbox, unread FROM emails WHERE msg_id = ?",
            (msg_id,)).fetchone()
    if row is None:
        raise ValueError(f"no cached message {msg_id!r} — read the range it"
                         " landed in first, which syncs that day")
    from . import email_triage  # late import — the SDK is heavy, keep get() light
    # Unlike the refresh-time excerpts, a failure here is the answer: an empty
    # body would read as "this email says nothing". NotAuthorized -> ValueError
    # (400 / one-line CLI error), an HTTP refusal is already a RuntimeError
    # (502), and a lost network becomes one too rather than a bare 500.
    markup = None
    try:
        with oauth.as_value_error(), message_memo():   # one Gmail call for both
            token = access_token()
            text = email_triage.fetch_body(token, msg_id)
            if html:
                markup = email_triage.fetch_html(token, msg_id)
    except Exception as e:
        if offline.is_network_error(e):
            raise RuntimeError(f"gmail unreachable: {e}") from None
        raise
    quoted_trimmed = False
    if len(text) > limit:
        stripped = email_triage.strip_quotes(text)
        if stripped and len(stripped) < len(text):
            text, quoted_trimmed = stripped, True
    truncated = len(text) > limit
    out = {"id": row["msg_id"], "day": row["day"],
           "received": row["received_at"],
           "from": row["from_name"] or row["from_addr"],
           "from_addr": row["from_addr"], "subject": row["subject"],
           "in_inbox": bool(row["in_inbox"]), "unread": bool(row["unread"]),
           "body": text[:limit], "chars": min(len(text), limit),
           "quoted_trimmed": quoted_trimmed, "truncated": truncated}
    if html:
        out["html"] = markup
    return out


def brief_line(m: dict) -> str:
    """One-line message for --format brief. '*' marks unread, [label] is
    where review filed it (or would). No id: the console cites by ref
    (console_tools._with_refs), and a raw Gmail id in the line is what it
    once cited instead — the JSON format carries the id."""
    mark = "*" if m["unread"] else " "
    label = f" [{m['label'].removeprefix('Filed/')}]" if m.get("label") else ""
    return f"{m['received'][:16]} {mark} {m['from']}: {m['subject']}{label}"


def search(text: str, subreddit: str | None = None, days: int = 365,
           limit: int = 25) -> dict:
    """`--search`: every cached message ranked (subject, sender, summary,
    label, and the search terms pulled out of each body), then Gmail's own
    full-text search when the cache has nothing. `subreddit` is the CLI's
    shared --sub and means nothing here. See email_search.py."""
    from . import email_search
    with oauth.as_value_error():
        hits = email_search.search(text, days=days, limit=min(max(limit, 1), 100))
    return {"source": NAME, "search": text, "days": days, **hits}


def cite(m: dict) -> str | None:
    """This row's citation ref, minted on the way out.

    `get()` flattens from_name/from_addr into one `from`, which is all the
    label needs — the ref points at `msg_id`, and the address is still in the
    row `citations.resolve()` reads back.
    """
    if not m.get("id"):
        return None
    return citations.remember(
        "email", m["id"], f"{m.get('from') or 'unknown'}: "
        f"{m.get('subject') or '(no subject)'}",
        m.get("day") or (m.get("received") or "")[:10])


def get(rng: Range, metric: str | None = None) -> dict:
    """Messages received in the range, newest first; cold days synced on
    demand (one Gmail search spanning them) and backfilled into the cache."""
    with db.session() as con:
        missing = metric_cache.missing_days(con, "email_days", rng,
                                            upto=_today().isoformat())
        if missing:
            with offline.best_effort("email backfill"), oauth.as_value_error():
                now = datetime.now().isoformat(timespec="seconds")
                _sync_days(con, access_token(), date.fromisoformat(missing[0]),
                           date.fromisoformat(missing[-1]), now)
        rows = con.execute(
            "SELECT e.*, t.summary, t.action,"
            " IFNULL(t.user_label, t.label) AS label FROM emails e"
            " LEFT JOIN email_triage t ON t.msg_id = e.msg_id"
            " WHERE e.day BETWEEN ? AND ?"
            " ORDER BY e.received_at DESC",
            (rng.start_key, rng.end_key)).fetchall()
        refreshed = metric_cache.oldest_refresh(con, "email_days", rng)
    data = [{
        "id": r["msg_id"],
        "day": r["day"],
        "received": r["received_at"],
        "from": r["from_name"] or r["from_addr"],
        "subject": r["subject"],
        "in_inbox": bool(r["in_inbox"]),
        "unread": bool(r["unread"]),
        "summary": r["summary"],
        "label": r["label"],
    } for r in rows]
    return {"data": data, "count": len(data),
            "unread": sum(d["unread"] for d in data), "refreshed_at": refreshed}
