"""Messages provider: chat.db (read-only, needs Full Disk Access) -> `messages` cache.

Day is day_key. Traps: dropping `messages` also needs `sync_state` cleared
(`messages_last_rowid`), or the old watermark leaves it empty. `message.text`
is often NULL (content in `attributedBody`). `message.date` is Apple-epoch ns.
Design: DESIGN.md → §9 Messages.
"""

import glob
import re
import sqlite3
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from statistics import median

from .. import citations, db
from . import attachments, heartbeat
from ..config import ADDRESSBOOK_GLOB, CHAT_DB_PATH, MESSAGES_BACKFILL_DAYS
from .ranges import Range

NAME = "messages"
METRICS = ("messages", "sent", "received", "contacts", "unanswered",
           "reply_latency")
DEFAULT_METRIC = "messages"
ALLOW_FUTURE = False  # no next-week messages

APPLE_EPOCH = 978307200  # 2001-01-01 00:00:00 UTC, seconds since Unix epoch
UNANSWERED_HOURS = 24
TEXT_CAP = 2000
PREVIEW_CAP = 160  # one or two lines in the widget; the thread has the rest
SYNC_KEY = "messages_last_rowid"
# Own watermark: attachments backfill independently of the message watermark.
ATTACHMENT_SYNC_KEY = "attachments_last_rowid"
# Receipts mutate chat.db rows in place, past the ROWID watermark; re-read this window by `date`.
RECEIPT_WINDOW_DAYS = 7
RECEIPT_BACKFILL_KEY = "messages_receipts_backfilled"

# Past this gap an outbound message is a new conversation, not a reply.
REPLY_MAX_GAP_MIN = 48 * 60
# Fewer replies is a gap, not a median (single-sample days read "2556 minutes"); costs 12 of 97 days.
REPLY_MIN_SAMPLES = 3


def _ro(path) -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


# ---------- contacts ----------

def normalize_handle(raw: str | None) -> str:
    """phone -> last 10 digits; email -> lowercase; else ''. The one
    normalization for every handle comparison (people.py, import_birthdays)."""
    if not raw:
        return ""
    raw = raw.strip()
    if "@" in raw:
        return raw.lower()
    digits = re.sub(r"\D", "", raw)
    return digits[-10:] if len(digits) >= 10 else digits


def addressbook_records():
    """Yield (card_id, display_name, {normalized identifiers}) per Contacts card
    (grouped by ZOWNER) across every AddressBook source; unreadable sources are skipped.

    `card_id` is ZUNIQUEID, what Contacts.app calls `id of person`. Never merge
    clusters on it: it is per-source.
    """
    for path in glob.glob(ADDRESSBOOK_GLOB):
        try:
            con = _ro(path)
        except Exception:
            continue  # this source file unreadable/absent — others may still work
        try:
            records = {r["Z_PK"]: (r["ZFIRSTNAME"], r["ZLASTNAME"],
                                   r["ZORGANIZATION"], r["ZUNIQUEID"])
                       for r in con.execute(
                           "SELECT Z_PK, ZFIRSTNAME, ZLASTNAME, ZORGANIZATION,"
                           " ZUNIQUEID FROM ZABCDRECORD")}

            def _name_for(pk) -> str | None:
                first, last, org, _uid = records.get(pk, (None, None, None, None))
                full = " ".join(p for p in (first, last) if p)
                return full or org or None

            def _uid_for(pk) -> str | None:
                return records.get(pk, (None, None, None, None))[3]

            ids_by_owner: dict[int, set[str]] = {}
            for r in con.execute("SELECT ZOWNER, ZFULLNUMBER FROM ZABCDPHONENUMBER"):
                h = normalize_handle(r["ZFULLNUMBER"])
                if h:
                    ids_by_owner.setdefault(r["ZOWNER"], set()).add(h)
            for r in con.execute("SELECT ZOWNER, ZADDRESS FROM ZABCDEMAILADDRESS"):
                h = normalize_handle(r["ZADDRESS"])
                if h:
                    ids_by_owner.setdefault(r["ZOWNER"], set()).add(h)
            for pk, ids in ids_by_owner.items():
                yield _uid_for(pk), _name_for(pk), ids
        except Exception:
            continue  # malformed/schema-drift source — others may still work
        finally:
            con.close()


