"""People / CRM-lite router (not a provider). `people` holds the only real
facts (birthday, cadence, notes) and is NOT droppable; contact activity is a
live merge over emails/messages/events, never cached here. Birthdays come from
scripts/import_birthdays.py. Design: DESIGN.md → §17 People.
"""

import sys
from datetime import date, datetime

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from . import agent, db, macos, providers, reply_draft
from .providers import attachments
from .providers.messages import normalize_handle

router = APIRouter(prefix="/api/people")


class PersonPatch(BaseModel):
    name: str | None = None
    birthday: str | None = None          # 'MM-DD' or 'YYYY-MM-DD'
    watch_cadence_days: int | None = None
    notes: str | None = None
    hidden: bool | None = None           # mute a spam/automated contact
    day_one: bool | None = None          # been there from the jump
    crew: str | None = None              # where they're known from (Kramden, NCAAT, ...)


UNANSWERED_DAYS = 3          # outbound email/message with no reply -> "waiting"
WAITING_MAX_DAYS = 30        # past this it's a finished conversation, not a todo
BIRTHDAY_LOOKAHEAD_DAYS = 7
HISTORY_LIMIT = 200          # per source, in the person-detail view

DRAFT_CONTEXT_MESSAGES = 20  # recent turns sent to the drafter model
DRAFT_COUNT = 3              # candidate replies per request


def _last_email_at(con, ids: list[str]) -> str | None:
    marks = ",".join("?" * len(ids))
    row = con.execute(
        f"SELECT MAX(received_at) AS t FROM emails"
        f" WHERE LOWER(from_addr) IN ({marks}) OR LOWER(to_addr) IN ({marks})",
        (*ids, *ids)).fetchone()
    return row["t"]


def _last_msg_at(con, ids: list[str]) -> str | None:
    marks = ",".join("?" * len(ids))
    row = con.execute(
        f"SELECT MAX(ts) AS t FROM messages WHERE handle IN ({marks})", ids).fetchone()
    return row["t"]


def _derived_name(con, person_key: str) -> str | None:
    """Fallback display name when people.name is unset: AddressBook cluster first
    (resolves across identifiers), then the newest message/email contact name."""
    row = con.execute(
        "SELECT name FROM contact_identities"
        " WHERE (cluster_key=? OR identifier=?) AND name IS NOT NULL AND name != ''"
        " LIMIT 1", (person_key, person_key)).fetchone()
    if row and row["name"]:
        return row["name"]
    row = con.execute(
        "SELECT contact_name FROM messages WHERE handle=?"
        " AND contact_name IS NOT NULL AND contact_name != ''"
        " ORDER BY ts DESC LIMIT 1", (person_key,)).fetchone()
    if row and row["contact_name"]:
        return row["contact_name"]
    row = con.execute(
        "SELECT from_name FROM emails WHERE LOWER(from_addr)=?"
        " AND from_name IS NOT NULL AND from_name != ''"
        " ORDER BY received_at DESC LIMIT 1", (person_key,)).fetchone()
    return row["from_name"] if row and row["from_name"] else None


def _last_event_at(con, name: str | None) -> str | None:
    """Best-effort: no attendee list, so substring-match the name on summary/location."""
    if not name:
        return None
    like = db.like_contains(name)
    row = con.execute(
        "SELECT MAX(day) AS d FROM events"
        " WHERE summary LIKE ? ESCAPE '\\' OR location LIKE ? ESCAPE '\\'",
        (like, like)).fetchone()
    return row["d"]


# User-owned CRM fields; they pick/merge the canonical row of a cluster.
_CRM_FIELDS = ("name", "birthday", "notes", "watch_cadence_days")


def _merge(con, p: dict, index=None) -> dict:
    id_to_cluster, cluster_ids = index or _identity_index(con)
    ids = _identifiers_for(p["person_key"], id_to_cluster, cluster_ids)
    return {
        **p,
        "last_email_at": _last_email_at(con, ids),
        "last_msg_at": _last_msg_at(con, ids),
        "last_event_at": _last_event_at(con, p["name"]),
    }


