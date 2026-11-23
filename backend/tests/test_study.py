"""Study mode (app/study.py): the syllabus read as "where are we now", and the
per-course review log.

The fixtures are cut-down copies of the real extractions under
`02-Areas/usc/fall-2026/syllabi/text/`, in the three shapes they actually
come in: dated rows (CSCI 170), week blocks (MATH 229), and a table that
extraction scrambled into two unaligned columns (CSCI 103). Every vault write
goes to tmp_path.
"""

import asyncio
import json

import frontmatter
import pytest

from app import console_tools as ct
from app import courses, study

TERM = "fall-2026"

CS170_SYLLABUS = """\
CSCI 170 Fall 2026 Syllabus
Grading
Artifact Weight Date
Midterm 1 20% Tuesday, October 6th, 7-8:50pm
Midterm 2 20% Tuesday, November 10th, 7-8:50pm
Final 28% Wednesday, December 16th, 2-4pm
Exam Information
You may bring a single page of notes (8.5x11, front&back) to all exams.
Counseling and Mental Health - (213) 740-9355 24/7 on call
Day Topic Reading Homework
8/24, 8/25 Introduction Ch. 1
9/17, 9/21 Sets Ch. 5
9/29, 9/30 Strong Induction Ch. 4
10/1, 10/5 Recursion
10/6 7pm Midterm 1
10/6, 10/7 Recursion HW 5 Out
10/12, 10/13 Order Notation Ch. 21
11/10 7pm Midterm 2
11/10, 11/11 No Lecture
12/16 2pm Final Exam
"""

MATH_SYLLABUS = """\
Midterm Exams:   There will be two midterm exams scheduled on Wednesday, September 30 (Exam 1)
and Wednesday, November 4 (Exam 2) during class. No make-up exam.
Tentative Weekly Schedule
Week 1
 8/24-28
Sec. 10.1 Three-dimensional coordinate
systems
Week 6
 9/28-10/02
Sec. 11.6 Directional derivatives
Midterm Exam 1
Week 11
 11/02-06
Sec. 13.2 Line integrals Midterm Exam 2
Week 15
 11/30-12/04
Sec. 13.8 Stokes' Theorem Final exam review

Course letter grade policy: the standard scaling
applies to the total weighted score (after dropping scores of some
quizzes and homework established by the course instructor).
"""

MATH_NOTE = """\
## Topics

Roughly one section per lecture; Midterm 1 falls in week 6 (9/28–10/2) and
Midterm 2 in week 11.
"""

CS103_SYLLABUS = (
    "Exams For fall 2026 we will have 4 exams tied to each project.  Time and "
    "Location: The exams will take place in class on Sept. 24th, Oct. 16th, "
    "Nov. 19 and Dec. 3rd.  Academic Accommodations: If you have USC approved "
    "academic accommodations, please schedule a time with the OSAS testing "
    "center at least 1 week before the exam AND let the instructor know.\n"
    "Topics Readings Deliverables\nWeek 1\nWeek 2\nWeek 3\n"
    "Final See schedule of classes: Saturday December 12th 11am\n"
    "Course Overview; C++ Bootcamp\nPass-by-value, pass-by-reference\n"
    "Dynamic AllocationPointer Arithmetic PR 1\n"
    " Page 7 \nCourse Content Distribution policies go on for a while here.\n"
)

CS103_NOTE = """\
### Lab schedule

| Wk | Date | Topic |
|---|---|---|
| 1 | 8/28 | Codio (individual, online) |
| 2 | 9/4 | Coding Challenge |
| 7 | 10/9 | **No lab** — Fall Recess |
"""

ENGR_NOTE = """\
| Assignment | Due | Points |
|---|---|---|
| — Final Presentations | in discussion, **12/1/2026** | 25 |
| — Final Project Reports | Brightspace, **12/11/2026 19:00 PT** | 25 |
"""


