"""What counts as a message when reading chat.db: tapbacks and group housekeeping live in `message` too.

Design: DESIGN.md → §9 Messages, "Tapbacks are kept, not dropped".
"""

import sqlite3
from datetime import datetime, timedelta

import pytest

from app import db
from app.providers import messages

APPLE_EPOCH = 978307200


def _apple(dt):
    return int((dt.timestamp() - APPLE_EPOCH) * 1e9)


@pytest.fixture
def chat_con():
    """The columns _sync() reads, in a chat.db-shaped in-memory database."""
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.executescript("""
        CREATE TABLE message (
          ROWID INTEGER PRIMARY KEY, guid TEXT, date INTEGER, text TEXT,
          attributedBody BLOB, is_from_me INTEGER, handle_id INTEGER,
          associated_message_type INTEGER DEFAULT 0, item_type INTEGER DEFAULT 0,
          is_read INTEGER DEFAULT 0, date_read INTEGER DEFAULT 0,
          is_delivered INTEGER DEFAULT 0, date_delivered INTEGER DEFAULT 0);
        CREATE TABLE handle (ROWID INTEGER PRIMARY KEY, id TEXT);
        CREATE TABLE chat (ROWID INTEGER PRIMARY KEY, guid TEXT);
        CREATE TABLE chat_message_join (chat_id INTEGER, message_id INTEGER);
        CREATE TABLE attachment (ROWID INTEGER PRIMARY KEY, mime_type TEXT, uti TEXT);
        CREATE TABLE message_attachment_join (message_id INTEGER, attachment_id INTEGER);
        INSERT INTO handle VALUES (1, '+12025551234');
        INSERT INTO chat VALUES (1, 'any;-;+12025551234');
    """)
    return con


def _attach(con, rowid, *mimes):
    for mime in mimes:
        cur = con.execute("INSERT INTO attachment (mime_type, uti) VALUES (?,?)",
                          (mime if "/" in mime else None, mime))
        con.execute("INSERT INTO message_attachment_join VALUES (?,?)",
                    (rowid, cur.lastrowid))


def _add(con, rowid, text, *, assoc=0, item=0, mins_ago=10):
    ts = datetime.now() - timedelta(minutes=mins_ago)
    con.execute("INSERT INTO message (ROWID, guid, date, text, is_from_me,"
                " handle_id, associated_message_type, item_type)"
                " VALUES (?,?,?,?,0,1,?,?)",
                (rowid, f"guid-{rowid}", _apple(ts), text, assoc, item))
    con.execute("INSERT INTO chat_message_join VALUES (1, ?)", (rowid,))


def _stored(app_con):
    return [r["text"] for r in app_con.execute("SELECT text FROM messages ORDER BY guid")]


def _run(app_con, chat_con):
    now = datetime.now().isoformat(timespec="seconds")
    return messages._sync(app_con, chat_con, {}, now)


def test_a_real_message_is_stored(dbfile, chat_con):
    _add(chat_con, 1, "hey are we still on for friday")
    app_con = db.connect()
    assert _run(app_con, chat_con) == 1
    assert _stored(app_con) == ["hey are we still on for friday"]


@pytest.mark.parametrize("assoc", [3000, 3001, 3005, 1000])
def test_unreacting_and_stickers_are_dropped(dbfile, chat_con, assoc):
    """Taking a ❤️ back isn't a reply, and a sticker has no text to show."""
    _add(chat_con, 1, 'Removed a heart from "see you then"', assoc=assoc)
    app_con = db.connect()
    assert _run(app_con, chat_con) == 0
    assert _stored(app_con) == []


@pytest.mark.parametrize("assoc", [2000, 2001, 2004, 2006, 2007])
def test_tapbacks_are_kept_but_flagged(dbfile, chat_con, assoc):
    """Tapbacks are stored (the waiting feed must tell yours from theirs) but flagged."""
    _add(chat_con, 1, 'Loved "Yes we will lock each other in"', assoc=assoc)
    app_con = db.connect()
    assert _run(app_con, chat_con) == 1
    row = app_con.execute("SELECT text, is_reaction FROM messages").fetchone()
    assert row["is_reaction"] == 1
    assert row["text"] == 'Loved "Yes we will lock each other in"'


@pytest.mark.parametrize("item", [1, 2, 3, 4])
def test_group_housekeeping_is_not_a_message(dbfile, chat_con, item):
    """Renames and joins/leaves land as a blank message from whoever triggered them."""
    _add(chat_con, 1, None, item=item)
    app_con = db.connect()
    assert _run(app_con, chat_con) == 0
    assert _stored(app_con) == []


@pytest.mark.parametrize("mime,want", [
    ("image/heic", "(photo)"), ("image/jpeg", "(photo)"),
    ("image/gif", "(GIF)"), ("video/quicktime", "(video)"),
    ("audio/x-m4a", "(audio)"), ("com.apple.coreaudio-format", "(audio)"),
    ("text/vcard", "(contact card)"), ("application/pdf", "(PDF)"),
    ("dyn.age81a5dzq7y066dbtf0g82peqf4hk2pdrb00n5xy", "(attachment)"),
])
def test_an_uncaptioned_attachment_is_named(dbfile, chat_con, mime, want):
    """U+FFFC (attachment placeholder) is a real send, not mojibake."""
    _add(chat_con, 1, "￼")
    _attach(chat_con, 1, mime)
    app_con = db.connect()
    assert _run(app_con, chat_con) == 1
    assert _stored(app_con) == [want]


def test_a_lossy_decode_placeholder_is_stripped_too(dbfile, chat_con):
    """U+FFFD beside U+FFFC (unmappable attributedBody byte) is still an attachment."""
    _add(chat_con, 1, "�￼")
    _attach(chat_con, 1, "image/heic")
    app_con = db.connect()
    _run(app_con, chat_con)
    assert _stored(app_con) == ["(photo)"]


