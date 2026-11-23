"""Email extractors (archive/ROUND34_PLAN.md §15 packages + §16 travel) — a shared
pipeline over the already-synced `emails` table: candidates by from/subject
heuristics -> body fetched transiently by msg_id (email_triage.body_excerpt
— never stored, same posture as triage) -> batched cheap-model extraction
via claude_agent_sdk.query() (mirrors email_triage exactly: subscription CLI
login, EXTRACT_MODEL=haiku, JSON-only prompt) -> cached per msg_id in
email_extract so nothing is parsed twice -> typed upserts into packages /
trips.

Not a provider (no METRICS/get(), not in REGISTRY) — packages/trips are
served by their own read routes (app/routes/extract.py). refresh(token) runs
inside email.refresh()'s cadence, called last, same way that module already
calls email_triage.summarize_pending(token).

§16 also polls FlightAware AeroAPI for live status on trips departing soon
(poll_flight_status) — a quiet no-op until AEROAPI_KEY is configured; trip
extraction from email works without it. T-24h before departure it
auto-creates a "Check in <flight>" task (source='travel', deduped on
source_ref=pnr:flight_no:dep_day via task_exists_for_source, same pattern as
finance.py's bill auto-task). A delay or gate change, and a package moving
forward a status, queue an at-desk banner through app/notify.py (§21 step 4) in
addition to surfacing in the daily brief.
"""

import asyncio
import json
import re
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .. import activity, agent, db, notify
from ..config import (AEROAPI_KEY, EXTRACT_BATCH, EXTRACT_MODEL,
                      FLIGHT_CHECKIN_LEAD_HOURS, FLIGHT_POLL_CLOSE_HOURS,
                      FLIGHT_POLL_HORIZON_HOURS, FLIGHT_POLL_INTERVAL_HOURS,
                      OFFICE_HOURS_SENDER_HINTS, PACKAGE_SENDER_HINTS,
                      TRIP_SENDER_DOMAINS)
from . import email_triage, heartbeat

AEROAPI_BASE = "https://aeroapi.flightaware.com/aeroapi"

PACKAGE_STATUSES = ("ordered", "shipped", "out_for_delivery", "delivered")
_PACKAGE_RANK = {s: i for i, s in enumerate(PACKAGE_STATUSES)}

PACKAGE_SYSTEM = (
    "You extract package/shipment info from personal inbox emails (online "
    "orders and carrier notifications — Amazon, USPS, UPS, FedEx, and "
    "similar). For each message you receive, decide whether it is actually "
    "about a package order or shipment. Reply with ONLY a JSON array, one "
    "object per message, no prose:\n"
    '[{"id": "...", "relevant": true, "merchant": "...", '
    '"item_desc": "short item description", "order_id": "...", '
    '"tracking_no": "...", "carrier": "usps|ups|fedex|amazon|...", '
    '"eta_day": "YYYY-MM-DD or null", '
    '"status": "ordered|shipped|out_for_delivery|delivered"}]\n'
    "Use null for any field the message does not state. If a message is not "
    "actually about a package, reply with just "
    '{"id": "...", "relevant": false} for it.'
)

TRIP_SYSTEM = (
    "You extract flight segments from airline confirmation and "
    "schedule-change emails. A single message may cover several flight "
    "segments (round trips, connections) — return one JSON object per "
    "segment, reusing the same id for every segment from the same message. "
    "Reply with ONLY a JSON array, no prose:\n"
    '[{"id": "...", "relevant": true, "pnr": "confirmation code", '
    '"carrier": "2-letter airline code, e.g. AA", "flight_no": "e.g. 1234", '
    '"dep_airport": "3-letter IATA", "arr_airport": "3-letter IATA", '
    '"dep_local": "ISO 8601 local datetime, e.g. 2026-08-01T09:15:00", '
    '"arr_local": "ISO 8601 local datetime"}]\n'
    "If a message is not actually a flight confirmation or schedule change, "
    'reply with just {"id": "...", "relevant": false} for it.'
)

# §22. Unlike the two above, nothing here is applied on the model's word — the
# output is a *proposal* you approve, so the prompt's real job is to make the
# proposal checkable. Hence `quote`, which is required and must be verbatim:
# it's the one field a human can verify at a glance, and every other field is
# just this extractor's reading of it.
#
# `course_codes` is injected per batch instead of being asked for freely.
# A model guessing course codes invents "CS 103" for "CSCI 103" and the
# proposal then points at a note that doesn't exist; constraining it to the
# codes actually in the vault turns that into a `relevant: false`.
#
# `add` exists because the 10-02 CSCI 103 digest was dropped: "Starting next
# week, I will be holding office hours from 12-2 pm on Thursdays" is a new
# weekly block, and the prompt then said a standing schedule was never an
# amendment. `current_hours` is injected beside the codes so a post that only
# restates what the note already says can still be told apart from news —
# and _upsert_oh_proposal checks that again against the note itself.
OFFICE_HOURS_SYSTEM = (
    "You read university course mail (Piazza posts and daily digests, Canvas, "
    "instructor and TA messages) and extract *changes to office hours*. You "
    "are proposing an amendment a human will review, not applying one.\n"
    "Three kinds:\n"
    "- cancel: no hours on one specific date.\n"
    "- change: hours held at a different time or place on one specific date "
    "(including a one-off extra session).\n"
    "- add: a NEW or CHANGED weekly block — someone starting regular hours, or "
    "moving their regular hours for the rest of term ('starting next week, "
    "OH Thursdays 12-2'). Not for hours that current_hours already lists "
    "unchanged.\n"
    "Only use a course code from the provided course_codes list. If the "
    "message is about a course not in that list, it is not relevant.\n"
    "A daily digest bundles many posts: read every post, ignore the ones "
    "that aren't about office hours, and emit one object per amendment.\n"
    "Reply with ONLY a JSON array, one object per amendment (a message may "
    "carry several — reuse its id), no prose:\n"
    '[{"id": "...", "relevant": true, "course": "exact code from '
    'course_codes", "who": "name as signed — a first name alone is fine — '
    'else null", "role": "TA|instructor|null", '
    '"day": "YYYY-MM-DD — the date for cancel/change; for add, the first '
    'date the new block meets", "action": "cancel|change|add", '
    '"when": "cancel: null; change: HH:MM-HH:MM 24h; add: weekday(s) then '
    'HH:MM-HH:MM 24h, e.g. Thu 12:00-14:00 or Mon, Wed 15:00-16:00", '
    '"where": "room if the message gives one, else null", '
    '"note": "at most 8 words, e.g. moved for the midterm", '
    '"quote": "the sentence from the message that says this, copied '
    'verbatim"}]\n'
    "Rules: resolve relative dates ('this Thursday', 'starting next week') "
    "against the message's own sent date, given as `sent`; for an add that "
    "starts 'next week', `day` is that block's weekday in the week after "
    "`sent`. Never invent a date you cannot resolve. An add needs a weekday "
    "and a time; without both it is not relevant. `quote` must appear in the "
    "body_excerpt word for word.\n"
    "If a message proposes no office-hours change, reply with just "
    '{"id": "...", "relevant": false} for it.'
)

# A Piazza daily digest is several posts in one body, and the 1500-char
# excerpt every other message gets holds roughly the first one. Digests get
# this much instead, so a post further down is still read.
DIGEST_EXCERPT_CHARS = 8000


# ---------- candidates ----------