def _load_contacts() -> dict[str, str]:
    """normalized handle -> display name; empty when AddressBook is unreadable."""
    names: dict[str, str] = {}
    for _uid, name, ids in addressbook_records():
        if name:
            for h in ids:
                names[h] = name
    return names


# ---------- attributedBody fallback ----------

_NSSTRING = b"NSString"
_MARKER_WINDOW = 12  # bytes between the class name and the '+' length marker


def _extract_text(text: str | None, attributed_body: bytes | None) -> str:
    """`text`, else the payload after `NSString` + `+` in the typedstream blob
    (one length byte, or 0x81 + LE uint16). Not a general parser; fails soft to ''."""
    if text:
        return text
    if not attributed_body:
        return ""
    try:
        idx = attributed_body.find(_NSSTRING)
        if idx == -1:
            return ""
        i = idx + len(_NSSTRING)
        plus = attributed_body.find(b"+", i, i + _MARKER_WINDOW)
        if plus == -1:
            return ""
        i = plus + 1
        length = attributed_body[i]
        i += 1
        if length == 0x81:  # marker: real length is the next 2 bytes, little-endian
            length = int.from_bytes(attributed_body[i:i + 2], "little")
            i += 2
        return attributed_body[i:i + length].decode("utf-8", errors="replace").strip()
    except Exception:
        return ""


# U+FFFC marks an attachment in the text; U+FFFD appears when the blob decode hit a bad byte.
_PLACEHOLDERS = "￼�"

_ATTACHMENT_KINDS = [
    ("image/gif", "GIF"), ("image/", "photo"), ("video/", "video"),
    ("audio/", "audio"), ("com.apple.coreaudio-format", "audio"),
    ("text/vcard", "contact card"), ("text/x-vlocation", "location"),
    ("application/pdf", "PDF"), ("com.apple.pkpass", "pass"),
]


def _attachment_label(types: str | None, n: int) -> str:
    """Label for an attachment-only message; mixed types fall back to "attachment"."""
    # Kinds are ordered most-specific first ("image/gif" before "image/").
    kinds = {next((label for prefix, label in _ATTACHMENT_KINDS
                   if t.startswith(prefix)), "attachment")
             for t in (types or "").split(",")}
    kind = kinds.pop() if len(kinds) == 1 else "attachment"
    if n < 2:
        return f"({kind})"
    plural = kind + ("s" if not kind.endswith("s") else "")
    return f"({n} {plural})"


def _message_text(text: str | None, attributed_body: bytes | None,
                  attachment_types: str | None) -> str:
    """The caption if any, else the attachment label. A bare placeholder with no
    attachment row (expired media) still reads "(attachment)"."""
    raw = _extract_text(text, attributed_body)
    body = raw.strip(_PLACEHOLDERS + " ").strip()
    if body:
        return body
    if attachment_types:
        return _attachment_label(attachment_types, len(attachment_types.split(",")))
    return "(attachment)" if "￼" in raw else ""


def _ts_of(apple_ns: int) -> datetime:
    return datetime.fromtimestamp(apple_ns / 1e9 + APPLE_EPOCH).astimezone()


# ---------- sync ----------

def _watermark(con) -> int | None:
    row = con.execute("SELECT value FROM sync_state WHERE key=?", (SYNC_KEY,)).fetchone()
    return int(row["value"]) if row else None


