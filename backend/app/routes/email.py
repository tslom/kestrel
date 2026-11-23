"""Email review queue — the WRITE face of the email provider (IDEAS §5).

The provider (providers/email.py) only reads; every mailbox mutation is an
explicit action here, against Gmail itself.

Review is three actions — archive, unsubscribe, keep — and every one of them
files the message under a label (email_triage.TAXONOMY, all under `Filed/`),
because once a message is reviewed the only thing it is still for is being
found. The card arrives with the label already picked (the sender's own
record, else the model's) and a correction is just picking another before
acting. Labels are real Gmail labels, created on first use, so the filing
shows in Gmail as one tree and Gmail's own search can use it.

Unsubscribe ladder: RFC 8058 one-click (single HTTP POST) → mailto: variant
(send an email via the Gmail API) → hand the https link back for the browser.

Drafts are the one write that stops short of the mailbox's outside world: a
draft lands in Gmail, and sending it stays a human action in the Gmail UI. That
is the same line reply_draft.py draws for messages, for the same reason — an
auto-sent reply in someone's real conversation is the one mistake here that
can't be taken back (DESIGN.md §17: the feed suggests rather than acts).
The one exception is scheduled send (app/email_schedule.py): the user approves
a draft and a time on a card, and the draft goes at that time unattended.

Routes (all localhost-only, like everything else):
  GET  /api/email/queue           inbox cards: message + summary + label + capabilities
  POST /api/email/queue/refresh   sync Gmail + summarize new messages now
  GET  /api/email/labels          the label set for the picker
  GET  /api/email/search          ?q=&label=&days=&gmail= — find filed mail
  GET  /api/email/{msg_id}/body   the message text itself, read through to Gmail
  POST /api/email/{msg_id}/action {action: archive|unsubscribe|keep, label?}
  POST /api/email/{msg_id}/undo
  POST /api/email/draft           {to?, subject?, body, reply_to?, cc?, attach?, replaces?}
  GET  /api/email/scheduled       pending scheduled sends, then recent outcomes
  POST /api/email/scheduled       {send_at, draft_id? | body, to?, subject?, reply_to?, cc?, attach?}
  POST /api/email/scheduled/{id}/cancel
"""

import base64
import json
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from email.message import EmailMessage

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from .. import db
from ..config import GMAIL_ACCOUNT
from ..providers import attachments
from ..providers import email as email_provider
from ..providers import email_learn, email_search, email_triage
from ..providers import oauth, offline

router = APIRouter(prefix="/api/email")

API_BASE = email_provider.API_BASE


def access_token() -> str:
    try:
        return oauth.get_token("google", GMAIL_ACCOUNT)
    except oauth.NotAuthorized as e:
        raise HTTPException(400, str(e))


def api_call(url: str, token: str, method: str = "GET", body: dict | None = None,
         **kwargs) -> dict:
    """`idempotent=True` (see http.json_request) for a POST that's safe to
    re-send. Network failures pass through json_request untouched, so without
    the second except a dropped connection left this as a bare 500."""
    try:
        return email_provider.api_request(url, token, method=method, body=body,
                                       **kwargs)
    except RuntimeError as e:
        raise HTTPException(502, str(e))
    except offline.NETWORK_ERRORS as e:
        raise HTTPException(502, f"gmail unreachable: {e!r}")


# ---------- unsubscribe header parsing ----------

def _unsub_targets(header: str | None) -> dict:
    """List-Unsubscribe -> {'https': url|None, 'mailto': url|None}."""
    out = {"https": None, "mailto": None}
    for part in (header or "").split(","):
        url = part.strip().strip("<>")
        if url.startswith("https://") and not out["https"]:
            out["https"] = url
        elif url.startswith("mailto:") and not out["mailto"]:
            out["mailto"] = url
    return out


def _unsub_capability(row) -> str:
    """What unsubscribe can do for this message: one_click|mailto|link|none."""
    t = _unsub_targets(row["list_unsubscribe"])
    if row["list_unsubscribe_post"] and t["https"]:
        return "one_click"
    if t["mailto"]:
        return "mailto"
    if t["https"]:
        return "link"
    return "none"


