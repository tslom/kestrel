"""Office hours, and the dated exceptions that override them.

The interesting case is the one a professor actually sends: "my Tuesday hours
start at 2pm, not 1pm — tomorrow only". Amending the weekly rule makes the card
wrong from next Tuesday on; ignoring the mail makes it wrong tomorrow. So these
tests pin that the rule and the exception both survive, and that each of the
three answers the card gives — today's rows, the week view, "next" — reads the
right one of the two.

2026-08-31 is a Monday, so 09-01 is the Tuesday throughout.
"""

import json
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime

import pytest

from app import courses, indexer

TUE, WED = "2026-09-01", "2026-09-02"


def _seed(monkeypatch, office_hours, *, code="CSCI 103"):
    """Stand up an index.db holding one course note.

    Frontmatter goes through `indexer._jsonable` exactly as the real indexer
    writes it, so a YAML date in an exception is tested along the path it
    actually travels rather than one this fixture invented."""
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.execute("CREATE TABLE notes (path TEXT, title TEXT, frontmatter TEXT)")
    meta = indexer._jsonable(
        {"type": "course", "code": code, "title": "Intro to Programming",
         "term": "fall-2026", "instructor": "Martin Halvorsen",
         "office_hours": office_hours})
    con.execute(
        "INSERT INTO notes VALUES (?,?,?)",
        (f"02-Areas/usc/fall-2026/{code.replace(' ', '-')}.md", code, json.dumps(meta)),
    )

    @contextmanager
    def session():
        yield con

    monkeypatch.setattr(indexer, "session", session)