def _merge_batch(con, people: list[dict]) -> list[dict]:
    """Batched _merge(): one GROUP BY scan per table instead of 2 queries per
    person. Events stay per-person (LIKE on name). Aggregates over the
    caller-attached `_ids` (popped), else the bare person_key."""
    email_max: dict[str, str] = {}
    for r in con.execute(
            "SELECT LOWER(from_addr) AS a, MAX(received_at) AS t FROM emails"
            " WHERE from_addr IS NOT NULL GROUP BY 1"):
        email_max[r["a"]] = r["t"]
    for r in con.execute(
            "SELECT LOWER(to_addr) AS a, MAX(received_at) AS t FROM emails"
            " WHERE to_addr IS NOT NULL GROUP BY 1"):
        prev = email_max.get(r["a"])
        if prev is None or r["t"] > prev:
            email_max[r["a"]] = r["t"]
    msg_max = {r["handle"]: r["t"] for r in con.execute(
        "SELECT handle, MAX(ts) AS t FROM messages GROUP BY handle")}
    out = []
    for p in people:
        ids = p.pop("_ids", None) or [p["person_key"]]
        out.append({
            **p,
            "last_email_at": max((email_max[i] for i in ids if i in email_max), default=None),
            "last_msg_at": max((msg_max[i] for i in ids if i in msg_max), default=None),
            "last_event_at": _last_event_at(con, p["name"]),
        })
    return out


def _merge_people_rows(rows: list[dict]) -> dict:
    """Collapse `people` rows for one human: canonical = most CRM fields set
    (newest-updated breaks ties), gaps filled from the others so nothing user-entered is lost."""
    if len(rows) == 1:
        return dict(rows[0])
    canon = dict(sorted(
        rows,
        key=lambda p: (sum(1 for f in _CRM_FIELDS if p.get(f)), p.get("updated_at") or ""),
        reverse=True)[0])
    for f in (*_CRM_FIELDS, "created_at"):
        if not canon.get(f):
            for p in rows:
                if p.get(f):
                    canon[f] = p[f]
                    break
    return canon


def _cluster_merge(con, rows: list[dict]) -> list[dict]:
    """Collapse rows sharing a contact cluster, then batch-fill cluster-wide
    activity. Rows with no cluster stay their own person."""
    id_to_cluster, cluster_ids = _identity_index(con)
    groups: dict[str, list[dict]] = {}
    for p in rows:
        ck = id_to_cluster.get(p["person_key"], p["person_key"])
        groups.setdefault(ck, []).append(p)
    merged = []
    for g in groups.values():
        person = _merge_people_rows(g)
        person["_ids"] = _identifiers_for(person["person_key"], id_to_cluster, cluster_ids)
        merged.append(person)
    return _merge_batch(con, merged)


def _all(con) -> list[dict]:
    rows = [dict(r) for r in con.execute("SELECT * FROM people WHERE hidden=0")]
    out = _cluster_merge(con, rows)
    out.sort(key=lambda p: (p.get("name") or "").lower())
    return out


@router.get("")
def list_people(day_one: bool | None = None, crew: str | None = None):
    """`day_one=true` narrows to the people tagged as day ones; `crew=NCAAT`
    narrows to one group. Filtering happens after the cluster merge so a
    person tagged on one of their identities still matches."""
    with db.session() as con:
        data = _all(con)
        if day_one is not None:
            data = [p for p in data if bool(p.get("day_one")) == day_one]
        if crew:
            data = [p for p in data if (p.get("crew") or "").lower() == crew.lower()]
        return {"data": data}


@router.get("/search")
def search_people(q: str):
    """Name lookup — person_key is an opaque email/phone, so this is how
    the console/a human finds it: substring-match `people.name`, then hit
    GET /api/people/{person_key} on whichever row is the right person."""
    with db.session() as con:
        rows = [dict(r) for r in con.execute(
            "SELECT * FROM people WHERE name LIKE ? ESCAPE '\\' ORDER BY name",
            (db.like_contains(q),))]
        return {"data": _cluster_merge(con, rows)}