@pytest.fixture
def vault(tmp_path, monkeypatch, empty_index, freeze_day):
    """A vault on disk plus the index rows that point at it. Returns an
    `add(code, note_body, syllabus_text)` helper."""
    monkeypatch.setattr(study, "VAULT_PATH", tmp_path)
    monkeypatch.setattr(courses, "VAULT_PATH", tmp_path)
    freeze_day("2026-10-04")    # a Sunday: week 6, two days before CSCI 170's midterm

    def add(code, note="", syllabus=None):
        slug = code.replace(" ", "-")
        rel = f"02-Areas/usc/{TERM}/{slug}.md"
        meta = {"type": "course", "code": code, "title": f"{code} title", "term": TERM}
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text(frontmatter.dumps(frontmatter.Post(note, **meta)),
                                    encoding="utf-8")
        empty_index.execute("INSERT INTO notes VALUES (?,?,?)", (rel, code, json.dumps(meta)))
        if syllabus is not None:
            srel = f"02-Areas/usc/{TERM}/syllabi/text/{slug}-syllabus-text.md"
            smeta = {"type": "syllabus-text", "code": code, "term": TERM}
            (tmp_path / srel).parent.mkdir(parents=True, exist_ok=True)
            (tmp_path / srel).write_text(
                frontmatter.dumps(frontmatter.Post(syllabus, **smeta)), encoding="utf-8")
            empty_index.execute("INSERT INTO notes VALUES (?,?,?)",
                                (srel, slug, json.dumps(smeta)))
        return tmp_path / rel

    add.root = tmp_path
    add.index = empty_index
    return add


# ---------- the schedule and its exams ----------

def test_dated_rows_and_one_exam_per_day(vault):
    """The grading table and the schedule row both name Midterm 1; that's one
    exam with both quotes, not two exams."""
    vault("CSCI 170", syllabus=CS170_SYLLABUS)
    sched = study.schedule("CSCI 170")
    assert sched["source"] == "syllabus"
    assert [r["when"] for r in sched["rows"]][:3] == ["8/24, 8/25", "9/17, 9/21", "9/29, 9/30"]
    found = study.exams(sched)
    assert [(e["label"], str(e["date"])) for e in found] == [
        ("Midterm 1", "2026-10-06"), ("Midterm 2", "2026-11-10"),
        ("Final Exam", "2026-12-16")]
    assert len(found[0]["quotes"]) == 2


def test_the_lecture_after_an_exam_row_is_not_an_exam(vault):
    """"10/6, 10/7 Recursion" follows "10/6 7pm Midterm 1". Borrowing the
    previous line's keyword is for prose wrapped across lines; a schedule row
    borrowing it put a phantom exam on 10/7."""
    vault("CSCI 170", syllabus=CS170_SYLLABUS)
    days = [str(e["date"]) for e in study.exams(study.schedule("CSCI 170"))]
    assert "2026-10-07" not in days and "2026-11-11" not in days


def test_context_places_today_and_lists_what_comes_before_the_exam(vault):
    vault("CSCI 170", syllabus=CS170_SYLLABUS)
    out = study.context("CSCI 170")
    assert "week 6 of term (week 1 began 2026-08-24)" in out
    assert "Midterm 1: 2026-10-06 Tue (in 2 days)" in out
    before = next(ln for ln in out.splitlines() if "before it (since 2026-08-24)" in ln)
    assert "Sets Ch. 5" in before and "Recursion" in before
    assert "Order Notation" not in before        # after the midterm
    assert "Midterm" not in before.split(":", 1)[1]
    assert "[>] 10/1, 10/5 — Recursion" in out
    assert "[x] 9/29, 9/30 — Strong Induction Ch. 4" in out
    assert "[ ] 10/12, 10/13 — Order Notation Ch. 21" in out