def _clock(monkeypatch, when: str):
    """Pin both the day-key and the wall clock to one moment."""
    day, _, _ = when.partition(" ")
    fixed = datetime.fromisoformat(when)

    class _DT(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed

    monkeypatch.setattr(courses.db, "day_key", lambda dt=None: day)
    monkeypatch.setattr(courses, "datetime", _DT)


def _rule(exc=None, **over):
    """The standing Tuesday rule, optionally amended. `except` is a keyword,
    so it can only go in through a dict."""
    base = {"who": "Martin Halvorsen", "role": "instructor",
            "when": "Tue 13:00-14:00", "where": "GCS Lower Level One"}
    if exc is not None:
        base["except"] = exc
    return {**base, **over}


# --- the amended week ------------------------------------------------------

def test_exception_moves_todays_hours_but_not_the_rule(monkeypatch):
    _seed(monkeypatch, [_rule([{"date": TUE, "when": "14:00-15:00",
                                "note": "starts at 2pm this week"}])])
    _clock(monkeypatch, f"{TUE} 12:00")
    data = courses.load()

    (row,) = data["today"]
    assert (row["start"], row["end"]) == (14 * 60, 15 * 60), "today follows the exception"
    assert row["exception"]["note"] == "starts at 2pm this week"
    assert row["state"] == "later"

    (rule,) = data["courses"][0]["office_hours"]
    assert (rule["start"], rule["end"]) == (13 * 60, 14 * 60), "the week keeps the rule"
    assert rule["exception"]["date"] == TUE, "and flags the one-off beside it"


def test_open_now_uses_the_exception_not_the_rule(monkeypatch):
    """13:30 is inside the rule and outside the amended hours. The card must
    not say 'open now' about a room nobody is in yet."""
    _seed(monkeypatch, [_rule([{"date": TUE, "when": "14:00-15:00"}])])
    _clock(monkeypatch, f"{TUE} 13:30")
    assert courses.load()["open_now"] == []

    _clock(monkeypatch, f"{TUE} 14:30")
    assert [s["code"] for s in courses.load()["open_now"]] == ["CSCI 103"]


def test_cancelled_hours_show_as_cancelled_and_never_open(monkeypatch):
    _seed(monkeypatch, [_rule([{"date": TUE, "cancelled": True, "note": "conference"}])])
    _clock(monkeypatch, f"{TUE} 13:30")
    data = courses.load()

    (row,) = data["today"]
    assert row["state"] == "cancelled", "still on the board — 'don't walk over' is the point"
    assert row["exception"]["note"] == "conference"
    assert data["open_now"] == []
    assert data["next"]["at"].startswith("2026-09-08"), "next is the following Tuesday"


def test_next_skips_a_cancelled_week(monkeypatch):
    _seed(monkeypatch, [_rule([{"date": TUE, "cancelled": True}])])
    _clock(monkeypatch, "2026-08-31 09:00")
    assert courses.load()["next"]["at"].startswith("2026-09-08")


def test_hours_moved_to_another_day_are_found_there(monkeypatch):
    """A move is two exceptions, because two things happened."""
    _seed(monkeypatch, [_rule([
        {"date": TUE, "cancelled": True, "note": "moved to Wednesday"},
        {"date": WED, "when": "13:00-14:00"},
    ])])
    _clock(monkeypatch, "2026-08-31 09:00")
    assert courses.load()["next"]["at"].startswith(WED)

    _clock(monkeypatch, f"{WED} 12:00")
    row = courses.load()["today"][0]
    # `day` is today's, because the row is about today — so the weekday it was
    # moved off has to survive separately or the card can't name it.
    assert row["day"] == "Wed" and row["moved_from"] == "Tue"
    assert row["state"] == "later"


def test_hours_held_on_their_usual_day_are_not_marked_moved(monkeypatch):
    """`moved_from` names a day, so it has to be empty when there wasn't one —
    an amended time on the usual day is not a move."""
    _seed(monkeypatch, [_rule([{"date": TUE, "when": "14:00-15:00"}])])
    _clock(monkeypatch, f"{TUE} 12:00")
    assert courses.load()["today"][0]["moved_from"] is None


# --- degrading gracefully --------------------------------------------------

def test_unparseable_exception_annotates_rather_than_drops(monkeypatch):
    """Same principle as an unparseable `when`: say the odd thing, don't lose
    the person holding the hours."""
    _seed(monkeypatch, [_rule([{"date": TUE, "when": "sometime after lunch",
                                "note": "check Piazza"}])])
    _clock(monkeypatch, f"{TUE} 12:00")
    row = courses.load()["today"][0]
    assert (row["start"], row["end"]) == (13 * 60, 14 * 60), "keeps the rule's hours"
    assert row["exception"]["note"] == "check Piazza"


def test_undated_exception_is_ignored(monkeypatch):
    _seed(monkeypatch, [_rule([{"when": "14:00-15:00"}, "nonsense"])])
    _clock(monkeypatch, f"{TUE} 12:00")
    row = courses.load()["today"][0]
    assert (row["start"], row["end"]) == (13 * 60, 14 * 60)
    assert row["exception"] is None


def test_past_exceptions_do_not_annotate_the_week(monkeypatch):
    _seed(monkeypatch, [_rule([{"date": TUE, "when": "14:00-15:00"}])])
    _clock(monkeypatch, "2026-09-08 09:00")
    rule = courses.load()["courses"][0]["office_hours"][0]
    assert rule["exception"] is None, "last week's amendment is not this week's news"


def test_a_yaml_date_object_works_as_well_as_a_string(monkeypatch):
    """python-frontmatter hands back `datetime.date` for an unquoted date."""
    from datetime import date as _date
    _seed(monkeypatch, [_rule([{"date": _date(2026, 9, 1),
                                "when": "14:00-15:00"}])])
    _clock(monkeypatch, f"{TUE} 12:00")
    assert courses.load()["today"][0]["start"] == 14 * 60


def test_no_exceptions_is_the_old_behaviour(monkeypatch):
    _seed(monkeypatch, [_rule(), _rule(**{"when": "TBA", "who": "TA"})])
    _clock(monkeypatch, f"{TUE} 13:30")
    data = courses.load()

    assert [s["state"] for s in data["today"]] == ["now"]
    assert len(data["courses"][0]["office_hours"]) == 2
    assert all("_exc" not in s for s in data["courses"][0]["office_hours"]), \
        "working state stays out of the payload"
    assert all("_exc" not in s for s in data["today"])
    assert "_exc" not in data["next"]


def test_jsonable_survives_a_nested_date():
    """A shallow json-ability test flattened the whole list to a repr for one
    nested date, and the hours vanished from the card."""
    from datetime import date as _date

    out = indexer._jsonable([{"who": "G", "except": [{"date": _date(2026, 9, 1)}]}])
    assert out == [{"who": "G", "except": [{"date": "2026-09-01"}]}]
    json.dumps(out)


# --- writing the rule --------------------------------------------------

def _note(monkeypatch, tmp_path, office_hours, *, code="CSCI 170"):
    """A real note on disk, indexed, with `_save`'s reindex stubbed out.

    `set_hours` reads the frontmatter off the file and the *path* out of
    index.db, so both have to exist for the write path to be exercised at
    all."""
    rel = f"02-Areas/usc/fall-2026/{code.replace(' ', '-')}.md"
    path = tmp_path / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    body = ["---", "type: course", f"code: {code}", "title: Discrete Methods",
            "term: fall-2026", "office_hours:"]
    for e in office_hours:
        body.append(f'- who: {e["who"]}')
        body.append(f'  role: {e.get("role", "TA")}')
        body.append(f'  when: {e["when"]}')
        body.append(f'  where: {e.get("where", "")!r}')
    body += ["---", "", "# notes", ""]
    path.write_text("\n".join(body), encoding="utf-8")

    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.execute("CREATE TABLE notes (path TEXT, title TEXT, frontmatter TEXT)")
    con.execute("INSERT INTO notes VALUES (?,?,?)",
                (rel, code, json.dumps({"type": "course", "code": code})))

    @contextmanager
    def session():
        yield con

    monkeypatch.setattr(indexer, "session", session)
    monkeypatch.setattr(indexer, "rebuild", lambda *a, **k: None)
    monkeypatch.setattr(courses, "VAULT_PATH", tmp_path)
    return path


def _hours_on(path):
    import frontmatter
    return frontmatter.load(path).metadata["office_hours"]


def test_a_second_block_for_one_person_adds_rather_than_overwrites(monkeypatch, tmp_path):
    """The 2026-09-01 loss: one TA's four blocks written one call at a time,
    each amending the entry the last call wrote, three of them gone."""
    path = _note(monkeypatch, tmp_path, [])
    for when, where in [("Mon, Wed 16:00-18:00", "GCS SB9"),
                        ("Tue 09:00-12:00", "https://usc.zoom.us/j/99180444912"),
                        ("Tue, Thu 15:00-18:00", "https://usc.zoom.us/j/96871893750")]:
        out = courses.set_hours("CSCI 170", "Pavel Czempin", when,
                                where=where, role="TA")
        assert out.startswith("added"), out

    hours = _hours_on(path)
    assert [e["when"] for e in hours] == ["Mon, Wed 16:00-18:00",
                                          "Tue 09:00-12:00",
                                          "Tue, Thu 15:00-18:00"]
    assert hours[1]["where"] == "https://usc.zoom.us/j/99180444912"


def test_the_same_weekday_amends_that_block_and_leaves_the_others(monkeypatch, tmp_path):
    """The move this tool is for: same person, same day, new time."""
    path = _note(monkeypatch, tmp_path, [
        {"who": "Pavel Czempin", "when": "Mon, Wed 16:00-18:00", "where": "GCS SB9"},
        {"who": "Pavel Czempin", "when": "Tue 09:00-12:00", "where": "Zoom"},
    ])
    out = courses.set_hours("CSCI 170", "Pavel Czempin", "Tue 10:00-13:00")

    assert out.startswith("updated")
    assert "Mon, Wed 16:00-18:00" in out, "says which blocks it left alone"
    hours = _hours_on(path)
    assert [e["when"] for e in hours] == ["Mon, Wed 16:00-18:00", "Tue 10:00-13:00"]
    assert hours[1]["where"] == "Zoom", "where survives an amend that omits it"


def test_two_blocks_on_the_same_days_need_a_slot(monkeypatch, tmp_path):
    _note(monkeypatch, tmp_path, [
        {"who": "Pavel Czempin", "when": "Tue 09:00-12:00"},
        {"who": "Pavel Czempin", "when": "Tue 15:00-18:00"},
    ])
    try:
        courses.set_hours("CSCI 170", "Pavel Czempin", "Tue 10:00-13:00")
    except courses.CourseError as e:
        assert "slot" in str(e)
    else:
        raise AssertionError("amended one of two Tuesday blocks blind")


def test_a_slot_addresses_one_block_and_a_wrong_one_is_an_error(monkeypatch, tmp_path):
    path = _note(monkeypatch, tmp_path, [
        {"who": "Pavel Czempin", "when": "Tue 09:00-12:00"},
        {"who": "Pavel Czempin", "when": "Tue 15:00-18:00"},
    ])
    courses.set_hours("CSCI 170", "Pavel Czempin", "Tue 10:00-13:00", slot="09:00")
    assert [e["when"] for e in _hours_on(path)] == ["Tue 10:00-13:00", "Tue 15:00-18:00"]

    try:
        courses.set_hours("CSCI 170", "Pavel Czempin", "Fri 10:00-11:00", slot="Mon")
    except courses.CourseError as e:
        assert "no office-hours entry" in str(e)
    else:
        raise AssertionError("a slot naming no block appended silently")


def test_posted_hours_do_not_clobber_a_block_that_cannot_be_placed(monkeypatch, tmp_path):
    """'TBA' has no weekdays to compare, so posted hours are added, not
    written over it — but the reply has to say the placeholder is still
    there, or the card carries both and nobody knows why."""
    path = _note(monkeypatch, tmp_path, [
        {"who": "Ludwika Twardecka", "when": "TBA"},
    ])
    out = courses.set_hours("CSCI 170", "Ludwika Twardecka", "Wed 14:00-16:00")
    assert out.startswith("added")
    assert "placeholder" in out
    assert [e["when"] for e in _hours_on(path)] == ["TBA", "Wed 14:00-16:00"]

    courses.set_hours("CSCI 170", "Ludwika Twardecka", "TBA", remove=True)
    assert [e["when"] for e in _hours_on(path)] == ["Wed 14:00-16:00"]


def test_an_unparseable_when_amends_the_block_it_cannot_compare(monkeypatch, tmp_path):
    """The other direction: writing 'TBA' over posted hours is a rewrite of
    that block, not a second row saying two things at once."""
    path = _note(monkeypatch, tmp_path, [
        {"who": "Ludwika Twardecka", "when": "Wed 14:00-16:00"},
    ])
    out = courses.set_hours("CSCI 170", "Ludwika Twardecka", "TBA")
    assert out.startswith("updated")
    assert "plain text" in out
    assert [e["when"] for e in _hours_on(path)] == ["TBA"]


def test_a_different_person_is_always_a_new_block(monkeypatch, tmp_path):
    path = _note(monkeypatch, tmp_path, [
        {"who": "Pavel Czempin", "when": "Wed 16:00-18:00"},
    ])
    out = courses.set_hours("CSCI 170", "Ludwika Twardecka", "Wed 14:00-16:00")
    assert out.startswith("added")
    assert "other blocks" not in out, "another person's hours are not theirs"
    assert len(_hours_on(path)) == 2


def test_a_dated_override_picks_the_block_that_meets_that_day(monkeypatch, tmp_path):
    """The 2026-09-25 409: a TA with Mon and Thu blocks, a mailed "Thursday
    next week on Zoom" — ambiguous by name, not by date."""
    path = _note(monkeypatch, tmp_path, [
        {"who": "Martin Lindqvist", "when": "Mon 13:00-14:00"},
        {"who": "Martin Lindqvist", "when": "Thu 08:00-10:00"},
    ])
    courses.set_exception("CSCI 170", "Martin", "2026-10-01",
                          when="08:00-10:00", note="Zoom only")
    hours = _hours_on(path)
    assert "except" not in hours[0]
    assert hours[1]["except"] == [{"date": "2026-10-01", "when": "08:00-10:00",
                                   "note": "Zoom only"}]

    courses.set_exception("CSCI 170", "Martin", "2026-10-01", remove=True)
    assert all("except" not in e for e in _hours_on(path)), "undo finds it too"


def test_a_middle_name_still_finds_the_person(monkeypatch, tmp_path):
    """The 11-17 409s: mail signed "Martin Paul Halvorsen", the note says
    "Martin Halvorsen", and approving the moved hours failed three times."""
    path = _note(monkeypatch, tmp_path, [
        {"who": "Martin Halvorsen", "when": "Tue 13:00-15:00", "where": "GCS Lower Level 1"},
        {"who": "Devon", "when": "Thu 12:00-14:00"},
    ])
    out = courses.set_exception("CSCI 170", "Martin Paul Halvorsen", "2026-11-17",
                                when="13:00-15:00", where="Lower Level 2 (B2)",
                                note="moved for building event")
    assert "in Lower Level 2 (B2)" in out
    assert _hours_on(path)[0]["except"] == [{
        "date": "2026-11-17", "when": "13:00-15:00", "where": "Lower Level 2 (B2)",
        "note": "moved for building event"}]
    # and no new false friends: a different Martin doesn't match
    with pytest.raises(courses.CourseError):
        courses.set_exception("CSCI 170", "Martin Lindqvist", "2026-11-17", cancelled=True)


def test_todays_row_shows_the_rooms_exception(monkeypatch):
    entry = {"who": "G", "when": "Tue 13:00-15:00", "where": "GCS LL1",
             "except": [{"date": "2026-11-17", "where": "Lower Level 2"}]}
    slot = courses._slots(entry, "CSCI 103", "OOP")[0]
    assert slot["_exc"]["2026-11-17"]["where"] == "Lower Level 2"
    assert courses._effective(slot, date(2026, 11, 17)) is not None, "same hours, new room"


def test_a_date_neither_block_meets_still_asks_for_a_slot(monkeypatch, tmp_path):
    _note(monkeypatch, tmp_path, [
        {"who": "Martin Lindqvist", "when": "Mon 13:00-14:00"},
        {"who": "Martin Lindqvist", "when": "Thu 08:00-10:00"},
    ])
    try:
        courses.set_exception("CSCI 170", "Martin", "2026-10-02", cancelled=True)
    except courses.CourseError as e:
        assert "slot" in str(e)
    else:
        raise AssertionError("guessed a block for a Friday")
