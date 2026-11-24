"""Writing a draft into Gmail (`POST /api/email/draft`, the console's
email_draft tool).

Tests the RFC822 message: Gmail threads on In-Reply-To/References, not `threadId`
alone, so the headers are the feature. Also that nothing reaches `messages/send`.
"""

import base64
import email as stdlib_email
import email.policy

import pytest
from fastapi import HTTPException

from app.routes import email as email_routes

PARENT_ID = "1a00ad13fcc2d430"

PARENT = {
    "id": PARENT_ID,
    "threadId": "1a004433c5dd0046",
    "payload": {"headers": [
        {"name": "Message-ID", "value": "<parent@mail.example.org>"},
        {"name": "Subject", "value": "Chess Club Onboarding"},
        {"name": "From", "value": "Events UPIFC <events@universityparkifc.org>"},
        {"name": "References", "value": "<older@mail.example.org>"},
    ]},
}


@pytest.fixture
def gmail(monkeypatch):
    """Stand in for Gmail: record calls, serve PARENT, return a draft id."""
    calls = []

    def fake_api(url, token, method="GET", body=None):
        calls.append({"url": url, "method": method, "body": body})
        if "/drafts" in url:
            return {"id": "r123", "message": {"id": "m1",
                                              "threadId": PARENT["threadId"]}}
        return PARENT

    monkeypatch.setattr(email_routes, "api_call", fake_api)
    monkeypatch.setattr(email_routes, "access_token", lambda: "tok")
    return calls


def sent_message(calls):
    """The RFC822 message posted to /drafts, parsed back."""
    post = [c for c in calls if "/drafts" in c["url"]][-1]
    raw = post["body"]["message"]["raw"]
    return stdlib_email.message_from_bytes(base64.urlsafe_b64decode(raw),
                                           policy=stdlib_email.policy.default)


# ---------- replies ----------

def test_a_reply_is_threaded_on_the_message_it_answers(gmail):
    out = email_routes.create_draft(body="Tuesday works.", reply_to=PARENT_ID)

    msg = sent_message(gmail)
    assert msg["In-Reply-To"] == "<parent@mail.example.org>"
    # References is the whole chain, oldest first (dropping it restarts threading).
    assert msg["References"] == "<older@mail.example.org> <parent@mail.example.org>"
    post = [c for c in gmail if "/drafts" in c["url"]][-1]
    assert post["body"]["message"]["threadId"] == PARENT["threadId"]
    assert out["draft_id"] == "r123"


def test_a_reply_takes_its_recipient_and_subject_from_the_parent(gmail):
    email_routes.create_draft(body="Tuesday works.", reply_to=PARENT_ID)

    msg = sent_message(gmail)
    assert msg["To"] == "events@universityparkifc.org"
    assert msg["Subject"] == "Re: Chess Club Onboarding"


def test_an_already_re_subject_is_not_re_re(gmail):
    PARENT["payload"]["headers"][1]["value"] = "Re: Chess Club Onboarding"
    try:
        email_routes.create_draft(body="ok", reply_to=PARENT_ID)
        assert sent_message(gmail)["Subject"] == "Re: Chess Club Onboarding"
    finally:
        PARENT["payload"]["headers"][1]["value"] = "Chess Club Onboarding"


def test_reply_to_prefers_the_reply_to_header_over_from(gmail):
    PARENT["payload"]["headers"].append(
        {"name": "Reply-To", "value": "desk@universityparkifc.org"})
    try:
        email_routes.create_draft(body="ok", reply_to=PARENT_ID)
        assert sent_message(gmail)["To"] == "desk@universityparkifc.org"
    finally:
        PARENT["payload"]["headers"].pop()


def test_an_explicit_recipient_overrides_the_parent(gmail):
    email_routes.create_draft(body="ok", reply_to=PARENT_ID,
                              to="someone@example.com", cc="cc@example.com")

    msg = sent_message(gmail)
    assert msg["To"] == "someone@example.com"
    assert msg["Cc"] == "cc@example.com"