# urllib's default "Python-urllib/3.x" is on Cloudflare's banned-signature
# list: loops.so (and anything else fronted that way) answers 403 "error code:
# 1010" before the request reaches the sender's app. That was every one-click
# failure from 10-26 on. Any honest non-default agent gets through.
_UNSUB_UA = "Mozilla/5.0 (compatible; Kestrel/1.0; RFC 8058 one-click unsubscribe)"


def _one_click(url: str) -> None:
    """RFC 8058: a single POST, no auth, fixed body."""
    req = urllib.request.Request(
        url, data=b"List-Unsubscribe=One-Click", method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded",
                 "User-Agent": _UNSUB_UA})
    try:
        with urllib.request.urlopen(req, timeout=30):
            pass
    except urllib.error.HTTPError as e:
        raise HTTPException(502, f"unsubscribe endpoint returned {e.code}")
    except Exception as e:
        raise HTTPException(502, f"unsubscribe POST failed: {e}")


def _mailto_unsubscribe(token: str, mailto: str) -> None:
    """Send the unsubscribe email the mailto: URL asks for, via Gmail."""
    parsed = urllib.parse.urlparse(mailto)
    params = urllib.parse.parse_qs(parsed.query)
    msg = EmailMessage()
    msg["To"] = parsed.path
    msg["Subject"] = params.get("subject", ["unsubscribe"])[0]
    msg.set_content(params.get("body", ["unsubscribe"])[0])
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    api_call(f"{API_BASE}/messages/send", token, method="POST", body={"raw": raw})


# ---------- queue ----------

@router.get("/queue")
def get_queue():
    """One card per inbox message not yet reviewed, newest first."""
    # One connection for the whole request: nothing below touches the network.
    with db.session() as con:
        return _queue(con)


def _queue(con) -> dict:
    rows = con.execute(
        "SELECT e.*, t.summary, t.action, t.label, t.reason"
        " FROM emails e LEFT JOIN email_triage t ON t.msg_id = e.msg_id"
        " WHERE e.in_inbox=1 AND t.user_action IS NULL"
        " ORDER BY e.received_at DESC").fetchall()
    refreshed = con.execute(
        "SELECT MAX(refreshed_at) AS r FROM emails WHERE in_inbox=1"
    ).fetchone()["r"]
    # The recommendation is the learner's (providers/email_learn.py), not the
    # model's. Measured over 875 decisions the model agreed 28.9% of the time
    # against 78.6% for a bare "archive", so its action is not used as the
    # recommendation at all — it stays on the card as `model_action` so the
    # disagreement is visible rather than quietly discarded.
    #
    # The one thing the model is genuinely good at is spotting a newsletter:
    # it flagged 28 of the 30 messages actually unsubscribed from (93%
    # recall), at 28% precision. Too imprecise to *recommend*, too useful to
    # drop — so it rides along as `unsubscribable`, an offer rather than the
    # headline, and only where Gmail gives us a way to act on it.
    email_learn.ensure_warm(con)

    cards = []
    for r in rows:
        hint = email_learn.predict(con, r["from_addr"])
        # The label, like the action, is the record's before it is the
        # model's: where this sender's mail was last filed twice running.
        # That is also the whole correction loop — relabel a sender twice and
        # its next card already carries the new label.
        filed = email_learn.predict_label(con, r["from_addr"])
        unsub_cap = _unsub_capability(r)
        cards.append({
            "id": r["msg_id"],
            "thread_id": r["thread_id"],
            "day": r["day"],
            "received": r["received_at"],
            "from_name": r["from_name"],
            "from_addr": r["from_addr"],
            "subject": r["subject"],
            "snippet": r["snippet"],
            "unread": bool(r["unread"]),
            "summary": r["summary"],
            # Falling back to "archive" rather than the model's guess is the
            # whole point: it recommended "keep" 331 times and you kept 8.
            "action": hint["action"] if hint else "archive",
            "why": hint["reason"] if hint else "no history for this sender yet",
            "basis": hint["basis"] if hint else "default",
            "confidence": hint["confidence"] if hint else None,
            "model_action": r["action"],
            "reason": r["reason"],
            "label": filed["label"] if filed else r["label"],
            "label_basis": ("sender" if filed else
                            "model" if r["label"] else None),
            "label_why": filed["reason"] if filed else None,
            "unsubscribable": r["action"] == "unsubscribe" and unsub_cap != "none",
            "unsubscribe": unsub_cap,
        })
    # Files the mail carried, same as the message log shows for texts — an
    # attached PDF is often the whole point of the mail, and a card that
    # doesn't mention it pins to the console as if it were body text only.
    attachments.decorate(con, cards, source="email", key="id")
    return {"count": len(cards),
            "unread": sum(c["unread"] for c in cards),
            "unsummarized": sum(1 for c in cards if c["summary"] is None),
            "refreshed_at": refreshed,
            "cards": cards}


