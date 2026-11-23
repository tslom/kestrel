"""Reading one email whole (`--body`, the console's email_body tool, and the
dashboard's own reader via GET /api/email/{id}/body).

The rule the provider keeps is "never stored", not "no bodies" — so body()
fetches live and returns nothing to the cache. The part worth testing is what
happens when a message is too long: the quoted reply chain goes before any
truncation, because a forwarded invite carries its link *below* the quote
marker and a blind cut is how it goes missing.
"""

import urllib.error

import pytest

from app import db
from app.providers import email, email_triage

FORWARD = """---------- Forwarded message ---------
From: Advisor <advisor@example.org>

Monday, 8/3 at 630pm
https://example.zoom.us/j/86241138053

On Tue, Jul 14, 2026 at 3:27 PM Advisor <advisor@example.org>
wrote:

> Thanks for everyone's patience.
> More quoted history nobody needs.
"""


# ---------- strip_quotes ----------

@pytest.mark.parametrize("body, kept", [
    ("keep\nOn Tue, Jul 14 Bob wrote:\n> old", "keep"),
    # Gmail wraps the attribution over two lines, and sends CRLF — the pattern
    # has to survive both or it only ever matches the '>' block, leaving a
    # dangling "On ... wrote:" behind.
    ("keep\r\nOn Tue, Jul 14, 2026 at 3:27 PM Bob <b@c.d>\r\nwrote:\r\n> old",
     "keep"),
    ("keep\n-----Original Message-----\nFrom: x", "keep"),
    ("keep\n________________________________\nFrom: x", "keep"),
    ("keep\nFrom: Bob <b@c.d>\nSent: Monday\nTo: Me\nSubject: hi\nold", "keep"),
    ("a short body with no quote markers", "a short body with no quote markers"),
])
def test_strip_quotes_cuts_at_the_chain(body, kept):
    assert email_triage.strip_quotes(body) == kept


def test_a_body_that_is_entirely_quoted_strips_to_nothing():
    """body() relies on this being falsy to fall back to plain truncation."""
    assert email_triage.strip_quotes("> only quoted\n> lines") == ""


# ---------- body() ----------

def _cache_one(msg_id="abc123"):
    con = db.connect()
    try:
        con.execute(
            "INSERT INTO emails (msg_id, thread_id, day, received_at,"
            " from_name, from_addr, subject, snippet, in_inbox, unread,"
            " is_sent, refreshed_at) VALUES (?,?,?,?,?,?,?,?,1,0,0,?)",
            (msg_id, "t1", "2026-08-03", "2026-08-03T14:10:29-04:00",
             "Hollis", "b@example.org", "Fwd: Advisor meeting", "snip",
             "2026-08-03T15:00:00"))
        con.commit()
    finally:
        con.close()
    return msg_id


@pytest.fixture
def fetched(dbfile, monkeypatch):
    """body() with Gmail stubbed out; the test picks the body text."""
    monkeypatch.setattr(email, "access_token", lambda: "tok")

    def _set(text):
        monkeypatch.setattr(email_triage, "fetch_body", lambda t, m: text)
    return _set


def test_short_body_comes_back_whole(fetched):
    _cache_one()
    fetched(FORWARD)
    out = email.body("abc123")
    assert out["body"] == FORWARD                # under the cap: untouched
    assert out["quoted_trimmed"] is False
    assert out["truncated"] is False
    assert out["subject"] == "Fwd: Advisor meeting"


def test_over_the_cap_the_quote_chain_goes_before_the_text_does(fetched):
    _cache_one()
    fetched(FORWARD)
    out = email.body("abc123", limit=len(FORWARD) - 40)
    assert out["quoted_trimmed"] is True
    assert "86241138053" in out["body"]          # the link survived
    assert "Thanks for everyone's patience" not in out["body"]
    assert out["truncated"] is False             # trimming was enough


def test_truncation_is_the_fallback_when_trimming_is_not_enough(fetched):
    _cache_one()
    fetched(FORWARD)
    out = email.body("abc123", limit=30)
    assert out["truncated"] is True
    assert out["chars"] == 30
    assert len(out["body"]) == 30


