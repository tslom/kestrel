"""The `unanswered` metric — the People widget's "you owe them a reply" feed.

Every case here is a bug that shipped: the widget claimed a friend had been
ignored for 94 days because she was the last to speak in a 15-person group
chat, and the rest of the list was 2FA shortcodes.
"""

from datetime import datetime, timedelta

import pytest

from app import db
from app.providers import messages


def _seed(con, rows, chats=()):
    """rows: (chat_guid, handle, is_from_me, ts[, is_reaction]). handle is
    None on outgoing, matching chat.db, which leaves handle_id unset on
    messages you sent."""
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    with con:
        for guid, is_group in chats:
            con.execute("INSERT INTO chats (chat_guid, display_name, is_group,"
                        " refreshed_at) VALUES (?,NULL,?,?)", (guid, is_group, now))
        for i, row in enumerate(rows):
            guid, handle, mine, ts = row[:4]
            reaction = row[4] if len(row) > 4 else 0
            con.execute(
                "INSERT INTO messages (guid, day, ts, chat_guid, handle,"
                " contact_name, is_from_me, text, refreshed_at, is_reaction)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                (f"g{i}", db.day_key(datetime.fromisoformat(ts)), ts, guid,
                 handle, None, mine, "hi", now, reaction))


def _ago(days):
    return (datetime.now().astimezone() - timedelta(days=days)).isoformat(timespec="seconds")


def _name(con, identifier, name):
    with con:
        con.execute("INSERT INTO contact_identities (identifier, cluster_key,"
                    " name, refreshed_at) VALUES (?,?,?,?)",
                    (identifier, identifier, name, "2026-01-01T00:00:00"))


def test_group_chat_is_not_a_reply_you_owe(con):
    """The original bug: last speaker in a big group chat got billed for the
    whole group going quiet."""
    _name(con, "2025550166", "Iris")
    _seed(con, [("any;+;chatGROUP", "2025550166", 0, _ago(94))],
          chats=[("any;+;chatGROUP", 1)])
    data, _ = messages._unanswered(con)
    assert data == []


def test_reply_in_one_thread_answers_the_person(con):
    """Same human over iMessage and SMS. Answering either clears both — the
    per-chat_guid version left the dead thread accusing you forever."""
    _name(con, "2025551212", "Dad")
    _seed(con, [
        ("any;-;+12025551212", "2025551212", 0, _ago(90)),   # old thread, unreplied
        ("SMS;-;+12025551212", "2025551212", 0, _ago(3)),
        ("SMS;-;+12025551212", None, 1, _ago(2)),            # ...but you wrote back
    ], chats=[("any;-;+12025551212", 0), ("SMS;-;+12025551212", 0)])
    data, _ = messages._unanswered(con)
    assert data == []


def test_person_who_texted_last_surfaces_once(con):
    _name(con, "2025551212", "Dad")
    _seed(con, [
        ("any;-;+12025551212", "2025551212", 0, _ago(90)),
        ("any;-;+12025551212", None, 1, _ago(80)),
        ("SMS;-;+12025551212", "2025551212", 0, _ago(3)),    # newest: theirs
    ], chats=[("any;-;+12025551212", 0), ("SMS;-;+12025551212", 0)])
    data, _ = messages._unanswered(con)
    assert len(data) == 1
    assert data[0]["contact_name"] == "Dad"
    assert data[0]["person_key"] == "2025551212"
    assert data[0]["hours"] == pytest.approx(72, abs=1)


def test_shortcodes_and_strangers_are_filtered(con):
    """A bank's 2FA sender and a number you've never written to are not
    correspondents; a stranger you did answer once is."""
    _name(con, "2025559999", "Jade")
    _seed(con, [
        ("SMS;-;42302", "42302", 0, _ago(40)),          # shortcode
        ("SMS;-;+18885551234", "8885551234", 0, _ago(40)),  # unknown, never answered
        ("SMS;-;+12025559999", "2025559999", 0, _ago(40)),  # in Contacts
        ("SMS;-;+12025558888", "2025558888", 0, _ago(41)),  # answered once before
        ("SMS;-;+12025558888", None, 1, _ago(40)),
        ("SMS;-;+12025558888", "2025558888", 0, _ago(39)),
    ], chats=[(g, 0) for g in ("SMS;-;42302", "SMS;-;+18885551234",
                               "SMS;-;+12025559999", "SMS;-;+12025558888")])
    keys = {d["person_key"] for d in messages._unanswered(con)[0]}
    assert keys == {"2025559999", "2025558888"}


