"""People / CRM-lite (archive/ROUND34_PLAN.md §17). Not a provider: no range/metric
axis, no refresh() — `people` holds the only real facts (birthday,
watch_cadence_days, notes) and is NOT droppable (see db.py's v19 migration
comment). Everything else here is a live merge over emails/messages/events,
which stay the sources of truth for contact history — nothing about contact
activity is cached in `people` itself.

PATCH /api/people/{person_key} upserts name/birthday/watch_cadence_days/notes
(dashboard widget uses this) — it's the same upsert shown below, just done
from the UI instead of by hand:
    INSERT INTO people (person_key, name, watch_cadence_days, created_at, updated_at)
    VALUES ('grandma@example.com', 'Grandma', 42, datetime('now'), datetime('now'))
    ON CONFLICT(person_key) DO UPDATE SET
      watch_cadence_days=excluded.watch_cadence_days, updated_at=excluded.updated_at;

Birthdays come from scripts/import_birthdays.py (AddressBook, rerunnable),
not synced live — the dashboard can edit an imported birthday but doesn't
re-run the import.
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
HISTORY_LIMIT = 200          # per source, in the person-detail view — plenty for a lifetime of one contact

DRAFT_CONTEXT_MESSAGES = 20  # recent turns handed to the drafter — enough to
                             # catch what's being answered without shipping a
                             # year of someone's texts to a model
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
    """Fallback display name for a key with no people.name set — the contact
    name Messages/Mail already resolved from macOS Contacts (most recent wins).
    Lets a texted-but-not-CRM'd person show as 'Iris Hale' in the detail
    view instead of a bare phone number.

    AddressBook first: it's the authoritative card, and it's keyed by the
    identity cluster, so it still resolves for a person whose key is one
    identifier but whose messages arrived on another."""
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
    """Best-effort: the events table has no attendee list, so this is a
    substring match on summary/location against the person's name, not a
    real RSVP join."""
    if not name:
        return None
    like = db.like_contains(name)
    row = con.execute(
        "SELECT MAX(day) AS d FROM events"
        " WHERE summary LIKE ? ESCAPE '\\' OR location LIKE ? ESCAPE '\\'",
        (like, like)).fetchone()
    return row["d"]


# CRM fields the user actually owns — used to pick/merge the canonical row
# when a cluster collapses several `people` rows into one person.
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
    """Batched _merge() for listing many people at once (_all(),
    brief_signals()) — one GROUP BY scan each over emails/messages instead
    of _merge()'s 2 queries per person (~2N scans across all people).
    _last_event_at can't be grouped (LIKE match against each person's name),
    so that part stays per-person. Each person's activity is aggregated over
    its cluster identifiers (`_ids`, attached by the caller); absent that it
    falls back to the bare person_key."""
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
    """Collapse several `people` rows for the same human (a card keyed by email
    AND a row captured under a phone) into one. Canonical = the row carrying
    the most CRM signal (name/birthday/notes/cadence), newest-updated as the
    tiebreak; its person_key stays the clickable identity. Gaps are then filled
    from the other rows so no user-entered datum is lost in the merge."""
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
    """Collapse `people` rows sharing a contact cluster into one person each,
    then batch-fill activity aggregated over the cluster's identifiers. Rows
    with no cluster stay their own person (bare key). Shared by _all() and
    search so both present unified people, not per-identifier fragments."""
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
    """Threads whose most recent message is outbound (is_sent) and older
    than UNANSWERED_DAYS — the email equivalent of messages.py's
    'unanswered' metric. Scans whatever's cached (email.py backfills cold
    days on demand, doesn't retain a rolling window on its own), so a
    thread gone cold outside the sync range just won't surface here —
    best effort, same posture as messages.py's contact_name lookup."""
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
    """Outbound email threads + iMessage chats with no reply — the console
    drafts 'waiting' tasks from this feed; nothing here auto-creates
    (decided in archive/ROUND34_PLAN.md).

    Three ways a row leaves this feed, deliberately kept separate:
      - **muted** (`people.hidden`) — the sender is a bot or someone you don't
        care about. Permanent, applies to everything they ever send.
      - **dismissed** (`waiting_dismissed`) — this particular dangling thread
        doesn't need a reply. The person stays; only the current last message
        is written off, so a new one brings them back on its own.
      - **aged out** (`WAITING_MAX_DAYS`) — see below.
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
            # person_key is the identity cluster the provider collapsed the
            # person's threads by — muting them mutes every thread at once.
            "to": m.get("contact_name") or m["handle"],
            "key": m.get("person_key") or m["handle"],
            "last_at": m["last_ts"], "days": round(m["hours"] / 24, 1),
            # what they actually said. Email rows have no equivalent: those
            # are threads *you* sent last, so there's no message owed a reply.
            "preview": m.get("text") or "",
        })
    # Drop muted spam/automated senders — normalize each key the same way
    # person rows are keyed so an email/phone match lands.
    out = [w for w in out
           if (normalize_handle(w.get("key") or "") or (w.get("key") or "").lower())
           not in hidden]
    # Drop threads dismissed as "nothing to reply to" — but only while the
    # dismissed message is still their latest. A newer last_at means they
    # wrote again, which un-dismisses the row without any action from you.
    out = [w for w in out
           if not (t := dismissed.get((w["kind"], w.get("key"))))
           or w["last_at"] > t]
    # Past the ceiling this stops being "you owe a reply" and becomes "that
    # conversation ended" — a friend's three-month-old 'chillin' isn't a
    # todo, and those rows sat permanently at the top because the feed sorts
    # by longest silence. Aged out, not dismissed: nothing to undo, and a new
    # message starts the clock over on its own.
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
    last_at: str            # the message being replied to — see source_ref below
    name: str | None = None  # display name, so the task doesn't read "+12025550123"
    preview: str | None = None
    subject: str | None = None  # email rows have a subject where texts have a preview


@router.post("/waiting/remind")
def post_remind(body: Reminder):
    """Turn a dangling thread into a task — the other half of dismissing one.

    Dismiss says "this needs nothing"; this says "it needs something, but not
    right now". Nothing here auto-creates: §17 settled that the waiting feed
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
        # The waiting row is the whole context: who, and what they said. Put
        # it in the note so the task still means something in tasks.md, away
        # from the People panel it was created in.
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
        # The table stores only the key it was dismissed by, so a phone-keyed
        # row reads as a bare number — useless for deciding whether to undo.
        out = [dict(r) | {"name": _derived_name(con, r["key"]) or r["key"]}
               for r in rows]
    return {"data": out}