def _waiting_email(con) -> list[dict]:
    """Threads whose latest message is outbound and older than UNANSWERED_DAYS.
    Best effort: only sees cached days, so a thread cold outside them won't surface."""
    rows = con.execute(
        "SELECT e.thread_id, e.to_addr, e.subject, e.received_at, e.is_sent"
        " FROM emails e"
        " JOIN (SELECT thread_id, MAX(received_at) AS max_t FROM emails"
        "       WHERE thread_id IS NOT NULL GROUP BY thread_id) latest"
        "   ON latest.thread_id = e.thread_id AND latest.max_t = e.received_at"
    ).fetchall()
    now = datetime.now().astimezone()
    seen: set[str] = set()
    out = []
    for r in rows:
        if r["thread_id"] in seen:
            continue  # tie on max(received_at) within a thread — keep first
        seen.add(r["thread_id"])
        if not r["is_sent"]:
            continue
        age_days = (now - datetime.fromisoformat(r["received_at"])).total_seconds() / 86400
        if age_days >= UNANSWERED_DAYS:
            out.append({
                "kind": "email", "thread_id": r["thread_id"],
                "to": r["to_addr"], "key": r["to_addr"],
                "subject": r["subject"], "last_at": r["received_at"],
                "days": round(age_days, 1),
            })
    return out


def waiting_on() -> list[dict]:
    """Outbound email threads + iMessage chats with no reply; suggestions only,
    nothing auto-creates tasks. A row leaves the feed three separate ways:
    muted (`people.hidden`, permanent), dismissed (`waiting_dismissed`, only
    that last message), or aged out (`WAITING_MAX_DAYS`).
    """
    with db.session() as con:
        hidden = {r["person_key"] for r in con.execute(
            "SELECT person_key FROM people WHERE hidden=1")}
        dismissed = {(r["kind"], r["key"]): r["through_ts"] for r in con.execute(
            "SELECT kind, key, through_ts FROM waiting_dismissed")}
        out = _waiting_email(con)
    for m in providers.get("messages", "today", "unanswered")["data"]:
        out.append({
            "kind": "message", "chat_guid": m["chat_guid"],
            # person_key is the identity cluster: muting it mutes every thread.
            "to": m.get("contact_name") or m["handle"],
            "key": m.get("person_key") or m["handle"],
            "last_at": m["last_ts"], "days": round(m["hours"] / 24, 1),
            # Email rows have none: you sent last.
            "preview": m.get("text") or "",
        })
    # Key normalized like person rows so email/phone matches land.
    out = [w for w in out
           if (normalize_handle(w.get("key") or "") or (w.get("key") or "").lower())
           not in hidden]
    # A newer last_at un-dismisses the row.
    out = [w for w in out
           if not (t := dismissed.get((w["kind"], w.get("key"))))
           or w["last_at"] > t]
    # Aged out, not dismissed: nothing to undo; a new message restarts the clock.
    out = [w for w in out if w["days"] <= WAITING_MAX_DAYS]
    out.sort(key=lambda d: -d["days"])
    return out


@router.get("/waiting")
def get_waiting():
    return {"data": waiting_on()}


class Dismissal(BaseModel):
    kind: str
    key: str
    through: str  # the row's last_at — dismisses that message, not the person


@router.post("/waiting/dismiss")
def post_dismiss(body: Dismissal):
    """Write off one dangling thread as 'nothing to respond to'.

    Scoped to the message, not the person: `through` is the last_at you saw,
    so the row disappears now and comes back by itself the moment they send
    something newer. To silence someone permanently — a bot, a shop, anyone
    you don't care about — PATCH `hidden` on their person row instead."""
    with db.session() as con:
        with con:
            con.execute(
                "INSERT INTO waiting_dismissed (kind, key, through_ts, created_at)"
                " VALUES (?,?,?,?)"
                " ON CONFLICT(kind, key) DO UPDATE SET"
                "  through_ts=excluded.through_ts, created_at=excluded.created_at",
                (body.kind, body.key, body.through,
                 datetime.now().isoformat(timespec="seconds")))
    return {"ok": True, "dismissed": {"kind": body.kind, "key": body.key}}


@router.delete("/waiting/dismiss/{kind}/{key:path}")
def delete_dismiss(kind: str, key: str):
    """Undo a dismissal — the thread returns to the feed if still stale."""
    with db.session() as con, con:
        removed = con.execute(
            "DELETE FROM waiting_dismissed WHERE kind=? AND key=?",
            (kind, key)).rowcount
    return {"ok": True, "removed": removed}