def test_week_blocks_and_prose_dates(vault):
    """MATH 229's shape: week blocks, exam dates in a wrapped prose sentence,
    and a note line naming the midterm's *week* — which must fold into the
    exact date, not become a second exam on 9/28."""
    vault("MATH 229", note=MATH_NOTE, syllabus=MATH_SYLLABUS)
    sched = study.schedule("MATH 229")
    assert [r["when"] for r in sched["rows"]] == ["Week 1", "Week 6", "Week 11", "Week 15"]
    assert sched["rows"][0]["text"].startswith("Sec. 10.1 Three-dimensional")
    found = study.exams(sched)
    assert [(str(e["date"]), e["approx"]) for e in found] == [
        ("2026-09-30", False), ("2026-11-04", False)]
    assert found[1]["label"] == "Exam 2"


def test_the_last_week_block_stops_before_the_grading_policy(vault):
    """Week 15 has no next header to stop at; reading on into the policy made
    "quizzes" (and its 12/9) an exam once."""
    vault("MATH 229", syllabus=MATH_SYLLABUS)
    sched = study.schedule("MATH 229")
    assert "quizzes" not in sched["rows"][-1]["text"]
    assert not any(e["label"].lower().startswith("quiz") for e in study.exams(sched))


def test_an_undatable_syllabus_falls_back_to_the_note_and_shows_its_own_text(vault):
    """CSCI 103: the syllabus's topic table has no dates, so the schedule is
    the note's lab table — and the syllabus's section is shown verbatim, up to
    the page footer, rather than aligned by guesswork. Its exam sentence
    abbreviates months ("Sept. 24th, Oct. 16th"), which a naive sentence split
    cuts after "Sept."."""
    vault("CSCI 103", note=CS103_NOTE, syllabus=CS103_SYLLABUS)
    out = study.context("CSCI 103")
    assert "Schedule (from the course note" in out
    assert "10/9 — 7 · No lab — Fall Recess" in out
    assert "Dynamic AllocationPointer Arithmetic PR 1" in out
    assert "Course Content Distribution" not in out
    days = [str(e["date"]) for e in study.exams(study.schedule("CSCI 103"))]
    assert days == ["2026-09-24", "2026-10-16", "2026-11-19", "2026-12-03", "2026-12-12"]


def test_final_presentations_are_not_an_exam(vault):
    vault("ENGR 102", note=ENGR_NOTE, syllabus="")
    assert study.exams(study.schedule("ENGR 102")) == []
    assert "Exams: none dated" in study.context("ENGR 102")


def test_no_dates_anywhere_says_so(vault):
    vault("WRIT 150", note="# Writing\n\nNo schedule here.\n", syllabus="Grading contract.\n")
    out = study.context("WRIT 150")
    assert "nothing in the syllabus text or the course note carries dates" in out


def test_query_finds_the_syllabus_line_or_says_it_is_absent(vault):
    vault("CSCI 103", note=CS103_NOTE, syllabus=CS103_SYLLABUS)
    out = study.context("CSCI 103", query="dynamic")
    assert '"Dynamic AllocationPointer Arithmetic PR 1"' in out
    out = study.context("CSCI 103", query="stack heap")
    assert "not mentioned in the syllabus text or the course note" in out


# ---------- the review log ----------

def _log(code="CSCI 170", concept="size of a power set", **kw):
    return study.add_review(code, concept, kw.pop("mistake", "said |P(A)| = 2|A|"),
                            kw.pop("correct", "|P(A)| = 2^|A|: each element is in or out"),
                            **kw)


def test_first_entry_creates_the_note_in_generated(vault):
    vault("CSCI 170", syllabus=CS170_SYLLABUS)
    out = _log(topic="Sets (Ch. 5)", session="c799b266-6813")
    path = vault.root / "06-Generated" / "CSCI-170-review.md"
    assert "logged" in out and path.exists()
    post = frontmatter.load(path)
    assert post["type"] == "course-review" and post["code"] == "CSCI 170"
    assert post["course"] == "[[CSCI-170]]" and post["origin"] == "generated"
    assert "### 2026-10-04 — size of a power set" in post.content
    assert "- **Before:** Midterm 1, 2026-10-06" in post.content
    assert "- **Session:** `c799b266-6813`" in post.content
    assert "- **Topic:** Sets (Ch. 5)" in post.content


