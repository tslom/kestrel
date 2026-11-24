"""The email learner: a recommendation taken from the record, not the model.

Properties held: recommends what was actually done with a sender; quiet when mixed;
retired actions (delete, bin) read as archive without rewriting the record; undo
retracts instead of teaching; rebuild() reproduces the aggregates from email_triage
(so the table is droppable); the label prior follows the last two filings.
"""
from app import db
from app.providers import email_learn


def _email(con, msg_id, addr, received_at, *, in_inbox=1):
    con.execute(
        "INSERT INTO emails (msg_id, thread_id, day, received_at, from_name,"
        " from_addr, subject, snippet, in_inbox, unread, refreshed_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,0,'now')",
        (msg_id, "t" + msg_id, received_at[:10], received_at, "Sender",
         addr, "Subject", "snippet", in_inbox))


def _decided(con, msg_id, addr, action, at, label=None):
    _email(con, msg_id, addr, at, in_inbox=0)
    con.execute(
        "INSERT INTO email_triage (msg_id, user_action, user_action_at,"
        " user_label) VALUES (?,?,?,?)", (msg_id, action, at, label))


def test_two_matching_decisions_decide_a_sender(dbfile):
    con = db.connect()
    with con:
        for i, day in enumerate(("01", "02")):
            _decided(con, f"m{i}", "news@example.com", "unsubscribe",
                     f"2026-09-{day}T10:00:00")
            email_learn.record(con, "news@example.com", "unsubscribe",
                               f"2026-09-{day}T10:00:00")
    hint = email_learn.predict(con, "news@example.com")
    assert hint["action"] == "unsubscribe"
    assert hint["basis"] == "sender"
    # The reason is the point of the feature, not decoration: something
    # learned from two clicks has to say so before it is trusted.
    assert hint["reason"] == ("you unsubscribed from every one of the last "
                              "2 messages from this sender")
    con.close()


def test_a_mixed_sender_says_nothing(dbfile):
    """50% agreement is under the 0.75 floor: the learner declines."""
    con = db.connect()
    with con:
        for i, action in enumerate(("archive", "keep", "archive", "keep")):
            email_learn.record(con, "mixed@example.com", action,
                               f"2026-09-0{i + 1}T10:00:00")
    assert email_learn.predict(con, "mixed@example.com") is None
    con.close()


def test_retired_actions_read_as_archive(dbfile):
    """The learner reads retired delete/bin as archive; the record is not rewritten."""
    con = db.connect()
    with con:
        for i, action in enumerate(("delete", "archive", "bin", "delete")):
            email_learn.record(con, "promo@example.com", action,
                               f"2026-09-0{i + 1}T10:00:00")
    hint = email_learn.predict(con, "promo@example.com")
    assert hint["action"] == "archive"
    assert "every one of the last 4 messages" in hint["reason"]
    stored = {r["action"] for r in con.execute(
        "SELECT action FROM email_sender_stats WHERE key='promo@example.com'")}
    assert stored == {"delete", "archive", "bin"}
    con.close()


def test_undo_retracts_the_decision(dbfile):
    """Undo retracts a decision; it must not teach the opposite."""
    con = db.connect()
    with con:
        for i in range(2):
            _decided(con, f"u{i}", "boss@example.com", "keep",
                     f"2026-09-0{i + 1}T10:00:00")
            email_learn.record(con, "boss@example.com", "keep",
                               f"2026-09-0{i + 1}T10:00:00")
    assert email_learn.predict(con, "boss@example.com")["action"] == "keep"
    with con:
        email_learn.unrecord(con, "u1", "keep")
        email_learn.unrecord(con, "u0", "keep")
    # Both retracted: no prediction and no stale row.
    assert email_learn.predict(con, "boss@example.com") is None
    con.close()