def _set_watermark(con, rowid: int) -> None:
    con.execute(
        "INSERT INTO sync_state (key, value) VALUES (?,?)"
        " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (SYNC_KEY, str(rowid)))


def _chat_rows(chat_con, where: str, params: tuple) -> list:
    """chat.db messages matching `where`, in ROWID order, filtered to what `messages` keeps."""
    return chat_con.execute(
        "SELECT m.ROWID, m.guid, m.date, m.text, m.attributedBody, m.is_from_me,"
        " m.associated_message_type,"
        " m.is_read, m.date_read, m.is_delivered, m.date_delivered,"
        " h.id AS handle_id,"
        " (SELECT c.guid FROM chat_message_join cmj JOIN chat c ON c.ROWID=cmj.chat_id"
        "  WHERE cmj.message_id=m.ROWID LIMIT 1) AS chat_guid,"
        " (SELECT group_concat(COALESCE(a.mime_type, a.uti))"
        "  FROM message_attachment_join maj JOIN attachment a ON a.ROWID=maj.attachment_id"
        "  WHERE maj.message_id=m.ROWID) AS attachment_types"
        " FROM message m LEFT JOIN handle h ON h.ROWID=m.handle_id"
        f" WHERE {where} AND m.item_type=0"
        " AND (m.associated_message_type=0"
        "      OR m.associated_message_type BETWEEN 2000 AND 2999)"
        " ORDER BY m.ROWID", params).fetchall()


def _insert(app_con, r, contacts: dict[str, str], now: str) -> tuple[bool, str, str, int]:
    """One chat.db row into `messages`: (inserted, handle, text, is_reaction)."""
    ts = _ts_of(r["date"])
    handle = normalize_handle(r["handle_id"])
    text = _message_text(r["text"], r["attributedBody"],
                         r["attachment_types"])[:TEXT_CAP]
    is_reaction = int(r["associated_message_type"] != 0)
    cur = app_con.execute(
        "INSERT OR IGNORE INTO messages (guid, day, ts, chat_guid, handle,"
        " contact_name, is_from_me, text, refreshed_at, is_reaction,"
        " is_read, read_at, is_delivered, delivered_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (r["guid"], db.day_key(ts), ts.isoformat(timespec="seconds"),
         r["chat_guid"], handle, contacts.get(handle), r["is_from_me"],
         text, now, is_reaction, *_receipt(r)))
    return bool(cur.rowcount), handle, text, is_reaction


def _receipt_ts(apple_ns: int | None) -> str | None:
    """chat.db writes 0, not NULL, for "never"."""
    return _ts_of(apple_ns).isoformat(timespec="seconds") if apple_ns else None


def _receipt(r) -> tuple[int, str | None, int, str | None]:
    """(is_read, read_at, is_delivered, delivered_at) off a chat.db row."""
    return (int(bool(r["is_read"])), _receipt_ts(r["date_read"]),
            int(bool(r["is_delivered"])), _receipt_ts(r["date_delivered"]))


def _sync_receipts(app_con, chat_con, since_ns: int) -> int:
    """Copy read/delivered state for messages dated since `since_ns` onto cached
    rows, writing only changes. Returns rows updated."""
    rows = chat_con.execute(
        "SELECT guid, is_read, date_read, is_delivered, date_delivered"
        " FROM message WHERE date >= ? AND item_type=0"
        " AND (is_read OR date_read OR is_delivered OR date_delivered)",
        (since_ns,)).fetchall()
    n = 0
    with app_con:
        for r in rows:
            is_read, read_at, is_delivered, delivered_at = _receipt(r)
            n += app_con.execute(
                "UPDATE messages SET is_read=?, read_at=?, is_delivered=?,"
                " delivered_at=? WHERE guid=? AND NOT (is_read=? AND"
                " read_at IS ? AND is_delivered=? AND delivered_at IS ?)",
                (is_read, read_at, is_delivered, delivered_at, r["guid"],
                 is_read, read_at, is_delivered, delivered_at)).rowcount
    return n