# ---------- new messages ----------

def test_a_new_message_carries_no_threading_headers(gmail):
    email_routes.create_draft(body="hello", to="a@b.co", subject="Hi")

    msg = sent_message(gmail)
    assert msg["In-Reply-To"] is None and msg["References"] is None
    assert msg["Subject"] == "Hi"
    post = [c for c in gmail if "/drafts" in c["url"]][-1]
    assert "threadId" not in post["body"]["message"]


def test_a_new_message_without_a_recipient_is_refused(gmail):
    with pytest.raises(HTTPException) as e:
        email_routes.create_draft(body="hello", subject="Hi")
    assert e.value.status_code == 400
    assert not gmail            # nothing was posted


def test_an_empty_body_is_refused_before_gmail_is_touched(gmail):
    with pytest.raises(HTTPException) as e:
        email_routes.create_draft(body="   ", to="a@b.co")
    assert e.value.status_code == 400
    assert not gmail


# ---------- attachments ----------

PDF = b"%PDF-1.4 fake"


@pytest.fixture
def files(monkeypatch):
    """Attachment ids 7 (a PDF) and 8 (a photo); anything else is missing."""
    from app.providers import attachments
    table = {7: ("CP_575_E.pdf", "application/pdf", PDF),
             8: ("IMG_1.jpeg", "image/jpeg", b"\xff\xd8\xffjpeg")}

    def load(aid):
        if aid not in table:
            raise ValueError(f"no file #{aid}")
        return table[aid]

    monkeypatch.setattr(attachments, "load", load)


def test_attached_files_ride_on_the_draft(gmail, files):
    out = email_routes.create_draft(body="EIN letter attached.", to="a@b.co",
                                    subject="EIN", attach=[7, 8])

    msg = sent_message(gmail)
    parts = list(msg.iter_attachments())
    assert [p.get_filename() for p in parts] == ["CP_575_E.pdf", "IMG_1.jpeg"]
    assert parts[0].get_content_type() == "application/pdf"
    assert parts[0].get_payload(decode=True) == PDF
    # The body is still the first, plain-text part.
    assert "EIN letter attached." in msg.get_body(("plain",)).get_content()
    assert out["attached"] == ["CP_575_E.pdf", "IMG_1.jpeg"]


def test_a_repeated_id_attaches_once(gmail, files):
    email_routes.create_draft(body="x", to="a@b.co", attach=[7, 7])
    assert len(list(sent_message(gmail).iter_attachments())) == 1


def test_a_missing_file_refuses_the_draft_before_gmail(gmail, files):
    with pytest.raises(HTTPException) as e:
        email_routes.create_draft(body="x", to="a@b.co", attach=[7, 99])
    assert e.value.status_code == 400 and "#99" in e.value.detail
    assert not gmail            # no attachment-less draft left behind


def test_an_oversized_draft_is_refused(gmail, monkeypatch):
    from app.providers import attachments
    monkeypatch.setattr(attachments, "load",
                        lambda aid: ("big.bin", "application/octet-stream",
                                     b"\0" * 1024))
    monkeypatch.setattr(email_routes, "DRAFT_MAX_BYTES", 1024)
    with pytest.raises(HTTPException) as e:
        email_routes.create_draft(body="x", to="a@b.co", attach=[1])
    assert "too large" in e.value.detail
    assert not [c for c in gmail if "/drafts" in c["url"]]


def test_the_console_tool_names_what_it_attached(gmail, files):
    said = _call(body="attached", to="a@b.co", attach=["#7"])
    assert "CP_575_E.pdf attached" in said
    assert len(list(sent_message(gmail).iter_attachments())) == 1


def test_the_console_tool_refuses_a_non_numeric_id(gmail):
    assert "message_files" in _call(body="x", to="a@b.co", attach=["the pdf"])
    assert not gmail


# ---------- the console tool ----------