@router.post("/queue/refresh")
def post_queue_refresh():
    """Sync Gmail and summarize anything new, right now (poller does the same
    every PROVIDER_REFRESH_SECONDS). Runs in FastAPI's worker threadpool."""
    try:
        return email_provider.refresh()
    except oauth.NotAuthorized as e:
        raise HTTPException(400, str(e))
    except (RuntimeError, ValueError) as e:
        raise HTTPException(502, str(e))


@router.get("/labels")
def get_labels():
    """The label set for the picker: the fixed taxonomy, this term's course
    sublabels, and whatever has been added under Filed/ in Gmail. Works with
    Gmail unreachable — the fixed part never needed it."""
    try:
        names = email_triage.gmail_label_names(oauth.get_token("google", GMAIL_ACCOUNT))
    except oauth.NotAuthorized:
        names = []
    return email_triage.labels(names)


@router.get("/search")
def get_search(q: str = "", label: str | None = None, days: int | None = None,
               gmail: bool | None = None, limit: int = 30):
    """Find mail again: the local index first, Gmail after (see
    providers/email_search.py). `gmail=true` forces the Gmail leg, `false`
    forbids it; omitted, it runs only when nothing local matched."""
    if not q.strip() and not label:
        return {"query": q, "count": 0, "data": [], "gmail": False}
    try:
        return email_search.search(q, label=label, days=days,
                                   limit=min(max(limit, 1), 100), use_gmail=gmail)
    except oauth.NotAuthorized as e:
        raise HTTPException(400, str(e))
    except RuntimeError as e:
        raise HTTPException(502, str(e))


# ---------- labels ----------

# Gmail label name -> id, filled on first use. Label ids never change once
# made, so the only way this goes stale is a label deleted in Gmail — and
# the modify that follows fails loudly (400), which clears the entry.
_label_ids: dict[str, str] = {}


def _label_id(token: str, name: str) -> str:
    """The Gmail id for a label under Filed/, creating it (and its parents,
    so it nests) the first time it's used."""
    if name not in _label_ids:
        found = api_call(f"{API_BASE}/labels", token).get("labels", [])
        _label_ids.update({l["name"]: l["id"] for l in found})
    parts = name.split("/")
    for i in range(1, len(parts) + 1):
        prefix = "/".join(parts[:i])
        if prefix not in _label_ids:
            made = api_call(f"{API_BASE}/labels", token, method="POST",
                        body={"name": prefix})
            _label_ids[prefix] = made["id"]
    return _label_ids[name]


def _clean_label(raw: str | None) -> str | None:
    """A picker value -> a full Filed/ name, or None. A bare name is taken as
    a new label under Filed/ — review never files outside its own tree."""
    parts = [p.strip() for p in (raw or "").split("/") if p.strip()]
    if not parts:
        return None
    if parts[0] != email_triage.PARENT:
        parts.insert(0, email_triage.PARENT)
    if len(parts) < 2:
        return None
    name = "/".join(parts)
    if len(name) > 120:
        raise HTTPException(400, "label name too long")
    return name