def _refresh_receipts(app_con, chat_con) -> int:
    """The recent window every cycle; the whole cache once (RECEIPT_BACKFILL_KEY)."""
    done = app_con.execute("SELECT 1 FROM sync_state WHERE key=?",
                           (RECEIPT_BACKFILL_KEY,)).fetchone()
    since = (datetime.now() - timedelta(days=RECEIPT_WINDOW_DAYS)).timestamp()
    if not done:
        oldest = app_con.execute("SELECT MIN(ts) FROM messages").fetchone()[0]
        if oldest:
            since = min(since, datetime.fromisoformat(oldest).timestamp())
    n = _sync_receipts(app_con, chat_con, int((since - APPLE_EPOCH) * 1e9))
    if not done:
        with app_con:
            app_con.execute("INSERT INTO sync_state (key, value) VALUES (?, '1')",
                            (RECEIPT_BACKFILL_KEY,))
    return n


def backfill_older(days: int) -> dict:
    """Extend `messages` back to `days` ago, below the oldest cached row. Leaves
    the ROWID watermark alone; idempotent (INSERT OR IGNORE on guid)."""
    cutoff = int(((datetime.now() - timedelta(days=days)).timestamp()
                  - APPLE_EPOCH) * 1e9)
    now = datetime.now().isoformat(timespec="seconds")
    with db.session() as app_con:
        oldest = app_con.execute("SELECT MIN(ts) FROM messages").fetchone()[0]
        upper = (int((datetime.fromisoformat(oldest).timestamp() - APPLE_EPOCH) * 1e9)
                 if oldest else None)
        chat_con = _ro(CHAT_DB_PATH)
        try:
            where, params = "m.date >= ?", (cutoff,)
            if upper is not None:
                where, params = "m.date >= ? AND m.date <= ?", (cutoff, upper)
            rows = _chat_rows(chat_con, where, params)
            _sync_chats(app_con, chat_con, now)
        finally:
            chat_con.close()
        contacts = _load_contacts()
        n = 0
        with app_con:
            for r in rows:
                n += _insert(app_con, r, contacts, now)[0]
    return {"seen": len(rows), "inserted": n}


def _sync(app_con, chat_con, contacts: dict[str, str], now: str) -> int:
    watermark = _watermark(app_con)
    if watermark is None:
        cutoff = int(((datetime.now() - timedelta(days=MESSAGES_BACKFILL_DAYS))
                      .timestamp() - APPLE_EPOCH) * 1e9)
        where, params = "m.date >= ?", (cutoff,)
    else:
        where, params = "m.ROWID > ?", (watermark,)
    # Dropped: item_type<>0 (group housekeeping), 3000-3999 (un-reacts), 1000 (stickers).
    # Kept, flagged is_reaction: 2000-2999 tapbacks (a reaction you left counts as answering).
    rows = _chat_rows(chat_con, where, params)

    n = 0
    max_rowid = watermark or 0
    with app_con:
        for r in rows:
            max_rowid = max(max_rowid, r["ROWID"])
            n += _insert(app_con, r, contacts, now)[0]
        # An empty first backfill must not write watermark=0, or the next sync pulls all history.
        if rows or watermark is not None:
            _set_watermark(app_con, max_rowid)
    return n


def _attachment_path(raw: str | None) -> str | None:
    """Expand chat.db's '~' path; None means the file isn't on this Mac (row is still kept)."""
    if not raw:
        return None
    return str(Path(raw).expanduser())


