"""Messages provider + self-chat task capture.

chat.db is truth; `messages` in app.db is a genuine cache here (unlike
Spotify's `plays` — chat.db keeps full local history, so a cache drop only
costs a re-sync, up to MESSAGES_BACKFILL_DAYS back, not lost data). Day is
**day_key** (4am rollover), a behavioral source like screen/spotify/location
— 1am texting belongs to yesterday.

To force a clean re-sync (e.g. after a schema change), drop `messages` AND
clear `sync_state` (`DELETE FROM sync_state WHERE key='messages_last_rowid'`)
— they're separate tables with independent lifecycles. Dropping only
`messages` leaves the old high-water ROWID in `sync_state`, so the next sync
resumes past it and the table stays empty instead of re-backfilling.

No OAuth: this just needs Full Disk Access granted to whichever process runs
uvicorn (System Settings -> Privacy & Security -> Full Disk Access; test with
a manual sqlite3 read first — CLAUDE.md's Run section). Both source
databases are opened read-only (`file:...?mode=ro`) — this provider never
writes to Messages or Contacts.

Known landmine: on modern macOS `message.text` is frequently NULL, with the
real content buried in `attributedBody` (an NSKeyedArchiver typedstream
blob). `_extract_text` is not a general typedstream parser — it decodes just
the one shape needed: a length-prefixed UTF-8 payload right after the
`NSString` class marker (see its docstring). The parsing logic scored
300/300 against real attributedBody blobs on this machine, using each row's
`text` column as ground truth — but that sample was necessarily rows where
`text` was ALSO populated (simple single-run messages), which is exactly the
case where `_extract_text` short-circuits and never runs its parser. The
fallback path is actually exercised on `text IS NULL` rows (quoted replies,
messages with rich attribute runs) that weren't part of the validated
sample; it fails soft (empty or, in principle, a wrong string from an
earlier `NSString` in the blob) rather than raising, which is an acceptable
risk for a personal single-user cache but not a guarantee. `message.date` is
Apple epoch **nanoseconds** (÷1e9 + APPLE_EPOCH), not the seconds some older
docs describe.

Texts you send yourself (MESSAGES_SELF_HANDLES) are synced like any
other; app/selftexts.py picks them out of `messages` and routes them.
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
# Separate from SYNC_KEY: the attachments table arrives on a DB whose messages
# are already synced, so it has to backfill on its own schedule rather than
# inherit a watermark that is already at the end of history.
ATTACHMENT_SYNC_KEY = "attachments_last_rowid"
# Read receipts land on chat.db rows in place, after the row was synced, so the
# ROWID watermark never revisits them. Each cycle re-reads this window by
# `date` (indexed in chat.db) — a receipt for a message older than a week is
# rare, and the one-time backfill below covered everything before it.
RECEIPT_WINDOW_DAYS = 7
RECEIPT_BACKFILL_KEY = "messages_receipts_backfilled"

# Past this gap an outbound message isn't a reply to the inbound one before it
# — it's you opening a new conversation days later, and counting it would put
# a 90-hour "latency" in the same median as a 4-minute one. 48h is generous
# enough to keep a genuinely slow reply (which is the signal) and tight enough
# to drop "texted them again the following week".
REPLY_MAX_GAP_MIN = 48 * 60
# Below this many replies a day's median is one or two conversations, not a
# habit: measured on real history, the days under it were single-sample days
# reporting things like "2556 minutes" off one slow answer. Gap, not zero —
# the HR_MIN_HOURS / GENRE_MIN_PLAYS precedent. Costs 12 of 97 days.
REPLY_MIN_SAMPLES = 3


def _ro(path) -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


# ---------- contacts ----------

def normalize_handle(raw: str | None) -> str:
    """phone -> last 10 digits; email -> lowercase. Empty/unrecognized ->
    ''. Same normalization on both sides (chat.db handles, AddressBook
    numbers/emails, MESSAGES_SELF_HANDLES) so they compare equal. Public
    (no leading underscore): people.py's person_key and
    scripts/import_birthdays.py's contact keying both depend on it."""
    if not raw:
        return ""
    raw = raw.strip()
    if "@" in raw:
        return raw.lower()
    digits = re.sub(r"\D", "", raw)
    return digits[-10:] if len(digits) >= 10 else digits