# ---------- reading ----------

@router.get("/{msg_id}/body")
def get_body(msg_id: str, chars: int | None = None, html: bool = False):
    """The message itself, so a card can be read without leaving for Gmail.

    Read-through, not cached: the body comes from Gmail on each request and
    `emails` stays metadata-only (§5). `body` is always text — text/plain, or
    the HTML part with its tags stripped. `?html=true` adds the HTML part too
    (defanged, see email_triage.fetch_html), which only the reader modal asks
    for: it renders it in a script-less sandboxed iframe whose CSP blocks
    remote loads, so a tracking pixel fires only if you press "Load images".
    Reading here doesn't mark the message read in Gmail; only an action does.
    """
    _row(msg_id)                     # 404 before the round-trip, as actions do
    try:
        out = email_provider.body(msg_id, chars, html)
        with db.session() as con:
            out["attachments"] = attachments.for_source(
                con, "email", [msg_id]).get(msg_id, [])
        return out
    except ValueError as e:          # unauthorized, or the cache row vanished
        raise HTTPException(400, str(e))
    except RuntimeError as e:
        # Gmail's 404 is an answer, not an outage: the message was deleted or
        # moved since the queue was cached. As a 502 it filed itself in the
        # failure log as a broken upstream.
        if "gmail API 404:" in str(e):
            raise HTTPException(404, "message is no longer in Gmail — "
                                     "refresh the queue")
        raise HTTPException(502, str(e))


# ---------- actions ----------

ACTIONS = ("archive", "unsubscribe", "keep")


class ActionIn(BaseModel):
    action: str                    # archive | unsubscribe | keep
    label: str | None = None       # Filed/… to file it under (created if new)


def _record(msg_id: str, action: str, *, left_inbox: bool, label: str | None,
            undo: dict | None = None) -> None:
    now = datetime.now().isoformat(timespec="seconds")
    state = json.dumps(undo) if undo else None
    with db.session() as con, con:
        con.execute(
            "INSERT INTO email_triage (msg_id, user_action, user_action_at,"
            " user_label, undo_state) VALUES (?,?,?,?,?)"
            " ON CONFLICT(msg_id) DO UPDATE SET"
            " user_action=excluded.user_action,"
            " user_action_at=excluded.user_action_at,"
            " user_label=excluded.user_label,"
            " undo_state=excluded.undo_state",
            (msg_id, action, now, label, state))
        if left_inbox:
            con.execute("UPDATE emails SET in_inbox=0, unread=0 WHERE msg_id=?",
                        (msg_id,))
        # Fold the decision into the sender prior. Same transaction as the
        # triage row on purpose: the aggregate is derived from it, and a
        # commit that recorded one without the other would leave the learner
        # counting a decision the record does not contain (or missing one it
        # does) until the next rebuild().
        sender = con.execute("SELECT from_addr FROM emails WHERE msg_id=?",
                             (msg_id,)).fetchone()
        if sender:
            email_learn.record(con, sender["from_addr"], action, now)


def _row(msg_id: str):
    with db.session() as con:
        row = con.execute("SELECT * FROM emails WHERE msg_id=?", (msg_id,)).fetchone()
    if row is None:
        raise HTTPException(404, f"unknown message {msg_id!r} — refresh the queue")
    return row