def _sync_attachments(app_con, chat_con, now: str) -> None:
    """Record attachment metadata, one row per file, on its own watermark
    (first run: MESSAGES_BACKFILL_DAYS). Opens its own transaction. Never opens
    a file; extraction is on demand in providers/attachments.py."""
    row = app_con.execute("SELECT value FROM sync_state WHERE key=?",
                          (ATTACHMENT_SYNC_KEY,)).fetchone()
    if row is not None:
        # Watermark from app.db: a chat.db "newest with attachment" subquery always includes itself.
        where, params = "m.ROWID > ?", (int(row["value"]),)
    else:
        cutoff = datetime.now() - timedelta(days=MESSAGES_BACKFILL_DAYS)
        where = "m.date > ?"
        params = (int((cutoff.timestamp() - APPLE_EPOCH) * 1e9),)

    rows = chat_con.execute(
        "SELECT m.ROWID, m.guid AS msg_guid, m.date,"
        " a.filename, a.transfer_name, a.mime_type, a.uti,"
        " a.total_bytes, a.is_sticker"
        " FROM message m"
        " JOIN message_attachment_join maj ON maj.message_id = m.ROWID"
        " JOIN attachment a ON a.ROWID = maj.attachment_id"
        f" WHERE {where} AND m.item_type=0"
        " ORDER BY m.ROWID, maj.attachment_id", params).fetchall()

    # Advance past every message scanned, or the window re-walks the same tail forever.
    scanned_max = chat_con.execute(
        f"SELECT COALESCE(MAX(m.ROWID), 0) AS m FROM message m"
        f" WHERE {where} AND m.item_type=0", params).fetchone()["m"]

    ord_by_msg: dict[str, int] = {}
    with app_con:
        for r in rows:
            guid = r["msg_guid"]
            ord_ = ord_by_msg.get(guid, 0)
            ord_by_msg[guid] = ord_ + 1
            day = db.day_key(_ts_of(r["date"]))
            app_con.execute(
                "INSERT OR IGNORE INTO attachments (source, source_ref, ord,"
                " day, filename, mime, size_bytes, locator, is_sticker,"
                " seen_at) VALUES ('message',?,?,?,?,?,?,?,?,?)",
                (guid, ord_, day, r["transfer_name"],
                 r["mime_type"] or r["uti"], r["total_bytes"],
                 _attachment_path(r["filename"]), r["is_sticker"] or 0, now))
        if scanned_max:
            app_con.execute(
                "INSERT INTO sync_state (key, value) VALUES (?,?)"
                " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (ATTACHMENT_SYNC_KEY, str(scanned_max)))


def _sync_chats(app_con, chat_con, now: str) -> int:
    """Upsert every chat's custom name + group flag (style 43 = group, 45 = 1:1).
    Runs every cycle, before the no-new-messages early return: renames add no messages."""
    try:
        rows = chat_con.execute(
            "SELECT guid, display_name, style FROM chat").fetchall()
    except Exception as e:  # schema drift on some macOS — leave stale cache, don't crash the poller
        print(f"[messages] chat metadata sync skipped: {e}")
        heartbeat.log_event("messages", "degraded", f"chat metadata: {e}")
        return 0
    with app_con:
        for r in rows:
            name = (r["display_name"] or "").strip() or None
            app_con.execute(
                "INSERT INTO chats (chat_guid, display_name, is_group, refreshed_at)"
                " VALUES (?,?,?,?)"
                " ON CONFLICT(chat_guid) DO UPDATE SET"
                "  display_name=excluded.display_name, is_group=excluded.is_group,"
                "  refreshed_at=excluded.refreshed_at",
                (r["guid"], name, int(r["style"] == 43), now))
    return len(rows)


def sync_identities(app_con, now: str) -> int:
    """Full-replace the contact_identities clusters (union-find over identifiers
    on a card and across cards sharing one). Never merge on name. An empty
    AddressBook leaves the cache untouched so a read failure doesn't un-merge everyone."""
    groups = [(name, ids) for _uid, name, ids in addressbook_records() if ids]
    if not groups:
        return 0

    parent: dict[str, str] = {}

    def find(x: str) -> str:
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: str, b: str) -> None:
        parent[find(a)] = find(b)

    for _name, ids in groups:
        ids = list(ids)
        for other in ids[1:]:
            union(ids[0], other)

    clusters: dict[str, set[str]] = defaultdict(set)
    for _name, ids in groups:
        root = find(next(iter(ids)))
        for i in ids:
            clusters[root].add(i)
    cluster_name: dict[str, str] = {}
    for name, ids in groups:
        root = find(next(iter(ids)))
        if name and root not in cluster_name:
            cluster_name[root] = name

    with app_con:
        app_con.execute("DELETE FROM contact_identities")
        for root, ids in clusters.items():
            # Email-first, matching import_birthdays, so cluster_key usually equals people.person_key.
            emails = sorted(i for i in ids if "@" in i)
            canonical = emails[0] if emails else sorted(ids)[0]
            name = cluster_name.get(root)
            for i in ids:
                app_con.execute(
                    "INSERT OR REPLACE INTO contact_identities"
                    " (identifier, cluster_key, name, refreshed_at) VALUES (?,?,?,?)",
                    (i, canonical, name, now))
    return len(clusters)