def addressbook_records():
    """Yield (card_id, display_name, {normalized identifiers}) per AddressBook
    contact RECORD across every source file — one group per Contacts card,
    grouped by the card's own ZOWNER (its ZABCDRECORD.Z_PK). This record
    grouping is the authoritative "these identifiers are the same person"
    signal, used by both the handle→name map and the identity clustering.
    Best-effort: an unreadable/schema-drifted source file is skipped, never
    fatal.

    `card_id` is ZUNIQUEID (`UUID:ABPerson`), the same string Contacts.app's
    AppleScript dictionary hands back as `id of person` — which is what lets
    app/contacts.py address a card for editing. Identity clustering ignores
    it: a card id is per-source, so two sources holding the same human still
    have to merge on shared identifiers, never on this.
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
    """normalized handle -> display name, from every AddressBook source
    file. Best-effort: Contacts may never have run, or FDA may cover
    chat.db but not AddressBook yet — an empty dict just means no
    contact_name, never a hard failure."""
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
    """typedstream is a real binary archive format (NSKeyedArchiver's
    predecessor); this isn't a general parser for it, just the one shape
    that matters here — after the `NSString` class-name marker, a `+` byte
    introduces an inline payload: one length byte, or 0x81 + a little-endian
    uint16 length for longer strings, then that many UTF-8 bytes. Validated
    against 300 real (text, attributedBody) pairs on this machine (message
    rows where both happen to be populated) — 300/300 exact matches, vs.
    ~3% for the naive 'decode everything, keep the longest printable run'
    version this replaced (it kept picking up typedstream framing bytes or
    unrelated NSString instances, e.g. rich-text attribute-run metadata)."""
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


# A photo or video sits in the text as U+FFFC OBJECT REPLACEMENT CHARACTER,
# the placeholder Apple leaves where the attachment renders. U+FFFD turns up
# next to it when the attributedBody decode hit a byte it couldn't map.
_PLACEHOLDERS = "￼�"

_ATTACHMENT_KINDS = [
    ("image/gif", "GIF"), ("image/", "photo"), ("video/", "video"),
    ("audio/", "audio"), ("com.apple.coreaudio-format", "audio"),
    ("text/vcard", "contact card"), ("text/x-vlocation", "location"),
    ("application/pdf", "PDF"), ("com.apple.pkpass", "pass"),
]


def _attachment_label(types: str | None, n: int) -> str:
    """What to show for a message whose only content is an attachment.

    Uncaptioned photos are most of these, and "(photo)" beats the raw
    placeholder — it's a real thing someone sent, so it has to stay in the
    thread rather than being dropped as empty. Mixed sets fall back to the
    generic word instead of claiming a type that's only true of one."""
    # _ATTACHMENT_KINDS is ordered most- to least-specific ("image/gif" ahead
    # of "image/"), so each type takes its first match, not every match.
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
    """The line to store: what they typed, or what they sent if they typed
    nothing. A caption wins — it says more than "(photo)" ever could.

    A placeholder with no attachment row behind it still gets named: media
    that expired or was deleted leaves the U+FFFC but drops the `attachment`
    entry, and "(attachment)" beats rendering the raw mojibake."""
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
    """chat.db messages matching `where`, in ROWID order, filtered to what
    `messages` keeps (see the comment in _sync)."""
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
    """chat.db writes 0, not NULL, for "never" — no receipt yet, or none coming
    (SMS, or receipts turned off on their end)."""
    return _ts_of(apple_ns).isoformat(timespec="seconds") if apple_ns else None


def _receipt(r) -> tuple[int, str | None, int, str | None]:
    """(is_read, read_at, is_delivered, delivered_at) off a chat.db row."""
    return (int(bool(r["is_read"])), _receipt_ts(r["date_read"]),
            int(bool(r["is_delivered"])), _receipt_ts(r["date_delivered"]))