def test_the_same_concept_again_is_counted_not_duplicated(vault, freeze_day):
    vault("CSCI 170", syllabus=CS170_SYLLABUS)
    _log()
    _log(concept="De Morgan for quantifiers", mistake="negated ∀ as ∀",
         correct="¬∀x P(x) ≡ ∃x ¬P(x)")
    freeze_day("2026-10-05")
    out = _log(concept="Size of a  power set!", mistake="said 2|A| again")
    assert "marked again (2 times now)" in out
    items = study.entries("CSCI 170")
    assert len(items) == 2
    assert items[0]["again"] == ["2026-10-05 — said 2|A| again"]
    # The append landed inside its own entry, not after the next one.
    assert items[1]["concept"] == "De Morgan for quantifiers" and items[1]["again"] == []


def test_review_reads_back_most_missed_first_and_filters(vault, freeze_day):
    vault("CSCI 170", syllabus=CS170_SYLLABUS)
    freeze_day("2026-09-20")
    _log(concept="pigeonhole bound", mistake="used n/k", correct="⌈n/k⌉")
    freeze_day("2026-10-04")
    _log()
    _log()                                       # missed twice
    out = study.review_text("CSCI 170")
    first = out.splitlines()[1]
    assert "size of a power set (missed 2x)" in first
    assert "pigeonhole" not in study.review_text("CSCI 170", since="2026-10-01")
    assert "1 of 2" in study.review_text("CSCI 170", query="midterm 1 power")


def test_the_users_own_edits_survive_and_are_read(vault):
    vault("CSCI 170", syllabus=CS170_SYLLABUS)
    _log()
    path = vault.root / "06-Generated" / "CSCI-170-review.md"
    path.write_text(path.read_text() + "Think of bit strings of length |A|.\n")
    _log(concept="strong induction base cases", mistake="one base case",
         correct="as many base cases as the step reaches back")
    (first, second) = study.entries("CSCI 170")
    assert first["notes"] == ["Think of bit strings of length |A|."]
    assert second["concept"] == "strong induction base cases"
    assert "note: Think of bit strings" in study.review_text("CSCI 170")


def test_a_moved_review_note_is_still_found_by_its_frontmatter(vault):
    """The user files 06-Generated away. The log must follow, not restart."""
    vault("CSCI 170", syllabus=CS170_SYLLABUS)
    _log()
    old = vault.root / "06-Generated" / "CSCI-170-review.md"
    new_rel = f"02-Areas/usc/{TERM}/CSCI-170-review.md"
    (vault.root / new_rel).write_text(old.read_text())
    old.unlink()
    vault.index.execute("INSERT INTO notes VALUES (?,?,?)", (
        new_rel, "x", json.dumps({"type": "course-review", "code": "CSCI 170"})))
    _log(concept="recursion unrolling", mistake="m", correct="c")
    assert not old.exists()
    assert len(study.entries("CSCI 170")) == 2


def test_nothing_logged_and_unknown_course(vault):
    vault("CSCI 170", syllabus=CS170_SYLLABUS)
    assert study.review_text("CSCI 170") == "CSCI 170: nothing logged yet."
    with pytest.raises(courses.CourseError):
        _log(code="PHYS 151")
    with pytest.raises(courses.CourseError):
        _log(mistake="")


def test_desk_names_the_exam_and_the_count(vault):
    vault("CSCI 170", syllabus=CS170_SYLLABUS)
    vault("WRIT 150", note="nothing", syllabus="")
    _log()
    # Logged today: not due even two days out — cramming skips what was
    # just seen.
    assert study.desk() == [{"code": "CSCI 170", "review_items": 1, "review_due": 0, "exam": {
        "label": "Midterm 1", "date": "2026-10-06", "approx": False, "in_days": 2}}]


# ---------- the console tools ----------