def _package_candidates(con) -> list[dict]:
    conds, params = [], []
    for hint in PACKAGE_SENDER_HINTS:
        conds.append("(e.from_addr LIKE ? OR e.subject LIKE ?)")
        params += [f"%{hint}%", f"%{hint}%"]
    rows = con.execute(
        "SELECT e.msg_id, e.from_addr, e.subject FROM emails e"
        " LEFT JOIN email_extract x ON x.msg_id = e.msg_id"
        f" WHERE x.msg_id IS NULL AND ({' OR '.join(conds)})"
        " ORDER BY e.received_at ASC", params).fetchall()
    return [dict(r) for r in rows]


def _oh_preamble() -> str:
    """The course codes the vault actually has, and the weekly hours each note
    already lists, handed to the model up front — the second so an `add` is
    news rather than a digest repeating the syllabus."""
    from .. import courses  # late: reads the vault
    current: dict[str, list[str]] = {}
    for c in courses.load()["courses"]:
        seen = current.setdefault(c["code"], [])
        for s in c["office_hours"]:
            line = f'{s.get("who") or "?"}: {s.get("when")}'
            if s.get("where"):
                line += f' @ {s["where"]}'
            if line not in seen:        # one slot per weekday; one line per block
                seen.append(line)
    return (f"course_codes: {json.dumps(courses.known_codes())}\n"
            f"current_hours: {json.dumps(current)}\n\n")


# ---------- §22 proposals: read and decide ----------

def pending_oh_proposals(con=None) -> list[dict]:
    """Proposals still waiting on you, soonest date first.

    Past dates are dropped rather than shown: approving "cancel last Tuesday"
    writes an override for a day that has already happened, which is noise in
    the vault and can't change a decision you already made by walking over
    there or not. An `add` is the exception — its day is when the weekly block
    *starts*, and a block that started last week is still this week's hours.
    """
    with db.borrow(con) as con:
        return [dict(r) for r in con.execute(
            "SELECT * FROM oh_proposals WHERE status='pending'"
            " AND (day >= ? OR action = 'add')"
            " ORDER BY day ASC, id ASC", (db.day_key(),))]


def decide_oh_proposal(proposal_id: int, approve: bool) -> dict:
    """Approve (write the override to the vault) or reject (record and move on).

    The write goes through courses.set_exception, so the vault stays the only
    place that says what the hours are and this table never becomes a second
    answer to that question. A CourseError — the note moved, the entry can't
    be matched, the code no longer exists — leaves the row pending rather than
    marking it approved, because a proposal that failed to apply is still
    waiting whatever the model thought.
    """
    from .. import courses

    with db.session() as con:
        row = con.execute("SELECT * FROM oh_proposals WHERE id=?",
                          (proposal_id,)).fetchone()
        if row is None:
            raise KeyError(f"no proposal {proposal_id}")
        row = dict(row)
        if row["status"] != "pending":
            return row      # already decided; idempotent by design

        now = datetime.now().isoformat(timespec="seconds")
        applied = prior = None
        if approve and row["action"] == "add":
            # A new weekly block goes through set_hours, which amends this
            # person's block on the same weekdays in place — so what it
            # replaced is read first and kept for the undo.
            block = courses.standing_block(row["course"], row["who"] or "",
                                           row["when_str"])
            prior = json.dumps(block, default=str) if block else None
            applied = courses.set_hours(
                row["course"], row["who"] or "", row["when_str"],
                where=row["where_str"], role=row["role"])
        elif approve:
            # The room too: "office hours on lower level 2 today" is a change
            # of where, not when, and dropping it applied nothing (11-17).
            applied = courses.set_exception(
                row["course"], row["who"] or "", row["day"],
                when=row["when_str"],
                where=row["where_str"] if row["action"] != "cancel" else None,
                cancelled=row["action"] == "cancel",
                note=row["note"] or f'from mail: {row["quote"][:60]}')
        with con:
            con.execute(
                "UPDATE oh_proposals SET status=?, decided_at=?, applied=?,"
                " prior=? WHERE id=?",
                ("approved" if approve else "rejected", now, applied, prior,
                 proposal_id))
        row.update(status="approved" if approve else "rejected",
                   decided_at=now, applied=applied, prior=prior)
        return row


def undecide_oh_proposal(proposal_id: int) -> dict:
    """Take a decision back (the plan view's undo): an approval's override
    comes back out of the vault note, and the proposal is pending again.

    Removing the override drops whatever that date's exception now says. That
    is only wrong if something else rewrote the same date in the seconds
    between the approve and the undo — the undo window is the toast's.
    """
    from .. import courses

    with db.session() as con:
        row = con.execute("SELECT * FROM oh_proposals WHERE id=?",
                          (proposal_id,)).fetchone()
        if row is None:
            raise KeyError(f"no proposal {proposal_id}")
        row = dict(row)
        if row["status"] == "pending":
            return row      # nothing decided; idempotent like decide
        if row["status"] == "approved" and row["action"] == "add":
            # Addressed by slot=the rule just written, which is what the block
            # now says. Put back the block it replaced, or drop it if it was new.
            prior = json.loads(row["prior"]) if row.get("prior") else None
            if prior:
                courses.set_hours(row["course"], row["who"] or "",
                                  str(prior.get("when") or ""),
                                  where=prior.get("where") or "",
                                  role=prior.get("role"), slot=row["when_str"])
            else:
                courses.set_hours(row["course"], row["who"] or "", "",
                                  slot=row["when_str"], remove=True)
        elif row["status"] == "approved":
            courses.set_exception(row["course"], row["who"] or "", row["day"], remove=True)
        with con:
            con.execute("UPDATE oh_proposals SET status='pending', decided_at=NULL,"
                        " applied=NULL, prior=NULL WHERE id=?", (proposal_id,))
        row.update(status="pending", decided_at=None, applied=None, prior=None)
        return row


def _office_hours_candidates(con) -> list[dict]:
    """Course mail that might be amending office hours.

    Carries `received_at`, which the other two don't need: course mail says
    "this Thursday" and the model can only resolve that against the send date.

    A message already rejected once must not come back — `email_extract`
    handles that for the other kinds, but a *rejected proposal* is a different
    thing from an unextracted message, and one message can carry amendments
    for several dates. The LEFT JOIN on email_extract is what actually stops
    the retry; oh_proposals rows are the record of your decisions.
    """
    conds, params = [], []
    for hint in OFFICE_HOURS_SENDER_HINTS:
        conds.append("(e.from_addr LIKE ? OR e.subject LIKE ?)")
        params += [f"%{hint}%", f"%{hint}%"]
    rows = con.execute(
        "SELECT e.msg_id, e.from_addr, e.subject, e.received_at FROM emails e"
        " LEFT JOIN email_extract x ON x.msg_id = e.msg_id"
        f" WHERE x.msg_id IS NULL AND e.is_sent = 0 AND ({' OR '.join(conds)})"
        " ORDER BY e.received_at ASC", params).fetchall()
    return [dict(r) for r in rows]


_WEEKDAY_NAMES = ["monday", "tuesday", "wednesday", "thursday", "friday",
                  "saturday", "sunday"]


def _snap_to_quoted_weekday(day: date, quote: str) -> date:
    """Move `day` onto the weekday the quote names, if the model missed it.

    The model resolves "Thursday next week" against the send date and gets the
    arithmetic off by one — 2026-09-25's "Thursday next week" came back as
    Friday 10-02. The weekday in the sentence is the part a human wrote, so
    when the quote names exactly one and the date disagrees, the nearest date
    on that weekday (within three days) wins. Several weekdays, or none, and
    the model's date stands."""
    named = {i for i, n in enumerate(_WEEKDAY_NAMES)
             if re.search(rf"\b{n}s?\b", quote, re.I)}
    if len(named) != 1:
        return day
    want = named.pop()
    shift = (want - day.weekday() + 3) % 7 - 3      # -3..+3
    return day + timedelta(days=shift)