class Reminder(BaseModel):
    kind: str
    key: str
    last_at: str            # the message being replied to; keys source_ref
    name: str | None = None  # display name, so the task doesn't read "+12025550123"
    preview: str | None = None
    subject: str | None = None  # email rows have a subject where texts have a preview


@router.post("/waiting/remind")
def post_remind(body: Reminder):
    """Turn a dangling thread into a task — the other half of dismissing one.

    Dismiss says "this needs nothing"; this says "it needs something, but not
    right now". Nothing here auto-creates: the waiting feed
    only ever suggests, so a task exists because it was asked for.

    `source_ref` carries the message's own timestamp, exactly like a
    dismissal's `through`. That makes the dedupe say "you already have a
    reminder for *this* message" rather than "for this person, forever" —
    `task_exists_for_source` matches completed and dropped tasks too, so a
    person-keyed ref would mean answering someone once permanently blocks
    ever being reminded about them again."""
    ref = f"{body.kind}:{body.key}:{body.last_at}"
    if db.task_exists_for_source("waiting", ref):
        return {"ok": True, "created": False, "task": None}
    who = body.name or body.key
    excerpt = (body.preview or body.subject or "").strip() or None
    task = db.create_task(
        title=f"Reply to {who}"[:120],
        due=db.day_key(),
        source="ui", source_kind="waiting", source_ref=ref,
        source_excerpt=excerpt[:500] if excerpt else None,
        # Self-contained note: the task must read on its own in tasks.md.
        note=f"{body.kind} from {who} on {body.last_at[:10]}"
             + (f": {excerpt}" if excerpt else ""),
        owed_to=who)
    return {"ok": True, "created": True, "task": task}


class DraftRequest(BaseModel):
    kind: str
    name: str | None = None
    chat_guid: str | None = None   # message rows
    thread_id: str | None = None   # email rows


@router.post("/waiting/draft")
def post_draft(body: DraftRequest):
    """Candidate replies for one waiting thread — suggestions to copy, never
    sent (see reply_draft.py). Synchronous by design: it's one model call
    behind a button press, so the panel can just wait on it."""
    try:
        with agent.interactive():
            return reply_draft.drafts_for(
                kind=body.kind, name=body.name, chat_guid=body.chat_guid,
                thread_id=body.thread_id,
                limit=DRAFT_CONTEXT_MESSAGES, count=DRAFT_COUNT)
    except ValueError as e:  # model returned something unparseable
        raise HTTPException(502, f"couldn't draft a reply: {e}") from e


@router.get("/waiting/dismissed")
def get_dismissed():
    """Dismissed threads, so the UI can offer to bring one back."""
    with db.session() as con:
        rows = con.execute(
            "SELECT kind, key, through_ts, created_at FROM waiting_dismissed"
            " ORDER BY created_at DESC").fetchall()
        # Keys are bare numbers/emails; resolve a name so the user can decide to undo.
        out = [dict(r) | {"name": _derived_name(con, r["key"]) or r["key"]}
               for r in rows]
    return {"data": out}


@router.get("/hidden")
def get_hidden():
    """Muted contacts, so the UI can list them and offer an unhide."""
    with db.session() as con:
        rows = con.execute(
            "SELECT person_key, name FROM people WHERE hidden=1 ORDER BY name").fetchall()
        # people.name is set only for CRM'd contacts; fall back like the detail view.
        out = [{"key": r["person_key"],
                "name": r["name"] or _derived_name(con, r["person_key"])
                or r["person_key"]} for r in rows]
    return {"data": sorted(out, key=lambda p: p["name"].lower())}


@router.get("/signals")
def get_signals():
    """Same birthdays/watch_due suggestions brief.py pulls into the daily
    brief, exposed directly so the dashboard widget doesn't need to parse
    the brief text to show a badge."""
    return brief_signals(db.day_key())


# ---------- conversations (group-chat / per-chat rollup) ----------
#
# Per-chat_guid axis (the views above are per person), so a group chat is one row.

CONVERSATION_LIMIT = 100          # conversations listed, most-recent-active first
CONVERSATION_HISTORY_LIMIT = 200  # messages shown in one conversation's detail


def _is_group_guid(chat_guid: str | None, sender_count: int) -> bool:
    """Group if the guid has `;+;` (1:1 uses `;-;`; catches quiet groups) or there are >1 senders."""
    if chat_guid and ";+;" in chat_guid:
        return True
    return sender_count > 1


