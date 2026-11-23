"""Study mode: where a course is right now, and what the user got wrong.

Tutoring is the console's heaviest use — proofs for CSCI 170, C++ for 103,
exam review — and it had two blind spots. It didn't know where in the term the
material sat ("is this on the test?", "did I miss a lecture?" were answered by
grepping, or by guessing), and nothing outlived a session of what the user had
got wrong, so "review everything I've missed" had nothing to review.

**Context** is read off the syllabus, never written anywhere. The extracted
text under `syllabi/text/` is the ground: its dated rows ("10/1, 10/5
Recursion"), its week blocks ("Week 7 / 10/05-09 / ..."), and any line that
names an exam next to a date. Layout is lost in extraction, so this is
deliberately a *reader* rather than a parser of record — every row it returns
is the syllabus's own words with a date resolved beside them, and a syllabus
it can't date (CSCI 103's table comes out as two unaligned columns) falls back
to the course note's curated tables, then to the verbatim schedule text.
Nothing is ever invented to fill a gap: no rows is reported as no rows.

**The review log** is vault markdown — one note per course, `type:
course-review`, found through index.db's frontmatter exactly like the course
notes themselves, so the user can read it in Obsidian, annotate it, and move it
out of `06-Generated/` without the console losing it. A mistake is the user's
study material, not a fact about their life (that's the Brain) and not a row
any card needs to join against, so there's no table and no second copy. Each
entry is a `###` heading plus a few `- **Key:** value` bullets; anything else
the user writes inside an entry is kept and read back as notes.
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta
from pathlib import Path

import frontmatter

from . import courses, db, indexer
from .config import GENERATED_DIR, VAULT_PATH

# ---------- dates ----------

_MONTH_WORDS = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
    "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7,
    "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9, "oct": 10,
    "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12,
}
# Which months a term's dates can fall in. A date outside them is noise —
# "24/7" in a support-line blurb, a fraction, a phone extension — and the term
# filter is what lets the patterns below stay loose.
_TERM_MONTHS = {"fall": range(8, 13), "spring": range(1, 6), "summer": range(5, 9)}

_DASH = r"[-–—−]"
# 8/24, 9/28-10/02, 10/05−09, 12/1/2026. A range is one token: "week 6
# (9/28–10/2)" is a week, and reading it as two point dates is how a midterm
# would land on both ends of it.
_SLASH = re.compile(
    rf"(?<![\d/.])(\d{{1,2}})/(\d{{1,2}})(?:/(\d{{4}}|\d{{2}}))?(?![\d/])"
    rf"(?:\s*{_DASH}\s*(?:(\d{{1,2}})/)?(\d{{1,2}})(?![\d/]))?")
# Tue Oct 6, Tuesday, October 6th, Sept. 24th, Saturday December 12th. The
# month word is checked against _MONTH_WORDS afterwards, so "Marx 2" isn't March.
_NAMED = re.compile(r"\b([A-Z][a-z]{2,8})\.?\s+(\d{1,2})(?:st|nd|rd|th)?\b(?!\s*[%:/])")


def _term_year(term: str) -> tuple[str, int]:
    season, _, year = (term or "").partition("-")
    try:
        return season.lower(), int(year)
    except ValueError:
        today = date.fromisoformat(db.day_key())
        return season.lower(), today.year


def _mkdate(year: int, month: int, day: int, months) -> date | None:
    if month not in months:
        return None
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _dates_in(text: str, term: str) -> list[tuple[date, date | None, int]]:
    """(day, range_end or None, position) for every term date in `text`."""
    season, year = _term_year(term)
    months = _TERM_MONTHS.get(season, range(1, 13))
    out = []
    for m in _SLASH.finditer(text):
        y = int(m.group(3)) if m.group(3) else year
        y = y + 2000 if y < 100 else y
        d = _mkdate(y, int(m.group(1)), int(m.group(2)), months)
        if d is None:
            continue
        end = None
        if m.group(5):
            em = int(m.group(4)) if m.group(4) else d.month
            end = _mkdate(y, em, int(m.group(5)), range(1, 13))
            if end is not None and end < d:     # "11/30-12/04" written "11/30-04"
                end = _mkdate(y, em + 1 if em < 12 else 12, int(m.group(5)), range(1, 13))
        out.append((d, end, m.start()))
    for m in _NAMED.finditer(text):
        month = _MONTH_WORDS.get(m.group(1).lower())
        if month is None:
            continue
        d = _mkdate(year, month, int(m.group(2)), months)
        if d is not None:
            out.append((d, None, m.start()))
    out.sort(key=lambda t: t[2])
    return out


def _monday(d: date) -> date:
    return d - timedelta(days=d.weekday())


# ---------- finding the files ----------

def _notes_of_type(kind: str) -> list[tuple[str, dict]]:
    with indexer.session() as con:
        rows = con.execute("SELECT path, frontmatter FROM notes ORDER BY path").fetchall()
    out = []
    for row in rows:
        meta = json.loads(row["frontmatter"] or "{}")
        if meta.get("type") == kind:
            out.append((row["path"], meta))
    return out


def _syllabus_text(code: str, term: str) -> Path | None:
    """The extracted-text note for a course (`type: syllabus-text`)."""
    want = courses.course_key(code)
    hits = [(p, m) for p, m in _notes_of_type("syllabus-text")
            if courses.course_key(m.get("code") or "") == want]
    hits.sort(key=lambda pm: pm[1].get("term") != term)    # this term first
    for rel, _ in hits:
        path = VAULT_PATH / rel
        if path.exists():
            return path
    return None


def _body(path: Path | None) -> str:
    if path is None:
        return ""
    try:
        return frontmatter.load(path).content
    except Exception:  # unreadable syllabus text is "no syllabus"
        return ""


# ---------- the schedule ----------

_ROW = re.compile(
    rf"^\s*(?P<dates>\d{{1,2}}/\d{{1,2}}(?:\s*(?:,|{_DASH}|and)\s*\d{{1,2}}(?:/\d{{1,2}})?)*)"
    r"\s+(?P<rest>\S.*?)\s*$")
_CELL_DATE = re.compile(
    rf"^\d{{1,2}}/\d{{1,2}}(?:\s*(?:,|{_DASH})\s*\d{{1,2}}(?:/\d{{1,2}})?)*$")
_WEEK = re.compile(r"^\s*Week\s*(\d{1,2})\b(?P<rest>.*)$", re.I)
_WEEK_START = re.compile(r"(?<!\d)(\d{1,2})[/-](\d{1,2})(?!\d)")


def _span(dates_text: str, term: str) -> tuple[date, date] | None:
    """First and last day named in a row's date cell: "10/19–10/27",
    "8/24, 8/25", "11/2–11/5"."""
    season, year = _term_year(term)
    months = _TERM_MONTHS.get(season, range(1, 13))
    days: list[date] = []
    month = None
    for tok in re.split(rf"\s*(?:,|{_DASH}|and)\s*", dates_text.strip()):
        if "/" in tok:
            mm, dd = tok.split("/")[:2]
            month = int(mm)
        elif tok.isdigit() and month is not None:
            dd = tok
        else:
            continue
        d = _mkdate(year, month, int(dd), months)
        if d is not None:
            days.append(d)
    return (min(days), max(days)) if days else None


def _line_rows(text: str, term: str) -> list[dict]:
    rows = []
    for line in text.splitlines():
        m = _ROW.match(line)
        if m and (span := _span(m.group("dates"), term)):
            rows.append({"start": span[0], "end": span[1],
                         "when": m.group("dates").strip(), "text": m.group("rest")})
    return rows


def _table_rows(text: str, term: str) -> list[dict]:
    """Dated rows out of the course note's markdown tables — a date in one of
    the first two cells ("| 8/24, 8/25 | Introduction |", "| 6 | 10/2 | Files
    and Linux |")."""
    rows = []
    for line in text.splitlines():
        if not line.lstrip().startswith("|") or set(line.strip()) <= set("|-: "):
            continue
        cells = [c.replace("**", "").strip() for c in line.strip().strip("|").split("|")]
        for i, cell in enumerate(cells[:2]):
            if _CELL_DATE.match(cell) and (span := _span(cell, term)):
                rest = " · ".join(c for j, c in enumerate(cells) if j != i and c)
                rows.append({"start": span[0], "end": span[1], "when": cell, "text": rest})
                break
    return rows


def _week_blocks(text: str, term: str) -> list[dict]:
    """"Week 7 / 10/05−09 / ..." blocks — MATH 229's and ENGR 102's shape. The
    start date is all that's trusted (ENGR's end date is split across lines),
    so a block runs a week from it."""
    season, year = _term_year(term)
    months = _TERM_MONTHS.get(season, range(1, 13))
    lines = text.splitlines()
    heads = [i for i, ln in enumerate(lines) if _WEEK.match(ln)]
    # The last block has no next header to stop at, and runs straight on into
    # the grading policy — where MATH 229's "quizzes" once became an exam. A
    # typical block's length is the best stop there is.
    gaps = sorted(b - a for a, b in zip(heads, heads[1:]))
    typical = gaps[len(gaps) // 2] if gaps else 12
    blocks = []
    for n, i in enumerate(heads):
        stop = heads[n + 1] if n + 1 < len(heads) else min(len(lines), i + typical)
        start, body_from = None, None
        for j in range(i, min(i + 3, stop)):
            probe = _WEEK.match(lines[j]).group("rest") if j == i else lines[j]
            m = _WEEK_START.search(probe)
            if m and (d := _mkdate(year, int(m.group(1)), int(m.group(2)), months)):
                start, body_from = d, j + 1
                break
        if start is None:
            continue
        body = " ".join(" ".join(lines[body_from:stop]).split())
        # Column-wrapped extraction: "Artificial In- telligence", and the tail
        # of a date range split onto the next line ("8-24 thru 8-" / "28").
        body = re.sub(r"(\w)- ([a-z])", r"\1\2", body)
        body = re.sub(rf"^\d{{1,2}}(?:\s*{_DASH}\s*\d{{1,2}})?\s+", "", body)
        blocks.append({"start": start, "end": start + timedelta(days=6),
                       "when": f"Week {_WEEK.match(lines[i]).group(1)}", "text": body})
    return blocks


def schedule(code: str) -> dict:
    """The course's dated schedule, and where it was read from.

    Syllabus first, because it's the ground truth; the course note's tables
    only when the syllabus text has nothing datable (they were curated from
    the same PDF, so they're the next-best reading of it). Three rows is the
    bar — one stray "Week 3" mention isn't a schedule."""
    note_path, post = courses.find_course(code)
    meta = post.metadata
    term = meta.get("term") or courses.current_term()
    syl = _syllabus_text(meta.get("code") or code, term)
    text = _body(syl)
    rows, source = [], None
    for found, label in ((_week_blocks(text, term), "syllabus"),
                         (_line_rows(text, term), "syllabus"),
                         (_table_rows(post.content, term) + _line_rows(post.content, term),
                          "course note")):
        if len(found) >= 3:
            rows, source = found, label
            break
    rows.sort(key=lambda r: (r["start"], r["end"]))
    return {"code": meta.get("code") or code, "title": meta.get("title"),
            "term": term, "note": note_path, "syllabus": syl, "syllabus_body": text,
            "note_body": post.content, "rows": rows, "source": source}


# ---------- exams ----------

# "Final Presentations", "Final Project Reports", "Final exam review", "Exam
# Information" — the word is there and the exam isn't. Struck out before the
# search rather than excluded by lookahead, which backtracks "Final exam
# review" into a match on "Final".
_NOT_EXAM = re.compile(
    r"\b(?:final|midterm|exam)s?(?:\s+exams?)?\s+(?:review|project|presentation|"
    r"report|portfolio|grade|score|information|period|week)\w*", re.I)
_EXAM = re.compile(r"\b(midterm(?:\s+exam)?(?:\s*#?\d)?|final(?:\s+exam)?|"
                   r"exams?(?:\s*#?\d)?|quiz(?:zes)?(?:\s*#?\d)?)\b", re.I)
_BLOCK_EXAM = re.compile(r"\b(midterm(?:\s+exam)?(?:\s*#?\d)?|final\s+exam|"
                         r"exam(?:\s*#?\d)?|quiz(?:\s*#?\d)?)\b", re.I)


def _is_exam_row(r: dict) -> bool:
    """A schedule row that *is* an exam ("10/6 7pm Midterm 1"), as opposed to
    a week block that merely contains one beside its lectures."""
    return not r["when"].startswith("Week") and bool(
        _EXAM.search(_NOT_EXAM.sub(" ", r["text"])))


def _units(text: str) -> list[str]:
    """Lines, with the paragraph-per-page extractions (CSCI 103's whole page
    is one line) broken into sentences so a date is read beside its own
    sentence's words."""
    out = []
    for line in text.splitlines():
        if len(line) > 300:
            # Only before a capital: "Sept. 24th, Oct. 16th" is one sentence.
            out.extend(s for s in re.split(r"(?<=[.!?])\s+(?=[A-Z])|\s{2,}", line)
                       if s.strip())
        else:
            out.append(line)
    return out


def _exam_label(found: str) -> str:
    words = " ".join(found.split())
    words = re.sub(r"(?i)^exams$", "Exam", words)    # one date of "the exams ... on"
    return words[:1].upper() + words[1:]


def _candidates(text: str, term: str, origin: str) -> list[dict]:
    out = []
    units = _units(text)
    for i, unit in enumerate(units):
        clean = _NOT_EXAM.sub(" ", unit)
        dates = _dates_in(unit, term)
        if not dates:
            continue
        m = _EXAM.search(clean)
        # A sentence wrapped onto the next line ("...scheduled on Wednesday,
        # September 30 (Exam 1)" / "and Wednesday, November 4 (Exam 2)...")
        # borrows the previous line's keyword — but a schedule row never does,
        # or the lecture row after "10/6 7pm Midterm 1" becomes an exam too.
        if m is None and i and not _ROW.match(unit):
            m = _EXAM.search(_NOT_EXAM.sub(" ", units[i - 1]))
        if m is None:
            continue
        quote = " ".join(unit.split())
        for d, end, _ in dates:
            out.append({"date": d, "end": end, "label": _exam_label(m.group(1)),
                        "quotes": [(origin, quote)]})
    return out


def exams(sched: dict) -> list[dict]:
    """Every exam the syllabus or the note dates, merged by day.

    A week-range mention ("Midterm 1 falls in week 6 (9/28–10/2)", a week
    block holding "Midterm Exam 1") is only an approximate date, and is folded
    into an exact one inside it rather than listed beside it."""
    term = sched["term"]
    cands = (_candidates(sched["syllabus_body"], term, "syllabus")
             + _candidates(sched["note_body"], term, "note"))
    for r in sched["rows"]:
        # Not a bare "final" here: in a week of course plans it is final
        # presentations, final projects, "FINAL- Team Report" (ENGR 102).
        if r["when"].startswith("Week") and (m := _BLOCK_EXAM.search(_NOT_EXAM.sub(" ", r["text"]))):
            cands.append({"date": r["start"], "end": r["end"], "label": _exam_label(m.group(1)),
                          "quotes": [("syllabus", f'{r["when"]}: {r["text"][:160]}')]})
    exact = {}
    for c in (c for c in cands if c["end"] is None):
        have = exact.setdefault(c["date"], {**c, "quotes": []})
        if _better(c["label"], have["label"]):
            have["label"] = c["label"]
        have["quotes"].extend(q for q in c["quotes"] if q not in have["quotes"])
    out = list(exact.values())
    for c in (c for c in cands if c["end"] is not None):
        inside = [e for e in out if c["date"] <= e["date"] <= c["end"]]
        if inside:
            continue
        out.append({**c, "approx": True})
    for e in out:
        e.setdefault("approx", False)
        # The syllabus's own words first; at most two, a quote is evidence
        # rather than a transcript.
        e["quotes"] = sorted(e["quotes"], key=lambda q: q[0] != "syllabus")[:2]
    out.sort(key=lambda e: e["date"])
    return out


def _better(new: str, old: str) -> bool:
    """'Midterm 1' over 'Exam', 'Final Exam' over 'Final'."""
    def score(s: str) -> tuple:
        s = s.lower()
        return (any(ch.isdigit() for ch in s), "midterm" in s or "final" in s, len(s))
    return score(new) > score(old)


# ---------- course_context ----------

def term_start(term: str) -> date | None:
    """Monday of the term's first scheduled day, across every course note in
    the term — one course whose syllabus can't be dated still gets a week."""
    firsts = []
    for _, meta in _notes_of_type("course"):
        if meta.get("term") != term or not meta.get("code"):
            continue
        try:
            rows = schedule(meta["code"])["rows"]
        except courses.CourseError:
            continue
        if rows:
            firsts.append(rows[0]["start"])
    return _monday(min(firsts)) if firsts else None


def _rel(path: Path | None) -> str:
    if path is None:
        return "none"
    try:
        return str(path.relative_to(VAULT_PATH))
    except ValueError:
        return str(path)


def _when(d: date, today: date) -> str:
    n = (d - today).days
    if n == 0:
        return "today"
    if n == 1:
        return "tomorrow"
    if n == -1:
        return "yesterday"
    return f"in {n} days" if n > 0 else f"{-n} days ago"


def _excerpt(text: str, chars: int = 1600) -> str:
    """The syllabus's schedule section verbatim, for a syllabus nothing above
    could date — from the first "Week 1" (or "schedule") to a size cap."""
    m = re.search(r"^\s*Week\s*1\b", text, re.I | re.M)
    if not m:
        return ""
    start = text.rfind("\n", 0, max(0, m.start() - 1))
    out = []
    for line in text[start + 1:start + 1 + chars].splitlines():
        # A page footer or a paragraph-length line is the policy text after
        # the table, not more of it.
        if re.match(r"^\s*Page \d+\s*$", line) or len(line) > 300:
            break
        out.append(line.rstrip())
    return "\n".join(out).strip()


def context(code: str, query: str | None = None) -> str:
    sched = schedule(code)
    today = date.fromisoformat(db.day_key())
    start = term_start(sched["term"])
    head = f'{sched["code"]} — {sched["title"] or ""} ({sched["term"]})'.rstrip()
    lines = [head]
    if start and start <= today:
        week = (today - start).days // 7 + 1
        lines.append(f"Today {today} ({today:%a}), week {week} of term "
                     f"(week 1 began {start}).")
    else:
        lines.append(f"Today {today} ({today:%a}).")
    lines.append(f'Note: {_rel(sched["note"])} · syllabus text: {_rel(sched["syllabus"])}')

    found = exams(sched)
    lines.append("")
    if found:
        lines.append("Exams (dates read off the syllabus/note; quotes verbatim):")
        prev = start
        for e in found:
            when = (f'week of {e["date"]}' if e["approx"]
                    else f'{e["date"]} {e["date"]:%a}')
            lines.append(f'- {e["label"]}: {when} ({_when(e["date"], today)})')
            for origin, q in e["quotes"]:
                lines.append(f'    {origin}: "{q[:200]}"')
            if e["date"] >= today and sched["rows"]:
                # What the schedule puts between the previous exam and this
                # one. Stated as schedule, not as coverage: a final is often
                # cumulative and CSCI 103's exams follow projects, so what an
                # exam *covers* is the syllabus's to say, quoted above.
                before = [r for r in sched["rows"]
                          if (prev is None or r["end"] >= prev)
                          and r["start"] < e["date"] and not _is_exam_row(r)]
                since = f"since {prev}" if prev else "so far"
                if before and before[0]["when"].startswith("Week"):
                    lines.append(f"    on the schedule before it ({since}): "
                                 f'{before[0]["when"]} … {before[-1]["when"]}')
                elif before:
                    lines.append(f"    on the schedule before it ({since}): "
                                 + " · ".join(_dedupe(r["text"][:60] for r in before)))
            prev = e["date"]
    else:
        lines.append("Exams: none dated in the syllabus text or the course note.")

    lines.append("")
    if sched["rows"]:
        lines.append(f'Schedule (from the {sched["source"]}; [x] past, [>] this '
                     f'week, [ ] ahead):')
        for r in sched["rows"]:
            mark = ("[x]" if r["end"] < _monday(today)
                    else "[>]" if r["start"] <= _monday(today) + timedelta(days=6)
                    else "[ ]")
            if r["end"] < today and mark == "[>]":
                mark = "[x]"
            text = r["text"] if len(r["text"]) <= 240 else r["text"][:240] + "…"
            lines.append(f'{mark} {r["when"]} — {text}')
    else:
        lines.append("Schedule: nothing in the syllabus text or the course note "
                     "carries dates.")
    # A syllabus that couldn't be dated may still list its topics week by week
    # (CSCI 103's lecture table comes out as two unaligned columns). Shown as
    # is, beside whatever the note could date — usually labs, not lectures.
    if sched["source"] != "syllabus" and (ex := _excerpt(sched["syllabus_body"])):
        lines.append("")
        lines.append("The syllabus's own schedule section, verbatim — it has no "
                     "dates, and extraction lost the layout, so its columns may "
                     "not line up; count weeks only where the text plainly "
                     "lists them in order:")
        lines.append(ex)

    if query:
        lines.append("")
        lines.extend(_search(sched, query))

    n, d = review_counts(sched["code"])
    lines.append("")
    lines.append(f"Review log: {n} item{'s' if n != 1 else ''}"
                 + (f", {d} due today — course_review reads them (due=true for "
                    "just those)." if n else "."))
    return "\n".join(lines)


def _dedupe(items) -> list[str]:
    out = []
    for s in items:
        s = " ".join(s.split())
        if s and (not out or out[-1] != s):
            out.append(s)
    return out


def _search(sched: dict, query: str) -> list[str]:
    """Sentences in the syllabus and note naming any of the query's words,
    each with the scheduled row it falls in when it has a date."""
    words = [w for w in re.split(r"[\s,|/]+", query.lower()) if len(w) >= 3]
    if not words:
        return [f"Search {query!r}: give at least one word of 3+ letters."]
    hits = []
    for origin, text in (("syllabus", sched["syllabus_body"]), ("note", sched["note_body"])):
        for unit in _units(text):
            low = unit.lower()
            if any(w in low for w in words):
                hits.append(f'- {origin}: "{" ".join(unit.split())[:220]}"')
    for r in sched["rows"]:
        if any(w in r["text"].lower() for w in words):
            hits.append(f'- scheduled {r["start"]}: {r["when"]} — {r["text"][:160]}')
    if not hits:
        return [f"Search {query!r}: not mentioned in the syllabus text or the course "
                "note. That says the syllabus doesn't name it — not that it isn't "
                "taught; lecture slides go further than a syllabus does."]
    return [f"Search {query!r}:"] + _dedupe(hits)[:15]


# ---------- the review log ----------

_ENTRY = re.compile(r"^###\s+(\d{4}-\d{2}-\d{2})\s+[—–-]\s+(.+?)\s*$", re.M)
_FIELD = re.compile(r"^\s*-\s+\*\*([A-Za-z ]+):\*\*\s*(.*)$")


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def _review_note(code: str) -> tuple[Path, str, dict] | None:
    """The existing review note for a course — wherever the user filed it."""
    want = courses.course_key(code)
    for rel, meta in _notes_of_type("course-review"):
        if courses.course_key(meta.get("code") or "") == want and (VAULT_PATH / rel).exists():
            return VAULT_PATH / rel, rel, meta
    return None


def _default_path(code: str) -> Path:
    return VAULT_PATH / GENERATED_DIR / f"{re.sub(r'[^A-Za-z0-9]+', '-', code).strip('-')}-review.md"


def _review_path(code: str) -> Path | None:
    hit = _review_note(code)
    if hit:
        return hit[0]
    # Not indexed yet (the watcher reindexes on a debounce) but already on disk.
    path = _default_path(code)
    return path if path.exists() else None


def _parse(text: str) -> list[dict]:
    """Entries out of the note's raw text, with their spans for rewriting."""
    out = []
    heads = list(_ENTRY.finditer(text))
    for n, m in enumerate(heads):
        end = heads[n + 1].start() if n + 1 < len(heads) else len(text)
        e = {"date": m.group(1), "concept": m.group(2), "fields": {}, "again": [],
             "reviewed": [], "events": [(m.group(1), "miss")],
             "notes": [], "span": (m.start(), end)}
        for line in text[m.end():end].splitlines():
            f = _FIELD.match(line)
            if f:
                key = f.group(1).strip().lower()
                if key == "again":
                    e["again"].append(f.group(2).strip())
                    e["events"].append((f.group(2).strip()[:10], "miss"))
                elif key == "reviewed":
                    e["reviewed"].append(f.group(2).strip())
                    e["events"].append((f.group(2).strip()[:10], "right"))
                else:
                    e["fields"][key] = f.group(2).strip()
            elif line.strip() and not line.startswith("#"):
                e["notes"].append(line.strip())
        out.append(e)
    return out


def entries(code: str) -> list[dict]:
    path = _review_path(code)
    if path is None:
        return []
    return _parse(path.read_text(encoding="utf-8"))


# ---------- spaced review ----------
#
# A logged miss used to sit in the note until someone thought to ask for it.
# Now each entry comes back on a widening schedule, worked out from the
# entry's own history — no table, no second copy: the miss dates are its
# heading and `Again:` lines, and each right answer in review is a
# `Reviewed:` line the console appends. A right answer moves it one step
# along INTERVALS; a miss (in review or anywhere) starts it over; past the
# last step it's learned and stops coming back.

INTERVALS = (1, 3, 7, 14)
# An exam this close makes every unlearned item due, schedule or not — the
# night before is not the time to wait for day 7.
CRAM_DAYS = 2


def _day(s: str) -> date | None:
    try:
        return date.fromisoformat(s[:10])
    except ValueError:
        return None


def schedule_of(e: dict, today: date, cram: bool = False) -> dict:
    """Where one entry is in its review schedule."""
    streak, last = 0, None
    for s, kind in e["events"]:
        d = _day(s)
        if d is None:
            continue
        if kind == "miss":
            streak, last = 0, d
        elif last is None or d > last:
            # A second right the same day isn't a second interval survived.
            streak, last = streak + 1, d
    if last is None:
        last = today
    learned = streak >= len(INTERVALS)
    next_at = None if learned else last + timedelta(days=INTERVALS[streak])
    due = not learned and (next_at <= today or (cram and last < today))
    return {"streak": streak, "learned": learned, "due": due,
            "next": next_at.isoformat() if next_at else None}


def _cramming(code: str, today: date) -> bool:
    nxt = _next_exam(code)
    return bool(nxt and 0 <= (nxt["date"] - today).days <= CRAM_DAYS)


def review_counts(code: str) -> tuple[int, int]:
    """(logged, due today) for a course."""
    items = entries(code)
    if not items:
        return 0, 0
    today = date.fromisoformat(db.day_key())
    cram = _cramming(code, today)
    return len(items), sum(schedule_of(e, today, cram)["due"] for e in items)


def _find_entry(text: str, concept: str) -> dict | None:
    items = _parse(text)
    want = _norm(concept)
    hit = next((e for e in items if _norm(e["concept"]) == want), None)
    if hit is None and want:
        loose = [e for e in items if want in _norm(e["concept"])]
        hit = loose[0] if len(loose) == 1 else None
    return hit


def mark_review(code: str, concept: str, right: bool, mistake: str | None = None) -> str:
    """Record one review of a logged concept: right moves it along the
    schedule, wrong logs it missed again and starts it over."""
    _, post = courses.find_course(code)
    code = post.metadata.get("code") or code
    path = _review_path(code)
    if path is None:
        raise courses.CourseError(f"{code}: nothing logged yet — nothing to review")
    text = path.read_text(encoding="utf-8")
    e = _find_entry(text, concept)
    if e is None:
        names = ", ".join(f'"{x["concept"]}"' for x in _parse(text)[:12])
        raise courses.CourseError(f'{code}: no logged concept "{concept}" — pass one '
                                  f"verbatim from course_review: {names}")
    today = db.day_key()
    a, b = e["span"]
    line = (f"- **Reviewed:** {today} — right" if right else
            f"- **Again:** {today} — {' '.join((mistake or 'missed in review').split())}")
    text = text[:a] + text[a:b].rstrip("\n") + "\n" + line + "\n\n" + text[b:].lstrip("\n")
    path.write_text(text.rstrip("\n") + "\n", encoding="utf-8")
    s = schedule_of(_find_entry(text, e["concept"]), date.fromisoformat(today))
    if s["learned"]:
        after = "learned — it won't come back"
    else:
        after = f"next review {s['next']} (step {s['streak'] + 1} of {len(INTERVALS)})"
    return f'{code}: "{e["concept"]}" {"right" if right else "missed again"} — {after}'


def _next_exam(code: str) -> dict | None:
    try:
        sched = schedule(code)
    except courses.CourseError:
        return None
    today = date.fromisoformat(db.day_key())
    return next((e for e in exams(sched) if e["date"] >= today), None)


def add_review(code: str, concept: str, mistake: str, correct: str, *,
               topic: str | None = None, session: str | None = None) -> str:
    concept = " ".join((concept or "").split())
    mistake = " ".join((mistake or "").split())
    correct = " ".join((correct or "").split())
    if not concept:
        raise courses.CourseError("pass concept — a few words naming the idea, "
                                  "e.g. 'size of a power set'")
    if not (mistake and correct):
        raise courses.CourseError("pass both mistake (what the user said or did) "
                                  "and correct (the right idea, in a sentence)")
    note_path, post = courses.find_course(code)
    code = post.metadata.get("code") or code
    term = post.metadata.get("term") or courses.current_term()
    today = db.day_key()

    path = _review_path(code)
    if path is None:
        path = _default_path(code)
        path.parent.mkdir(parents=True, exist_ok=True)
        fresh = frontmatter.Post(
            f"# {code} — review log\n\n"
            "What went wrong while studying, logged by Kestrel as it happened, "
            "oldest first. Edit freely: each `###` entry is read back by the "
            "console's `course_review`, and anything you add under one is kept "
            "as a note. Move this note anywhere — it's found by its frontmatter.\n",
            type="course-review", code=code, term=term,
            course=f"[[{note_path.stem}]]",
            tags=["usc", "review", term],
            # The provenance the PostToolUse hook stamps on a console Write;
            # this note is written from the backend, so the hook never sees it.
            origin="generated", generated_by="console",
            generated_at=datetime.now().isoformat(timespec="seconds"),
            reviewed=False)
        path.write_text(frontmatter.dumps(fresh, sort_keys=False) + "\n", encoding="utf-8")

    text = path.read_text(encoding="utf-8")
    # The same idea missed again is the strongest signal this log carries, so
    # it's counted on the first entry rather than filed as a second one that
    # would read as two unrelated things.
    same = next((e for e in _parse(text) if _norm(e["concept"]) == _norm(concept)), None)
    if same is not None:
        a, b = same["span"]
        block = text[a:b].rstrip("\n")
        block += f"\n- **Again:** {today} — {mistake}"
        text = text[:a] + block + "\n\n" + text[b:].lstrip("\n")
        path.write_text(text.rstrip("\n") + "\n", encoding="utf-8")
        times = len(same["again"]) + 2
        return (f'{code}: "{same["concept"]}" was already logged {same["date"]} — '
                f"marked again ({times} times now) in {_rel(path)}")

    entry = [f"### {today} — {concept}",
             f"- **Got wrong:** {mistake}",
             f"- **Correct:** {correct}"]
    if topic:
        entry.append(f"- **Topic:** {' '.join(topic.split())}")
    if nxt := _next_exam(code):
        entry.append(f'- **Before:** {nxt["label"]}, {nxt["date"]}')
    if session:
        entry.append(f"- **Session:** `{session}`")
    path.write_text(text.rstrip("\n") + "\n\n" + "\n".join(entry) + "\n", encoding="utf-8")
    return f'{code}: logged "{concept}" in {_rel(path)}'


def review_text(code: str, since: str | None = None, query: str | None = None,
                due: bool = False) -> str:
    _, post = courses.find_course(code)
    code = post.metadata.get("code") or code
    path = _review_path(code)
    if path is None:
        return f"{code}: nothing logged yet."
    items = _parse(path.read_text(encoding="utf-8"))
    total = len(items)
    today = date.fromisoformat(db.day_key())
    cram = _cramming(code, today)
    for e in items:
        e["sched"] = schedule_of(e, today, cram)
    if due:
        nxt = min((e["sched"]["next"] for e in items if not e["sched"]["learned"]), default=None)
        items = [e for e in items if e["sched"]["due"]]
        if not items:
            return (f"{code}: nothing due today ({total} logged"
                    + (f"; next due {nxt}" if nxt else "; all learned") + ").")
    if since:
        try:
            since = date.fromisoformat(since.strip()).isoformat()
        except ValueError:
            raise courses.CourseError(f"{since!r} isn't a date — pass YYYY-MM-DD")
        # An entry counts from its latest miss: a thing got wrong in week 2
        # and again yesterday is fresh material for this week's exam.
        items = [e for e in items
                 if max([e["date"]] + [a[:10] for a in e["again"]]) >= since]
    if query:
        words = _norm(query).split()
        items = [e for e in items if all(
            w in _norm(" ".join([e["concept"], *e["fields"].values(), *e["notes"]]))
            for w in words)]
    if not items:
        return f"{code}: {total} logged, none match ({_rel(path)})."
    # Missed most often first, then in the order the course taught them.
    items.sort(key=lambda e: (-len(e["again"]), e["date"]))
    head = f"{len(items)} due today of {total}" if due else f"{len(items)} of {total}"
    out = [f"{code}: {head} logged ({_rel(path)})"]
    if due and cram:
        out.append("(exam within 2 days: every unlearned item is due)")
    for e in items:
        times = f" (missed {len(e['again']) + 1}x)" if e["again"] else ""
        s = e["sched"]
        state = ("learned" if s["learned"] else "DUE" if s["due"] else f"next {s['next']}")
        state += f", {s['streak']}/{len(INTERVALS)} reviews right" if s["streak"] else ""
        f = e["fields"]
        out.append(f'- {e["date"]} {e["concept"]}{times} [{state}]')
        out.append(f'    wrong: {f.get("got wrong", "?")}')
        out.append(f'    right: {f.get("correct", "?")}')
        extra = [f"{k}: {v}" for k, v in f.items()
                 if k not in ("got wrong", "correct", "session")]
        if extra:
            out.append("    " + " · ".join(extra))
        for a in e["again"]:
            out.append(f"    again {a}")
        for note in e["notes"]:
            out.append(f"    note: {note}")
    return "\n".join(out)


# ---------- coverage: what has actually been drilled ----------
#
# The review log knows what went wrong; it can't say what was never asked.
# StudyBlock.jsx posts every answered quiz question and flashcard here, tagged
# with the page it was worked from, so "have I gone through Unit 2b?" and
# "where do my misses sit?" are answered from attempts rather than memory.

_PAGE_REF = re.compile(r"^\s*(d[0-9a-f]{5,10})#p(\d{1,4})\s*$", re.I)
KINDS = ("choice", "free", "card")
# StudyBlock.jsx's order. Anything else is "didn't say". Normally said before
# answering, so it can't be fitted to the verdict; `rated_after` marks the ones
# that weren't, and coverage counts them separately.
CONFIDENCE = ("dont_know", "guess", "unsure", "sure")


def _doc_page(con, ref: str | None) -> tuple[str | None, int | None]:
    m = _PAGE_REF.match(ref or "")
    if not m:
        return None, None
    row = con.execute("SELECT target FROM cite_refs WHERE ref = ? AND kind = 'doc'",
                      (m.group(1).lower(),)).fetchone()
    return (row["target"], int(m.group(2))) if row else (None, None)


def record_attempts(block: str, course: str | None, items: list[dict]) -> int:
    """Upsert one study block's answered items. Returns how many were kept."""
    block = (block or "").strip()[:64]
    if not block:
        raise ValueError("block is required")
    course = " ".join((course or "").split())[:40] or None
    day, at = db.day_key(), datetime.now().isoformat(timespec="seconds")
    n = 0
    with db.session() as con, con:
        for it in items:
            kind = it.get("kind")
            question = " ".join(str(it.get("question") or "").split())[:500]
            if kind not in KINDS or not question or not isinstance(it.get("idx"), int):
                continue
            sha, page = _doc_page(con, it.get("ref"))
            conf = it.get("confidence") if it.get("confidence") in CONFIDENCE else None
            con.execute(
                "INSERT INTO study_attempts (block, idx, day, at, course, kind, question,"
                " correct, self_graded, doc_sha, page, confidence, rated_after)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT (block, idx) DO UPDATE SET day=excluded.day, at=excluded.at,"
                " course=excluded.course, correct=excluded.correct,"
                " self_graded=excluded.self_graded, confidence=excluded.confidence,"
                " rated_after=excluded.rated_after",
                (block, it["idx"], day, at, course, kind, question,
                 1 if it.get("correct") else 0, 1 if it.get("self_graded") else 0,
                 sha, page, conf, 1 if conf and it.get("rated_after") else 0))
            n += 1
    return n


def _ranges(pages: list[int]) -> str:
    out, run = [], []
    for p in sorted(set(pages)):
        if run and p == run[-1] + 1:
            run.append(p)
        else:
            if run:
                out.append(f"{run[0]}" if len(run) == 1 else f"{run[0]}-{run[-1]}")
            run = [p]
    if run:
        out.append(f"{run[0]}" if len(run) == 1 else f"{run[0]}-{run[-1]}")
    return ",".join(out)


def coverage_text(code: str, since: str | None = None) -> str:
    _, post = courses.find_course(code)
    code = post.metadata.get("code") or code
    want = courses.course_key(code)
    with db.session() as con:
        rows = [r for r in con.execute(
                    "SELECT a.*, d.filename, d.page_count FROM study_attempts a"
                    " LEFT JOIN docs d ON d.sha = a.doc_sha"
                    " WHERE (? IS NULL OR a.day >= ?) ORDER BY a.day",
                    (since, since))
                if courses.course_key(r["course"] or "") == want]
        # Docs nothing has been asked from, by anyone: probably dropped for
        # some course and never drilled. Not filtered by course — a doc
        # doesn't know its course until a quiz is tagged with one.
        idle = con.execute(
            "SELECT filename, page_count, added_at FROM docs d WHERE mime = 'application/pdf'"
            " AND NOT EXISTS (SELECT 1 FROM study_attempts a WHERE a.doc_sha = d.sha)"
            " ORDER BY opened_at DESC LIMIT 10").fetchall()
    if not rows:
        out = [f"{code}: no quiz or card answers recorded"
               + (f" since {since}" if since else "") + "."]
    else:
        right = sum(r["correct"] for r in rows)
        out = [f"{code}: {len(rows)} answered, {right} right "
               f"({round(100 * right / len(rows))}%), first {rows[0]['day']}, "
               f"last {rows[-1]['day']}."]
        # Confidence splits right/wrong the way the totals can't: a miss
        # they were sure of is a misconception to undo, a right they guessed
        # is a pass that won't hold. Only rows that were asked (see CONFIDENCE).
        sure_miss = [r for r in rows if not r["correct"] and r["confidence"] == "sure"]
        shaky = [r for r in rows if r["correct"] and r["confidence"] in ("guess", "unsure")]
        late = lambda rs: (f", {n} rated after seeing the answer"
                           if (n := sum(r["rated_after"] for r in rs)) else "")
        if sure_miss:
            out.append(f"Missed while sure ({len(sure_miss)}{late(sure_miss)}) — likely misconceptions: "
                       + "; ".join(r["question"][:90] for r in sure_miss[-5:]))
        if shaky:
            out.append(f"Right but guessed or unsure ({len(shaky)}{late(shaky)}) — not yet known: "
                       + "; ".join(r["question"][:90] for r in shaky[-5:]))
        by_doc: dict[str, list] = {}
        loose = []
        for r in rows:
            (by_doc.setdefault(r["doc_sha"], []) if r["doc_sha"] else loose).append(r)
        for sha, rs in sorted(by_doc.items(), key=lambda kv: kv[1][-1]["day"], reverse=True):
            name, total = rs[0]["filename"] or sha[:8], rs[0]["page_count"]
            pages = [r["page"] for r in rs if r["page"]]
            ok = sum(r["correct"] for r in rs)
            missed = [r["page"] for r in rs if not r["correct"] and r["page"]]
            out.append(f"- {name}: {len(rs)} answered, {ok} right, last {rs[-1]['day']}")
            out.append(f"    pages asked from: {_ranges(pages) or '—'}"
                       + (f" of {total}" if total else ""))
            if total:
                gaps = [p for p in range(1, total + 1) if p not in set(pages)]
                out.append(f"    never asked: {_ranges(gaps) or 'none'}")
            if missed:
                out.append(f"    misses on pages: {_ranges(missed)}")
        if loose:
            ok = sum(r["correct"] for r in loose)
            out.append(f"- not tied to a document: {len(loose)} answered, {ok} right")
    if idle:
        out.append("Dropped documents never quizzed (any course): "
                   + "; ".join(f'{r["filename"]} ({r["page_count"]}p, added {r["added_at"][:10]})'
                               for r in idle))
    out.append("Page gaps are pages no question was tied to — title and section "
               "slides included, so judge them by what's on them.")
    return "\n".join(out)


# ---------- the desk ----------

def desk(term: str | None = None) -> list[dict]:
    """Per course: the next exam inside two weeks and how many review items
    are logged. Feeds /api/courses; only courses with either thing appear."""
    out = []
    today = date.fromisoformat(db.day_key())
    want = term or courses.current_term()
    for _, meta in _notes_of_type("course"):
        code = meta.get("code")
        if not code or (meta.get("term") and meta.get("term") != want):
            continue
        try:
            nxt = _next_exam(code)
            n, due = review_counts(code)
        except courses.CourseError:
            continue
        soon = nxt if nxt and (nxt["date"] - today).days <= 14 else None
        if not (soon or n):
            continue
        out.append({"code": code, "review_items": n, "review_due": due,
                    "exam": ({"label": soon["label"], "date": soon["date"].isoformat(),
                              "approx": soon["approx"],
                              "in_days": (soon["date"] - today).days}
                             if soon else None)})
    out.sort(key=lambda c: (c["exam"] is None, (c["exam"] or {}).get("in_days", 0), c["code"]))
    return out