def _upsert_oh_proposal(con, item: dict, msg_id: str, now: str) -> None:
    """Record one proposed amendment, pending your approval.

    Validates rather than trusts, because everything here came out of a model
    reading someone else's prose: an unparseable date, an unknown course or a
    missing quote drops the proposal instead of surfacing a card you can't act
    on. The quote check is the important one — a proposal you can't verify
    against the sentence it came from is exactly what the approval step exists
    to prevent, so an empty quote is a dropped proposal, not a blank field.

    An `add` is held to more: a weekly rule the card can place, a name to file
    it under, and not already on the note — a digest re-announcing hours you
    have is not a proposal.

    Nothing here touches the vault. `courses.set_exception` (or `set_hours`,
    for an add) runs on approval.
    """
    from .. import courses  # late: reads the vault, and this module is imported early

    action = str(item.get("action") or "").strip().lower()
    if action not in ("cancel", "change", "add"):
        return
    quote = str(item.get("quote") or "").strip()
    if not quote:
        return
    try:
        day = _snap_to_quoted_weekday(
            date.fromisoformat(str(item.get("day")).strip()), quote).isoformat()
    except (TypeError, ValueError, AttributeError):
        return

    code = str(item.get("course") or "").strip()
    if not code or code not in courses.known_codes():
        return

    when = (str(item.get("when") or "").strip() or None)
    if action == "change" and not when:
        return      # a "change" with no new time says nothing to apply
    who = str(item.get("who")).strip() if item.get("who") else None
    where = str(item.get("where")).strip() if item.get("where") else None
    role = {"ta": "TA", "instructor": "instructor"}.get(
        str(item.get("role") or "").strip().lower())

    if action == "add":
        # set_hours files a block under a name, and the card can only place a
        # weekly rule — "Thursdays sometime" is neither.
        if not who or not when or not courses.is_weekly(when):
            return
        try:
            block = courses.standing_block(code, who, when)
        except courses.CourseError:
            block = None    # ambiguous on the note: let the card ask
        # Rooms compared on letters and digits only: the same post read twice
        # came back "GCS, Lower Level 2" and "GCS Lower Level 2".
        def room(s):
            return re.sub(r"[^a-z0-9]", "", str(s or "").lower())
        if block and courses.same_rule(str(block.get("when") or ""), when) and (
                not where or room(where) in room(block.get("where"))):
            return          # already on the note as written

    # The shared runner hands the upsert only the model's object, so the
    # message's own headers come back from `emails` rather than by widening
    # every extractor's signature for one caller.
    hdr = con.execute("SELECT subject, from_addr FROM emails WHERE msg_id=?",
                      (msg_id,)).fetchone()

    con.execute(
        "INSERT INTO oh_proposals (msg_id, course, who, day, action, when_str,"
        " where_str, note, quote, subject, from_addr, status, created_at, role)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,'pending',?,?)"
        " ON CONFLICT(msg_id, course, day, action) DO NOTHING",
        (msg_id, code, who, day, action, when, where,
         (str(item.get("note")).strip() if item.get("note") else None),
         quote, (hdr["subject"] if hdr else None),
         (hdr["from_addr"] if hdr else None), now, role))


def _trip_candidates(con) -> list[dict]:
    conds = ["e.from_addr LIKE ?" for _ in TRIP_SENDER_DOMAINS]
    params = [f"%{d}" for d in TRIP_SENDER_DOMAINS]
    rows = con.execute(
        "SELECT e.msg_id, e.from_addr, e.subject FROM emails e"
        " LEFT JOIN email_extract x ON x.msg_id = e.msg_id"
        f" WHERE x.msg_id IS NULL AND ({' OR '.join(conds)})"
        " ORDER BY e.received_at ASC", params).fetchall()
    return [dict(r) for r in rows]


# ---------- model call ----------

def _batch_prompt(batch: list[dict], preamble: str = "") -> str:
    payload = []
    for m in batch:
        item = {"id": m["msg_id"], "from": m["from_addr"],
                "subject": m["subject"], "body_excerpt": m["excerpt"]}
        # Only the extractors that need it ask for `received_at`, and only
        # those get `sent` — a relative date ("this Thursday") is unresolvable
        # without it, and the other two have no relative dates to resolve.
        if m.get("received_at"):
            item["sent"] = m["received_at"]
        payload.append(item)
    return f"{preamble}Messages:\n{json.dumps(payload, indent=1)}"


def _parse_reply(text: str) -> list[dict]:
    m = re.search(r"\[.*\]", text, flags=re.S)
    if not m:
        raise ValueError(f"no JSON array in model reply: {text[:200]!r}")
    return [it for it in json.loads(m.group(0)) if it.get("id")]


# ---------- typed upserts ----------

def _merchant_key(merchant: str | None) -> str | None:
    """Normalize a merchant for matching. The extractor names the same seller
    from whatever the email gives it — the order confirmation said "Campus
    Ready", the pickup-date reply said "campusready.com" — so the match has to
    survive case, spacing and a sending domain."""
    if not merchant:
        return None
    m = merchant.strip().lower()
    if "." in m and " " not in m:            # a domain, not a display name
        m = m.split("//")[-1].removeprefix("www.").split("/")[0].split(".")[0]
    return re.sub(r"[^a-z0-9]", "", m) or None


def _match_open_package(con, merchant: str | None) -> str | None:
    """The order this email is about, when it states neither an order id nor a
    tracking number.

    A follow-up in an order's thread often carries only the merchant and the
    fact that changed — Campus Ready's reply gave a pickup date and nothing
    else, so it was dropped and the card kept showing no ETA while the date
    sat correctly parsed in `email_extract`. Attach it to the merchant's one
    open order; abstain when there are none or several, because guessing which
    of two orders a date belongs to is worse than leaving it blank.
    """
    key = _merchant_key(merchant)
    if not key:
        return None
    rows = con.execute(
        "SELECT order_key, merchant FROM packages WHERE status != 'delivered'"
    ).fetchall()
    hits = [r["order_key"] for r in rows if _merchant_key(r["merchant"]) == key]
    return hits[0] if len(hits) == 1 else None