def _conversation_label(is_group: bool, participants: list[dict],
                        chat_guid: str | None) -> str:
    # Dedupe by name (order kept): one person on two handles is one participant.
    names: list[str] = []
    for p in participants:
        n = p["name"] or p["handle"]
        if n not in names:
            names.append(n)
    if not is_group:
        return names[0] if names else (chat_guid or "unknown")
    if not names:
        return "group chat"
    label = ", ".join(names[:3])
    if len(names) > 3:
        label += f" +{len(names) - 3}"
    return label


def conversations(con, guids: set[str] | None = None) -> list[dict]:
    """One row per chat_guid, newest-active first; participants are non-me
    senders, busiest first; last_* describe the newest message.

    `guids` scopes the scan and lifts CONVERSATION_LIMIT: the detail view can
    open a chat older than the cap and needs its header."""
    if guids is not None:
        if not guids:
            return []
        args = list(guids)
        scope = f" AND chat_guid IN ({','.join('?' * len(args))})"
    else:
        args, scope = [], ""
    parts: dict[str, list[dict]] = {}
    for r in con.execute(
            "SELECT chat_guid, handle, contact_name, COUNT(*) AS n FROM messages"
            " WHERE chat_guid IS NOT NULL AND handle IS NOT NULL AND handle != ''"
            " AND is_reaction = 0" + scope +
            " GROUP BY chat_guid, handle ORDER BY n DESC", args):
        parts.setdefault(r["chat_guid"], []).append(
            {"handle": r["handle"], "name": r["contact_name"]})
    totals = {r["chat_guid"]: {"count": r["c"], "last_ts": r["last_ts"]}
              for r in con.execute(
        "SELECT chat_guid, COUNT(*) AS c, MAX(ts) AS last_ts FROM messages"
        " WHERE chat_guid IS NOT NULL AND is_reaction = 0" + scope +
        " GROUP BY chat_guid", args)}
    last: dict[str, dict] = {}
    for r in con.execute(
            "SELECT m.chat_guid, m.ts, m.text, m.is_from_me, m.handle, m.contact_name"
            " FROM messages m JOIN ("
            "  SELECT chat_guid, MAX(ts) AS max_ts FROM messages"
            "  WHERE chat_guid IS NOT NULL" + scope + " GROUP BY chat_guid"
            " ) l ON l.chat_guid=m.chat_guid AND l.max_ts=m.ts", args):
        last.setdefault(r["chat_guid"], r)  # tie on max(ts): keep first
    # Custom group name + authoritative group flag (messages._sync_chats).
    meta = {r["chat_guid"]: r for r in con.execute(
        "SELECT chat_guid, display_name, is_group FROM chats"
        + (f" WHERE chat_guid IN ({','.join('?' * len(args))})" if scope else ""),
        args)}
    out = []
    for guid, tot in totals.items():
        plist = parts.get(guid, [])
        m = meta.get(guid)
        # Fallback heuristic for chats not in the cache.
        is_group = bool(m["is_group"]) if m else _is_group_guid(guid, len(plist))
        custom_name = m["display_name"] if m else None
        lm = last.get(guid)
        out.append({
            "chat_guid": guid,
            "is_group": is_group,
            "label": custom_name or _conversation_label(is_group, plist, guid),
            "participants": plist,
            "message_count": tot["count"],
            "last_ts": tot["last_ts"],
            "last_text": lm["text"] if lm else None,
            "last_from_me": bool(lm["is_from_me"]) if lm else False,
            "last_sender": (lm["contact_name"] or lm["handle"])
                           if lm and not lm["is_from_me"] else None,
            # 1:1: the UI drills into person detail with this key.
            "person_key": plist[0]["handle"] if not is_group and plist else None,
        })
    out.sort(key=lambda c: c["last_ts"] or "", reverse=True)
    return out if guids is not None else out[:CONVERSATION_LIMIT]


def _conversation_messages(con, chat_guid: str) -> list[dict]:
    return attachments.decorate(con, [dict(r) for r in con.execute(
        "SELECT guid, ts, handle, contact_name, is_from_me, text"
        " FROM messages WHERE chat_guid=? ORDER BY ts DESC LIMIT ?",
        (chat_guid, CONVERSATION_HISTORY_LIMIT))])