@router.post("/{msg_id}/action")
def post_action(msg_id: str, body: ActionIn):
    if body.action not in ACTIONS:
        raise HTTPException(400, f"unknown action {body.action!r}; "
                                 f"expected one of {', '.join(ACTIONS)}")
    row = _row(msg_id)
    label = _clean_label(body.label)
    token = access_token()

    # Snapshot for undo: whether the message was unread before we touched it.
    # Gmail won't tell us afterwards, so it has to be recorded going in.
    was_unread = bool(row["unread"])
    undo = {"unread": was_unread}

    performed = body.action
    if body.action == "unsubscribe":
        cap = _unsub_capability(row)
        targets = _unsub_targets(row["list_unsubscribe"])
        if cap == "one_click":
            try:
                _one_click(targets["https"])
            except HTTPException:
                # A refused or dropped POST reached nobody, so the next rung
                # down is still safe to try — the header usually offers both.
                if not targets["mailto"]:
                    raise
                _mailto_unsubscribe(token, targets["mailto"])
                cap = "mailto"
        elif cap == "mailto":
            _mailto_unsubscribe(token, targets["mailto"])
        elif cap == "link":
            # nothing programmatic — browser opens the link; card stays
            return {"ok": False, "performed": "link", "open_url": targets["https"]}
        else:
            raise HTTPException(400, "message has no List-Unsubscribe header")
        # The unsubscribe request itself can't be taken back — only the
        # archive that follows it. Undo says so rather than pretending.
        undo["unsubscribe_sent"] = True
        performed = cap

    # One modify does the whole filing: the label on, and — for everything
    # but keep — the inbox and unread flags off. Keep files too: a message
    # you're leaving in the inbox is still one you'll want to find later.
    add = [_label_id(token, label)] if label else []
    remove = [] if body.action == "keep" else ["INBOX", "UNREAD"]
    if add or remove:
        try:
            # Setting the same labels twice changes nothing, so this POST gets
            # the retry a read would: Gmail dropping the socket shouldn't fail
            # a card whose unsubscribe may already have gone out.
            api_call(f"{API_BASE}/messages/{msg_id}/modify", token, method="POST",
                 body={"addLabelIds": add, "removeLabelIds": remove},
                 idempotent=True)
        except HTTPException:
            _label_ids.clear()       # a label deleted in Gmail: re-read next time
            raise
    if add:
        undo["label_id"] = add[0]
    _record(msg_id, body.action, left_inbox=body.action != "keep",
            label=label, undo=undo)
    return {"ok": True, "performed": performed, "label": label}


def archive(msg_id: str) -> dict:
    """Archive one message by id, for the console's email_archive tool.

    A message the cache holds goes through `post_action` — the card's own
    archive, so the queue drops it, the learner counts it and the card's undo
    works. One the cache doesn't hold (found by search, older than the sync
    window) still leaves the Gmail inbox; there's just nothing local to update.
    """
    with db.session() as con:
        cached = con.execute("SELECT 1 FROM emails WHERE msg_id=?",
                             (msg_id,)).fetchone()
    if cached:
        post_action(msg_id, ActionIn(action="archive"))
        return {"id": msg_id, "cached": True}
    api_call(f"{API_BASE}/messages/{msg_id}/modify", access_token(), method="POST",
         body={"addLabelIds": [], "removeLabelIds": ["INBOX", "UNREAD"]},
         idempotent=True)
    return {"id": msg_id, "cached": False}


@router.post("/{msg_id}/undo")
def post_undo(msg_id: str):
    """Reverse the last action on a message and put it back in the queue.

    Undoes the *Gmail* side (re-add INBOX, take the filing label back off,
    restore unread; untrash for a `delete` from before review dropped it) and
    clears the triage row's user_action/user_label so the card returns to
    the review queue. An unsubscribe request already sent to the sender can't
    be recalled — the reply says so and the archive is still undone."""
    with db.session() as con:
        row = con.execute(
            "SELECT t.user_action, t.undo_state FROM email_triage t WHERE t.msg_id=?",
            (msg_id,)).fetchone()
    if row is None or not row["user_action"]:
        raise HTTPException(404, f"nothing to undo for {msg_id!r}")

    action = row["user_action"]
    state = json.loads(row["undo_state"]) if row["undo_state"] else {}
    token = access_token()

    if action == "delete":
        api_call(f"{API_BASE}/messages/{msg_id}/untrash", token, method="POST")

    add = ([] if action == "keep" else
           ["INBOX"] + (["UNREAD"] if state.get("unread") else []))
    remove = [state["label_id"]] if state.get("label_id") else []
    if add or remove:
        api_call(f"{API_BASE}/messages/{msg_id}/modify", token, method="POST",
             body={"addLabelIds": add, "removeLabelIds": remove})

    now = datetime.now().isoformat(timespec="seconds")
    with db.session() as con, con:
        con.execute(
            "UPDATE email_triage SET user_action=NULL, user_action_at=?,"
            " user_label=NULL, undo_state=NULL WHERE msg_id=?", (now, msg_id))
        if action != "keep":
            con.execute("UPDATE emails SET in_inbox=1, unread=? WHERE msg_id=?",
                        (1 if state.get("unread") else 0, msg_id))
        # An undone action is a decision retracted, so the learner has to
        # forget it — otherwise undo is the one gesture that teaches the
        # learner the opposite of what it means, and a sender gets more
        # confidently wrong every time the call is corrected.
        email_learn.unrecord(con, msg_id, action)
    return {"ok": True, "undone": action,
            "unsubscribe_sent": bool(state.get("unsubscribe_sent"))}