def _sync_receipts(app_con, chat_con, since_ns: int) -> int:
    """Copy read/delivered state for chat.db messages dated since `since_ns`
    onto the cached rows. Only rows whose state actually changed are written,
    so a quiet cycle is a read of a week of chat.db and no writes. Returns
    the number of rows updated."""
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
    """The recent window every cycle; the whole cache once, for the rows that
    were synced before receipts were stored at all."""
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
    """Extend `messages` back to `days` ago, below the oldest row already
    cached — the first sync only reached MESSAGES_BACKFILL_DAYS. The Brain's
    first ingest reads a year (config.BRAIN_BACKFILL_DAYS).

    The ROWID watermark is left alone, and app/selftexts.py only routes texts
    newer than its own start mark — a year-old note-to-self is not a task today, and the live sync's place in
    chat.db hasn't moved. Idempotent — INSERT OR IGNORE on guid."""
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
    # `message` holds three things, only two of which belong here.
    #   item_type<>0 — group housekeeping (renames, joins, leaves). No text
    #     at all, so it lands as a blank message from whoever triggered it.
    #     Never wanted.
    #   associated_message_type 2000-2999 — tapbacks ("Loved/Liked … to
    #     <quote>"). Kept, flagged is_reaction: they're not messages, but a
    #     reaction you left is still you answering, so the waiting feed has
    #     to see them. Consumers counting volume filter them back out.
    #   3000-3999 (un-reacting) and 1000 (stickers) — dropped. Removing a
    #     ❤️ isn't a reply, and a sticker carries no text to show.
    rows = _chat_rows(chat_con, where, params)

    n = 0
    max_rowid = watermark or 0
    with app_con:
        for r in rows:
            max_rowid = max(max_rowid, r["ROWID"])
            n += _insert(app_con, r, contacts, now)[0]
        # Only persist the watermark if this run actually saw rows, or was
        # already past the first backfill (watermark not None). Otherwise an
        # empty first 90-day backfill would write watermark=0, and the NEXT
        # sync would take the `ROWID > 0` branch — syncing chat.db's entire
        # history instead of retrying the bounded backfill.
        if rows or watermark is not None:
            _set_watermark(app_con, max_rowid)
    return n


def _attachment_path(raw: str | None) -> str | None:
    """chat.db stores the path '~'-prefixed; absolute is what a reader needs.

    NULL/empty means the file isn't on this Mac — iCloud-offloaded, or never
    downloaded. The row is still worth keeping (transfer_name says what it
    was), it just has nothing to extract from."""
    if not raw:
        return None
    return str(Path(raw).expanduser())


