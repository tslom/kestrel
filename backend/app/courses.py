"""Course reference — office hours, instructors, syllabus links.

The vault stays the source of truth: one note per course under
`02-Areas/usc/<term>/` with `type: course` frontmatter. There's no table and
no migration here — index.db already stores frontmatter as JSON, so a course
you add or edit in Obsidian shows up on the next reindex.

The only computed thing is *when*: office hours are written as "Mon 09:30-10:30",
and the point of the widget is to answer "is anyone in right now, and if not,
who's next" without you doing that arithmetic in your head.

Professors amend hours for one week without changing the standing rule, so an
entry may carry an `except:` list of dated overrides — see `_exceptions`. The
rule and the exception are both kept: the week view answers "when are 103's
hours", today's rows answer "can I walk in now", and those differ on exactly
the days that matter most.
"""

import json
import re
from datetime import date, datetime, timedelta
from pathlib import Path

import frontmatter

from . import db, indexer
from .config import VAULT_PATH

DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
_DAY_INDEX = {d.lower(): i for i, d in enumerate(DAYS)}
_DAY_INDEX.update({"tues": 1, "thur": 3, "thurs": 3, "weds": 2})

# "Mon 09:30-10:30", "Mon, Wed 14:00-16:00", "Tue & Thu 11:00-12:00".
_WHEN_RE = re.compile(
    r"^\s*(?P<days>[A-Za-z][A-Za-z,&/ ]*?)\s+"
    r"(?P<start>\d{1,2}:\d{2})\s*[-–—to]+\s*(?P<end>\d{1,2}:\d{2})\s*$"
)

# An exception's own `when` may drop the weekday — its `date` already fixes
# which day it is. "14:00-15:00" and "Tue 14:00-15:00" both parse.
_EXC_WHEN_RE = re.compile(
    r"^\s*(?:[A-Za-z][A-Za-z,&/ ]*?\s+)?"
    r"(?P<start>\d{1,2}:\d{2})\s*[-–—to]+\s*(?P<end>\d{1,2}:\d{2})\s*$"
)

# Aug–Dec is fall, Jan–May spring, Jun–Jul summer. Used only to pick which
# term's notes the widget shows once a second term's folder exists.
_SEASONS = [(1, "spring"), (6, "summer"), (8, "fall")]


def current_term(today: date | None = None) -> str:
    today = today or date.fromisoformat(db.day_key())
    season = [s for m, s in _SEASONS if today.month >= m][-1]
    return f"{season}-{today.year}"