def health() -> dict:
    """Live probe: can *this* process read chat.db? refresh() no-ops silently
    when it can't (FDA doesn't carry over to the .app bundle)."""
    if not CHAT_DB_PATH.exists():
        return {"state": "off", "detail": f"no {CHAT_DB_PATH.name} on this machine"}
    try:
        con = _ro(CHAT_DB_PATH)
        try:
            con.execute("SELECT 1 FROM message LIMIT 1")
        finally:
            con.close()
    except Exception as e:  # any failure here means unreadable
        return {"state": "broken",
                "detail": "chat.db unreadable — grant Full Disk Access to "
                          "whatever runs uvicorn (System Settings -> Privacy "
                          f"& Security), then re-sync. [{e}]"}
    return {"state": "ok", "detail": "chat.db readable"}


def refresh() -> dict:
    """Poller entrypoint: incremental by ROWID watermark; no-op if chat.db is unreadable."""
    try:
        chat_con = _ro(CHAT_DB_PATH)
    except Exception as e:
        return {"skipped": f"can't read {CHAT_DB_PATH}: {e}"}
    try:
        chat_con.execute("SELECT 1 FROM message LIMIT 1")
    except Exception as e:
        chat_con.close()
        return {"skipped": f"can't read {CHAT_DB_PATH}: {e}"}
    now = datetime.now().isoformat(timespec="seconds")
    try:
        with db.session() as app_con:
            _sync_chats(app_con, chat_con, now)  # cheap; refresh GC names every tick
            sync_identities(app_con, now)       # contact clusters for person unification
            # Not gated on new messages: it windows itself and must backfill history.
            _sync_attachments(app_con, chat_con, now)
            watermark = _watermark(app_con)
            n = 0
            if watermark is None or (chat_con.execute(
                    "SELECT MAX(ROWID) AS m FROM message").fetchone()["m"] or 0) > watermark:
                # Gated: this pays for the AddressBook reload.
                n = _sync(app_con, chat_con, _load_contacts(), now)
            # After _sync, so new rows exist; not gated, since a read writes no new row.
            receipts = _refresh_receipts(app_con, chat_con)
    finally:
        chat_con.close()
    return {"new_messages": n, "receipts": receipts}


def cite(it: dict) -> str | None:
    """One message's citation ref; None for aggregates and `unanswered` (no single message)."""
    guid = it.get("guid")
    if not guid:
        return None
    who = it.get("contact_name") or it.get("handle") or "unknown"
    text = (it.get("text") or "").strip().replace("\n", " ")
    label = f"{who}: {text[:80]}" if text else who
    return citations.remember("message", guid, label,
                              it.get("day") or (it.get("ts") or "")[:10])


def brief_line(it: dict) -> str:
    if "hours" in it:  # unanswered
        return f"{it['contact_name'] or it['handle']} — {it['hours']}h ({it['last_ts']})"
    if "text" in it:  # messages
        who = it["contact_name"] or it["handle"] or "?"
        arrow = "->" if it["is_from_me"] else "<-"
        line = f"{it['ts']} {arrow} {who}: {it['text'][:80]}"
        # Outbound only; SMS carries no receipts, so it gets no mark.
        if it["is_from_me"]:
            if it.get("read_at"):
                line += f"  [read {it['read_at'][11:16]}]"
            elif it.get("is_delivered"):
                line += "  [delivered]"
        # Named (actionable); "(unread)" = text not extracted yet.
        for a in it.get("attachments") or []:
            line += f"  📎 {a['filename'] or a['mime']}"
            line += "" if a["status"] == "ok" else " (unread)"
        return line
    return f"{it['day']} {it['value']}"  # sent/received/contacts