def _upsert_package(con, item: dict, msg_id: str, now: str) -> str | None:
    """Returns the order_key written, or None when the item couldn't be keyed
    to an order. create_package() needs the key back to read the row it just
    opened; re-deriving the rule at the call site is how the two would drift."""
    tracking_no = (item.get("tracking_no") or "").strip() or None
    order_id = (item.get("order_id") or "").strip() or None
    merchant = (item.get("merchant") or "").strip() or None
    # merchant:order_id is the canonical key — it's present on nearly every
    # email in an order's lifecycle (confirmation through delivery), unlike
    # tracking_no which often only shows up once shipped. Falling back to
    # tracking_no when order_id is still unknown, then folding that stray
    # row in below once order_id does appear, keeps one order from forking
    # into two packages (observed live: a "shipped" email with only
    # tracking_no + a later "delivered" email with only order_id).
    if merchant and order_id:
        order_key = f"{merchant}:{order_id}"
    elif tracking_no:
        order_key = tracking_no
    else:
        order_key = _match_open_package(con, merchant)
        if order_key is None:
            return  # not enough to identify the order
        # An email matched this way is trusted for what changed — eta, status,
        # carrier, tracking — but not for what the order IS. The row was
        # opened by a confirmation that named both properly, and a follow-up
        # describes them in passing: keeping its versions turned "Campus
        # Ready" into "campusready.com" and an itemized pack into "Linen
        # order". Dropping them here leaves the COALESCE holding the good one.
        merchant = None
        item = {**item, "item_desc": None}
    status = item.get("status")
    if status not in _PACKAGE_RANK:
        status = "ordered"
    # merchant/item_desc/tracking_no come back too, for the at-desk banner
    # below: the row already standing is the one the confirmation email named
    # properly, which the follow-up "shipped" email usually doesn't.
    existing = con.execute(
        "SELECT status, merchant, item_desc, tracking_no FROM packages"
        " WHERE order_key=?",
        (order_key,)).fetchone()
    if existing is None and tracking_no and order_key != tracking_no:
        stray = con.execute(
            "SELECT status, merchant, item_desc, tracking_no FROM packages"
            " WHERE tracking_no=? AND order_key != ?",
            (tracking_no, order_key)).fetchone()
        if stray is not None:
            con.execute("DELETE FROM packages WHERE tracking_no=? AND order_key != ?",
                       (tracking_no, order_key))
            existing = stray
    if existing and _PACKAGE_RANK.get(existing["status"], 0) > _PACKAGE_RANK[status]:
        status = existing["status"]   # status only ever moves forward
    con.execute(
        "INSERT INTO packages (order_key, merchant, item_desc, order_id,"
        " tracking_no, carrier, eta_day, status, last_msg_id, updated_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?)"
        " ON CONFLICT(order_key) DO UPDATE SET"
        " merchant=COALESCE(excluded.merchant, packages.merchant),"
        " item_desc=COALESCE(excluded.item_desc, packages.item_desc),"
        " order_id=COALESCE(excluded.order_id, packages.order_id),"
        " tracking_no=COALESCE(excluded.tracking_no, packages.tracking_no),"
        " carrier=COALESCE(excluded.carrier, packages.carrier),"
        " eta_day=COALESCE(excluded.eta_day, packages.eta_day),"
        " status=excluded.status,"
        # COALESCE, not overwrite: create_package() passes msg_id=None, and a
        # hand-amended row must not lose the pointer to the email that opened
        # it. The email path always supplies one, so this is a no-op there.
        " last_msg_id=COALESCE(excluded.last_msg_id, packages.last_msg_id),"
        " updated_at=excluded.updated_at",
        (order_key, merchant, item.get("item_desc"), order_id, tracking_no,
         item.get("carrier"), item.get("eta_day"), status, msg_id, now))
    # Unconditional, because the clamp above means `status` is already the
    # status the row now holds: a backwards move reads as no move at all.
    _notify_package_move(con, order_key, existing, status, merchant)
    return order_key


def _notify_package_move(con, order_key: str, existing, status: str,
                         merchant: str | None) -> None:
    """At-desk banner when a package's status moves forward (§21 step 4).

    **On a transition, never on discovery.** A row that didn't exist a moment ago
    gets nothing, even if it arrives already reading 'delivered' — we can't tell
    "it just happened" from "we just learned about it", and the honest reading of
    a first sighting is the second one. That is also what keeps this quiet the
    first time it runs and through `--backfill-packages`, which deletes rows and
    re-extracts them: every package in the inbox is a discovery, not a move.

    'ordered' is skipped because the confirmation email is the transition — you
    pressed the button thirty seconds ago. The alarm channel already owns arrival
    day (PACKAGE_ALARM_HOUR); this is the part that has no future time to sit at.
    """
    if existing is None or status == existing["status"] or status == "ordered":
        return
    # Derived, not a lookup table: PACKAGE_STATUSES is the registry and a fourth
    # status added there shouldn't have to remember a second place to be named.
    label = status.replace("_", " ")
    what = merchant or existing["merchant"] or "Package"
    # Not order_key as the last resort: "Vuori:27901251" is a database row, not
    # something to read off a banner. The tracking number at least tells you
    # which parcel; failing that the title already said everything known.
    notify.send("package", order_key, status, title=f"{what} {label}",
                body=existing["item_desc"] or existing["tracking_no"] or "",
                con=con)


def _upsert_trip(con, item: dict, msg_id: str, now: str) -> None:
    dep_local = (item.get("dep_local") or "").strip() or None
    dep_day = dep_local[:10] if dep_local else None
    pnr = (item.get("pnr") or "").strip().upper() or None
    flight_no = (item.get("flight_no") or "").strip().upper() or None
    if not (pnr and flight_no and dep_day):
        return
    con.execute(
        "INSERT INTO trips (pnr, flight_no, dep_day, carrier, dep_airport,"
        " arr_airport, dep_local, arr_local, status, last_msg_id, updated_at)"
        " VALUES (?,?,?,?,?,?,?,?,'confirmed',?,?)"
        " ON CONFLICT(pnr, flight_no, dep_day) DO UPDATE SET"
        " carrier=COALESCE(excluded.carrier, trips.carrier),"
        " dep_airport=COALESCE(excluded.dep_airport, trips.dep_airport),"
        " arr_airport=COALESCE(excluded.arr_airport, trips.arr_airport),"
        " dep_local=COALESCE(excluded.dep_local, trips.dep_local),"
        " arr_local=COALESCE(excluded.arr_local, trips.arr_local),"
        # last_msg_id COALESCEs for the reason the packages one does: create_trip
        # passes msg_id=None, and a hand-write must not erase the link to the
        # email that opened the row.
        " last_msg_id=COALESCE(excluded.last_msg_id, trips.last_msg_id),"
        " updated_at=excluded.updated_at",
        (pnr, flight_no, dep_day, item.get("carrier"), item.get("dep_airport"),
         item.get("arr_airport"), dep_local, item.get("arr_local"), msg_id, now))
        # status/gate deliberately absent from the UPDATE SET — once the
        # AeroAPI poller starts owning them, a later confirmation email must
        # not downgrade a live status back to 'confirmed'.


# ---------- extraction runner ----------

def _excerpt_limit(m: dict) -> int | None:
    """More body for a digest (several posts in one message), the default for
    everything else."""
    return DIGEST_EXCERPT_CHARS if "digest" in (m.get("subject") or "").lower() else None


def _cached_items(payload: str) -> list[dict]:
    """A cached email_extract payload as its list of objects — a bare object
    for one, a list once a message carried several."""
    data = json.loads(payload)
    return data if isinstance(data, list) else [data]