@router.get("/hidden")
def get_hidden():
    """Muted contacts, so the UI can list them and offer an unhide."""
    with db.session() as con:
        rows = con.execute(
            "SELECT person_key, name FROM people WHERE hidden=1 ORDER BY name").fetchall()
        # people.name is only set for CRM'd contacts; muting is mostly aimed
        # at people you never made a card for, so fall back the same way the
        # detail view does rather than printing the raw key.
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
# The People views above are keyed per-handle (one person). This is the other
# axis the dashboard was missing (BUG_CHECKS pass; archive/ROUND34_PLAN.md):
# every chat_guid as its own conversation, so group chats — which fan a
# single thread across many
# handles — finally show up as one row instead of dissolving into their
# members. Everything here rolls up the `messages` cache alone; chat.db's
# custom group names aren't cached, so a group is labeled by its participants
# (e.g. "Alice, Bob +2") rather than a nickname like "Family 👨‍👩‍👧".

CONVERSATION_LIMIT = 100          # conversations listed, most-recent-active first
CONVERSATION_HISTORY_LIMIT = 200  # messages shown in one conversation's detail


def _is_group_guid(chat_guid: str | None, sender_count: int) -> bool:
    """Group if Messages' guid carries the group marker (the `;+;` segment;
    1:1 chats use `;-;`) OR we've seen more than one distinct sender. The guid
    marker catches quiet groups where only one member ever spoke; the sender
    count is the fallback for any guid that doesn't follow the convention."""
    if chat_guid and ";+;" in chat_guid:
        return True
    return sender_count > 1