def _sync_attachments(app_con, chat_con, now: str) -> None:
    """Record what each message carried — one row per file, not per message.

    Windows itself rather than reusing the message sync's watermark: this
    table arrives on a DB whose messages are already synced, so keying off
    the watermark would mean only ever seeing attachments sent from now on,
    with the whole existing history invisible. First run walks the same
    MESSAGES_BACKFILL_DAYS span the message sync used; later runs go
    incremental off the highest message ROWID already recorded. Opens its own
    transaction.

    Metadata only: nothing here opens a file. Extraction is a separate,
    on-demand step (providers/attachments.py) precisely because parsing
    untrusted bytes is the part that carries a cost and a risk."""
    row = app_con.execute("SELECT value FROM sync_state WHERE key=?",
                          (ATTACHMENT_SYNC_KEY,)).fetchone()
    if row is not None:
        # The watermark has to come from app.db, not from chat.db: a subquery
        # for "newest message that has an attachment" evaluates over the same
        # rows being selected, so it always includes the newest one and the
        # window comes back empty forever.
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

    # Advance past every message scanned, not just those carrying a file —
    # otherwise the window re-walks the same tail forever. MAX over the whole
    # message table is bounded by the same predicate, so a quiet week still
    # moves the mark.
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
    """Cache each chat's own metadata (custom group name + group flag) so the
    People conversations view can label a group by its real name instead of a
    participant list. Full get-and-replace every run — chat metadata is tiny
    (a few hundred rows) and mutable (groups get renamed, members join), so a
    watermark isn't worth it; it also self-heals if the table is dropped. This
    runs BEFORE the message-sync watermark early-return, since a group can be
    renamed with no new messages. chat.db `style`: 43 = group, 45 = 1:1."""
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
    """Cache contact identity clusters so a person's fragments (a CRM row
    keyed by email, texts under a phone) resolve to one human. Clusters come
    from AddressBook's record grouping via union-find over shared identifiers:
    all identifiers on one card merge, and cards that share an identifier
    (an iCloud + a local duplicate of the same person) merge too. A shared
    NAME never merges — the union key is always an identifier — so two
    different people named the same stay separate. Full-replace each run;
    empty AddressBook leaves the (droppable) cache untouched rather than
    wiping it, so a transient FDA/read failure doesn't un-merge everyone."""
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
            # canonical = an email if the person has one, else a phone —
            # matches import_birthdays' email-first keying so the cluster_key
            # usually equals the existing `people` row's person_key.
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
    """Is chat.db actually readable by *this* process? refresh() degrades to a
    quiet no-op when it isn't, which looks identical to a week of not texting
    — and that's the real-world failure here: launched from the .app bundle
    instead of the terminal, Full Disk Access doesn't carry over, so the sync
    silently writes nothing forever. Cheap (open + one row), so /api/status
    can probe it live rather than trusting the last refresh."""
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
    """Poller entrypoint: incremental by ROWID watermark (90-day backfill on
    first run). Quiet no-op if chat.db isn't readable yet (no Full Disk
    Access granted, or this machine has no Messages)."""
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
            # Above the no-new-messages early return on purpose: this table lands
            # on an already-synced DB and windows itself, so gating it on new
            # messages would leave the entire back catalogue unrecorded.
            _sync_attachments(app_con, chat_con, now)
            watermark = _watermark(app_con)
            n = 0
            if watermark is None or (chat_con.execute(
                    "SELECT MAX(ROWID) AS m FROM message").fetchone()["m"] or 0) > watermark:
                # Only when there is something new — this is what pays for
                # the AddressBook reload.
                n = _sync(app_con, chat_con, _load_contacts(), now)
            # After _sync, so a message that arrived and was read since the
            # last cycle has its row to land on. Not gated on new messages:
            # someone reading your text writes no new row anywhere.
            receipts = _refresh_receipts(app_con, chat_con)
    finally:
        chat_con.close()
    return {"new_messages": n, "receipts": receipts}


def cite(it: dict) -> str | None:
    """One message's citation ref, or None for the aggregate metrics.

    `sent` / `received` / `contacts` / `reply_latency` are day counts — there
    is no one message behind a number, and a pill offering to open one would
    be picking arbitrarily. `unanswered` rows are a person, not a message, so
    they get none either.
    """
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
        # Outbound only: whether they've seen yours is the question a receipt
        # answers. "delivered" alone on iMessage means receipts are off or it
        # is still unread; SMS carries neither, so it gets no mark.
        if it["is_from_me"]:
            if it.get("read_at"):
                line += f"  [read {it['read_at'][11:16]}]"
            elif it.get("is_delivered"):
                line += "  [delivered]"
        # Named, not counted: "(2 photos)" is what `text` already falls back
        # to, and the name is the part you can act on — you go looking for
        # the packet. The mark says whether its text is searchable yet.
        for a in it.get("attachments") or []:
            line += f"  📎 {a['filename'] or a['mime']}"
            line += "" if a["status"] == "ok" else " (unread)"
        return line
    return f"{it['day']} {it['value']}"  # sent/received/contacts


# Whether a row moves the "who owes whom" needle. Tapbacks are asymmetric:
# one you left answers the thread, one they left asks nothing of you — so
# an incoming ❤️ must not start the clock, and must not hide the message
# underneath it that's still genuinely unanswered.
COUNTS_AS_TRAFFIC = "NOT (is_reaction = 1 AND is_from_me = 0)"