def _run_extraction(con, token: str, kind: str, candidates: list[dict],
                    system: str, upsert, preamble: str = "") -> int:
    if not candidates:
        return 0
    now = datetime.now().isoformat(timespec="seconds")
    done = 0
    for i in range(0, len(candidates), EXTRACT_BATCH):
        agent.raise_if_blocked()  # before the body fetches, not after them
        batch = candidates[i:i + EXTRACT_BATCH]
        # This runs inside a sync refresh already parked on its own worker
        # thread (poller.py's asyncio.to_thread) — a thread pool, not more
        # asyncio, is what actually overlaps these blocking Gmail calls.
        # body_excerpt already swallows fetch errors and returns "", so a
        # failed fetch just leaves that candidate's excerpt empty.
        with ThreadPoolExecutor(max_workers=6) as pool:
            excerpts = pool.map(lambda m: email_triage.body_excerpt(
                token, m["msg_id"], limit=_excerpt_limit(m)), batch)
        for m, excerpt in zip(batch, excerpts):
            m["excerpt"] = excerpt
        reply = asyncio.run(agent.ask(_batch_prompt(batch, preamble),
                                      model=EXTRACT_MODEL, system=system))
        # Grouped by message, every object kept. All three prompts ask for one
        # object per segment/amendment under the same id, and this used to keep
        # only the first — a round trip's return leg, or the second office-
        # hours post in a digest, was parsed and then thrown away.
        by_msg: dict[str, list[dict]] = {}
        wanted = {m["msg_id"] for m in batch}
        for it in _parse_reply(reply):
            if it["id"] in wanted:
                by_msg.setdefault(it["id"], []).append(it)
        seen = set(by_msg)
        with con:
            for mid, its in by_msg.items():
                con.execute(
                    "INSERT INTO email_extract (msg_id, kind, payload, extracted_at)"
                    " VALUES (?,?,?,?)"
                    " ON CONFLICT(msg_id) DO UPDATE SET"
                    " kind=excluded.kind, payload=excluded.payload,"
                    " extracted_at=excluded.extracted_at",
                    # One object stays a bare object, the shape every cached
                    # row already has; several are a list (see _cached_items).
                    (mid, kind, json.dumps(its[0] if len(its) == 1 else its), now))
                for it in its:
                    if it.get("relevant", True):
                        upsert(con, it, mid, now)
                        done += 1
            # messages the model dropped or marked irrelevant still count as
            # processed, so a false-positive candidate isn't retried forever
            for mid in wanted - seen:
                con.execute(
                    "INSERT OR IGNORE INTO email_extract (msg_id, kind, payload,"
                    " extracted_at) VALUES (?,?,?,?)",
                    (mid, kind, json.dumps({"relevant": False}), now))
    return done


# ---------- §16 live flight status ----------

def _match_flight(flights: list[dict], dep_day: str) -> dict | None:
    """Pick the operation that departs on dep_day out of an ident's several.

    AeroAPI keys /flights/{ident} on a UTC window, but a trip row stores
    dep_local — so a window of one UTC day built from a local date silently
    misses any departure whose UTC instant lands outside it. Westbound evening
    departures are the ordinary case: 21:00 at LAX is 04:00Z the *next* day, so
    the flight comes back empty and the poller records "checked, nothing" for a
    flight it never actually looked at. Hence a window wide enough to hold the
    local day from any zone, and hence this — with a wider window the ident's
    other daily operations come back too, newest first, so flights[0] is
    whichever one ran most recently rather than the one we asked about.

    Matched on the origin's timezone as the response itself reports it, which
    is deliberately *not* tz.zone_on(): that answers "which zone is the user in
    that day", and on a moving day the two disagree — the 08-17 RDU->LAX flight
    departs in Eastern on a date TIMELINE files under Pacific. The airport's own
    zone is the only right frame for a scheduled departure.
    """
    for f in flights:
        out = f.get("scheduled_out") or f.get("estimated_out")
        if not out:
            continue        # 'result unknown' rows carry no schedule at all
        zone = (f.get("origin") or {}).get("timezone")
        try:
            when = datetime.fromisoformat(out.replace("Z", "+00:00"))
            if zone:
                when = when.astimezone(ZoneInfo(zone))
        except (ValueError, ZoneInfoNotFoundError):
            continue
        if when.date().isoformat() == dep_day:
            return f
    return None


def _fetch_flight_status(carrier: str | None, flight_no: str | None,
                         dep_local: str) -> dict | None:
    if not (carrier and flight_no):
        return None
    ident = f"{carrier}{flight_no}"
    day = dep_local[:10]
    d = date.fromisoformat(day)
    # A day either side, so the local day is covered from any zone. Only the
    # *start* bound is capped at 2 days out (measured — the end bound accepts
    # +3d fine), and start runs a day earlier than a departure the poller's
    # 48h horizon already keeps inside that cap.
    start = (d - timedelta(days=1)).isoformat() + "T00:00:00Z"
    end = (d + timedelta(days=2)).isoformat() + "T00:00:00Z"
    req = urllib.request.Request(
        f"{AEROAPI_BASE}/flights/{ident}?start={start}&end={end}",
        headers={"x-apikey": AEROAPI_KEY})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        # 400 is this request being wrong (a window bound, near enough always),
        # not FlightAware being down, so it must not mark the source degraded —
        # a status panel that cries outage at a structural limit gets ignored.
        # 401/403 still has to show: a dead credential behind a quiet no-op is
        # exactly how one stays dead for a week.
        print(f"[email_extract] AeroAPI {ident}: HTTP {e.code}")
        if e.code != 400:
            heartbeat.log_event("email", "degraded",
                                f"AeroAPI {ident}: HTTP {e.code}")
        return None
    except (urllib.error.URLError, ValueError) as e:
        print(f"[email_extract] AeroAPI fetch failed for {ident}: {e}")
        heartbeat.log_event("email", "degraded", f"AeroAPI {ident}: {e}")
        return None
    f = _match_flight(data.get("flights") or [], day)
    if f is None:
        return None
    status = f.get("status")
    if f.get("cancelled"):
        status = "Cancelled"
    gate = f.get("gate_origin")
    return {"status": status, "gate": gate}


def _checkin_task(con, row: dict) -> bool:
    source_ref = f"{row['pnr']}:{row['flight_no']}:{row['dep_day']}"
    if db.task_exists_for_source("travel", source_ref):
        return False
    t = db.create_task(
        title=f"Check in {(row['carrier'] or '')}{row['flight_no']}",
        due=row["dep_day"], source="travel", source_kind="travel",
        source_ref=source_ref)
    activity.record("travel", "task", f"New task: {t['title']}",
                    {"task_id": t["id"]})
    return True


def ensure_checkin_tasks(con) -> int:
    """T-24h check-in tasks for every flight departing soon; returns how many
    were opened.

    Deliberately outside poll_flight_status(). This used to be one call inside
    that function's loop, which meant the single piece of §16 needing no
    external service at all sat behind the credential for the one that does:
    with AEROAPI_KEY unset the poller returns 0 before the loop, so no check-in
    task had ever been created. A task needs a row and a clock, nothing else.

    The window lives in the SQL rather than in a per-row hours check, so there
    is one definition of "departing soon" here instead of two that can drift.
    """
    now_dt = datetime.now()
    horizon = (now_dt + timedelta(hours=FLIGHT_CHECKIN_LEAD_HOURS)
               ).isoformat(timespec="seconds")
    rows = con.execute(
        "SELECT * FROM trips WHERE dep_local IS NOT NULL"
        " AND dep_local BETWEEN ? AND ?",
        (now_dt.isoformat(timespec="seconds"), horizon)).fetchall()
    return sum(_checkin_task(con, dict(r)) for r in rows)