@pytest.fixture(autouse=True)
def _inline(monkeypatch):
    """The in-memory index connection is bound to this thread; the handlers'
    worker-thread hop is not what's under test."""
    async def inline(fn, *a, **k):
        return fn(*a, **k)
    monkeypatch.setattr(ct, "_blocking", inline)


def _served(name, **args):
    from mcp.types import CallToolRequest, CallToolRequestParams
    srv = ct.build_server()["instance"]
    req = CallToolRequest(method="tools/call",
                          params=CallToolRequestParams(name=name, arguments=args))
    r = asyncio.run(srv.request_handlers[CallToolRequest](req)).root
    return r.content[0].text, bool(r.isError)


def test_review_add_records_the_calling_session(vault):
    """console.py sets SESSION before connect(); a handler sees the socket's
    state and, through it, the session the CLI announced."""
    vault("CSCI 170", syllabus=CS170_SYLLABUS)

    class State:
        announced_id = "5719a6e8-aaaa"

    token = ct.SESSION.set(State())
    try:
        text, err = _served("course_review_add", code="CSCI 170",
                            concept="power set", mistake="2|A|", correct="2^|A|")
    finally:
        ct.SESSION.reset(token)
    assert not err, text
    assert "`5719a6e8-aaaa`" in (vault.root / "06-Generated" / "CSCI-170-review.md").read_text()

    text, err = _served("course_review", code="CSCI 170")
    assert not err and "power set" in text
    text, err = _served("course_context", code="CSCI 170", query="recursion")
    assert not err and "Review log: 1 item" in text and "Search 'recursion'" in text


def test_tools_refuse_a_missing_course_and_a_wrong_argument(vault):
    vault("CSCI 170", syllabus=CS170_SYLLABUS)
    text, err = _served("course_context", code="PHYS 151")
    assert err and text == "error: no course note for 'PHYS 151'"
    text, err = _served("course_review_add", code="CSCI 170", concept="x",
                        mistake="y", correct="z", text="stray")
    assert err and "'text' was unexpected" in text


def test_review_add_takes_a_batch_with_a_shared_code(vault):
    """58 of 58 course_review_add calls in the 14 days to 11-17 came in runs
    of 3+: `items` makes a run one call, and a bad item doesn't sink the rest."""
    vault("CSCI 170", syllabus=CS170_SYLLABUS)
    text, err = _served("course_review_add", code="CSCI 170", items=[
        {"concept": "power set", "mistake": "2|A|", "correct": "2^|A|"},
        {"concept": "integer division", "mistake": "7/2 = 3.5", "correct": "7/2 = 3 in C++"},
        {"concept": "no answer", "mistake": "?"}])
    assert not err, text
    assert "FAILED: no answer: pass correct" in text
    log = (vault.root / "06-Generated" / "CSCI-170-review.md").read_text()
    assert "power set" in log and "integer division" in log and "no answer" not in log
    # Single calls still need the four fields.
    text, _ = _served("course_review_add", code="CSCI 170", concept="x")
    assert text == "error: x: pass mistake, correct"


# ---------- spaced review ----------

def _entry(*events):
    return {"events": list(events)}


def test_schedule_widens_on_rights_and_restarts_on_a_miss():
    from datetime import date
    d = date.fromisoformat
    s = study.schedule_of(_entry(("2026-10-01", "miss")), d("2026-10-02"))
    assert s == {"streak": 0, "learned": False, "due": True, "next": "2026-10-02"}
    e = _entry(("2026-10-01", "miss"), ("2026-10-02", "right"))
    assert study.schedule_of(e, d("2026-10-04"))["due"] is False
    assert study.schedule_of(e, d("2026-10-05"))["next"] == "2026-10-05"
    # A second right the same day doesn't count as surviving an interval.
    e["events"].append(("2026-10-02", "right"))
    assert study.schedule_of(e, d("2026-10-05"))["streak"] == 1
    e["events"] += [("2026-10-05", "right"), ("2026-10-12", "right")]
    assert study.schedule_of(e, d("2026-10-26"))["due"] is True
    e["events"].append(("2026-10-26", "right"))
    assert study.schedule_of(e, d("2026-12-01"))["learned"] is True
    e["events"].append(("2026-12-01", "miss"))
    assert study.schedule_of(e, d("2026-12-02")) == {
        "streak": 0, "learned": False, "due": True, "next": "2026-12-02"}