def test_a_wholly_quoted_body_truncates_rather_than_emptying(fetched):
    """Stripping would leave nothing, so it must not be applied."""
    _cache_one()
    fetched("> quoted\n" * 50)
    out = email.body("abc123", limit=40)
    assert out["quoted_trimmed"] is False
    assert out["body"].startswith("> quoted")


def test_an_uncached_message_says_so_instead_of_calling_gmail(dbfile,
                                                              monkeypatch):
    monkeypatch.setattr(email, "access_token",
                        lambda: pytest.fail("must not reach Gmail"))
    with pytest.raises(ValueError, match="no cached message"):
        email.body("nope")


# ---------- the route the dashboard reads through ----------

@pytest.fixture
def route_client(router_client):
    """TestClient over the email router alone — no lifespan, no poller."""
    from app.routes import email as email_routes

    return router_client(email_routes.router)


def test_route_serves_the_message_text(route_client, fetched):
    _cache_one()
    fetched(FORWARD)
    r = route_client.get("/api/email/abc123/body")
    assert r.status_code == 200
    assert r.json()["body"] == FORWARD
    assert r.json()["subject"] == "Fwd: Advisor meeting"


def test_route_honours_the_char_cap(route_client, fetched):
    _cache_one()
    fetched(FORWARD)
    out = route_client.get("/api/email/abc123/body", params={"chars": 30}).json()
    assert out["truncated"] is True
    assert len(out["body"]) == 30


def test_route_404s_an_unknown_message_without_reaching_gmail(route_client,
                                                              monkeypatch):
    monkeypatch.setattr(email, "access_token",
                        lambda: pytest.fail("must not reach Gmail"))
    assert route_client.get("/api/email/nope/body").status_code == 404


def test_route_reports_a_missing_login_as_a_bad_request(route_client,
                                                        monkeypatch):
    """Not authorized is the one failure a reader will actually hit; it must
    arrive as a message the card can print, not a 500."""
    _cache_one()
    monkeypatch.setattr(email, "body",
                        lambda *a, **k: (_ for _ in ()).throw(
                            ValueError("not authorized for google")))
    r = route_client.get("/api/email/abc123/body")
    assert r.status_code == 400
    assert "not authorized" in r.json()["detail"]


@pytest.mark.parametrize("err,status", [
    ('gmail API 404: {"error": {"code": 404, "status": "NOT_FOUND"}}', 404),
    ("gmail API 500: backend error", 502),
])
def test_route_tells_a_deleted_message_from_an_outage(route_client, monkeypatch,
                                                      err, status):
    """Gmail's 404 means the message went away after the queue was cached —
    an answer, not a broken upstream for the failure log."""
    _cache_one()
    monkeypatch.setattr(email, "body", lambda *a, **k: (_ for _ in ()).throw(
        RuntimeError(err)))
    assert route_client.get("/api/email/abc123/body").status_code == status


# ---------- a failed fetch: raised to the reader, degraded for refresh ----------

def _gmail_fails(monkeypatch, exc):
    calls = []

    def boom(url, token, *a, **k):
        calls.append(url)
        raise exc
    monkeypatch.setattr(email, "api_request", boom)
    monkeypatch.setattr(email, "access_token", lambda: "tok")
    return calls


def test_fetch_body_raises_rather_than_returning_empty(monkeypatch):
    """An empty string is indistinguishable from an empty email."""
    _gmail_fails(monkeypatch, RuntimeError("gmail API 401: unauthorized"))
    with pytest.raises(RuntimeError, match="401"):
        email_triage.fetch_body("tok", "abc123")


def test_body_excerpt_still_degrades_to_empty(monkeypatch):
    """The refresh-time callers (triage, extraction) must survive one
    unreadable message, as they always have."""
    _gmail_fails(monkeypatch, RuntimeError("gmail API 401: unauthorized"))
    assert email_triage.body_excerpt("tok", "abc123") == ""
    _gmail_fails(monkeypatch, urllib.error.URLError("no route to host"))
    assert email_triage.body_excerpt("tok", "abc123") == ""


