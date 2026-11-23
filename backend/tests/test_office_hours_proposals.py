"""§22 — office-hours amendments read out of mail, waiting on approval.

A Piazza post saying "no OH Thursday" is the one piece of course data that
arrives by mail and goes stale in the vault. Applying it automatically was the
obvious build and is the wrong one: a misparse sends you across campus to a
locked door, and you find out by standing outside it. So the extractor
proposes and you approve, and the assertions worth having are about what the
proposal refuses to become — a row you can't check, or a write the vault
didn't agree to.
"""

import json

import pytest

from app import courses, db
from app.providers import email_extract


@pytest.fixture
def vault_courses(monkeypatch):
    """Two known course codes, without needing a vault on disk."""
    monkeypatch.setattr(courses, "known_codes",
                        lambda term=None: ["CSCI 103", "MATH 229"])


@pytest.fixture
def applied(monkeypatch):
    """Capture set_exception instead of writing to a real course note."""
    calls = []

    def fake(code, who, day, **kw):
        calls.append({"code": code, "who": who, "day": day, **kw})
        return f"{code} — {day} amended"
    monkeypatch.setattr(courses, "set_exception", fake)
    return calls


def _mail(msg_id="m1", subject="OH cancelled", from_addr="prof@usc.edu",
          day=None):
    day = day or db.day_key()
    with db.session() as con, con:
        con.execute(
            "INSERT INTO emails (msg_id, day, received_at, from_addr, subject,"
            " refreshed_at) VALUES (?,?,?,?,?,?)",
            (msg_id, day, f"{day}T09:00:00", from_addr, subject, f"{day}T09:00:00"))
    return msg_id


def _item(**over):
    base = {"id": "m1", "relevant": True, "course": "CSCI 103", "who": "Prof Kim",
            "day": db.day_key(), "action": "cancel", "when": None,
            "where": None, "note": "midterm week",
            "quote": "There will be no office hours that day."}
    base.update(over)
    return base


def _upsert(item, msg_id="m1"):
    with db.session() as con, con:
        email_extract._upsert_oh_proposal(con, item, msg_id, "2026-09-13T10:00:00")


def _rows():
    with db.session() as con:
        return [dict(r) for r in con.execute("SELECT * FROM oh_proposals")]


# ---------- what a proposal refuses to become ----------

def test_a_clean_cancellation_becomes_a_pending_proposal(dbfile, vault_courses):
    _mail()
    _upsert(_item())
    row = _rows()[0]

    assert row["status"] == "pending"
    assert row["course"] == "CSCI 103"
    assert row["action"] == "cancel"
    assert row["quote"] == "There will be no office hours that day."
    assert row["subject"] == "OH cancelled", "headers come back off the email row"


def test_no_quote_is_no_proposal(dbfile, vault_courses):
    """The quote is the only field a human can check at a glance. Without it
    the approval step is a coin flip with extra steps."""
    _mail()
    _upsert(_item(quote=""))
    assert _rows() == []


def test_an_unknown_course_is_dropped(dbfile, vault_courses):
    """A guessed code ("CS 103") points at a note _find can't open, so the
    proposal would be unapprovable — drop it rather than surface it."""
    _mail()
    _upsert(_item(course="CS 103"))
    assert _rows() == []


def test_an_unparseable_date_is_dropped(dbfile, vault_courses):
    _mail()
    _upsert(_item(day="next Thursday"))
    assert _rows() == []


def test_a_change_with_no_new_time_is_dropped(dbfile, vault_courses):
    """"Office hours are moving" with no time says nothing that can be
    applied; a cancel needs no time, a change does."""
    _mail()
    _upsert(_item(action="change", when=None))
    assert _rows() == []


def test_a_change_with_a_time_is_kept(dbfile, vault_courses):
    _mail()
    _upsert(_item(action="change", when="14:00-15:00"))
    row = _rows()[0]
    assert row["action"] == "change" and row["when_str"] == "14:00-15:00"


def test_an_unknown_action_is_dropped(dbfile, vault_courses):
    _mail()
    _upsert(_item(action="reschedule-ish"))
    assert _rows() == []


def test_the_same_amendment_twice_stays_one_row(dbfile, vault_courses):
    """One message re-extracted must not stack duplicate cards."""
    _mail()
    _upsert(_item())
    _upsert(_item())
    assert len(_rows()) == 1