def _apply_flight_status(con, row: dict, status: dict, now: str) -> None:
    """Land the new status, and announce it if it's the kind worth interrupting
    for. The announcement goes through app/notify.py rather than the local
    `_notify` this used to own — the channel had been sitting in this file since
    §16 without a second caller, which is exactly how it went unnoticed that it
    never fired (the only path to it is below AEROAPI_KEY's early return).

    The queue write shares the UPDATE's transaction, in both directions: if the
    row doesn't move there is nothing to announce, and if the announcement can't
    be recorded the row doesn't move either and the next poll cycle tries both
    again. Deliberately not swallowed — a notification path that fails quietly is
    the whole reason this step exists.
    """
    new_status = status.get("status") or row["status"]
    new_gate = status.get("gate") or row["gate"]
    changed_status = new_status != row["status"]
    # A gate learned for the first time is not a gate *change* — row["gate"] is
    # NULL until a poll fills it, so without the second clause every flight
    # buzzes once at T-48h announcing a gate nobody had been told before. The
    # status arm has always guarded the same first-poll case (rows open as
    # 'confirmed', so the first poll always changes status) via is_delay; this
    # is that guard on the other arm.
    changed_gate = (bool(status.get("gate")) and bool(row["gate"])
                    and status.get("gate") != row["gate"])
    is_delay = bool(new_status) and ("delay" in new_status.lower()
                                     or "cancel" in new_status.lower())
    with con:
        con.execute(
            "UPDATE trips SET status=?, gate=?, last_checked_at=?, updated_at=?"
            " WHERE pnr=? AND flight_no=? AND dep_day=?",
            (new_status, new_gate, now, now,
             row["pnr"], row["flight_no"], row["dep_day"]))
        if (changed_status and is_delay) or changed_gate:
            ident = f"{row['carrier'] or ''}{row['flight_no']}"
            detail = new_status or ""
            if changed_gate:
                detail = f"{detail} — gate {new_gate}".strip(" —")
            # Status and gate are both in the key: a flight that goes Delayed →
            # On Time → Delayed has genuinely told you three things, while the
            # same delay re-read on every poll cycle has told you one.
            notify.send("flight", row["pnr"], str(row["flight_no"]),
                        row["dep_day"], new_status or "-", new_gate or "-",
                        title=f"Flight {ident}", body=detail, con=con)


def poll_flight_status(con) -> int:
    """FlightAware AeroAPI live status for trips departing soon (poll-cost
    control per archive/ROUND34_PLAN.md §16): only flights within
    FLIGHT_POLL_HORIZON_HOURS, re-checked at most every
    FLIGHT_POLL_INTERVAL_HOURS until FLIGHT_POLL_CLOSE_HOURS out, then every
    poller cycle. Quiet no-op until AEROAPI_KEY is set."""
    if not AEROAPI_KEY:
        return 0
    now_dt = datetime.now()
    now = now_dt.isoformat(timespec="seconds")
    horizon = (now_dt + timedelta(hours=FLIGHT_POLL_HORIZON_HOURS)).isoformat()
    rows = [dict(r) for r in con.execute(
        "SELECT * FROM trips WHERE dep_local IS NOT NULL"
        " AND dep_local BETWEEN ? AND ?", (now, horizon)).fetchall()]
    checked = 0
    for row in rows:
        try:
            dep = datetime.fromisoformat(row["dep_local"])
        except ValueError:
            continue
        hours_out = (dep - now_dt).total_seconds() / 3600
        if row["last_checked_at"]:
            since = (now_dt - datetime.fromisoformat(row["last_checked_at"])
                     ).total_seconds() / 3600
            if hours_out > FLIGHT_POLL_CLOSE_HOURS and since < FLIGHT_POLL_INTERVAL_HOURS:
                continue
        status = _fetch_flight_status(row["carrier"], row["flight_no"], row["dep_local"])
        checked += 1
        if status is not None:
            _apply_flight_status(con, row, status, now)
        else:
            with con:
                con.execute("UPDATE trips SET last_checked_at=? WHERE pnr=?"
                           " AND flight_no=? AND dep_day=?",
                           (now, row["pnr"], row["flight_no"], row["dep_day"]))
    return checked


# ---------- entrypoint ----------

def refresh(token: str) -> dict:
    """Called from email.refresh(), same place email_triage.summarize_pending
    runs — packages/trips only ever extract from mail already synced by
    that call."""
    packages = trips = office_hours = 0
    deferred = None   # the model said no (usage limit, overload): try next cycle
    with db.session() as con:
        # A refused model call defers the three extractors, never the flight
        # poll and check-in tasks below — those use no model, and a spent
        # usage window must not freeze a gate change. Nothing is lost by
        # deferring: candidates are whatever has no email_extract row yet,
        # with no age cutoff, so next cycle picks up exactly where this left.
        try:
            packages = _run_extraction(con, token, "package",
                                       _package_candidates(con),
                                       PACKAGE_SYSTEM, _upsert_package)
            trips = _run_extraction(con, token, "trip",
                                    _trip_candidates(con),
                                    TRIP_SYSTEM, _upsert_trip)
        except agent.AgentError as e:
            deferred = str(e)
        # Last of the three, and guarded on its own: it reads the vault for
        # the course list, so an unindexed or missing vault must cost this
        # extractor only — packages and trips have already run by here.
        if deferred is None:
            try:
                office_hours = _run_extraction(
                    con, token, "office_hours", _office_hours_candidates(con),
                    OFFICE_HOURS_SYSTEM, _upsert_oh_proposal,
                    preamble=_oh_preamble())
            except agent.AgentError as e:
                deferred = str(e)
            except Exception as e:  # a proposal is never worth a cycle
                heartbeat.log_event("email", "oh_extract_failed", repr(e))
        flights_checked = poll_flight_status(con)
        # After the poll, so a flight whose row this same cycle opened or
        # amended gets its check-in task on the current values, and outside it,
        # so an unset AEROAPI_KEY doesn't take the tasks down with it.
        checkins = ensure_checkin_tasks(con)
    out = {"packages": packages, "trips": trips,
           "office_hours": office_hours,
           "flights_checked": flights_checked, "checkin_tasks": checkins}
    if deferred is not None:
        out["skipped"] = deferred
    return out


# ---------- read helpers (routes + brief) ----------

_PACKAGE_FIELDS = ("merchant", "item_desc", "order_id", "tracking_no",
                   "carrier", "eta_day", "status")


def _clean_package_fields(fields: dict) -> dict:
    """Drop the unset keys, validate the two that have a shape. Shared by both
    hand-write paths so a date typed into `package_add` is checked exactly as
    one typed into `package_update`."""
    sets = {k: v for k, v in fields.items()
            if k in _PACKAGE_FIELDS and v is not None}
    if "status" in sets and sets["status"] not in PACKAGE_STATUSES:
        raise ValueError("status must be one of " + ", ".join(PACKAGE_STATUSES))
    if "eta_day" in sets:
        try:
            date.fromisoformat(sets["eta_day"])
        except ValueError:
            raise ValueError(f"eta_day must be YYYY-MM-DD, got {sets['eta_day']!r}")
    return sets


def _manual_suffix(con, merchant: str) -> str:
    """A stand-in order_id for a package nobody emailed about, giving the row
    the key `Merchant:manual-<n>`.

    Shaped so it can never collide with the email path's `Merchant:order_id` —
    a real order id is the merchant's own numbering, never the literal string
    `manual-<n>` — while still reading as that merchant's in `db_query` output
    and staying stable for a later `package_update`.
    """
    n = 1
    while con.execute("SELECT 1 FROM packages WHERE order_key=?",
                      (f"{merchant}:manual-{n}",)).fetchone():
        n += 1
    return f"manual-{n}"