def test_body_surfaces_a_gmail_refusal(dbfile, monkeypatch):
    _cache_one()
    _gmail_fails(monkeypatch, RuntimeError("gmail API 401: unauthorized"))
    with pytest.raises(RuntimeError, match="401"):
        email.body("abc123")


def test_body_turns_a_lost_network_into_a_runtime_error(dbfile, monkeypatch):
    """So the route answers 502 and the CLI prints one line, not a trace."""
    _cache_one()
    _gmail_fails(monkeypatch, urllib.error.URLError("no route to host"))
    with pytest.raises(RuntimeError, match="gmail unreachable"):
        email.body("abc123")


@pytest.mark.parametrize("exc", [
    RuntimeError("gmail API 401: unauthorized"),
    urllib.error.URLError("no route to host"),
])
def test_route_reports_a_failed_fetch_as_a_bad_gateway(route_client,
                                                       monkeypatch, exc):
    _cache_one()
    _gmail_fails(monkeypatch, exc)
    r = route_client.get("/api/email/abc123/body")
    assert r.status_code == 502
    assert r.json()["detail"]


# ---------- one full fetch per message per refresh ----------

def _gmail_counts(monkeypatch):
    calls = []

    def fake(url, token, *a, **k):
        calls.append(url)
        return {"payload": {"mimeType": "text/plain",
                            "body": {"data": "aGVsbG8="}}}   # "hello"
    monkeypatch.setattr(email, "api_request", fake)
    return calls


def test_the_memo_serves_every_reader_inside_one_block(monkeypatch):
    calls = _gmail_counts(monkeypatch)
    with email.message_memo():
        email.full_message("tok", "m1")
        assert email_triage.body_excerpt("tok", "m1") == "hello"
        assert email_triage.fetch_body("tok", "m1") == "hello"
        email_triage.fetch_body("tok", "m2")
    assert len(calls) == 2                       # m1 once, m2 once
    email_triage.fetch_body("tok", "m1")         # outside: live again
    assert len(calls) == 3


def test_a_failed_fetch_is_not_memoized(monkeypatch):
    _gmail_fails(monkeypatch, RuntimeError("gmail API 503: busy"))
    with email.message_memo():
        assert email_triage.body_excerpt("tok", "m1") == ""
        calls = _gmail_counts(monkeypatch)
        assert email_triage.body_excerpt("tok", "m1") == "hello"
    assert len(calls) == 1


def test_refresh_fetches_each_message_once(dbfile, monkeypatch):
    """Attachments, triage and extraction all read m1 — one Gmail call.
    Extraction reads from a thread pool, as the real one does."""
    from concurrent.futures import ThreadPoolExecutor

    from app.providers import email_extract

    calls = _gmail_counts(monkeypatch)
    monkeypatch.setattr(email, "access_token", lambda: "tok")
    monkeypatch.setattr(email, "_sync_inbox", lambda con, token, now: {})
    monkeypatch.setattr(email, "_sync_days", lambda *a: 0)
    monkeypatch.setattr(email, "_sync_attachments",
                        lambda con, token, *a: bool(email.full_message(token, "m1")))
    monkeypatch.setattr(email_triage, "summarize_pending",
                        lambda token: len(email_triage.body_excerpt(token, "m1")))

    def extract(token):
        with ThreadPoolExecutor(max_workers=3) as pool:
            return sum(map(len, pool.map(
                lambda _: email_triage.body_excerpt(token, "m1"), range(3))))
    monkeypatch.setattr(email_extract, "refresh", extract)

    info = email.refresh()
    assert (info["attachments"], info["summarized"], info["extracted"]) == (True, 5, 15)
    assert len(calls) == 1
    assert email._memo is None                   # scoped to the refresh