# ---------- drafts ----------

# What a reply needs from the message it answers. Gmail threads a reply on the
# In-Reply-To/References headers, not on threadId alone — a draft posted with
# only threadId still opens a fresh visual thread on the recipient's side — so
# the parent is fetched for its Message-ID rather than trusting the caller to
# carry one.
_REPLY_HEADERS = ("Message-ID", "Subject", "From", "Reply-To", "References")
DRAFT_MAX_BYTES = 25 * 1024 * 1024


def _reply_context(token: str, msg_id: str) -> dict:
    """Thread id + headers for a reply to `msg_id`."""
    params = urllib.parse.urlencode(
        [("format", "metadata")]
        + [("metadataHeaders", h) for h in _REPLY_HEADERS])
    msg = api_call(f"{API_BASE}/messages/{msg_id}?{params}", token)
    payload = msg.get("payload", {})
    header = email_provider.header
    _, from_addr = email_provider.split_from(header(payload, "From"))
    _, reply_to = email_provider.split_from(header(payload, "Reply-To"))
    return {"thread_id": msg.get("threadId"),
            "message_id": header(payload, "Message-ID"),
            "references": header(payload, "References"),
            "subject": header(payload, "Subject"),
            "to": reply_to or from_addr}


def create_draft(*, body: str, to: str | None = None, subject: str | None = None,
                 reply_to: str | None = None, cc: str | None = None,
                 attach: list[int] | None = None,
                 replaces: str | None = None) -> dict:
    """Put a draft in Gmail. Never sends — `drafts.create` is the whole write
    (or `drafts.update`, when `replaces` names an earlier draft being revised).

    With `reply_to` (a message id) the draft is threaded onto that message and
    both recipient and subject default to it, so replying takes a body and
    nothing else. Without it this is a fresh message and `to` is required.

    `attach` is attachment-table ids (what the console's message_files prints
    as #id) — a file that arrived by iMessage or email, re-sent from here.
    Every one is loaded before Gmail is touched, so a missing file refuses the
    draft instead of leaving one in the mailbox without it.

    The account's own address is the sender: the Gmail API fills From from the
    authenticated user, and gmail.modify is scope enough for all of this.
    """
    text = (body or "").strip()
    if not text:
        raise HTTPException(400, "a draft needs a body")
    files = []
    if attach:
        from ..providers import attachments
        for aid in dict.fromkeys(attach):         # dedupe, keep order
            try:
                files.append(attachments.load(int(aid)))
            except ValueError as e:
                raise HTTPException(400, f"can't attach: {e}")
    token = access_token()

    parent = _reply_context(token, reply_to) if reply_to else {}
    to = (to or parent.get("to") or "").strip()
    if not to:
        raise HTTPException(
            400, "no recipient: pass `to`, or `reply_to` to answer a message")
    subject = (subject or "").strip() or parent.get("subject") or "(no subject)"
    if reply_to and not subject.lower().startswith("re:"):
        subject = f"Re: {subject}"

    msg = EmailMessage()
    msg["To"] = to
    if cc:
        msg["Cc"] = cc
    msg["Subject"] = subject
    if parent.get("message_id"):
        msg["In-Reply-To"] = parent["message_id"]
        # References is the whole chain, oldest first — appending keeps a long
        # thread threaded instead of restarting it at this reply.
        prior = parent.get("references")
        msg["References"] = (f"{prior} {parent['message_id']}" if prior
                             else parent["message_id"])
    msg.set_content(text)
    for name, mime, data in files:
        maintype, _, subtype = mime.partition("/")
        msg.add_attachment(data, maintype=maintype, subtype=subtype,
                           filename=name)

    raw = msg.as_bytes()
    # Gmail's own ceiling on a message it will send; a draft past it could be
    # saved but never delivered.
    if len(raw) > DRAFT_MAX_BYTES:
        raise HTTPException(
            400, f"attachments too large: {len(raw) / 1048576:.1f}MB encoded, "
                 f"Gmail sends at most {DRAFT_MAX_BYTES // 1048576}MB")
    payload = {"message": {"raw": base64.urlsafe_b64encode(raw).decode()}}
    if parent.get("thread_id"):
        payload["message"]["threadId"] = parent["thread_id"]
    replaced = False
    created = None
    replaces = (replaces or "").strip()
    if replaces:
        # drafts.update swaps the message under the same draft id, so a
        # revision leaves one draft in the mailbox instead of two. Session
        # 56a834a2 is why: a second email_draft for a revised USC parking
        # email saved a new draft and left the first — "[USC ID]"
        # placeholder and all — sitting in Gmail beside it. A 404 means the
        # earlier draft was sent or deleted in Gmail meanwhile; the revision
        # then becomes a new draft rather than failing.
        try:
            created = api_call(f"{API_BASE}/drafts/{urllib.parse.quote(replaces, safe='')}",
                           token, method="PUT", body={"id": replaces, **payload})
            replaced = True
        except HTTPException as e:
            if " 404:" not in str(e.detail):
                raise
    if created is None:
        created = api_call(f"{API_BASE}/drafts", token, method="POST", body=payload)
    return {"ok": True, "draft_id": created.get("id"),
            "replaced": replaced if replaces else None,
            "thread_id": created.get("message", {}).get("threadId"),
            "to": to, "subject": subject,
            "attached": [name for name, _, _ in files],
            "url": "https://mail.google.com/mail/u/0/#drafts"}