def test_rebuild_reproduces_the_aggregates(dbfile):
    """The table is derived: a rebuild from email_triage lands in the same place."""
    con = db.connect()
    with con:
        _decided(con, "r0", "a@example.com", "keep", "2026-09-01T10:00:00")
        _decided(con, "r1", "a@example.com", "keep", "2026-09-02T10:00:00")
        _decided(con, "r2", "b@example.com", "delete", "2026-09-03T10:00:00")
        n = email_learn.rebuild(con)
    assert n == 3
    before = email_learn.predict(con, "a@example.com")
    assert before["action"] == "keep"

    with con:
        con.execute("DELETE FROM email_sender_stats")
        assert email_learn.rebuild(con) == 3
    assert email_learn.predict(con, "a@example.com") == before
    con.close()


def test_a_dropped_table_rebuilds_itself(dbfile):
    """A dropped table comes back from email_triage on the next read (else the learner looks merely untrained)."""
    con = db.connect()
    with con:
        _decided(con, "w0", "a@example.com", "unsubscribe", "2026-09-01T10:00:00")
        _decided(con, "w1", "a@example.com", "unsubscribe", "2026-09-02T10:00:00")
    # Empty aggregates with a non-empty record: the post-drop state.
    assert email_learn.ensure_warm(con) == 2
    assert email_learn.predict(con, "a@example.com")["action"] == "unsubscribe"
    # Warm: no repeat work per queue render.
    assert email_learn.ensure_warm(con) == 0
    con.close()


def test_warming_a_mailbox_with_no_decisions_does_nothing(dbfile):
    """A cold start isn't a dropped table (else every render re-scans an empty history)."""
    con = db.connect()
    assert email_learn.ensure_warm(con) == 0
    con.close()


def test_domain_speaks_only_when_the_address_is_unknown(dbfile):
    """A domain is weaker evidence: it needs three decisions and never overrides a known address."""
    con = db.connect()
    with con:
        # Domain at 0.8 unsubscribe passes the floor; the colleague's own address says keep.
        for i in range(8):
            email_learn.record(con, f"list{i}@corp.com", "unsubscribe",
                               f"2026-09-01T1{i}:00:00")
        for i in range(2):
            email_learn.record(con, "real.person@corp.com", "keep",
                               f"2026-09-0{i + 5}T10:00:00")

    unknown = email_learn.predict(con, "newsletter@corp.com")
    assert unknown["action"] == "unsubscribe" and unknown["basis"] == "domain"

    known = email_learn.predict(con, "real.person@corp.com")
    assert known["action"] == "keep" and known["basis"] == "sender"
    con.close()


def test_a_label_needs_the_last_two_filings_to_agree(dbfile):
    con = db.connect()
    with con:
        _decided(con, "l0", "lyft@lyftmail.com", "archive",
                 "2026-09-01T10:00:00", "Filed/Receipts")
    # One filing isn't a habit: the model's pick stands.
    assert email_learn.predict_label(con, "lyft@lyftmail.com") is None
    with con:
        _decided(con, "l1", "lyft@lyftmail.com", "archive",
                 "2026-09-02T10:00:00", "Filed/Receipts")
    got = email_learn.predict_label(con, "LYFT@lyftmail.com")
    assert got["label"] == "Filed/Receipts"
    assert got["reason"] == "your last 2 messages from this sender went here"
    con.close()


def test_two_corrections_move_a_sender(dbfile):
    """Two corrections move a sender (a majority would take three more)."""
    con = db.connect()
    with con:
        for i in range(5):
            _decided(con, f"c{i}", "career@usc.edu", "archive",
                     f"2026-09-0{i + 1}T10:00:00", "Filed/Newsletters")
        _decided(con, "c5", "career@usc.edu", "archive",
                 "2026-09-07T10:00:00", "Filed/Career")
    # One correction: the last two disagree, so no prior.
    assert email_learn.predict_label(con, "career@usc.edu") is None
    with con:
        _decided(con, "c6", "career@usc.edu", "archive",
                 "2026-09-08T10:00:00", "Filed/Career")
    assert email_learn.predict_label(con, "career@usc.edu")["label"] == "Filed/Career"
    con.close()