def _conversation_label(is_group: bool, participants: list[dict],
                        chat_guid: str | None) -> str:
    # dedupe by display name, order-preserving: one person on two handles
    # (a phone + an email) is one participant, not "Felix, …, Felix".
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
    """One row per chat_guid, most-recently-active first. Participants are the
    distinct non-me senders (busiest first, so the label leads with the people
    who actually talk). last_* describe the newest message so the list reads
    like an inbox.

    `guids` scopes the scan to named chats and lifts CONVERSATION_LIMIT. The
    detail view can be opened on a chat older than the newest 100 (a person's
    history links straight to it), and capping there handed the UI messages
    with no header at all."""
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
    # chat.db metadata cache (providers/messages.py _sync_chats): a real custom
    # group name + the authoritative group flag, when the chat has been synced.
    meta = {r["chat_guid"]: r for r in con.execute(
        "SELECT chat_guid, display_name, is_group FROM chats"
        + (f" WHERE chat_guid IN ({','.join('?' * len(args))})" if scope else ""),
        args)}
    out = []
    for guid, tot in totals.items():
        plist = parts.get(guid, [])
        m = meta.get(guid)
        # Prefer chat.db's style flag; fall back to the guid/sender heuristic
        # for chats not in the cache yet (older rows, cache dropped).
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
            # a 1:1 chat is really one person — hand the UI the key so it can
            # drill straight into the existing person detail instead of a
            # thin single-handle "conversation".
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
    """{chat_guid: {is_group, label}} for a specific set of chats — used to
    tag a person's messages with which conversation each came from (a 1:1 vs a
    named group). Same labeling rules as conversations() (custom chat.db name
    wins, unnamed groups fall back to a participant list), but scoped to just
    these guids rather than the recency-capped global list, so even a person's
    old group chats resolve."""
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
    """Birthdays within BIRTHDAY_LOOKAHEAD_DAYS and contacts past their
    watch cadence — suggestions only for the brief, never auto-tasks
    (decided in archive/ROUND34_PLAN.md)."""
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
    """(identifier -> cluster_key, cluster_key -> [identifiers]) from the
    contact_identities cache (providers/messages.py sync_identities). Empty
    maps if the cache is missing/unpopulated, in which case callers fall back
    to the bare key — i.e. today's un-unified behavior, never an error."""
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
    """Every identifier belonging to the same person as `key`. Driven only by
    the AddressBook cluster (a shared Contacts card / shared identifier), never
    by name, so two different people who share a name are never merged. Falls
    back to just `key` when it isn't in any cluster (not a saved contact)."""
    cluster = id_to_cluster.get(key)
    ids = set(cluster_ids.get(cluster, [])) if cluster else set()
    ids.add(key)
    return list(ids)


def _imessage_handle(identifiers: list[str], messages: list[dict]) -> str | None:
    """The identifier to hand `imessage:` so Messages opens THIS person's
    thread, or None if there's nothing addressable.

    Prefers a phone number, since that's what a 1:1 iMessage/SMS thread is
    almost always keyed by, and re-expands it to E.164 (+1…) — `messages`
    stores handles normalized to the last 10 digits, which Messages won't
    resolve on its own. Falls back to an Apple-ID email. Ordering is by
    who you've actually texted: an identifier that appears in the message
    history beats one that only exists on the Contacts card, so a number
    they've never messaged from doesn't win over the one they use."""
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
    AppleScript — IDEAS §19), so replying means handing off to the real app.
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
    # List form, no shell: the handle is DB-derived, but it reaches an OS
    # launcher, so it stays an argv element and never a command string.
    try:
        macos.open_url(f"imessage://{handle}")
    except RuntimeError as e:
        raise HTTPException(502, f"couldn't open Messages: {e}") from e
    return {"ok": True, "handle": handle}


def _person_identity(con, key: str) -> tuple[list[str], list[str]]:
    """All (message-handles, email-addresses) that belong to this one person,
    so a CRM row keyed by a single identifier (say an email) still surfaces the
    texts they send from a phone and vice-versa. Identifiers are the same set
    for both message and email lookups — a phone simply never matches an email
    column and vice-versa, so one union list serves both."""
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
    """Same best-effort substring match as _last_event_at, just returning
    every row instead of only the most recent — 'meetings we've had' is
    calendar events whose summary/location mentions the person's name (no
    attendee list is cached, so this is the closest available signal)."""
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
        # No CRM name yet? Fall back to the Contacts name Messages/Mail resolved,
        # so the header reads "Iris" not "2025550166" (also sharpens the
        # meeting match below, which keys off the name).
        if not person.get("name"):
            person["name"] = _derived_name(con, key)
        # Unify identifiers: a CRM row keyed by one email/number still shows the
        # texts sent from the person's other handles (search opens people by
        # their CRM key, which often isn't the phone their messages live under).
        handles, addrs = _person_identity(con, key)
        emails = _emails_for(con, addrs)
        messages = _messages_for(con, handles)
        # Tag each message with the conversation it came from (a 1:1 vs a named
        # group chat) — this person's own texts span every chat they're in, so
        # a group message is theirs but said in front of others.
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
        # What POST /open-messages would hand to Messages.app, or null when
        # there's nothing addressable — lets the UI hide the button instead of
        # offering one that 404s.
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
        # hidden and day_one are NOT NULL; coalesce a never-set stub to 0
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