def _is_correspondent(handle: str, name: str | None, ever_replied: bool) -> bool:
    """Is this sender a person you actually correspond with?

    "Unanswered" is a nudge to reply, so it's only useful for humans. Two
    tells, either of which is enough: they're in Contacts (so `name` resolved),
    or you have written back to them at least once. A nameless number you have
    never answered is a robot, a wrong number, or spam — the list was ~65
    entries and mostly shortcodes before this.

    Shortcodes are rejected outright: SMS shortcodes are 3–8 digits (banks,
    2FA, delivery alerts) and can never be dialed back, so even a stray reply
    to one doesn't make it a correspondent."""
    if handle.isdigit() and len(handle) < 10:
        return False
    return bool(name) or ever_replied


def _unanswered(con) -> tuple[list[dict], str | None]:
    """People who texted you and haven't heard back, newest-silence last.

    Deliberately range-independent: a thread gone quiet for months is exactly
    what this should surface, so the candidate set is every conversation's
    true last message, not just ones with a message inside some range.

    Two things this is careful about, both of which used to produce phantom
    "you've ignored X for 94 days" entries in the People widget:

    1. **Group chats don't count.** A 15-person chat going quiet is not a
       reply you owe, but the last person to speak in it got billed for it.
       (`chats.is_group`; an unknown chat_guid is assumed 1:1, so a dropped
       chats cache degrades to the old behaviour instead of showing nothing.)
    2. **One row per person, not per thread.** The same human has an iMessage
       thread and an SMS thread; replying in one answers both. Threads are
       collapsed by the counterpart's identity cluster, and the person is
       only "unanswered" if their newest message *anywhere* went unreplied.
    3. **Only actual correspondents.** A 2FA shortcode or a delivery robot is
       not owed a reply, and unknown numbers you never wrote back to made the
       list mostly junk. See `_is_correspondent`.

    Outgoing rows mostly carry no handle (chat.db leaves `handle_id` unset on
    messages you sent), so a chat's counterpart is read off its inbound rows
    and then applied to the whole thread."""
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
                # the message actually owed a reply — "6d · Wren Ellis" says
                # nothing about whether it needs two words or ten minutes
                "text": (last["text"] or "").strip()[:PREVIEW_CAP],
                # when you opened it — None is "never even read", a
                # different kind of owing than "read it and said nothing"
                "read_at": last["read_at"],
            })
    data.sort(key=lambda d: -d["hours"])
    return data, refreshed


def reply_latency_by_day(con, start_key: str, end_key: str) -> dict[str, float]:
    """Median minutes you took to answer, per day — `{day: minutes}`.

    Shared by `get('reply_latency')` and features.py's extractor on purpose:
    Spotify's novelty bug was a second copy of the math in features.py drifting
    from the provider's, so there is exactly one implementation here.

    Each inbound message opens a debt in its 1:1 chat; your next outbound
    message closes it, and the pair's gap is one latency sample. Only the
    *first* unanswered inbound counts — if someone sends five texts and you
    answer once, you took as long as the first one waited, not the fifth.
    Group chats are excluded for the same reason `_unanswered` excludes them:
    nobody owes a group a reply, so the gap isn't a decision you made.

    Samples land on the day of **your reply**, not of their message. That
    keeps a day's value final once the day ends (keying on their message
    would revise last Tuesday every time you finally answered something),
    and it reads the way the feature is meant to: "how quickly was I
    answering people on this day".

    A day with inbound messages you never answered contributes nothing
    rather than a zero or an infinity — silence is what `unanswered`
    measures, and folding it in here as a huge number would let one ignored
    text swamp a day of fast replies.

    When the oldest unanswered message is older than REPLY_MAX_GAP_MIN, the
    pair is retried against their *most recent* message before falling back
    to dropping it. Without that, a thread with an ancient unreplied text in
    it discards every later reply as "a new conversation" — on real history
    that was 61 of 111 dropped pairs, most of them genuinely fast answers to
    something said minutes earlier."""
    # Reach back a full gap-window before the range: a message that arrived
    # Sunday night and was answered Monday morning is a Monday sample, and
    # the inbound half of that pair lives outside the requested range.
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
    """Slice the `messages` cache for the range — chat.db already keeps
    full local history, so unlike Spotify there's no on-demand backfill:
    refresh() staying current (poller cadence) is enough."""
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
            # Files a message carried, named — `text` only falls back to
            # "(photo)" when nothing was typed, so a captioned file is
            # otherwise invisible in the log.
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