def test_reading_never_touches_the_mailbox(route_client, fetched, monkeypatch):
    """The reader is a read: no modify, no label, nothing marked read."""
    from app.routes import email as email_routes

    monkeypatch.setattr(email_routes, "api_call",
                        lambda *a, **k: pytest.fail("route must not write"))
    _cache_one()
    fetched(FORWARD)
    assert route_client.get("/api/email/abc123/body").status_code == 200
    con = db.connect()
    try:
        row = con.execute("SELECT unread, in_inbox FROM emails"
                          " WHERE msg_id='abc123'").fetchone()
    finally:
        con.close()
    assert (row["unread"], row["in_inbox"]) == (0, 1)


# ---------- HTML to readable text ----------
#
# Most of the inbox is HTML-only, and what the reader shows for those messages
# is whatever _strip_html leaves behind — so this is where readability is won
# or lost.

def test_block_markup_becomes_lines_instead_of_one_run_of_words():
    html = "<p>First thing.</p><p>Second thing.</p><ul><li>one</li><li>two</li></ul>"
    assert email_triage._strip_html(html).split("\n") == [
        "First thing.", "", "Second thing.", "", "- one", "- two"]


def test_entities_arrive_as_characters():
    assert email_triage._strip_html(
        "<div>Ben &amp; Jerry&#39;s&nbsp;&mdash; we won&#8217;t meet</div>"
    ) == "Ben & Jerry's — we won’t meet"


def test_script_and_style_contents_never_reach_the_reader():
    html = "<style>.x{color:red}</style><script>alert(1)</script><p>words</p>"
    assert email_triage._strip_html(html) == "words"


def test_the_hidden_preheader_is_dropped_with_its_text():
    """The grey preview line repeats the opening sentence; no client shows it."""
    html = ('<div style="display:none;max-height:0px;">Preview text here</div>'
            '<p>Dear all,</p>')
    assert email_triage._strip_html(html) == "Dear all,"


def test_the_proofpoint_external_sender_banner_is_dropped():
    html = ("<p>real mail</p><table>ZjQcmQRYFpfptBannerStart<tr><td>External "
            "Sender: exercise caution.</td></tr>ZjQcmQRYFpfptBannerEnd</table>")
    assert email_triage._strip_html(html) == "real mail"


def test_a_short_link_keeps_its_address_and_a_tracking_link_does_not():
    """Anchor text always survives; the address only when it is worth reading."""
    tracker = "https://click.example.com/f/a/" + "x" * 200
    out = email_triage._strip_html(
        f'<p><a href="https://usc.zoom.us/j/95890">Join</a> or '
        f'<a href="{tracker}">Open now</a></p>')
    assert out == "Join (https://usc.zoom.us/j/95890) or Open now"


@pytest.mark.parametrize("wrapped, plain", [
    # The common case: a link with nothing Proofpoint had to encode.
    ("https://urldefense.com/v3/__https://usc.zoom.us/j/1234567890__;!!LIr3w8kk$",
     "https://usc.zoom.us/j/1234567890"),
    # '*' stands in for characters held in the token, so unwrapping this one
    # would hand back an address that does not resolve — leave it wrapped.
    ("https://urldefense.com/v3/__https://nl.nytimes.com/f/a**A/x__;fn5-!!q$",
     "https://urldefense.com/v3/__https://nl.nytimes.com/f/a**A/x__;fn5-!!q$"),
])
def test_proofpoint_wrapping_is_undone_only_when_it_is_lossless(wrapped, plain):
    assert email_triage.unwrap_link(wrapped) == plain
    assert email_triage._unwrap_links(f"(at the normal link: {wrapped} )") == \
        f"(at the normal link: {plain} )"


# ---------- the reader's formatted view ----------

def _b64(s):
    import base64
    return base64.urlsafe_b64encode(s.encode()).decode()


def _gmail_multipart(monkeypatch, plain, html):
    calls = []

    def fake(url, token, *a, **k):
        calls.append(url)
        return {"payload": {"mimeType": "multipart/alternative", "parts": [
            {"mimeType": "text/plain", "body": {"data": _b64(plain)}},
            {"mimeType": "text/html", "body": {"data": _b64(html)}}]}}
    monkeypatch.setattr(email, "api_request", fake)
    monkeypatch.setattr(email, "access_token", lambda: "tok")
    return calls