def test_one_message_can_propose_two_dates(dbfile, vault_courses):
    """The UNIQUE key is (msg_id, course, day, action) — "Thursday and Friday
    are off" is two amendments from one mail, not a conflict."""
    _mail()
    _upsert(_item(day="2026-09-20"))
    _upsert(_item(day="2026-09-21"))
    assert len(_rows()) == 2


# ---------- the pending list ----------

def test_past_dates_drop_off_the_pending_list(dbfile, vault_courses):
    """Approving "cancel last Tuesday" writes an override for a day that has
    already happened — noise in the vault, and it can't change a decision you
    already made by walking over there."""
    _mail()
    _upsert(_item(day="2020-01-06"))
    assert email_extract.pending_oh_proposals() == []


def test_decided_proposals_leave_the_pending_list(dbfile, vault_courses, applied):
    _mail()
    _upsert(_item(day="2099-05-05"))
    pid = _rows()[0]["id"]
    assert len(email_extract.pending_oh_proposals()) == 1

    email_extract.decide_oh_proposal(pid, approve=False)
    assert email_extract.pending_oh_proposals() == []


def test_a_borrowed_connection_is_left_open(dbfile, vault_courses):
    """The caller's connection is theirs: reading through it must not close it
    (nor commit its open transaction)."""
    _mail()
    with db.session() as con:
        con.execute("BEGIN")
        email_extract._upsert_oh_proposal(con, _item(day="2099-05-05"), "m1",
                                          "2026-09-13T10:00:00")
        assert len(email_extract.pending_oh_proposals(con)) == 1
        assert con.in_transaction
        con.execute("SELECT 1")     # still open
        con.rollback()
    assert email_extract.pending_oh_proposals() == []


# ---------- deciding ----------

def test_approving_writes_the_override_to_the_vault(dbfile, vault_courses, applied):
    _mail()
    _upsert(_item(day="2099-05-05"))
    pid = _rows()[0]["id"]

    out = email_extract.decide_oh_proposal(pid, approve=True)

    assert out["status"] == "approved"
    assert len(applied) == 1
    assert applied[0]["code"] == "CSCI 103"
    assert applied[0]["day"] == "2099-05-05"
    assert applied[0]["cancelled"] is True


def test_approving_a_room_change_passes_the_room(dbfile, vault_courses, applied):
    _mail()
    _upsert(_item(day="2099-05-05", action="change", when="13:00-15:00",
                  where="Lower Level 2 (B2)", note="moved for building event"))
    email_extract.decide_oh_proposal(_rows()[0]["id"], approve=True)
    assert applied[0]["where"] == "Lower Level 2 (B2)" and applied[0]["cancelled"] is False


def test_rejecting_touches_nothing(dbfile, vault_courses, applied):
    _mail()
    _upsert(_item(day="2099-05-05"))
    pid = _rows()[0]["id"]

    out = email_extract.decide_oh_proposal(pid, approve=False)

    assert out["status"] == "rejected"
    assert applied == [], "a rejection must never reach the vault"


def test_a_rejection_is_kept_not_deleted(dbfile, vault_courses, applied):
    """Or the next extraction proposes it again."""
    _mail()
    _upsert(_item(day="2099-05-05"))
    email_extract.decide_oh_proposal(_rows()[0]["id"], approve=False)
    assert _rows()[0]["status"] == "rejected"


def test_undoing_an_approval_takes_the_override_back_out(dbfile, vault_courses, applied):
    _mail()
    _upsert(_item(day="2099-05-05"))
    pid = _rows()[0]["id"]
    email_extract.decide_oh_proposal(pid, approve=True)

    out = email_extract.undecide_oh_proposal(pid)

    assert out["status"] == "pending" and _rows()[0]["status"] == "pending"
    assert applied[-1] == {"code": "CSCI 103", "who": "Prof Kim", "day": "2099-05-05",
                           "remove": True}
    assert [p["id"] for p in email_extract.pending_oh_proposals()] == [pid]


def test_undoing_a_rejection_touches_no_note(dbfile, vault_courses, applied):
    _mail()
    _upsert(_item(day="2099-05-05"))
    pid = _rows()[0]["id"]
    email_extract.decide_oh_proposal(pid, approve=False)
    email_extract.undecide_oh_proposal(pid)
    assert applied == [] and _rows()[0]["status"] == "pending"


