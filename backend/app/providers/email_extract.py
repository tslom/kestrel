"""Email extractors (packages, trips, office-hours proposals) over the synced `emails` table.

Candidates by heuristics -> body fetched transiently (never stored) -> batched Haiku extraction ->
cached per msg_id in `email_extract` -> typed upserts. Not a provider (not in REGISTRY); `refresh()`
runs inside email.refresh(). Also polls AeroAPI for live flight status (no-op without AEROAPI_KEY).
Design: DESIGN.md → §15 packages / §16 travel (extraction).
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

# Output is a proposal a human approves: `quote` is required and verbatim. DESIGN.md → §15 packages / §16 travel (extraction).
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

# A digest holds several posts; the default 1500-char excerpt holds roughly the first.
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
    """Vault course codes and each note's weekly hours, so the model can't invent codes or re-add known hours."""
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


# ---------- office-hours proposals: read and decide ----------

def pending_oh_proposals(con=None) -> list[dict]:
    """Pending proposals, soonest first. Past dates are dropped, except `add` (its day is when the block starts)."""
    with db.borrow(con) as con:
        return [dict(r) for r in con.execute(
            "SELECT * FROM oh_proposals WHERE status='pending'"
            " AND (day >= ? OR action = 'add')"
            " ORDER BY day ASC, id ASC", (db.day_key(),))]


def decide_oh_proposal(proposal_id: int, approve: bool) -> dict:
    """Approve (write through `courses`, so the vault stays the source) or reject. A CourseError leaves the row pending."""
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
            # set_hours amends in place, so read the replaced block first for undo.
            block = courses.standing_block(row["course"], row["who"] or "",
                                           row["when_str"])
            prior = json.dumps(block, default=str) if block else None
            applied = courses.set_hours(
                row["course"], row["who"] or "", row["when_str"],
                where=row["where_str"], role=row["role"])
        elif approve:
            # Keep the room: a change may be of where, not when.
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
    """Undo a decision: remove what approval wrote and make the proposal pending. Drops whatever that date's exception now says."""
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
            # slot= the rule just written; restore the replaced block or drop the new one.
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
    """Unextracted course mail; carries `received_at` (the model resolves "this Thursday" against it)."""
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
    """Snap `day` to the nearest date (within 3 days) on the one weekday the quote names; else keep it."""
    named = {i for i, n in enumerate(_WEEKDAY_NAMES)
             if re.search(rf"\b{n}s?\b", quote, re.I)}
    if len(named) != 1:
        return day
    want = named.pop()
    shift = (want - day.weekday() + 3) % 7 - 3      # -3..+3
    return day + timedelta(days=shift)


def _upsert_oh_proposal(con, item: dict, msg_id: str, now: str) -> None:
    """Record one pending proposal; a bad date, unknown course or empty quote (or an unplaceable/known `add`) drops it. Never touches the vault."""
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
        # Needs a name and a weekly rule the card can place.
        if not who or not when or not courses.is_weekly(when):
            return
        try:
            block = courses.standing_block(code, who, when)
        except courses.CourseError:
            block = None    # ambiguous on the note: let the card ask
        # Rooms compare on letters/digits only ("GCS, Lower Level 2" vs "GCS Lower Level 2").
        def room(s):
            return re.sub(r"[^a-z0-9]", "", str(s or "").lower())
        if block and courses.same_rule(str(block.get("when") or ""), when) and (
                not where or room(where) in room(block.get("where"))):
            return          # already on the note as written

    # The runner passes only the model's object, so headers come from `emails`.
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
        # `sent` only for extractors that carry received_at (relative dates).
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
    """Normalize a merchant for matching across case, spacing and a sending domain."""
    if not merchant:
        return None
    m = merchant.strip().lower()
    if "." in m and " " not in m:            # a domain, not a display name
        m = m.split("//")[-1].removeprefix("www.").split("/")[0].split(".")[0]
    return re.sub(r"[^a-z0-9]", "", m) or None


def _match_open_package(con, merchant: str | None) -> str | None:
    """The merchant's one open order, for mail with no order id or tracking number; None if zero or several."""
    key = _merchant_key(merchant)
    if not key:
        return None
    rows = con.execute(
        "SELECT order_key, merchant FROM packages WHERE status != 'delivered'"
    ).fetchall()
    hits = [r["order_key"] for r in rows if _merchant_key(r["merchant"]) == key]
    return hits[0] if len(hits) == 1 else None