def _minutes(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def _fmt(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def _exceptions(entry: dict) -> dict[str, dict]:
    """An entry's `except:` list, keyed by ISO date.

    A weekly `when` can't say "2pm this Tuesday only": amend the rule and it's
    wrong from next week, leave it and it's wrong tomorrow. A dated exception
    overrides the rule on exactly one date — new hours, or `cancelled: true`.
    Hours that *move* to another weekday are two exceptions, a cancellation on
    the old date and hours on the new one, because that is what happened.

    An exception whose `when` doesn't parse keeps the rule's hours and only
    annotates them, on the same principle as an unparseable `when` above: say
    the odd thing rather than drop the person holding the hours."""
    out: dict[str, dict] = {}
    for raw in entry.get("except") or []:
        if not isinstance(raw, dict):
            continue
        try:
            day = raw["date"]
            day = day.isoformat() if isinstance(day, date) else date.fromisoformat(str(day).strip()).isoformat()
        except (KeyError, ValueError, TypeError, AttributeError):
            continue  # undated exception can't apply to a date; drop it
        when = str(raw.get("when") or "").strip()
        m = _EXC_WHEN_RE.match(when)
        out[day] = {
            "date": day,
            "when": when,
            "start": _minutes(m.group("start")) if m else None,
            "end": _minutes(m.group("end")) if m else None,
            "cancelled": bool(raw.get("cancelled") or raw.get("canceled")),
            "note": raw.get("note"),
            # the room that day, when it isn't the usual one
            "where": str(raw.get("where") or "").strip() or None,
        }
    return out


def _effective(slot: dict, day: date) -> dict | None:
    """The slot's real hours on `day` — the rule unless an exception says
    otherwise. None when nobody is holding hours that day."""
    exc = slot["_exc"].get(day.isoformat())
    if exc:
        if exc["cancelled"]:
            return None
        start = slot["start"] if exc["start"] is None else exc["start"]
        end = slot["end"] if exc["end"] is None else exc["end"]
        if start is None:
            return None  # note-only exception on hours that have no time yet
        return {"start": start, "end": end, "exception": exc}
    if slot["day_i"] is None or day.weekday() != slot["day_i"]:
        return None
    return {"start": slot["start"], "end": slot["end"], "exception": None}


def _public(slot: dict, **over) -> dict:
    """A slot as the API returns it — overrides applied, working state gone."""
    out = {**slot, **over}
    out.pop("_exc", None)
    return out


def _label(slot: dict) -> str:
    return (f"{slot['day']} {_fmt(slot['start'])}–{_fmt(slot['end'])}"
            if slot["start"] is not None else slot["when"])


def _slots(entry: dict, code: str, title: str) -> list[dict]:
    """One office-hours entry -> one slot per weekday it covers.

    An unparseable `when` (a "TBA", a shape we haven't seen) still comes back
    as a slot with day_i None, so the card shows the text instead of dropping
    the person silently."""
    when = str(entry.get("when") or "").strip()
    base = {
        "code": code,
        "course": title,
        "who": entry.get("who"),
        "role": entry.get("role"),
        "where": entry.get("where"),
        "when": when,
        "_exc": _exceptions(entry),
    }
    m = _WHEN_RE.match(when)
    if not m:
        return [{**base, "day_i": None, "day": None, "start": None, "end": None}]
    start, end = _minutes(m.group("start")), _minutes(m.group("end"))
    out = []
    for token in re.split(r"[,&/]| and ", m.group("days")):
        t = token.strip().lower()
        # Mon is index 0, so `or` would swallow it — check membership, not truth.
        i = _DAY_INDEX.get(t[:5], _DAY_INDEX.get(t[:3]))
        if i is None:
            continue
        out.append({**base, "day_i": i, "day": DAYS[i], "start": start, "end": end})
    return out or [{**base, "day_i": None, "day": None, "start": None, "end": None}]


def _next_occurrence(slot: dict, now: datetime) -> tuple[datetime, dict] | None:
    """When this slot next opens, at or after `now`, and with which hours.

    Walks dates rather than jumping to the rule's weekday, because exceptions
    make the two different: a cancelled week has to be skipped rather than
    promised, and hours moved onto another day have to be found there.

    Three weeks, not the one a weekly rule needs: a cancellation pushes the
    next occurrence past the 7-day mark, and a horizon that stopped there
    would report no upcoming hours at all for the week they're called off."""
    for ahead in range(22):
        d = now.date() + timedelta(days=ahead)
        eff = _effective(slot, d)
        if not eff:
            continue
        opens = datetime.combine(d, datetime.min.time()) + timedelta(minutes=eff["start"])
        if opens >= now:
            return opens, eff
    return None


def load(term: str | None = None) -> dict:
    """Every course note for the term, with its office hours placed in the week.

    `day_key()` (not date.today()) decides which weekday counts as today, so
    the card agrees with the rest of the desk across the 4am rollover. "Open
    now" is still measured against the wall clock — nobody holds hours at 3am.
    """
    now = datetime.now()
    today_key = db.day_key()
    today_date = date.fromisoformat(today_key)
    today_i = today_date.weekday()
    midnight = datetime.combine(today_date, datetime.min.time())

    rows = []
    with indexer.session() as con:
        for row in con.execute("SELECT path, title, frontmatter FROM notes ORDER BY path"):
            meta = json.loads(row["frontmatter"] or "{}")
            if meta.get("type") == "course":
                rows.append((row["path"], meta))

    # Only filter by term when the term actually has notes — mid-August the
    # calendar says fall while the vault may still only hold last spring.
    want = term or current_term()
    if any(m.get("term") == want for _, m in rows):
        rows = [(p, m) for p, m in rows if m.get("term") == want]

    courses, slots = [], []
    for path, meta in rows:
        code = meta.get("code") or meta.get("title") or path
        title = meta.get("title") or code
        hours = []
        for entry in meta.get("office_hours") or []:
            if isinstance(entry, dict):
                hours.extend(_slots(entry, code, title))
        for s in hours:
            exc_today = s["_exc"].get(today_key)
            eff = _effective(s, today_date)
            # On today's board if the rule lands today *or* an exception is
            # dated today — a cancellation belongs on the card as much as
            # hours do, since "don't walk over" is the same decision.
            s["today"] = eff is not None or exc_today is not None
            if exc_today and exc_today["cancelled"]:
                s["state"] = "cancelled"
            elif eff is None:
                s["state"] = "unknown" if s["day_i"] is None else "other-day"
            else:
                # Against the slot's real datetime, not the bare clock: inside
                # the 4am rollover "today" is yesterday, and comparing 01:00 to
                # a 09:30 slot would call yesterday morning's hours upcoming.
                opens = midnight + timedelta(minutes=eff["start"])
                closes = midnight + timedelta(minutes=eff["end"])
                s["state"] = "now" if opens <= now < closes else "past" if now >= closes else "later"
            # The week view keeps showing the *rule*, annotated with the next
            # exception rather than quietly swapping its hours in: someone
            # opening the week asks "when are 103's hours", and the standing
            # answer with the one-off called out beside it is that answer.
            s["exception"] = next(
                (e for k, e in sorted(s["_exc"].items()) if k >= today_key), None)
            s["label"] = _label(s)
        hours.sort(key=lambda s: (s["day_i"] is None, s["day_i"] or 0, s["start"] or 0))
        slots.extend(hours)
        courses.append({
            "code": code,
            "title": title,
            "path": path,
            "units": meta.get("units"),
            "section": meta.get("section"),
            "instructor": meta.get("instructor"),
            "instructor_email": meta.get("instructor_email"),
            "lms": meta.get("lms"),
            "office_hours": hours,
        })
    courses.sort(key=lambda c: c["code"])

    # Today's rows carry the hours actually being held, where the week view
    # above carries the rule. Same slot, two questions.
    today = []
    for s in slots:
        if not s["today"]:
            continue
        eff = _effective(s, today_date)
        row = _public(s, day_i=today_i, day=DAYS[today_i],
                      exception=s["_exc"].get(today_key))
        if eff:
            row["start"], row["end"] = eff["start"], eff["end"]
            exc = s["_exc"].get(today_key)
            if exc and exc.get("where"):
                row["where"] = exc["where"]
            # The weekday it normally falls on, when that isn't today — the row
            # overwrites `day` with today's, so this is the only thing left
            # that can say a Tuesday block is being held on a Wednesday. None
            # (not False) when it's where it belongs, so the card can print it.
            row["moved_from"] = (s["day"] if s["day_i"] is not None
                                 and s["day_i"] != today_i else None)
        row["label"] = _label(row)
        today.append(row)
    # A cancelled slot keeps the rule's start so it sorts into place, and a
    # dated exception on TBA hours has no start at all — neither could reach
    # the old bare `s["start"]` key, both can now.
    today.sort(key=lambda s: (s["start"] is None, s["start"] or 0))
    open_now = [s for s in today if s["state"] == "now"]

    upcoming = []
    for s in slots:
        if hit := _next_occurrence(slot=s, now=now):
            upcoming.append((hit[0], s, hit[1]))
    nxt = None
    if upcoming:
        when, slot, eff = min(upcoming, key=lambda t: t[0])
        nxt = _public(slot, start=eff["start"], end=eff["end"],
                      day_i=when.weekday(), day=DAYS[when.weekday()],
                      exception=eff["exception"],
                      at=when.isoformat(timespec="minutes"),
                      in_min=int((when - now).total_seconds() // 60))
        nxt["label"] = _label(nxt)

    for s in slots:
        s.pop("_exc", None)  # parsed exceptions are working state, not payload

    return {
        "day": today_key,
        "term": want,
        "weekday": DAYS[today_i],
        "courses": courses,
        "today": today,
        "open_now": open_now,
        "next": nxt,
        "unscheduled": [c["code"] for c in courses if not c["office_hours"]],
    }


# ---------- writing ----------
#
# Everything above reads. These write, and they exist because the amendments
# arrive by mail: "hours are 2pm this week", "no hours Thursday, I'm away".
# Retyping those into Obsidian is exactly the chore that leaves the card
# stale, and a stale office-hours card is worse than none — you walk over.
#
# The note stays the source of truth, so these edit the note and reindex; they
# never write a table. What they add over the console's plain Edit tool is the
# schema: the weekly `when` grammar the card can actually place, dates that
# resolve, and an entry matched to the right person before it's overwritten.

# What may be amended. Deliberately closed: these are the syllabus facts the
# card reads. The rest of the note — the schedule table, your own notes — is
# yours, and a writer that could rewrite any key would eventually rewrite one.
EDITABLE = ("instructor", "instructor_email", "lms", "syllabus", "section",
            "units", "title")


class CourseError(ValueError):
    """A problem to report to the user rather than retry."""


def course_key(code: str) -> str:
    """'CSCI 103', 'csci-103' and 'CSCI103' are the same course."""
    return re.sub(r"[^a-z0-9]", "", str(code or "").lower())


def known_codes(term: str | None = None) -> list[str]:
    """Course codes in the vault for the term, exactly as written there.

    Exists so the office-hours mail extractor can be *given* the valid codes instead of
    being asked to produce them: a model left to guess writes "CS 103" for
    "CSCI 103", and the proposal then points at a note `find_course` can't open.
    Handing it the real list turns that whole class of miss into an honest
    `relevant: false`."""
    return [c["code"] for c in load(term)["courses"] if c.get("code")]


def find_course(code: str) -> tuple[Path, frontmatter.Post]:
    want = course_key(code)
    if not want:
        raise CourseError("which course? pass a code like 'CSCI 103'")
    hits = []
    with indexer.session() as con:
        for row in con.execute("SELECT path, frontmatter FROM notes ORDER BY path"):
            meta = json.loads(row["frontmatter"] or "{}")
            if meta.get("type") != "course":
                continue
            got = course_key(meta.get("code") or "")
            if got == want or (got and got.startswith(want)):
                hits.append((row["path"], meta))
    if not hits:
        raise CourseError(f"no course note for {code!r}")
    if len(hits) > 1:
        names = ", ".join(str(m.get("code") or p) for p, m in hits)
        raise CourseError(f"{code!r} matches several courses: {names}")
    path = VAULT_PATH / hits[0][0]
    if not path.exists():
        raise CourseError(f"{hits[0][0]} is indexed but missing on disk")
    return path, frontmatter.load(path)


def _save(path: Path, post: frontmatter.Post) -> None:
    """Rewrite the note and reindex it.

    `sort_keys=False` because python-frontmatter alphabetizes by default, and
    reordering every key of a hand-written note is not an edit anyone asked
    for. The reindex is the same full rebuild the file watcher would do a
    moment later — doing it here means the card is right before we answer."""
    path.write_text(frontmatter.dumps(post, sort_keys=False) + "\n", encoding="utf-8")
    indexer.rebuild()


def _hours_list(post: frontmatter.Post) -> list:
    hours = post.metadata.get("office_hours")
    if hours is None or hours == "":
        hours = []
    if not isinstance(hours, list):
        raise CourseError("this note's office_hours isn't a list — fix it in Obsidian first")
    post.metadata["office_hours"] = hours
    return hours


def _match(hours: list, who: str, slot: str | None) -> list[dict]:
    """Entries for `who`, narrowed by `slot` (a substring of the weekly when).

    One person often holds two blocks — Mon and Wed — so `who` alone is not an
    address, and silently amending the wrong one is the failure that matters
    here."""
    needle = (who or "").strip().lower()
    if not needle:
        raise CourseError("whose hours? pass the name as written on the note")
    words = set(re.findall(r"[a-z0-9']+", needle))

    # Substring as before ("halvorsen" finds Martin Halvorsen), or every word of
    # the name on file inside the one given: mail signs with a middle name,
    # and "Martin James Halvorsen" not matching "Martin Halvorsen" made an
    # office-hours move unapprovable (409 on every try).
    def same(on_file: str) -> bool:
        on_file = on_file.lower()
        file_words = set(re.findall(r"[a-z0-9']+", on_file))
        return needle in on_file or bool(file_words) and file_words <= words

    out = [e for e in hours if isinstance(e, dict) and same(str(e.get("who") or ""))]
    if slot:
        s = slot.strip().lower()
        out = [e for e in out if s in str(e.get("when") or "").lower()]
    return out


def _one(hours: list, who: str, slot: str | None) -> dict:
    found = _match(hours, who, slot)
    if not found:
        known = ", ".join(f'{e.get("who")} ({e.get("when")})'
                          for e in hours if isinstance(e, dict)) or "none"
        raise CourseError(f"no office-hours entry for {who!r}"
                          + (f" matching {slot!r}" if slot else "")
                          + f" — on file: {known}")
    if len(found) > 1:
        which = ", ".join(str(e.get("when")) for e in found)
        raise CourseError(f"{who!r} holds several blocks ({which}) — pass slot "
                          f"to say which, e.g. slot='Tue'")
    return found[0]


def _one_on(hours: list, who: str, slot: str | None, weekday: int) -> dict:
    """`_one`, but a dated override already says which block it's about.

    A TA with Mon and Thu blocks is ambiguous by name, not on a given Thursday —
    only one of them meets that day. Falls through to `_one` (and its question)
    when the date doesn't settle it: no slot given, and zero or several of
    `who`'s blocks on that weekday."""
    if not slot:
        on_day = [e for e in _match(hours, who, None)
                  if weekday in _weekdays(str(e.get("when") or ""))]
        if len(on_day) == 1:
            return on_day[0]
    return _one(hours, who, slot)


def _weekdays(when: str) -> frozenset[int]:
    """The weekdays a `when` covers — empty when it doesn't parse as a rule."""
    return frozenset(s["day_i"] for s in _slots({"when": when}, "", "")
                     if s["day_i"] is not None)


def _amendable(hours: list, who: str, when: str, slot: str | None) -> dict | None:
    """The entry `when` amends, or None when it's a second block for `who`.

    Same person, same weekdays is the same standing block, so a new time for
    it is an edit; different weekdays is another block. Matching on `who`
    alone is what once lost three of one TA's four blocks — each
    call amended the entry the call before it had just written, and each
    reported "updated", which nobody read. One person holding Mon 16:00-18:00,
    Tue 09:00-12:00 and Tue/Thu 15:00-18:00 is the ordinary case, not the
    ambiguous one."""
    candidates = _match(hours, who, slot)
    if slot:
        # An explicit slot is an address, not a hint: it names a block to
        # amend, so missing or ambiguous is a question rather than an append.
        return _one(hours, who, slot)
    want = _weekdays(when)
    # An unparseable `when` ("TBA", a shape we haven't seen) has no weekdays to
    # compare, so fall back to `who` alone — that's how a TBA gets replaced.
    same = [e for e in candidates
            if not want or _weekdays(str(e.get("when") or "")) == want]
    if len(same) > 1:
        which = ", ".join(str(e.get("when")) for e in same)
        raise CourseError(f"{who!r} already holds {which} — pass slot to amend "
                          f"one of them, or a distinct name to add a block")
    return same[0] if same else None


def set_hours(code: str, who: str, when: str, *, where: str | None = None,
              role: str | None = None, slot: str | None = None,
              remove: bool = False) -> str:
    """Add, amend or drop one weekly office-hours entry."""
    path, post = find_course(code)
    hours = _hours_list(post)

    if remove:
        entry = _one(hours, who, slot or when or None)
        hours.remove(entry)
        _save(path, post)
        return f'removed {entry.get("who")} {entry.get("when")} from {code}'

    when = (when or "").strip()
    if not when:
        raise CourseError("pass when, as 'Tue 13:00-14:00'")
    others = list(_match(hours, who, None))
    entry = _amendable(hours, who, when, slot)
    if entry is None:
        entry = {"who": who.strip(), "role": role or "instructor",
                 "when": when, "where": where or ""}
        hours.append(entry)
        verb = "added"
    else:
        others = [e for e in others if e is not entry]
        entry["when"] = when
        if where is not None:
            entry["where"] = where
        if role is not None:
            entry["role"] = role
        verb = "updated"
    _save(path, post)

    note = f'{verb} {entry["who"]}: {when}'
    # Which of the two happened is the thing to report back: "added" beside the
    # blocks left standing is how a caller writing a person's schedule one call
    # at a time can tell it landed as a third block rather than over the second.
    if others:
        kept = ", ".join(str(e.get("when")) for e in others)
        note += f' — their other blocks ({kept}) are unchanged'
        # A block the card can't place is the one shape that is often a
        # placeholder rather than a block. Posted hours don't overwrite it —
        # nothing here overwrites anything it can't compare — but leaving a
        # "TBA" row beside the real hours silently is its own wrong card.
        stale = [e for e in others
                 if not _WHEN_RE.match(str(e.get("when") or ""))]
        if verb == "added" and stale:
            which = ", ".join(str(e.get("when")) for e in stale)
            note += (f"; {which} stayed as written — remove it if that was a "
                     f"placeholder for these hours")
    if not _WHEN_RE.match(when):
        note += (f"\nheads up — {when!r} isn't a weekly time the card can place "
                 f"(it wants 'Tue 13:00-14:00'), so it will show as plain text")
    return note


def standing_block(code: str, who: str, when: str) -> dict | None:
    """A copy of the entry `set_hours(code, who, when)` would amend, or None
    when it would add a new block. Raises CourseError where set_hours would.

    For the office-hours mail proposals: one reads it to tell a post that only restates
    hours already on file from news, and approval keeps it so the undo can
    put back the block it replaced rather than delete it."""
    _, post = find_course(code)
    entry = _amendable(_hours_list(post), who, when, None)
    return dict(entry) if entry is not None else None


def is_weekly(when: str) -> bool:
    """Whether `when` is a weekly rule the card can place ("Thu 12:00-14:00")."""
    return bool(_WHEN_RE.match(when or "")) and bool(_weekdays(when))


def same_rule(a: str, b: str) -> bool:
    """Whether two weekly `when`s are the same hours, however they're spelled —
    "Thu 12:00-14:00" and "Thurs 12:00–14:00" are one rule."""
    ma, mb = _WHEN_RE.match(a or ""), _WHEN_RE.match(b or "")
    if not (ma and mb):
        return (a or "").strip().lower() == (b or "").strip().lower()
    return (_weekdays(a) == _weekdays(b)
            and _minutes(ma.group("start")) == _minutes(mb.group("start"))
            and _minutes(ma.group("end")) == _minutes(mb.group("end")))


def set_exception(code: str, who: str, day: str, *, when: str | None = None,
                  cancelled: bool = False, note: str | None = None,
                  slot: str | None = None, remove: bool = False,
                  where: str | None = None) -> str:
    """Add, amend or drop a dated override on one weekly entry. `where` is
    that day's room, for hours that move rooms rather than times."""
    try:
        iso = date.fromisoformat(str(day).strip()).isoformat()
    except (ValueError, TypeError, AttributeError):
        raise CourseError(f"{day!r} isn't a date — pass YYYY-MM-DD")

    path, post = find_course(code)
    hours = _hours_list(post)
    entry = _one_on(hours, who, slot, date.fromisoformat(iso).weekday())
    excs = entry.get("except")
    if not isinstance(excs, list):
        excs = []

    excs = [e for e in excs if not (isinstance(e, dict) and str(e.get("date")) == iso)]
    if remove:
        if excs:
            entry["except"] = excs
        else:
            entry.pop("except", None)
        _save(path, post)
        return f'dropped the {iso} exception on {entry["who"]} {entry["when"]}'

    when = (when or "").strip()
    where = (where or "").strip()
    if not (when or cancelled or note or where):
        raise CourseError("an exception needs when, cancelled, where or note")
    fresh: dict = {"date": iso}
    if when:
        fresh["when"] = when
    if where and not cancelled:
        fresh["where"] = where
    if cancelled:
        fresh["cancelled"] = True
    if note:
        fresh["note"] = note
    excs.append(fresh)
    excs.sort(key=lambda e: str(e.get("date")))
    entry["except"] = excs
    _save(path, post)

    weekday = DAYS[date.fromisoformat(iso).weekday()]
    what = "cancelled" if cancelled else (when or "noted")
    if where and not cancelled:
        what += f" in {where}"
    out = f'{code} — {entry["who"]} {entry["when"]}: {iso} ({weekday}) {what}'
    if when and not _EXC_WHEN_RE.match(when):
        out += (f"\nheads up — {when!r} doesn't parse as a time, so that date "
                f"keeps the usual hours and only carries the note")
    base = _slots(entry, code, code)[0]
    if not cancelled and base["day_i"] is not None \
            and base["day_i"] != date.fromisoformat(iso).weekday():
        out += (f'\nnote that {iso} is a {weekday} and the usual block is '
                f'{base["day"]} — recorded as hours moved onto {weekday}. If '
                f'the {base["day"]} block is off that week, cancel it too.')
    return out


def set_field(code: str, field: str, value: str) -> str:
    """Amend one syllabus fact on the note."""
    field = (field or "").strip().lower()
    if field not in EDITABLE:
        raise CourseError(f"{field!r} isn't editable here — one of {', '.join(EDITABLE)}")
    path, post = find_course(code)
    before = post.metadata.get(field)
    # `units: 4` is a number in every note that has it; writing "4" would make
    # this the one note where it's a string.
    post.metadata[field] = int(value) if field == "units" and str(value).isdigit() else value
    _save(path, post)
    return f'{code} {field}: {before!r} -> {post.metadata[field]!r}'