def _labels_for_chats(con, chat_guids: set[str]) -> dict[str, dict]:
    """{chat_guid: {is_group, label}} for these chats, same labeling as
    conversations() but not recency-capped, so old group chats resolve."""
    if not chat_guids:
        return {}
    guids = list(chat_guids)
    qmarks = ",".join("?" * len(guids))
    meta = {r["chat_guid"]: r for r in con.execute(
        f"SELECT chat_guid, display_name, is_group FROM chats"
        f" WHERE chat_guid IN ({qmarks})", guids)}
    parts: dict[str, list[dict]] = {}
    for r in con.execute(
            f"SELECT chat_guid, handle, contact_name, COUNT(*) AS n FROM messages"
            f" WHERE chat_guid IN ({qmarks}) AND handle IS NOT NULL AND handle != ''"
            f" AND is_reaction = 0"
            f" GROUP BY chat_guid, handle ORDER BY n DESC", guids):
        parts.setdefault(r["chat_guid"], []).append(
            {"handle": r["handle"], "name": r["contact_name"]})
    out = {}
    for guid in chat_guids:
        m = meta.get(guid)
        plist = parts.get(guid, [])
        is_group = bool(m["is_group"]) if m else _is_group_guid(guid, len(plist))
        custom = m["display_name"] if m else None
        out[guid] = {"is_group": is_group,
                     "label": custom or _conversation_label(is_group, plist, guid)}
    return out


@router.get("/conversations")
def get_conversations():
    with db.session() as con:
        return {"data": conversations(con)}


@router.get("/conversation")
def get_conversation(chat_guid: str):
    """One group chat's recent messages + its header (label/participants).
    chat_guid is a query param, not a path segment — the Messages guid is full
    of `;`/`+` that read awkwardly in a path."""
    with db.session() as con:
        rows = conversations(con, {chat_guid})
        messages = _conversation_messages(con, chat_guid)
    return {"chat_guid": chat_guid, "conversation": rows[0] if rows else None,
            "messages": messages}


def _mmdd(birthday: str | None) -> str | None:
    # birthday is 'MM-DD' or 'YYYY-MM-DD' — the last 5 chars are always MM-DD.
    return birthday[-5:] if birthday and len(birthday) >= 5 else None


def brief_signals(day: str) -> dict:
    """Birthdays within BIRTHDAY_LOOKAHEAD_DAYS and contacts past their watch
    cadence; suggestions only, never auto-tasks."""
    today = date.fromisoformat(day)
    with db.session() as con:
        people = [dict(r) for r in con.execute("SELECT * FROM people WHERE hidden=0")]
        birthdays = []
        for p in people:
            mmdd = _mmdd(p["birthday"])
            if not mmdd:
                continue
            month, dom = int(mmdd[:2]), int(mmdd[3:])
            try:
                next_bday = date(today.year, month, dom)
            except ValueError:
                continue  # Feb 29 in a non-leap year — skip, not worth the edge case
            if next_bday < today:
                next_bday = date(today.year + 1, month, dom)
            days_until = (next_bday - today).days
            if days_until <= BIRTHDAY_LOOKAHEAD_DAYS:
                birthdays.append({"name": p["name"], "days_until": days_until})

        watch_due = []
        cadence_people = [p for p in people if p["watch_cadence_days"]]
        for merged in _cluster_merge(con, cadence_people):
            last = max((t for t in (merged["last_email_at"], merged["last_msg_at"],
                                     merged["last_event_at"]) if t), default=None)
            if last is None:
                continue
            days_since = (today - date.fromisoformat(last[:10])).days
            if days_since >= merged["watch_cadence_days"]:
                watch_due.append({
                    "name": merged["name"], "days_since": days_since,
                    "cadence": merged["watch_cadence_days"],
                })
    birthdays.sort(key=lambda b: b["days_until"])
    watch_due.sort(key=lambda w: -w["days_since"])
    return {"birthdays": birthdays, "watch_due": watch_due}


