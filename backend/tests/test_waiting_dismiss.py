"""Dismiss vs mute vs age-out: the three ways a row leaves the waiting feed.

Dismissal is scoped to a message, not a person: a newer message brings them back.
"""

from datetime import datetime, timedelta

import pytest

from app import db, people


def _ago(days):
    return (datetime.now().astimezone() - timedelta(days=days)).isoformat(timespec="seconds")


@pytest.fixture
def feed(dbfile, monkeypatch):
    """Stub the messages provider so only waiting_on()'s own filtering is exercised."""
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
    """Scoped to `through_ts`: dismissing an April thread must not swallow next week's message."""
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
    """Dismissing leaves the person visible elsewhere; muting is the permanent one."""
    _msg(feed, "jordan", 5)
    row = next(w for w in people.waiting_on())
    people.post_dismiss(people.Dismissal(
        kind=row["kind"], key=row["key"], through=row["last_at"]))
    assert people.get_hidden()["data"] == []
    assert len(people.get_dismissed()["data"]) == 1


def test_finished_conversations_age_out(feed):
    """Past WAITING_MAX_DAYS a row ages out with no dismissal (nothing to undo)."""
    _msg(feed, "jordan", people.WAITING_MAX_DAYS + 1)
    assert _keys() == []
    assert people.get_dismissed()["data"] == []

    _msg(feed, "jordan", people.WAITING_MAX_DAYS - 1)
    assert _keys() == ["jordan"]


def test_dismissed_rows_carry_a_name(feed, dbfile):
    """A phone-keyed dismissal row still resolves to something an undo can decide on."""
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
    """Shortcodes and 800-numbers have no card: the raw key is the answer."""
    _msg(feed, "57513", 5)
    row = next(w for w in people.waiting_on())
    people.post_dismiss(people.Dismissal(
        kind=row["kind"], key=row["key"], through=row["last_at"]))
    assert people.get_dismissed()["data"][0]["name"] == "57513"