# Your tapbacks answer a thread; theirs must neither start the clock nor hide an unanswered message.
COUNTS_AS_TRAFFIC = "NOT (is_reaction = 1 AND is_from_me = 0)"


def _is_correspondent(handle: str, name: str | None, ever_replied: bool) -> bool:
    """In Contacts or ever replied to. SMS shortcodes (<10 digits) never qualify."""
    if handle.isdigit() and len(handle) < 10:
        return False
    return bool(name) or ever_replied


def _unanswered(con) -> tuple[list[dict], str | None]:
    """People who texted you and haven't heard back, longest silence first.

    Range-independent: the candidates are every chat's true last message.
    Excludes group chats (unknown chat_guid counts as 1:1), collapses threads
    per identity cluster (unanswered only if their newest message anywhere
    is), and keeps only `_is_correspondent`. A chat's counterpart is read off
    its inbound rows, since outgoing rows mostly lack a handle."""
    latest_ts: dict[str, str] = {}       # person key -> newest ts in any thread
    latest_row: dict[str, dict] = {}
    replied: set[str] = set()            # person keys you've ever written to
    refreshed: str | None = None

    rows = con.execute(
        "SELECT m.chat_guid, m.ts, m.is_from_me, m.contact_name, m.refreshed_at,"
        "       m.text, m.read_at,"
        "       peer.handle AS peer_handle, peer.mine,"
        "       COALESCE(ci.cluster_key, peer.handle) AS person_key,"
        "       COALESCE(m.contact_name, ci.name) AS person_name"
        " FROM messages m"
        " JOIN ("  # each 1:1 chat's counterpart, last traffic, and your replies
        "   SELECT chat_guid, MAX(handle) AS handle, MAX(ts) AS max_ts,"
        "          SUM(is_from_me) AS mine"
        f"   FROM messages WHERE chat_guid IS NOT NULL AND {COUNTS_AS_TRAFFIC}"
        "   GROUP BY chat_guid"
        " ) peer ON peer.chat_guid = m.chat_guid AND peer.max_ts = m.ts"
        " LEFT JOIN chats c ON c.chat_guid = m.chat_guid"
        " LEFT JOIN contact_identities ci ON ci.identifier = peer.handle"
        " WHERE COALESCE(c.is_group, 0) = 0 AND peer.handle IS NOT NULL"
        f"   AND {COUNTS_AS_TRAFFIC}"
    ).fetchall()

    for r in rows:
        refreshed = max(refreshed or "", r["refreshed_at"] or "") or None
        key = r["person_key"]
        if r["mine"]:
            replied.add(key)
        if r["ts"] <= latest_ts.get(key, ""):
            continue  # an older thread, or a tie inside one — first wins
        latest_ts[key] = r["ts"]
        latest_row[key] = r

    now = datetime.now().astimezone()
    data = []
    for key, last in latest_row.items():
        if last["is_from_me"]:
            continue
        if not _is_correspondent(last["peer_handle"], last["person_name"],
                                 key in replied):
            continue
        age_h = (now - datetime.fromisoformat(last["ts"])).total_seconds() / 3600
        if age_h >= UNANSWERED_HOURS:
            data.append({
                "chat_guid": last["chat_guid"], "handle": last["peer_handle"],
                "person_key": key, "contact_name": last["person_name"],
                "last_ts": last["ts"], "hours": round(age_h, 1),
                "text": (last["text"] or "").strip()[:PREVIEW_CAP],
                # None = never read, distinct from read-and-ignored
                "read_at": last["read_at"],
            })
    data.sort(key=lambda d: -d["hours"])
    return data, refreshed