def _identity_index(con) -> tuple[dict[str, str], dict[str, list[str]]]:
    """(identifier -> cluster_key, cluster_key -> [identifiers]) from contact_identities.
    Empty if the cache is missing; callers then use the bare key."""
    id_to_cluster: dict[str, str] = {}
    cluster_ids: dict[str, list[str]] = {}
    try:
        rows = con.execute("SELECT identifier, cluster_key FROM contact_identities")
    except Exception:
        return id_to_cluster, cluster_ids
    for r in rows:
        id_to_cluster[r["identifier"]] = r["cluster_key"]
        cluster_ids.setdefault(r["cluster_key"], []).append(r["identifier"])
    return id_to_cluster, cluster_ids


def _identifiers_for(key: str, id_to_cluster: dict[str, str],
                     cluster_ids: dict[str, list[str]]) -> list[str]:
    """Every identifier of the same person as `key`, by AddressBook cluster only
    (never by name); just `key` if unclustered."""
    cluster = id_to_cluster.get(key)
    ids = set(cluster_ids.get(cluster, [])) if cluster else set()
    ids.add(key)
    return list(ids)


def _imessage_handle(identifiers: list[str], messages: list[dict]) -> str | None:
    """The identifier for `imessage:`, or None if nothing is addressable.

    Phone first, re-expanded to +1 E.164 (stored handles are 10 digits, which
    Messages won't resolve), then email. Identifiers seen in message history
    beat card-only ones."""
    texted = {m["handle"] for m in messages if m.get("handle")}
    phones = sorted((i for i in identifiers if i.isdigit() and len(i) == 10),
                    key=lambda i: (i not in texted, i))
    if phones:
        return f"+1{phones[0]}"
    emails = sorted((i for i in identifiers if "@" in i),
                    key=lambda i: (i not in texted, i))
    return emails[0] if emails else None


@router.post("/{person_key}/open-messages")
def post_open_messages(person_key: str):
    """Open this person's thread in Messages.app.

    The dashboard's message history is read-only (sending would need
    AppleScript), so replying means handing off to the real app.
    Done backend-side via `open` rather than an <a href="imessage://"> so the
    behaviour doesn't depend on how the webview treats a non-http scheme:
    the same code then works in the pywebview window and a browser tab."""
    if sys.platform != "darwin":
        raise HTTPException(400, "Messages hand-off is macOS-only")
    key = normalize_handle(person_key) or person_key.lower()
    with db.session() as con:
        handles, _ = _person_identity(con, key)
        messages = _messages_for(con, handles)
    handle = _imessage_handle(handles, messages)
    if not handle:
        raise HTTPException(404, f"no phone or email on file for {key}")
    # The DB-derived handle reaches an OS launcher: argv element, never a command string.
    try:
        macos.open_url(f"imessage://{handle}")
    except RuntimeError as e:
        raise HTTPException(502, f"couldn't open Messages: {e}") from e
    return {"ok": True, "handle": handle}


def _person_identity(con, key: str) -> tuple[list[str], list[str]]:
    """(message handles, email addrs) of one person; both are the same union list
    (a phone never matches an email column)."""
    id_to_cluster, cluster_ids = _identity_index(con)
    ids = _identifiers_for(key, id_to_cluster, cluster_ids)
    return ids, ids


def _emails_for(con, addrs: list[str]) -> list[dict]:
    marks = ",".join("?" * len(addrs))
    return [dict(r) for r in con.execute(
        f"SELECT msg_id, thread_id, day, received_at, from_name, from_addr,"
        f" to_addr, subject, snippet, is_sent"
        f" FROM emails WHERE LOWER(from_addr) IN ({marks})"
        f" OR LOWER(to_addr) IN ({marks})"
        f" ORDER BY received_at DESC LIMIT ?",
        (*addrs, *addrs, HISTORY_LIMIT))]


def _messages_for(con, handles: list[str]) -> list[dict]:
    marks = ",".join("?" * len(handles))
    return attachments.decorate(con, [dict(r) for r in con.execute(
        f"SELECT guid, day, ts, chat_guid, is_from_me, text"
        f" FROM messages WHERE handle IN ({marks}) ORDER BY ts DESC LIMIT ?",
        (*handles, HISTORY_LIMIT))])