def test_cramming_makes_unlearned_items_due_unless_seen_today():
    from datetime import date
    e = _entry(("2026-10-01", "miss"), ("2026-10-02", "right"))
    assert study.schedule_of(e, date(2026, 10, 3), cram=True)["due"] is True
    assert study.schedule_of(e, date(2026, 10, 2), cram=True)["due"] is False


def test_mark_review_appends_to_the_entry_and_moves_the_schedule(vault, freeze_day):
    vault("WRIT 150", note="nothing", syllabus="")     # no exams: no cramming
    freeze_day("2026-10-01")
    _log(code="WRIT 150", concept="thesis vs topic")
    _log(code="WRIT 150", concept="counterargument placement")
    freeze_day("2026-10-02")
    assert "2 due today of 2" in study.review_text("WRIT 150", due=True)
    out = study.mark_review("WRIT 150", "Thesis vs topic", True)
    assert "next review 2026-10-05 (step 2 of 4)" in out
    out = study.mark_review("WRIT 150", "counterargument", False, mistake="put it first")
    assert "missed again" in out and "next review 2026-10-03" in out
    path = vault.root / "06-Generated" / "WRIT-150-review.md"
    a, b = study.entries("WRIT 150")
    assert a["reviewed"] == ["2026-10-02 — right"] and a["again"] == []
    assert b["again"] == ["2026-10-02 — put it first"] and b["reviewed"] == []
    assert "- **Reviewed:** 2026-10-02 — right\n\n### " in path.read_text()
    assert "nothing due today (2 logged; next due 2026-10-03)" in study.review_text("WRIT 150", due=True)
    freeze_day("2026-10-03")
    out = study.review_text("WRIT 150", due=True)
    assert "1 due today of 2" in out and "counterargument placement (missed 2x) [DUE]" in out
    with pytest.raises(courses.CourseError, match="no logged concept"):
        study.mark_review("WRIT 150", "semicolons", True)


def test_review_due_and_mark_through_the_tools(vault, freeze_day):
    vault("WRIT 150", note="nothing", syllabus="")
    freeze_day("2026-10-01")
    _log(code="WRIT 150", concept="thesis vs topic")
    freeze_day("2026-10-02")
    text, err = _served("course_review", code="WRIT 150", due=True)
    assert not err and "1 due today" in text
    text, err = _served("course_review_mark", code="WRIT 150", concept="thesis vs topic", right=True)
    assert not err and "step 2 of 4" in text
    text, err = _served("course_review_mark", code="WRIT 150", concept="thesis vs topic", right="yes")
    assert err


# ---------- coverage ----------

def _doc(con, sha, name, pages):
    from app import citations
    con.execute("INSERT INTO docs VALUES (?,?,?,?,?,?,?,?)",
                (sha, name, "application/pdf", 1, pages, "[]", "2026-10-01T00:00:00",
                 "2026-10-01T00:00:00"))
    con.commit()
    return citations.remember("doc", sha, name, "2026-10-01")