def reply_latency_by_day(con, start_key: str, end_key: str) -> dict[str, float]:
    """Median minutes you took to answer, per day: `{day: minutes}`.

    The single implementation, shared with features.py. One sample per
    inbound-to-next-outbound pair in 1:1 chats, measured from the *first*
    unanswered inbound (fallback: their newest, when the first is older than
    REPLY_MAX_GAP_MIN), landing on the day of *your reply*. Unanswered
    messages contribute nothing; days under REPLY_MIN_SAMPLES are omitted.
    Design: DESIGN.md → §9 Messages."""
    # Look back one gap-window: the inbound half of a Monday-morning reply may predate the range.
    lookback = (datetime.fromisoformat(start_key)
                - timedelta(minutes=REPLY_MAX_GAP_MIN)).date().isoformat()
    rows = con.execute(
        "SELECT m.chat_guid, m.day, m.ts, m.is_from_me FROM messages m"
        " LEFT JOIN chats c ON c.chat_guid = m.chat_guid"
        " WHERE m.day BETWEEN ? AND ? AND m.chat_guid IS NOT NULL"
        f"  AND COALESCE(c.is_group, 0) = 0 AND {COUNTS_AS_TRAFFIC}"
        " ORDER BY m.chat_guid, m.ts",
        (lookback, end_key)).fetchall()

    samples: dict[str, list[float]] = defaultdict(list)
    chat = None
    waiting_since: datetime | None = None  # oldest unanswered inbound, this chat
    newest_in: datetime | None = None      # their latest, for the stale fallback
    for r in rows:
        if r["chat_guid"] != chat:
            chat, waiting_since, newest_in = r["chat_guid"], None, None
        ts = datetime.fromisoformat(r["ts"])
        if not r["is_from_me"]:
            if waiting_since is None:
                waiting_since = ts
            newest_in = ts
            continue
        if waiting_since is not None:
            for opened_at in (waiting_since, newest_in):
                gap = (ts - opened_at).total_seconds() / 60
                if 0 <= gap <= REPLY_MAX_GAP_MIN:
                    if r["day"] >= start_key:
                        samples[r["day"]].append(gap)
                    break
            waiting_since = None
    return {d: round(median(v), 1) for d, v in samples.items()
            if len(v) >= REPLY_MIN_SAMPLES}


def get(rng: Range, metric: str | None = None) -> dict:
    """Slice the `messages` cache for the range (no on-demand backfill)."""
    metric = metric or DEFAULT_METRIC
    with db.session() as con:
        rows = [dict(r) for r in con.execute(
            "SELECT guid, day, ts, chat_guid, handle, contact_name, is_from_me,"
            " text, refreshed_at, is_read, read_at, is_delivered, delivered_at"
            " FROM messages"
            " WHERE day BETWEEN ? AND ? AND is_reaction = 0"
            " ORDER BY ts DESC", (rng.start_key, rng.end_key)).fetchall()]
        refreshed = max((r["refreshed_at"] for r in rows), default=None)

        if metric == "messages":
            attachments.decorate(con, rows)
            data = rows

        elif metric == "sent":
            by_day = defaultdict(int)
            for r in rows:
                if r["is_from_me"]:
                    by_day[r["day"]] += 1
            data = [{"day": d, "value": by_day[d]} for d in sorted(by_day)]

        elif metric == "received":
            by_day = defaultdict(int)
            for r in rows:
                if not r["is_from_me"]:
                    by_day[r["day"]] += 1
            data = [{"day": d, "value": by_day[d]} for d in sorted(by_day)]

        elif metric == "contacts":
            by_day = defaultdict(set)
            for r in rows:
                if r["handle"]:
                    by_day[r["day"]].add(r["handle"])
            data = [{"day": d, "value": len(by_day[d])} for d in sorted(by_day)]

        elif metric == "unanswered":
            data, seen_at = _unanswered(con)
            refreshed = seen_at or refreshed

        elif metric == "reply_latency":
            by_day = reply_latency_by_day(con, rng.start_key, rng.end_key)
            data = [{"day": d, "value": by_day[d]} for d in sorted(by_day)]

        else:
            raise ValueError(f"unknown metric {metric!r} for messages;"
                             f" expected one of: {', '.join(METRICS)}")

        return {"metric": metric, "data": data, "refreshed_at": refreshed}
