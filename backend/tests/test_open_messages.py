"""Which identifier gets handed to Messages.app.

A person is a cluster of identifiers (Contacts card + whatever they text
from), so picking the wrong one opens an empty thread next to the real one.
"""

import pytest

from app import macos, people


def test_phone_is_reexpanded_to_e164():
    """`messages` stores the last 10 digits; Messages won't resolve a bare
    10-digit string, so it has to go back to +1."""
    assert people._imessage_handle(["2025550166"], []) == "+12025550166"


def test_a_texted_number_beats_one_only_on_the_contact_card():
    ids = ["2025551111", "2025552222"]
    msgs = [{"handle": "2025552222"}]
    assert people._imessage_handle(ids, msgs) == "+12025552222"


def test_phone_preferred_over_email():
    """1:1 iMessage threads are keyed by phone far more often than Apple ID."""
    ids = ["someone@example.com", "2025551111"]
    assert people._imessage_handle(ids, []) == "+12025551111"


def test_email_only_person_falls_back_to_the_address():
    assert people._imessage_handle(["someone@example.com"], []) == "someone@example.com"


def test_nothing_addressable_is_none():
    """Drives hiding the button rather than offering one that 404s."""
    assert people._imessage_handle([], []) is None
    assert people._imessage_handle(["not-a-handle"], []) is None


def test_open_messages_404s_when_unaddressable(dbfile, monkeypatch):
    monkeypatch.setattr(people.sys, "platform", "darwin")
    called = []
    monkeypatch.setattr(macos.subprocess, "run", lambda *a, **k: called.append(a))
    with pytest.raises(people.HTTPException) as e:
        people.post_open_messages("not-a-handle")
    assert e.value.status_code == 404
    assert called == []  # never shells out without a handle


def test_open_messages_launches_the_right_url(dbfile, monkeypatch):
    monkeypatch.setattr(people.sys, "platform", "darwin")
    monkeypatch.setattr(people, "_person_identity",
                        lambda con, key: (["2025550166"], ["2025550166"]))
    monkeypatch.setattr(people, "_messages_for", lambda con, handles: [])
    calls = []
    monkeypatch.setattr(macos.subprocess, "run",
                        lambda argv, **k: calls.append(argv))
    out = people.post_open_messages("2025550166")
    assert out == {"ok": True, "handle": "+12025550166"}
    # argv list, never a shell string — this reaches an OS launcher.
    assert calls == [["open", "imessage://+12025550166"]]
