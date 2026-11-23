"""Dismiss vs mute vs age-out — the three ways a row leaves the waiting feed.

The interesting property is that dismissal is scoped to a *message*, not a
person: writing off a dangling thread must not silence someone permanently,
so a newer message brings them back with no action from the user.
"""

from datetime import datetime, timedelta

import pytest

from app import db, people


def _ago(days):
    return (datetime.now().astimezone() - timedelta(days=days)).isoformat(timespec="seconds")


@pytest.fixture
def feed(dbfile, monkeypatch):
    """Stub the messages provider so these tests exercise waiting_on()'s own
    filtering, not chat.db parsing (covered in test_messages_unanswered.py)."""
    rows = []

    def fake_get(source, rng, metric=None):
        return {"metric": metric, "data": list(rows), "refreshed_at": None}

    monkeypatch.setattr(people.providers, "get", fake_get)
    return rows


def _msg(rows, key, days):
    rows.clear()
    rows.append({"chat_guid": f"g-{key}", "handle": key, "person_key": key,
                 "contact_name": key.title(), "last_ts": _ago(days),
                 "hours": round(days * 24, 1)})
    return rows[0]


def _keys(kind="message"):
    return [w["key"] for w in people.waiting_on() if w["kind"] == kind]


def test_dismiss_removes_the_thread(feed):
    _msg(feed, "jordan", 5)
    assert _keys() == ["jordan"]
    row = next(w for w in people.waiting_on())
    people.post_dismiss(people.Dismissal(
        kind=row["kind"], key=row["key"], through=row["last_at"]))
    assert _keys() == []


def test_a_newer_message_undismisses_on_its_own(feed):
    """The whole point of scoping to `through_ts`: dismissing "chillin" from
    April must not swallow whatever they send next week."""
    _msg(feed, "jordan", 5)
    row = next(w for w in people.waiting_on())
    people.post_dismiss(people.Dismissal(
        kind=row["kind"], key=row["key"], through=row["last_at"]))
    assert _keys() == []

    _msg(feed, "jordan", 1)          # they wrote again
    assert _keys() == ["jordan"]


def test_undismiss_brings_it_back(feed):
    _msg(feed, "jordan", 5)
    row = next(w for w in people.waiting_on())
    people.post_dismiss(people.Dismissal(
        kind=row["kind"], key=row["key"], through=row["last_at"]))
    assert _keys() == []
    people.delete_dismiss(row["kind"], row["key"])
    assert _keys() == ["jordan"]


def test_dismissal_is_not_a_mute(feed):
    """Dismissing leaves the person visible everywhere else — muting is the
    permanent one, and they must not be confused."""
    _msg(feed, "jordan", 5)
    row = next(w for w in people.waiting_on())
    people.post_dismiss(people.Dismissal(
        kind=row["kind"], key=row["key"], through=row["last_at"]))
    assert people.get_hidden()["data"] == []
    assert len(people.get_dismissed()["data"]) == 1


def test_finished_conversations_age_out(feed):
    """Past WAITING_MAX_DAYS it's not a todo. No dismissal needed, and
    nothing to undo."""
    _msg(feed, "jordan", people.WAITING_MAX_DAYS + 1)
    assert _keys() == []
    assert people.get_dismissed()["data"] == []

    _msg(feed, "jordan", people.WAITING_MAX_DAYS - 1)
    assert _keys() == ["jordan"]


def test_dismissed_rows_carry_a_name(feed, dbfile):
    """The table stores only the key it was dismissed by, so a phone-keyed
    row read as a bare number — nothing to decide an undo on."""
    _msg(feed, "2025551111", 5)
    row = next(w for w in people.waiting_on())
    people.post_dismiss(people.Dismissal(
        kind=row["kind"], key=row["key"], through=row["last_at"]))
    con = db.connect()
    with con:
        con.execute("INSERT INTO contact_identities (identifier, cluster_key,"
                    " name, refreshed_at) VALUES (?,?,?,?)",
                    ("2025551111", "2025551111", "Wren Ellis", "2026-07-24"))
    assert people.get_dismissed()["data"][0]["name"] == "Wren Ellis"


def test_an_unnamed_key_still_shows_something(feed, dbfile):
    """Shortcodes and 800-numbers have no card by definition, so the raw key
    is the honest answer rather than a gap."""
    _msg(feed, "57513", 5)
    row = next(w for w in people.waiting_on())
    people.post_dismiss(people.Dismissal(
        kind=row["kind"], key=row["key"], through=row["last_at"]))
    assert people.get_dismissed()["data"][0]["name"] == "57513"