def _upsert_package(con, item: dict, msg_id: str, now: str) -> str | None:
    """Returns the order_key written, or None when the item can't be keyed (create_package reads the row back with it)."""
    tracking_no = (item.get("tracking_no") or "").strip() or None
    order_id = (item.get("order_id") or "").strip() or None
    merchant = (item.get("merchant") or "").strip() or None
    # merchant:order_id is the canonical key; a tracking-only row is folded in below once order_id appears.
    if merchant and order_id:
        order_key = f"{merchant}:{order_id}"
    elif tracking_no:
        order_key = tracking_no
    else:
        order_key = _match_open_package(con, merchant)
        if order_key is None:
            return  # not enough to identify the order
        # Trusted for what changed, not for what the order is: drop merchant/item_desc so COALESCE keeps the good ones.
        merchant = None
        item = {**item, "item_desc": None}
    status = item.get("status")
    if status not in _PACKAGE_RANK:
        status = "ordered"
    # merchant/item_desc/tracking_no feed the banner below (the standing row names them best).
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
        # COALESCE: create_package passes msg_id=None.
        " last_msg_id=COALESCE(excluded.last_msg_id, packages.last_msg_id),"
        " updated_at=excluded.updated_at",
        (order_key, merchant, item.get("item_desc"), order_id, tracking_no,
         item.get("carrier"), item.get("eta_day"), status, msg_id, now))
    # Unconditional: the forward-only clamp makes a backwards move read as no move.
    _notify_package_move(con, order_key, existing, status, merchant)
    return order_key


def _notify_package_move(con, order_key: str, existing, status: str,
                         merchant: str | None) -> None:
    """At-desk banner on a forward transition only: never for a new row (discovery), nor for 'ordered'."""
    if existing is None or status == existing["status"] or status == "ordered":
        return
    label = status.replace("_", " ")
    what = merchant or existing["merchant"] or "Package"
    # Body is never order_key (a DB row, not banner text).
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
        # COALESCE: create_trip passes msg_id=None.
        " last_msg_id=COALESCE(excluded.last_msg_id, trips.last_msg_id),"
        " updated_at=excluded.updated_at",
        (pnr, flight_no, dep_day, item.get("carrier"), item.get("dep_airport"),
         item.get("arr_airport"), dep_local, item.get("arr_local"), msg_id, now))
        # status/gate stay out of the UPDATE SET: the AeroAPI poller owns them.


# ---------- extraction runner ----------

def _excerpt_limit(m: dict) -> int | None:
    """More body for a digest, the default (None) otherwise."""
    return DIGEST_EXCERPT_CHARS if "digest" in (m.get("subject") or "").lower() else None


def _cached_items(payload: str) -> list[dict]:
    """A cached payload as a list: older rows are one bare object."""
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
        # Threads, not asyncio: we're already on a worker thread. body_excerpt returns "" on fetch errors.
        with ThreadPoolExecutor(max_workers=6) as pool:
            excerpts = pool.map(lambda m: email_triage.body_excerpt(
                token, m["msg_id"], limit=_excerpt_limit(m)), batch)
        for m, excerpt in zip(batch, excerpts):
            m["excerpt"] = excerpt
        reply = asyncio.run(agent.ask(_batch_prompt(batch, preamble),
                                      model=EXTRACT_MODEL, system=system))
        # Prompts return one object per segment/amendment under one id: keep them all.
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
                    # One object stays bare (the older shape); several are a list.
                    (mid, kind, json.dumps(its[0] if len(its) == 1 else its), now))
                for it in its:
                    if it.get("relevant", True):
                        upsert(con, it, mid, now)
                        done += 1
            # Dropped messages still get a row so they aren't retried forever.
            for mid in wanted - seen:
                con.execute(
                    "INSERT OR IGNORE INTO email_extract (msg_id, kind, payload,"
                    " extracted_at) VALUES (?,?,?,?)",
                    (mid, kind, json.dumps({"relevant": False}), now))
    return done


# ---------- live flight status ----------

def _match_flight(flights: list[dict], dep_day: str) -> dict | None:
    """The ident's operation departing on dep_day in the origin's own timezone (from the response, not tz.zone_on())."""
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
    # A day either side covers the local day from any zone; `start` is capped at 2 days out (measured).
    start = (d - timedelta(days=1)).isoformat() + "T00:00:00Z"
    end = (d + timedelta(days=2)).isoformat() + "T00:00:00Z"
    req = urllib.request.Request(
        f"{AEROAPI_BASE}/flights/{ident}?start={start}&end={end}",
        headers={"x-apikey": AEROAPI_KEY})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        # 400 is our request being wrong, not an outage: don't mark degraded. 401/403 must show.
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
    """T-24h check-in tasks for flights departing soon; returns how many opened. Needs no AEROAPI_KEY, so it stays outside the poller."""
    now_dt = datetime.now()
    horizon = (now_dt + timedelta(hours=FLIGHT_CHECKIN_LEAD_HOURS)
               ).isoformat(timespec="seconds")
    rows = con.execute(
        "SELECT * FROM trips WHERE dep_local IS NOT NULL"
        " AND dep_local BETWEEN ? AND ?",
        (now_dt.isoformat(timespec="seconds"), horizon)).fetchall()
    return sum(_checkin_task(con, dict(r)) for r in rows)


def _apply_flight_status(con, row: dict, status: dict, now: str) -> None:
    """Land the new status and notify on delay/cancel/gate change, in one transaction; a notify failure propagates (next poll retries)."""
    new_status = status.get("status") or row["status"]
    new_gate = status.get("gate") or row["gate"]
    changed_status = new_status != row["status"]
    # A first-learned gate (row["gate"] NULL) is not a change; is_delay guards the status arm likewise.
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
            # Status and gate are in the key: Delayed -> On Time -> Delayed is three messages, a repeated read one.
            notify.send("flight", row["pnr"], str(row["flight_no"]),
                        row["dep_day"], new_status or "-", new_gate or "-",
                        title=f"Flight {ident}", body=detail, con=con)