def _call(**args):
    import asyncio

    from app import console_tools
    handler = [t for t in console_tools.TOOLS if t.name == "email_draft"][0]
    out = asyncio.run(handler.handler(args))
    return out["content"][0]["text"]


def test_the_console_tool_says_the_draft_is_unsent(gmail):
    said = _call(body="Tuesday works.", reply_to=PARENT_ID)

    assert "events@universityparkifc.org" in said
    # The model must relay that a draft is not a sent reply.
    assert "NOT been sent" in said


def test_the_console_tool_drops_blank_optionals(gmail):
    """Blank "" fields fall back to the parent's sender, not an empty To header."""
    _call(body="ok", reply_to=PARENT_ID, to="", subject="", cc="")

    assert sent_message(gmail)["To"] == "events@universityparkifc.org"


def test_the_console_tool_reports_a_refusal_instead_of_raising(monkeypatch):
    def boom(**kwargs):
        raise HTTPException(400, "a draft needs a body")

    from app.routes import email as routes
    monkeypatch.setattr(routes, "create_draft", boom)
    assert "a draft needs a body" in _call(body=" ")


# ---------- revising a draft ----------
#
# A revision replaces the draft in place; a second email_draft call used to leave the first beside it.

def test_replaces_rewrites_the_draft_in_place(gmail):
    out = email_routes.create_draft(body="v2", reply_to=PARENT_ID,
                                    replaces="r123")
    writes = [c for c in gmail if "/drafts" in c["url"]]
    assert [(c["method"], c["url"]) for c in writes] == [
        ("PUT", f"{email_routes.API_BASE}/drafts/r123")]
    assert writes[0]["body"]["id"] == "r123"
    assert writes[0]["body"]["message"]["threadId"] == PARENT["threadId"]
    assert "v2" in sent_message(gmail).get_body(("plain",)).get_content()
    assert out["replaced"] is True and out["draft_id"] == "r123"


def test_replacing_a_draft_already_gone_saves_a_new_one(gmail, monkeypatch):
    real = email_routes.api_call

    def api(url, token, method="GET", body=None):
        if method == "PUT":
            gmail.append({"url": url, "method": method, "body": body})
            raise HTTPException(502, "gmail API 404: Requested entity was not found.")
        return real(url, token, method=method, body=body)

    monkeypatch.setattr(email_routes, "api_call", api)
    out = email_routes.create_draft(body="v2", to="a@b.co", replaces="rGONE")
    assert [c["method"] for c in gmail if "/drafts" in c["url"]] == ["PUT", "POST"]
    assert out["replaced"] is False and out["draft_id"] == "r123"


def test_replacing_fails_loudly_on_anything_but_404(gmail, monkeypatch):
    """A wrong id or a bad payload must not quietly turn into a second draft."""
    def api(url, token, method="GET", body=None):
        gmail.append({"url": url, "method": method, "body": body})
        raise HTTPException(502, "gmail API 400: Invalid draft id")

    monkeypatch.setattr(email_routes, "api_call", api)
    with pytest.raises(HTTPException):
        email_routes.create_draft(body="v2", to="a@b.co", replaces="bogus")
    assert [c["method"] for c in gmail] == ["PUT"]


def test_the_console_tool_hands_back_the_draft_id(gmail):
    said = _call(body="v1", to="a@b.co", subject="Parking")
    assert "r123" in said and "replaces" in said
    said = _call(body="v2", to="a@b.co", subject="Parking", replaces="r123")
    assert "rewritten" in said and "NOT been sent" in said
    assert [c["method"] for c in gmail if "/drafts" in c["url"]] == ["POST", "PUT"]


# ---------- the line this write does not cross ----------

def test_the_draft_path_never_sends(gmail):
    email_routes.create_draft(body="ok", reply_to=PARENT_ID)

    assert all("/messages/send" not in c["url"] for c in gmail)
    assert [c["url"] for c in gmail if c["method"] == "POST"] == [
        f"{email_routes.API_BASE}/drafts"]