def create_package(merchant: str, **fields) -> dict:
    """Open a package row from what the user says, no email involved.

    Every path in `refresh()` starts at a message in *this* account's mailbox,
    so an order placed on someone else's login, picked up in a store, or
    tracked by a number read aloud over the phone could never reach the
    deliveries card at all. Observed live: a backpack and shoe spray ordered
    on a parent's Amazon account went onto the shopping list marked bought,
    because that was the only store the console could write to, and their
    arrival dates survived only as prose in an item note.

    Keys on `tracking_no` when there is one — the same rule `_upsert_package`
    uses — so a later carrier email about that number lands on this row rather
    than forking a second card. Otherwise the key is synthetic.
    """
    merchant = (merchant or "").strip()
    if not merchant:
        raise ValueError("merchant is required")
    sets = _clean_package_fields(fields)
    sets["merchant"] = merchant
    sets.setdefault("status", "ordered")
    now = datetime.now().isoformat(timespec="seconds")
    with db.session() as con:
        with con:
            # order_id is what _upsert_package builds `Merchant:order_id`
            # from, so handing it the synthetic suffix reuses that function's
            # whole body — key choice, stray-tracking fold, forward-only
            # status, COALESCE — instead of a second INSERT that would drift
            # from it. A tracking number, when given, still wins the key.
            if not sets.get("tracking_no") and not sets.get("order_id"):
                sets["order_id"] = _manual_suffix(con, merchant)
            order_key = _upsert_package(con, sets, None, now)
            return dict(con.execute("SELECT * FROM packages WHERE order_key=?",
                                    (order_key,)).fetchone())


def update_package(order_key: str, **fields) -> dict | None:
    """Hand-amend one package row; None when there's no such order_key.

    The extractor records only what an email states, so a date the user was
    given some other way — by phone, at a desk, in a reply the parser couldn't
    key to an order — has no way into the deliveries card at all. This is that
    way in, for both the console's `package_update` tool and the card's own
    mark-delivered button.

    Unlike the email path this may move status backward: forward-only exists
    so emails arriving out of order can't undo a delivery, not to overrule the
    person looking at the package.
    """
    sets = _clean_package_fields(fields)
    if not sets:
        raise ValueError("nothing to change")
    assignments = ", ".join(f"{k}=?" for k in sets)
    with db.session() as con:
        with con:
            cur = con.execute(
                f"UPDATE packages SET {assignments}, updated_at=?"
                " WHERE order_key=?",
                (*sets.values(),
                 datetime.now().isoformat(timespec="seconds"), order_key))
            if not cur.rowcount:
                return None
            return dict(con.execute("SELECT * FROM packages WHERE order_key=?",
                                    (order_key,)).fetchone())


def delete_package(order_key: str) -> bool:
    """Drop one package row; False when there's no such order_key.

    For the rows that shouldn't be on the card at all, which `update_package`
    can't fix: a promotional mail the extractor read as an order, one physical
    parcel forked into two rows because the confirmation and the shipping
    notice shared no id, or a hand-added row typed wrong. Marking such a row
    delivered is the wrong verb — it isn't, and delivered rows still sit on
    the card for three days and still reach the brief's return window.

    Deletes only the `packages` row, not the `email_extract` cache behind it,
    because that cache is keyed by msg_id while a package is keyed by order —
    the row's `last_msg_id` names one message of the several an order's
    lifecycle sends, so there is no honest set of cache rows to drop with it.
    Two consequences, both intended: `--backfill-packages`, which replays the
    whole cache, re-opens a deleted row, and so does a genuinely new email
    about that order arriving on the next refresh. Deleting is for cleaning
    up the card as it stands, not for permanently blacklisting an order.
    """
    order_key = (order_key or "").strip()
    if not order_key:
        raise ValueError("order_key is required")
    with db.session() as con:
        with con:
            cur = con.execute("DELETE FROM packages WHERE order_key=?",
                              (order_key,))
            return bool(cur.rowcount)


def delete_trip(pnr: str, flight_no: str, dep_day: str) -> dict | None:
    """Drop one trips row by its primary key; the deleted row, or None when
    there's no such leg.

    For legs that shouldn't be on the travel card at all: one replaced by a
    rebooking or an exchanged ticket (the airline's change email opens the new
    leg but never says the old one is gone), or a duplicate. `AA2308` is
    accepted for `flight_no` and split, as `create_trip` does.

    Of what a row derives, only the check-in task is stored, so that is the one
    thing cleared here: its still-open T-24h task goes to `dropped` (kept, not
    deleted, so `task_exists_for_source` still refuses to re-open it). The 48h
    warning and the travel card read `trips` live, and the phone alarm is
    withdrawn by the next `alarms.reconcile()` cycle, which diffs against this
    table. Like `delete_package`, the email_extract cache stays, so only a
    backfill replay or a new email about the leg brings it back.
    """
    pnr = (pnr or "").strip().upper()
    flight_no = (flight_no or "").strip().upper()
    dep_day = (dep_day or "").strip()
    if not (pnr and flight_no and dep_day):
        raise ValueError("pnr, flight_no and dep_day are all required")
    m = _FLIGHT_NO_RE.match(flight_no)
    if m:
        flight_no = m.group(2)
    try:
        date.fromisoformat(dep_day)
    except ValueError:
        raise ValueError(f"dep_day must be YYYY-MM-DD, got {dep_day!r}")
    key = (pnr, flight_no, dep_day)
    with db.session() as con:
        with con:
            row = con.execute(
                "SELECT * FROM trips WHERE pnr=? AND flight_no=? AND dep_day=?",
                key).fetchone()
            if row is None:
                return None
            con.execute(
                "DELETE FROM trips WHERE pnr=? AND flight_no=? AND dep_day=?",
                key)
        tasks = [r["id"] for r in con.execute(
            "SELECT id FROM tasks WHERE source_kind='travel' AND source_ref=?"
            " AND status NOT IN ('done', 'dropped')", (":".join(key),))]
    for task_id in tasks:
        db.set_task_status(task_id, "dropped")
    return {**dict(row), "dropped_tasks": tasks}


_FLIGHT_NO_RE = re.compile(r"^([A-Z]{2})\s*0*(\d{1,4})$")


def _manual_pnr(con) -> str:
    """A stand-in confirmation code for a flight nobody emailed about.

    Shaped so it can never collide with a real one — an airline PNR is six
    alphanumerics, never hyphenated — while still reading as a placeholder in
    `db_query` output rather than as a booking the user could look up.
    """
    n = 1
    while con.execute("SELECT 1 FROM trips WHERE pnr=?",
                      (f"MANUAL-{n}",)).fetchone():
        n += 1
    return f"MANUAL-{n}"