def test_several_attachments_are_counted(dbfile, chat_con):
    _add(chat_con, 1, "￼")
    _attach(chat_con, 1, "image/heic", "image/jpeg", "image/png")
    app_con = db.connect()
    _run(app_con, chat_con)
    assert _stored(app_con) == ["(3 photos)"]


def test_a_mixed_set_does_not_claim_one_type(dbfile, chat_con):
    _add(chat_con, 1, "￼")
    _attach(chat_con, 1, "image/heic", "video/quicktime")
    app_con = db.connect()
    _run(app_con, chat_con)
    assert _stored(app_con) == ["(2 attachments)"]


def test_a_caption_wins_over_the_label(dbfile, chat_con):
    """The point of the label is to fill a gap, not to overwrite words."""
    _add(chat_con, 1, "￼look at this")
    _attach(chat_con, 1, "image/heic")
    app_con = db.connect()
    _run(app_con, chat_con)
    assert _stored(app_con) == ["look at this"]


def test_expired_media_is_still_named(dbfile, chat_con):
    """Deleted media leaves U+FFFC but no `attachment` row: still something sent, not blank."""
    _add(chat_con, 1, "￼")
    app_con = db.connect()
    _run(app_con, chat_con)
    assert _stored(app_con) == ["(attachment)"]


def test_a_message_with_no_content_at_all_stays_empty(dbfile, chat_con):
    """No placeholder, no attachment — nothing to name, so don't invent."""
    _add(chat_con, 1, None)
    app_con = db.connect()
    _run(app_con, chat_con)
    assert _stored(app_con) == [""]


def test_a_tapback_does_not_shadow_the_message_before_it(dbfile, chat_con):
    """Both tapback and message rows are stored; the flag keeps the real message visible downstream."""
    _add(chat_con, 1, "ill put u on to hella ABGs", mins_ago=90)
    _add(chat_con, 2, 'Loved "see you then"', assoc=2000, mins_ago=30)
    app_con = db.connect()
    assert _run(app_con, chat_con) == 2
    assert [(r["text"], r["is_reaction"]) for r in app_con.execute(
        "SELECT text, is_reaction FROM messages ORDER BY ts")] == [
        ("ill put u on to hella ABGs", 0), ('Loved "see you then"', 1)]


def _read(con, rowid, *, mins_ago):
    """chat.db stamps a read receipt on the existing row in place (ROWID never moves)."""
    con.execute("UPDATE message SET is_read=1, date_read=?, is_delivered=1,"
                " date_delivered=? WHERE ROWID=?",
                (_apple(datetime.now() - timedelta(minutes=mins_ago)),
                 _apple(datetime.now() - timedelta(minutes=mins_ago + 1)), rowid))


def _receipts(app_con):
    return [(r["guid"], r["is_read"], r["read_at"] is not None,
             r["is_delivered"], r["delivered_at"] is not None)
            for r in app_con.execute(
                "SELECT guid, is_read, read_at, is_delivered, delivered_at"
                " FROM messages ORDER BY guid")]


def test_a_receipt_already_there_is_stored_on_insert(dbfile, chat_con):
    _add(chat_con, 1, "on my way")
    _read(chat_con, 1, mins_ago=5)
    app_con = db.connect()
    _run(app_con, chat_con)
    assert _receipts(app_con) == [("guid-1", 1, True, 1, True)]


def test_a_receipt_that_arrives_later_reaches_the_cached_row(dbfile, chat_con):
    """A row synced unread then read must flip (the ROWID watermark never revisits it)."""
    _add(chat_con, 1, "on my way")
    app_con = db.connect()
    _run(app_con, chat_con)
    assert _receipts(app_con) == [("guid-1", 0, False, 0, False)]

    _read(chat_con, 1, mins_ago=1)
    assert messages._refresh_receipts(app_con, chat_con) == 1
    row = app_con.execute("SELECT read_at FROM messages").fetchone()
    assert _receipts(app_con) == [("guid-1", 1, True, 1, True)]
    assert datetime.fromisoformat(row["read_at"]) > datetime.now().astimezone() - timedelta(minutes=2)
    # Nothing changed since: a quiet cycle writes nothing.
    assert messages._refresh_receipts(app_con, chat_con) == 0


def test_first_receipt_pass_backfills_rows_older_than_the_window(dbfile, chat_con):
    """Rows cached before receipts existed are backfilled once; after that only the recent window."""
    old = messages.RECEIPT_WINDOW_DAYS * 24 * 60 + 600
    _add(chat_con, 1, "from last month", mins_ago=old)
    app_con = db.connect()
    _run(app_con, chat_con)
    _read(chat_con, 1, mins_ago=old - 5)
    assert messages._refresh_receipts(app_con, chat_con) == 1   # the backfill

    chat_con.execute("UPDATE message SET is_read=0, date_read=0 WHERE ROWID=1")
    assert messages._refresh_receipts(app_con, chat_con) == 0   # outside the window now


def test_brief_line_marks_whether_yours_was_read():
    base = {"ts": "2026-10-23T14:00:00-07:00", "contact_name": "Sam",
            "handle": "x", "text": "on my way", "is_from_me": 1}
    assert messages.brief_line({**base, "read_at": "2026-10-23T14:05:00-07:00",
                                "is_delivered": 1}).endswith("[read 14:05]")
    assert messages.brief_line({**base, "read_at": None,
                                "is_delivered": 1}).endswith("[delivered]")
    assert "[" not in messages.brief_line({**base, "is_from_me": 0,
                                           "read_at": "2026-10-23T14:05:00-07:00"})