class DraftIn(BaseModel):
    body: str
    to: str | None = None
    subject: str | None = None
    reply_to: str | None = None      # message id being answered
    cc: str | None = None
    attach: list[int] | None = None  # attachment ids (message_files' #id)
    replaces: str | None = None      # draft id being revised (drafts.update)


@router.post("/draft")
def post_draft(payload: DraftIn):
    """Write a draft into Gmail, ready for the user to read and send."""
    return create_draft(body=payload.body, to=payload.to,
                        subject=payload.subject, reply_to=payload.reply_to,
                        cc=payload.cc, attach=payload.attach,
                        replaces=payload.replaces)


# ---------- scheduled send (app/email_schedule.py) ----------

class ScheduleIn(BaseModel):
    send_at: str                     # ISO; naive = local time
    draft_id: str | None = None      # schedule an existing draft…
    body: str | None = None          # …or write one, as /draft takes
    to: str | None = None
    subject: str | None = None
    reply_to: str | None = None
    cc: str | None = None
    attach: list[int] | None = None


@router.get("/scheduled")
def get_scheduled():
    """Pending sends, then the latest outcomes."""
    from .. import email_schedule
    return {"items": email_schedule.recent()}


@router.post("/scheduled")
def post_scheduled(payload: ScheduleIn):
    from .. import email_schedule
    return email_schedule.schedule(**payload.model_dump())


@router.post("/scheduled/{sid}/cancel")
def post_scheduled_cancel(sid: int):
    from .. import email_schedule
    return email_schedule.cancel(sid)