def create_trip(flight_no: str, dep_local: str, pnr: str | None = None,
                carrier: str | None = None, dep_airport: str | None = None,
                arr_airport: str | None = None,
                arr_local: str | None = None) -> dict:
    """Open a trips row from what the user says, no email involved.

    Same hole `create_package` closed, one store over, and found the same way:
    the 2026-08-17 RDU->LAX move flight was on the calendar and in no other
    store, so the travel card read "2 trips booked" and meant Thanksgiving.
    Every path into `trips` starts at a message in *this* mailbox, and that
    booking predates the mail sync's own history — there is no message to
    extract and never will be. A flight booked on someone else's account, held
    at an agent's desk, or changed over the phone has the same shape.

    Goes through `_upsert_trip` rather than its own INSERT so the COALESCE
    rules, the derived dep_day and the deliberate status/gate protection stay
    written once. Passing a real `pnr` matters more than it looks: it is what
    lets a later schedule-change email for this leg land on this row instead of
    forking a second card — exactly the job `tracking_no` does for packages.
    """
    flight_no = (flight_no or "").strip().upper()
    if not flight_no:
        raise ValueError("flight_no is required")
    # The model reaches for "AA2299" as readily as carrier="AA", flight_no="2299",
    # and the card renders `{carrier}{flight_no}` — so an unsplit code prints
    # "AA2299" with an empty carrier and reads right while keying wrong.
    m = _FLIGHT_NO_RE.match(flight_no)
    if m:
        flight_no = m.group(2)
        carrier = (carrier or "").strip() or m.group(1)

    dep_local = (dep_local or "").strip()
    try:
        parsed = datetime.fromisoformat(dep_local)
    except ValueError:
        raise ValueError(f"dep_local must be an ISO local datetime, e.g. "
                         f"2026-08-17T15:00:00, got {dep_local!r}")
    # A bare date parses fine and lands at midnight, which would put the T-24h
    # check-in task on the wrong day and print "12:00 AM" on the card. Ask.
    if len(dep_local) <= 10:
        raise ValueError("dep_local needs a departure time, not just a date — "
                         "ask the user what time the flight leaves")
    dep_local = parsed.isoformat(timespec="seconds")

    if arr_local:
        try:
            arr_local = datetime.fromisoformat(arr_local.strip()).isoformat(
                timespec="seconds")
        except ValueError:
            raise ValueError(f"arr_local must be an ISO local datetime, got "
                             f"{arr_local!r}")

    item = {"flight_no": flight_no, "dep_local": dep_local,
            "arr_local": arr_local or None,
            "carrier": (carrier or "").strip().upper() or None}
    for key, val in (("dep_airport", dep_airport), ("arr_airport", arr_airport)):
        val = (val or "").strip().upper() or None
        if val is not None and not (len(val) == 3 and val.isalpha()):
            raise ValueError(f"{key} must be a 3-letter IATA code, got {val!r}")
        item[key] = val

    now = datetime.now().isoformat(timespec="seconds")
    with db.session() as con:
        with con:
            item["pnr"] = ((pnr or "").strip().upper() or _manual_pnr(con))
            _upsert_trip(con, item, None, now)
            return dict(con.execute(
                "SELECT * FROM trips WHERE pnr=? AND flight_no=? AND dep_day=?",
                (item["pnr"], flight_no, dep_local[:10])).fetchone())


def backfill_packages() -> int:
    """Replay cached package extractions through the upsert; returns the count.

    `email_extract` is keyed by msg_id and the upsert runs only at extraction
    time, so a fix to the matching rules never reaches mail already parsed —
    the Campus Ready pickup date sat correctly parsed in the cache while the
    card showed no ETA. Replaying is safe (the upsert is idempotent and status
    still only moves forward), and each row is written with its own email's
    received_at rather than now, so a long-delivered package isn't shoved back
    into the card's three-day return window.

    Run after changing the identity rules:
        python -m app.providers.email_extract --backfill-packages
    """
    with db.session() as con:
        rows = con.execute(
            "SELECT x.msg_id, x.payload, x.extracted_at, e.received_at"
            " FROM email_extract x LEFT JOIN emails e ON e.msg_id = x.msg_id"
            " WHERE x.kind = 'package'"
            " ORDER BY COALESCE(e.received_at, x.extracted_at) ASC").fetchall()
        n = 0
        with con:
            for r in rows:
                try:
                    items = _cached_items(r["payload"])
                except ValueError:
                    continue
                # [:19] drops received_at's UTC offset: every other writer of
                # this column stores a naive local isoformat, and the card's
                # "delivered in the last 3 days" test is a string comparison.
                stamp = (r["received_at"] or r["extracted_at"])[:19]
                for item in items:
                    if not item.get("relevant", True):
                        continue
                    _upsert_package(con, item, r["msg_id"], stamp)
                    n += 1
        return n


def reextract_office_hours(since: str) -> int:
    """Forget the office-hours extractions that found nothing, for mail
    received on or after `since` (YYYY-MM-DD); returns how many. The next
    email refresh reads them again under the current prompt.

    Unlike backfill_packages this can't replay the cache: a `relevant: false`
    carries nothing to replay, and that is exactly what the old prompt said
    about every new weekly block. Only the empty rows go — a message that did
    produce proposals keeps its row, so nothing you approved or dismissed
    comes back, and each re-read costs one cheap-model pass per message.

    Run after changing OFFICE_HOURS_SYSTEM:
        python -m app.providers.email_extract --reextract-office-hours 2026-09-01
    """
    date.fromisoformat(since)       # a typo shouldn't quietly match nothing
    with db.session() as con:
        rows = con.execute(
            "SELECT x.msg_id, x.payload FROM email_extract x"
            " JOIN emails e ON e.msg_id = x.msg_id"
            " WHERE x.kind = 'office_hours' AND e.day >= ?", (since,)).fetchall()
        empty = []
        for r in rows:
            try:
                items = _cached_items(r["payload"])
            except ValueError:
                items = []
            if not any(it.get("relevant", True) for it in items):
                empty.append(r["msg_id"])
        with con:
            con.executemany("DELETE FROM email_extract WHERE msg_id=?",
                            [(m,) for m in empty])
        return len(empty)


def active_packages() -> list[dict]:
    """Not delivered, or delivered within the last 3 days."""
    cutoff = (datetime.now() - timedelta(days=3)).isoformat(timespec="seconds")
    with db.session() as con:
        rows = con.execute(
            "SELECT * FROM packages WHERE status != 'delivered' OR updated_at >= ?"
            " ORDER BY eta_day IS NULL, eta_day", (cutoff,)).fetchall()
        return [dict(r) for r in rows]


def packages_for_brief(day: str) -> list[dict]:
    """ETA today/tomorrow (still in transit) + recently delivered (return
    window) — the brief's Travel section, not the full active list."""
    tomorrow = (date.fromisoformat(day) + timedelta(days=1)).isoformat()
    cutoff = (datetime.now() - timedelta(days=3)).isoformat(timespec="seconds")
    with db.session() as con:
        rows = con.execute(
            "SELECT * FROM packages WHERE (eta_day IN (?, ?) AND status != 'delivered')"
            " OR (status='delivered' AND updated_at >= ?)"
            " ORDER BY eta_day IS NULL, eta_day", (day, tomorrow, cutoff)).fetchall()
        return [dict(r) for r in rows]


def upcoming_trips(hours: int = 48) -> list[dict]:
    now = datetime.now()
    horizon = (now + timedelta(hours=hours)).isoformat(timespec="seconds")
    with db.session() as con:
        rows = con.execute(
            "SELECT * FROM trips WHERE dep_local BETWEEN ? AND ? ORDER BY dep_local",
            (now.isoformat(timespec="seconds"), horizon)).fetchall()
        return [dict(r) for r in rows]


# Repair entry point, not a provider CLI — this module isn't in REGISTRY, so
# it gets its own -m rather than a `python -m app.providers <source>` verb.
if __name__ == "__main__":                                  # pragma: no cover
    import sys
    if "--backfill-packages" in sys.argv:
        print(f"replayed {backfill_packages()} cached package extraction(s)")
    elif "--reextract-office-hours" in sys.argv[1:-1]:
        n = reextract_office_hours(sys.argv[sys.argv.index("--reextract-office-hours") + 1])
        print(f"{n} office-hours message(s) will be read again on the next email refresh")
    else:
        print("usage: python -m app.providers.email_extract --backfill-packages\n"
              "       python -m app.providers.email_extract --reextract-office-hours YYYY-MM-DD")