def test_undoing_a_pending_proposal_is_a_no_op(dbfile, vault_courses, applied):
    _mail()
    _upsert(_item(day="2099-05-05"))
    assert email_extract.undecide_oh_proposal(_rows()[0]["id"])["status"] == "pending"
    assert applied == []


def test_deciding_twice_is_idempotent(dbfile, vault_courses, applied):
    """Two clicks on the same card must not write the override twice."""
    _mail()
    _upsert(_item(day="2099-05-05"))
    pid = _rows()[0]["id"]

    email_extract.decide_oh_proposal(pid, approve=True)
    email_extract.decide_oh_proposal(pid, approve=True)

    assert len(applied) == 1


def test_a_failed_apply_leaves_it_pending(dbfile, vault_courses, monkeypatch):
    """The note moved, or the entry can't be matched. A proposal that failed
    to apply is still waiting — marking it approved would hide that the vault
    is unchanged."""
    def boom(*a, **kw):
        raise courses.CourseError("no office_hours entry for that TA")
    monkeypatch.setattr(courses, "set_exception", boom)

    _mail()
    _upsert(_item(day="2099-05-05"))
    pid = _rows()[0]["id"]

    with pytest.raises(courses.CourseError):
        email_extract.decide_oh_proposal(pid, approve=True)
    assert _rows()[0]["status"] == "pending"


def test_deciding_a_missing_proposal_raises_keyerror(dbfile):
    """The route turns this into a 404."""
    with pytest.raises(KeyError):
        email_extract.decide_oh_proposal(4321, approve=True)


# ---------- candidates ----------

def test_sent_mail_is_never_a_candidate(dbfile):
    """Your own "are office hours still on?" is not an amendment."""
    with db.session() as con, con:
        con.execute(
            "INSERT INTO emails (msg_id, day, received_at, from_addr, subject,"
            " refreshed_at, is_sent) VALUES ('s1','2026-09-13',"
            "'2026-09-13T09:00:00','me@x.test','office hours?','x',1)")
    with db.session() as con:
        assert email_extract._office_hours_candidates(con) == []


def test_an_already_extracted_message_is_not_re_asked(dbfile):
    _mail(msg_id="m9", subject="office hours moved")
    with db.session() as con, con:
        con.execute(
            "INSERT INTO email_extract (msg_id, kind, payload, extracted_at)"
            " VALUES ('m9','office_hours',?,'2026-09-13T10:00:00')",
            (json.dumps({"relevant": False}),))
    with db.session() as con:
        assert [c["msg_id"] for c in email_extract._office_hours_candidates(con)] == []


def test_a_candidate_carries_its_send_date(dbfile):
    """"this Thursday" is unresolvable without it."""
    _mail(msg_id="m3", subject="OH cancelled this week")
    with db.session() as con:
        cands = email_extract._office_hours_candidates(con)
    assert cands and cands[0]["received_at"]


def test_the_quoted_weekday_corrects_an_off_by_one_date(dbfile, vault_courses):
    """Sent Fri 2026-09-25, "Thursday next week" came back as Fri 10-02."""
    _mail()
    _upsert(_item(day="2026-10-02", action="change", when="08:00-10:00",
                  quote="I will be doing my Thursday 8-10AM Math Center hours "
                        "next week only on Zoom!"))
    assert _rows()[0]["day"] == "2026-10-01"


def test_a_quote_naming_no_or_several_weekdays_keeps_the_date(dbfile, vault_courses):
    _mail()
    _upsert(_item(day="2026-10-02", quote="No office hours tomorrow."))
    _upsert(_item(day="2026-10-06", quote="Moved from Monday to Tuesday."))
    assert sorted(r["day"] for r in _rows()) == ["2026-10-02", "2026-10-06"]


# ---------- a new weekly block (action='add') ----------
#
# The 10-02 CSCI 103 digest: a TA signing "Devon" announced standing Thursday
# hours "starting next week", and the extractor said relevant:false because
# the prompt then ruled every standing schedule out. These run against a real
# note on disk, because approval goes through courses.set_hours for real.

DEVON_QUOTE = ("Starting next week, I will be holding office hours from 12-2 pm "
              "on Thursdays in GCS, Lower Level 2.")