NEWSLETTER = """<html><head><meta http-equiv="refresh" content="0;url=https://evil.example/">
<base href="https://evil.example/"><link rel="stylesheet" href="https://t.example/a.css">
<style>td { color: red }</style></head><body>
<script>alert(1)</script><iframe src="https://t.example/"></iframe>
<table><tr><td><b>Big news</b></td></tr></table>
<a href="https://urldefense.com/v3/__https://example.org/page__;!!abc$">read</a>
</body></html>"""


def test_fetch_html_defangs_what_a_scriptless_frame_can_still_do(monkeypatch):
    _gmail_multipart(monkeypatch, "Big news", NEWSLETTER)
    out = email_triage.fetch_html("tok", "m1")
    for gone in ("<script", "alert(1)", "<iframe", "http-equiv", "<base",
                 "<link", "urldefense"):
        assert gone not in out
    assert "<style>td { color: red }</style>" in out     # layout survives
    assert "<b>Big news</b>" in out
    assert 'href="https://example.org/page"' in out       # unwrapped


def test_plain_only_message_has_no_html(monkeypatch):
    monkeypatch.setattr(email, "api_request", lambda *a, **k: {"payload": {
        "mimeType": "text/plain", "body": {"data": _b64("hi")}}})
    assert email_triage.fetch_html("tok", "m1") is None


def test_body_with_html_costs_one_gmail_call(dbfile, monkeypatch):
    _cache_one()
    calls = _gmail_multipart(monkeypatch, "Big news", NEWSLETTER)
    out = email.body("abc123", html=True)
    assert out["body"] == "Big news"                      # text still text/plain
    assert "<b>Big news</b>" in out["html"]
    assert len(calls) == 1
    assert "html" not in email.body("abc123")             # opt-in only


def test_css_dumped_into_the_plain_part_falls_back_to_the_html(monkeypatch):
    """Handshake's text/plain opens with its stylesheet."""
    soup = ("Sent on behalf of USC.\n\n  table.button td {\n  color: white;\n}\n"
            "a { color: #990000; }\na:hover { color: #a31919; }\n\nWorkshops")
    _gmail_multipart(monkeypatch, soup, "<p>Upcoming <b>workshops</b></p>")
    assert email_triage.fetch_body("tok", "m1") == "Upcoming workshops"


def test_a_stale_plain_part_falls_back_to_the_html(monkeypatch):
    """CASA's newsletter: plain part left over from a 2024 campaign."""
    stale = ("Week thirteen. November 13th, 2024. APASA Night Market Friday "
             "at McCarthy Quad, philanthropy event at the food bank "
             "(https://forms.gle/FTqRggCggivX1fGh9)")
    html = ("<p>Week eight. Halloween costume contest Wednesday October 21 "
            "in THH 101, then CASA Obsession party Friday October 23.</p>")
    _gmail_multipart(monkeypatch, stale, html)
    assert "Obsession" in email_triage.fetch_body("tok", "m1")


def test_a_plain_part_that_matches_the_html_is_kept(monkeypatch):
    plain = ("Halloween costume contest Wednesday October 21 in THH 101, "
             "then CASA Obsession party Friday October 23. Sign up here: "
             "https://example.com/signup")
    html = ("<p>Halloween <b>costume contest</b> Wednesday October 21 in THH "
            "101, then CASA Obsession party Friday October 23. "
            "<a href='https://example.com/signup'>Sign up</a></p>")
    _gmail_multipart(monkeypatch, plain, html)
    assert email_triage.fetch_body("tok", "m1") == plain


def test_a_plain_part_with_one_brace_is_still_prose(monkeypatch):
    text = "Meeting moved {tentatively} to 3pm."
    _gmail_multipart(monkeypatch, text, "<p>other</p>")
    assert email_triage.fetch_body("tok", "m1") == text