def test_recent_inbound_is_not_yet_unanswered(con):
    """Under UNANSWERED_HOURS you're just... busy."""
    _name(con, "2025551212", "Dad")
    _seed(con, [("SMS;-;+12025551212", "2025551212", 0, _ago(0.2))],
          chats=[("SMS;-;+12025551212", 0)])
    assert messages._unanswered(con)[0] == []


def test_unknown_chat_guid_is_treated_as_one_to_one(con):
    """The chats cache is droppable/derived — losing it must not blank the
    feed, so a chat with no cached row is assumed 1:1."""
    _name(con, "2025551212", "Dad")
    _seed(con, [("SMS;-;+12025551212", "2025551212", 0, _ago(5))])  # no chats row
    assert len(messages._unanswered(con)[0]) == 1


def test_a_reaction_you_left_counts_as_answering(dbfile):
    """Hitting ❤️ instead of typing is still a reply — the thread shouldn't
    keep nagging you about a message you visibly acknowledged."""
    con = db.connect()
    _seed(con, [
        ("c1", "2025551111", 0, _ago(9)),           # they wrote
        ("c1", None, 1, _ago(8), 1),                # you reacted to it
    ], chats=[("c1", 0)])
    _name(con, "2025551111", "Felix")
    assert [d["contact_name"] for d in messages._unanswered(con)[0]] == []


def test_a_reaction_they_left_does_not_answer_for_you(dbfile):
    """The Hazel bug: her ❤️ on your message was the newest row, so it both
    started the clock and hid the real message still owed a reply."""
    con = db.connect()
    _seed(con, [
        ("c1", "2025551111", 0, _ago(9)),           # they wrote — still owed
        ("c1", "2025551111", 0, _ago(8), 1),        # they reacted to you
    ], chats=[("c1", 0)])
    _name(con, "2025551111", "Hazel")
    got = messages._unanswered(con)[0]
    assert [d["contact_name"] for d in got] == ["Hazel"]
    # dated from the real message, not the tapback two days later
    assert round(got[0]["hours"] / 24) == 9


def test_their_reaction_alone_is_not_something_to_answer(dbfile):
    """You spoke last and they only reacted. Nothing is owed."""
    con = db.connect()
    _seed(con, [
        ("c1", None, 1, _ago(9)),                   # you wrote
        ("c1", "2025551111", 0, _ago(8), 1),        # they just reacted
    ], chats=[("c1", 0)])
    _name(con, "2025551111", "Hazel")
    assert messages._unanswered(con)[0] == []


def test_the_owed_message_comes_with_it(dbfile):
    """A name and an age don't say whether this needs two words or ten
    minutes, so the feed carries what they actually said."""
    con = db.connect()
    _seed(con, [("c1", "2025551111", 0, _ago(5))], chats=[("c1", 0)])
    with con:
        con.execute("UPDATE messages SET text=? WHERE chat_guid='c1'",
                    ("you free saturday?",))
    _name(con, "2025551111", "Wren")
    assert messages._unanswered(con)[0][0]["text"] == "you free saturday?"


def test_the_preview_is_capped(dbfile):
    con = db.connect()
    _seed(con, [("c1", "2025551111", 0, _ago(5))], chats=[("c1", 0)])
    with con:
        con.execute("UPDATE messages SET text=? WHERE chat_guid='c1'",
                    ("word " * 200,))
    _name(con, "2025551111", "Wren")
    assert len(messages._unanswered(con)[0][0]["text"]) == messages.PREVIEW_CAP