def poll_flight_status(con) -> int:
    """AeroAPI live status within FLIGHT_POLL_HORIZON_HOURS: every FLIGHT_POLL_INTERVAL_HOURS, every cycle inside FLIGHT_POLL_CLOSE_HOURS. No-op without AEROAPI_KEY."""
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
    """Called from email.refresh() after email_triage.summarize_pending; extracts from already-synced mail."""
    packages = trips = office_hours = 0
    deferred = None   # the model said no (usage limit, overload): try next cycle
    with db.session() as con:
        # A refused model call defers only the extractors; the poll and check-ins use no model.
        try:
            packages = _run_extraction(con, token, "package",
                                       _package_candidates(con),
                                       PACKAGE_SYSTEM, _upsert_package)
            trips = _run_extraction(con, token, "trip",
                                    _trip_candidates(con),
                                    TRIP_SYSTEM, _upsert_trip)
        except agent.AgentError as e:
            deferred = str(e)
        # Last and guarded on its own: it reads the vault, which may be unindexed.
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
        # After the poll (current values), outside it (works without AEROAPI_KEY).
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
    """Drop unset keys and validate status and eta_day; shared by package_add and package_update."""
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
    """A stand-in order_id `manual-<n>` that can't collide with a real merchant order id."""
    n = 1
    while con.execute("SELECT 1 FROM packages WHERE order_key=?",
                      (f"{merchant}:manual-{n}",)).fetchone():
        n += 1
    return f"manual-{n}"


def create_package(merchant: str, **fields) -> dict:
    """Open a package row with no email behind it. Keys on tracking_no when given (as `_upsert_package` does), else a synthetic order id."""
    merchant = (merchant or "").strip()
    if not merchant:
        raise ValueError("merchant is required")
    sets = _clean_package_fields(fields)
    sets["merchant"] = merchant
    sets.setdefault("status", "ordered")
    now = datetime.now().isoformat(timespec="seconds")
    with db.session() as con:
        with con:
            # A synthetic order_id reuses _upsert_package whole (key choice, fold, forward-only status, COALESCE).
            if not sets.get("tracking_no") and not sets.get("order_id"):
                sets["order_id"] = _manual_suffix(con, merchant)
            order_key = _upsert_package(con, sets, None, now)
            return dict(con.execute("SELECT * FROM packages WHERE order_key=?",
                                    (order_key,)).fetchone())


def update_package(order_key: str, **fields) -> dict | None:
    """Hand-amend one package row; None when there is no such order_key. Unlike the email path, may move status backward."""
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
    """Drop one package row (not its `email_extract` cache, so a backfill or new email re-opens it); False if absent."""
    order_key = (order_key or "").strip()
    if not order_key:
        raise ValueError("order_key is required")
    with db.session() as con:
        with con:
            cur = con.execute("DELETE FROM packages WHERE order_key=?",
                              (order_key,))
            return bool(cur.rowcount)


def delete_trip(pnr: str, flight_no: str, dep_day: str) -> dict | None:
    """Drop one trips row by (pnr, flight_no, dep_day); the deleted row or None. Drops its open check-in task; `AA2308` is split as in create_trip."""
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
    """A stand-in `MANUAL-<n>` PNR (never six alphanumerics, so it can't collide with a real one)."""
    n = 1
    while con.execute("SELECT 1 FROM trips WHERE pnr=?",
                      (f"MANUAL-{n}",)).fetchone():
        n += 1
    return f"MANUAL-{n}"


def create_trip(flight_no: str, dep_local: str, pnr: str | None = None,
                carrier: str | None = None, dep_airport: str | None = None,
                arr_airport: str | None = None,
                arr_local: str | None = None) -> dict:
    """Open a trips row with no email behind it, via `_upsert_trip`. A real `pnr` lets a later schedule-change email land on this row."""
    flight_no = (flight_no or "").strip().upper()
    if not flight_no:
        raise ValueError("flight_no is required")
    # The model passes "AA2299" joined; the card renders {carrier}{flight_no}, so split it.
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
    # A bare date lands at midnight: wrong T-24h day and a "12:00 AM" card.
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
    """Replay cached package extractions through the upsert (idempotent); returns the count.

    Run after changing the identity rules: python -m app.providers.email_extract --backfill-packages
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
                # [:19]: other writers store naive local isoformat; the card compares strings.
                stamp = (r["received_at"] or r["extracted_at"])[:19]
                for item in items:
                    if not item.get("relevant", True):
                        continue
                    _upsert_package(con, item, r["msg_id"], stamp)
                    n += 1
        return n


def reextract_office_hours(since: str) -> int:
    """Forget empty office-hours extractions for mail on/after `since` (YYYY-MM-DD); returns how many.

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


# Repair entry point, not a provider CLI (not in REGISTRY).
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