def test_attempts_are_recorded_once_per_block_and_read_back_as_coverage(vault, con, freeze_day):
    vault("CSCI 103", note="nothing", syllabus="")
    ref = _doc(con, "a" * 64, "Unit2a.pdf", 10)
    _doc(con, "b" * 64, "Unit2b.pdf", 5)
    freeze_day("2026-10-03")
    items = [
        {"idx": 0, "kind": "choice", "question": "q0", "correct": True, "ref": f"{ref}#p2"},
        {"idx": 1, "kind": "free", "question": "q1", "correct": False, "self_graded": True,
         "ref": f"{ref}#p3"},
        {"idx": 2, "kind": "card", "question": "q2", "correct": True, "ref": ""},
        {"idx": 3, "kind": "bogus", "question": "q3", "correct": True},
    ]
    assert study.record_attempts("blk1", "csci-103", items) == 3
    items[1]["correct"] = True                    # sent again after a retry
    assert study.record_attempts("blk1", "CSCI 103", items) == 3
    study.record_attempts("blk2", "MATH 229", items[:1])
    out = study.coverage_text("CSCI 103")
    assert "CSCI 103: 3 answered, 3 right (100%)" in out
    assert "- Unit2a.pdf: 2 answered, 2 right" in out
    assert "pages asked from: 2-3 of 10" in out and "never asked: 1,4-10" in out
    assert "not tied to a document: 1 answered" in out
    assert "Dropped documents never quizzed (any course): Unit2b.pdf (5p" in out
    assert "no quiz or card answers recorded since 2026-11-01" in \
        study.coverage_text("CSCI 103", since="2026-11-01")


def test_confidence_is_kept_and_splits_coverage(vault, con, freeze_day):
    vault("CSCI 103", note="nothing", syllabus="")
    freeze_day("2026-10-03")
    study.record_attempts("blk", "CSCI 103", [
        {"idx": 0, "kind": "choice", "question": "heap vs stack", "correct": False,
         "confidence": "sure"},
        {"idx": 1, "kind": "choice", "question": "ptr arithmetic", "correct": True,
         "confidence": "guess"},
        {"idx": 2, "kind": "free", "question": "q2", "correct": False, "confidence": "dont_know"},
        {"idx": 3, "kind": "card", "question": "q3", "correct": True, "confidence": "very"},
    ])
    got = dict(con.execute("SELECT idx, confidence FROM study_attempts WHERE block='blk'"))
    assert got == {0: "sure", 1: "guess", 2: "dont_know", 3: None}   # unknown → didn't say
    out = study.coverage_text("CSCI 103")
    assert "Missed while sure (1) — likely misconceptions: heap vs stack" in out
    assert "Right but guessed or unsure (1) — not yet known: ptr arithmetic" in out


def test_a_rating_given_after_the_verdict_is_counted_apart(vault, con, freeze_day):
    """Hindsight is allowed but must not pass for a rating given blind, and
    re-sending a block must be able to change it either way."""
    vault("CSCI 103", note="nothing", syllabus="")
    freeze_day("2026-10-03")
    study.record_attempts("blk", "CSCI 103", [
        {"idx": 0, "kind": "choice", "question": "heap vs stack", "correct": False,
         "confidence": "sure", "rated_after": True},
        {"idx": 1, "kind": "choice", "question": "linkage", "correct": False,
         "confidence": "sure"},
        {"idx": 2, "kind": "choice", "question": "no rating", "correct": True,
         "rated_after": True},
    ])
    late = dict(con.execute("SELECT idx, rated_after FROM study_attempts WHERE block='blk'"))
    assert late == {0: 1, 1: 0, 2: 0}, "a flag with no rating means nothing"
    assert "Missed while sure (2, 1 rated after seeing the answer)" in study.coverage_text("CSCI 103")
    study.record_attempts("blk", "CSCI 103", [
        {"idx": 0, "kind": "choice", "question": "heap vs stack", "correct": False,
         "confidence": "sure"}])
    assert con.execute("SELECT rated_after FROM study_attempts WHERE block='blk' AND idx=0"
                       ).fetchone()[0] == 0


def test_attempts_route(router_client, freeze_day):
    from app.routes import study as study_routes
    c = router_client(study_routes.router)
    r = c.post("/api/study/attempts", json={"block": "x1", "course": "CSCI 103", "items": [
        {"idx": 0, "kind": "free", "question": "what", "correct": False}]})
    assert r.status_code == 200 and r.json() == {"kept": 1}
    assert c.post("/api/study/attempts", json={"block": " ", "items": []}).status_code == 400