def _meetings_for(con, name: str | None) -> list[dict]:
    """Every event matched like _last_event_at (name substring; no attendee list)."""
    if not name:
        return []
    like = db.like_contains(name)
    return [dict(r) for r in con.execute(
        "SELECT day, event_id, summary, start_at, end_at, all_day, location"
        " FROM events WHERE summary LIKE ? ESCAPE '\\' OR location LIKE ? ESCAPE '\\'"
        " ORDER BY start_at DESC LIMIT ?",
        (like, like, HISTORY_LIMIT))]


@router.get("/{person_key}")
def get_person(person_key: str):
    """Full history for one person — the console/a human's 'what's the
    story with this person' view. Works even if person_key has no row in
    `people` yet (e.g. looked up straight from a raw phone/email): the
    email/message/meeting history is keyed off emails/messages/events
    regardless. person_key is normalized the same way messages.py handles
    are, so callers don't have to pre-format phone numbers."""
    key = normalize_handle(person_key) or person_key.lower()
    with db.session() as con:
        row = con.execute("SELECT * FROM people WHERE person_key=?", (key,)).fetchone()
        person = dict(row) if row else {"person_key": key, "name": None, "birthday": None,
                                         "watch_cadence_days": None, "notes": None}
        # Also feeds the meeting match below, which keys off the name.
        if not person.get("name"):
            person["name"] = _derived_name(con, key)
        # The CRM key often isn't the handle the messages live under.
        handles, addrs = _person_identity(con, key)
        emails = _emails_for(con, addrs)
        messages = _messages_for(con, handles)
        # Tag each message with its conversation: their texts span every chat they're in.
        labels = _labels_for_chats(con, {m["chat_guid"] for m in messages if m["chat_guid"]})
        for m in messages:
            info = labels.get(m["chat_guid"])
            m["is_group"] = info["is_group"] if info else False
            m["conversation"] = info["label"] if info else None
        meetings = _meetings_for(con, person["name"])
    contact_times = ([e["received_at"] for e in emails] + [m["ts"] for m in messages]
                      + [ev["start_at"] for ev in meetings])
    return {
        "person": person,
        "emails": emails,
        "messages": messages,
        "meetings": meetings,
        # What /open-messages would open; null lets the UI hide the button.
        "message_handle": _imessage_handle(handles, messages),
        "stats": {
            "email_count": len(emails),
            "message_count": len(messages),
            "meeting_count": len(meetings),
            "first_contact_at": min(contact_times) if contact_times else None,
            "last_contact_at": max(contact_times) if contact_times else None,
        },
    }


@router.patch("/{person_key}")
def patch_person(person_key: str, body: PersonPatch):
    """Upsert name/birthday/watch_cadence_days/notes — dashboard's inline
    edit form. Partial: only fields actually sent (exclude_unset) are
    changed, same semantics as PATCH /api/tasks/{id}. Unlike GET, this
    always creates the `people` row if it didn't exist yet (editing a
    stub person from the detail view is how most rows get created)."""
    key = normalize_handle(person_key) or person_key.lower()
    fields = body.model_dump(exclude_unset=True)
    now = datetime.now().isoformat(timespec="seconds")
    with db.session() as con:
        row = con.execute("SELECT * FROM people WHERE person_key=?", (key,)).fetchone()
        existing = dict(row) if row else {}
        merged = {k: fields.get(k, existing.get(k))
                  for k in ("name", "birthday", "watch_cadence_days", "notes", "crew")}
        # NOT NULL columns: coalesce a never-set stub to 0
        hidden = int(bool(fields.get("hidden", existing.get("hidden", 0))))
        day_one = int(bool(fields.get("day_one", existing.get("day_one", 0))))
        with con:
            con.execute(
                "INSERT INTO people (person_key, name, birthday, watch_cadence_days,"
                " notes, hidden, day_one, crew, created_at, updated_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(person_key) DO UPDATE SET"
                " name=excluded.name, birthday=excluded.birthday,"
                " watch_cadence_days=excluded.watch_cadence_days, notes=excluded.notes,"
                " hidden=excluded.hidden, day_one=excluded.day_one, crew=excluded.crew,"
                " updated_at=excluded.updated_at",
                (key, merged["name"], merged["birthday"], merged["watch_cadence_days"],
                 merged["notes"], hidden, day_one, merged["crew"], now, now))
        row = con.execute("SELECT * FROM people WHERE person_key=?", (key,)).fetchone()
        person = _merge(con, dict(row))
    return person