HALVORSEN = {"who": "Martin Halvorsen", "role": "instructor",
           "when": "Tue 13:00-15:00", "where": "GCS Lower Level 1"}


@pytest.fixture
def note(monkeypatch, tmp_path):
    """CSCI 103's note, on disk and indexed, holding `entries` as its hours."""
    import sqlite3
    from contextlib import contextmanager
    import frontmatter
    from app import indexer

    rel = "02-Areas/usc/fall-2026/CSCI-103.md"
    path = tmp_path / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(":memory:", check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.execute("CREATE TABLE notes (path TEXT, title TEXT, frontmatter TEXT)")

    def write(entries):
        meta = {"type": "course", "code": "CSCI 103", "office_hours": entries}
        path.write_text(frontmatter.dumps(frontmatter.Post("# notes", **meta)),
                        encoding="utf-8")
        con.execute("DELETE FROM notes")
        con.execute("INSERT INTO notes VALUES (?,?,?)",
                    (rel, "CSCI 103", json.dumps(meta)))
        return path

    @contextmanager
    def session():
        yield con

    monkeypatch.setattr(indexer, "session", session)
    monkeypatch.setattr(indexer, "rebuild", lambda *a, **k: None)
    monkeypatch.setattr(courses, "VAULT_PATH", tmp_path)
    write.hours = lambda: frontmatter.load(path).metadata["office_hours"]
    return write


def _devon(**over):
    return _item(**{"who": "Devon", "role": "TA", "action": "add",
                    "day": "2026-10-08", "when": "Thu 12:00-14:00",
                    "where": "GCS, Lower Level 2", "note": "new TA hours",
                    "quote": DEVON_QUOTE, **over})


def test_the_digest_s_new_ta_block_becomes_a_proposal(dbfile, note):
    """No surname and a relative start date are both fine: the name is what
    the post was signed with, and the date is the first Thursday it applies."""
    note([HALVORSEN])
    _mail(subject="10/02/26 Daily Digest for CSCI 103L on Piazza",
          from_addr="no-reply@piazza.com")
    _upsert(_devon(day="2026-10-09"))       # model off by one; the quote says Thursdays
    (row,) = _rows()
    assert (row["action"], row["who"], row["role"], row["when_str"], row["day"]) == \
        ("add", "Devon", "TA", "Thu 12:00-14:00", "2026-10-08")
    assert row["from_addr"] == "no-reply@piazza.com"


@pytest.mark.parametrize("where", [None, "GCS, Lower Level 2", "gcs lower level 2"])
def test_an_add_the_note_already_holds_is_dropped(dbfile, note, where):
    """A digest re-announcing the syllabus hours is not news — however the
    model punctuates the room."""
    note([HALVORSEN, {"who": "Devon", "role": "TA", "when": "Thurs 12:00–14:00",
                    "where": "GCS Lower Level 2"}])
    _mail()
    _upsert(_devon(where=where))
    assert _rows() == []


def test_an_add_in_a_new_room_is_still_news(dbfile, note):
    note([HALVORSEN, {"who": "Devon", "role": "TA", "when": "Thu 12:00-14:00",
                    "where": "GCS Lower Level 2"}])
    _mail()
    _upsert(_devon(where="SAL 109"))
    assert [r["where_str"] for r in _rows()] == ["SAL 109"]


@pytest.mark.parametrize("over", [
    {"who": None},                      # set_hours files a block under a name
    {"when": "12:00-14:00"},            # no weekday: the card can't place it
    {"when": None},
])
def test_an_add_the_card_could_not_place_is_dropped(dbfile, note, over):
    note([HALVORSEN])
    _mail()
    _upsert(_devon(**over))
    assert _rows() == []


def test_an_add_that_started_last_week_still_waits(dbfile, note):
    """A cancel for a past date is moot; a weekly block that began last week
    is this week's hours, so it stays on the card."""
    note([HALVORSEN])
    _mail()
    _upsert(_devon(day="2020-01-02", quote="I hold office hours on Thursdays now."))
    assert [p["action"] for p in email_extract.pending_oh_proposals()] == ["add"]


def test_approving_an_add_writes_the_block_and_undo_removes_it(dbfile, note):
    path = note([HALVORSEN])
    _mail()
    _upsert(_devon())
    pid = _rows()[0]["id"]

    out = email_extract.decide_oh_proposal(pid, approve=True)
    assert out["status"] == "approved" and out["applied"].startswith("added Devon")
    assert note.hours()[-1] == {"who": "Devon", "role": "TA", "when": "Thu 12:00-14:00",
                                "where": "GCS, Lower Level 2"}

    email_extract.undecide_oh_proposal(pid)
    assert note.hours() == [HALVORSEN], "undo leaves the note as it was"
    assert _rows()[0]["status"] == "pending"
    assert path.exists()


def test_undoing_an_add_that_amended_a_block_puts_the_old_one_back(dbfile, note):
    """Same person, same weekday is the same block, so set_hours amends it —
    and the undo must restore those hours, not delete the block."""
    note([HALVORSEN])
    _mail()
    _upsert(_devon(who="Martin Halvorsen", role="instructor", when="Tue 14:00-16:00",
                  where=None, quote="My Tuesday office hours move to 2-4 pm."))
    pid = _rows()[0]["id"]

    email_extract.decide_oh_proposal(pid, approve=True)
    assert note.hours() == [{**HALVORSEN, "when": "Tue 14:00-16:00"}]

    email_extract.undecide_oh_proposal(pid)
    assert note.hours() == [HALVORSEN]


def test_the_model_is_told_the_hours_already_on_file(dbfile, note):
    note([HALVORSEN])
    pre = email_extract._oh_preamble()
    current = json.loads(pre.split("current_hours: ", 1)[1].split("\n", 1)[0])
    assert current == {"CSCI 103": ["Martin Halvorsen: Tue 13:00-15:00 @ GCS Lower Level 1"]}


# ---------- a digest is several posts ----------

def test_every_amendment_in_one_message_is_kept(dbfile, vault_courses, monkeypatch):
    """The runner used to keep only the first object per id — the second post
    in a digest was parsed and then thrown away."""
    _mail(subject="Daily Digest for CSCI 103L")
    reply = [_item(day="2099-05-05", quote="No OH Tuesday."),
             _item(day="2099-05-07", quote="No OH Thursday.")]

    async def ask(prompt, **k):
        return json.dumps(reply)
    monkeypatch.setattr(email_extract.agent, "ask", ask)
    monkeypatch.setattr(email_extract.email_triage, "body_excerpt",
                        lambda token, msg_id, limit=None: "body")
    with db.session() as con:
        n = email_extract._run_extraction(
            con, "tok", "office_hours", email_extract._office_hours_candidates(con),
            "sys", email_extract._upsert_oh_proposal)
        (payload,) = [r[0] for r in con.execute("SELECT payload FROM email_extract")]
    assert n == 2
    assert sorted(r["day"] for r in _rows()) == ["2099-05-05", "2099-05-07"]
    assert len(json.loads(payload)) == 2, "the cache keeps both, as a list"


def test_a_digest_gets_a_longer_excerpt():
    assert email_extract._excerpt_limit(
        {"subject": "10/02/26 Daily Digest for CSCI 103L on Piazza"}) \
        == email_extract.DIGEST_EXCERPT_CHARS
    assert email_extract._excerpt_limit({"subject": "OH cancelled"}) is None


# ---------- re-reading what the old prompt dropped ----------

def _cached(msg_id, payload, day="2026-10-02"):
    _mail(msg_id=msg_id, day=day)
    with db.session() as con, con:
        con.execute("INSERT INTO email_extract VALUES (?,?,?,?)",
                    (msg_id, "office_hours", json.dumps(payload), f"{day}T11:13:11"))


def test_reextract_forgets_only_the_empty_rows_since_the_date(dbfile):
    _cached("dropped", {"id": "dropped", "relevant": False})
    _cached("old", {"id": "old", "relevant": False}, day="2026-08-20")
    _cached("found", {"id": "found", "relevant": True, "action": "cancel"})
    _cached("both", [{"id": "both", "relevant": True}, {"id": "both", "relevant": True}])

    assert email_extract.reextract_office_hours("2026-09-01") == 1
    with db.session() as con:
        left = {r[0] for r in con.execute("SELECT msg_id FROM email_extract")}
        cands = [c["msg_id"] for c in email_extract._office_hours_candidates(con)]
    assert left == {"old", "found", "both"}
    assert cands == ["dropped"]


def test_reextract_rejects_a_bad_date(dbfile):
    with pytest.raises(ValueError):
        email_extract.reextract_office_hours("last week")
