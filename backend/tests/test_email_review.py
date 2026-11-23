"""Review files every message so it can be found again.

Three things have to hold for that to be worth doing. The label set stays
closed — a model that invents a label is how free-form bins forked into
near-duplicates. Every action files, keep included, and undo takes the label
back off with everything else. And search finds a message by what was pulled
out of its body (an order number) as well as by its subject, ranked, across
the whole cache — with the index rebuilding itself when the cache moves
rather than depending on every writer to remember it.
"""

import http.client

import pytest

from app.providers import email_search, email_triage
from app.routes import email as email_routes


def _email(con, msg_id, *, subject="Subject", addr="a@example.com",
           name="Sender", day="2026-09-10", in_inbox=1, unread=1, **extra):
    con.execute(
        "INSERT INTO emails (msg_id, thread_id, day, received_at, from_name,"
        " from_addr, subject, snippet, in_inbox, unread, refreshed_at,"
        " list_unsubscribe, list_unsubscribe_post)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,'now',?,?)",
        (msg_id, "t" + msg_id, day, f"{day}T10:00:00-07:00", name, addr,
         subject, "snippet", in_inbox, unread,
         extra.get("list_unsubscribe"), extra.get("one_click", 0)))


def _triage(con, msg_id, **cols):
    keys = ", ".join(cols)
    con.execute(f"INSERT INTO email_triage (msg_id, {keys}) VALUES (?{', ?' * len(cols)})",
                (msg_id, *cols.values()))


# ---------- the label set ----------

ALLOWED = [l["name"] for l in email_triage.labels([])]


def test_parse_keeps_the_label_set_closed():
    reply = """```json
    [{"id": "a", "summary": "Lyft ride.", "label": "Filed/Receipts",
      "terms": ["$14.20", "Sept 6", "$14.20"], "action": "archive"},
     {"id": "b", "summary": "x", "label": "receipts", "terms": "one",
      "action": "bin"},
     {"id": "c", "summary": "x", "label": "Filed/School Stuff", "terms": [],
      "action": "keep"}]
    ```"""
    a, b, c = email_triage._parse_reply(reply, ALLOWED)
    assert a["label"] == "Filed/Receipts"
    assert a["terms"] == "$14.20; Sept 6"          # deduped, one string
    # A short name or wrong case is a right answer spelled wrong.
    assert b["label"] == "Filed/Receipts" and b["terms"] == "one"
    # A retired action is dropped, not trusted.
    assert b["action"] is None
    # An invented label is refused, and terms are still a string: NULL is
    # what marks a message as owed another pass.
    assert c["label"] is None and c["terms"] == ""


def test_owner_added_labels_join_the_set():
    got = email_triage.labels(["Filed/Courses/CSCI 104", "Jobs", "Filed/Receipts"])
    names = [l["name"] for l in got]
    assert "Filed/Courses/CSCI 104" in names       # last term's course
    assert "Jobs" not in names                     # hand-made, outside the tree
    assert names.count("Filed/Receipts") == 1


def test_pending_is_the_inbox_then_a_slice_of_the_backlog(con):
    with con:
        _email(con, "in1", day="2026-09-01")
        _email(con, "old1", in_inbox=0, day="2026-08-01")
        _email(con, "old2", in_inbox=0, day="2026-08-02")
        _email(con, "done", in_inbox=0, day="2026-08-03")
        _triage(con, "done", summary="s", terms="")
        _email(con, "sent", in_inbox=0, day="2026-08-04")
        con.execute("UPDATE emails SET is_sent=1 WHERE msg_id='sent'")
    assert [m["msg_id"] for m in email_triage.pending(con, backlog=1)] == ["in1", "old2"]
    assert [m["msg_id"] for m in email_triage.pending(con, backlog=0)] == ["in1"]


# ---------- actions ----------

@pytest.fixture
def gmail(monkeypatch):
    calls = []
    labels = {"Label_1": "Filed"}

    def fake_api(url, token, method="GET", body=None, **kwargs):
        calls.append({"url": url, "method": method, "body": body, **kwargs})
        if url.endswith("/labels") and method == "GET":
            return {"labels": [{"id": i, "name": n, "type": "user"}
                               for i, n in labels.items()]}
        if url.endswith("/labels") and method == "POST":
            new = f"Label_{len(labels) + 1}"
            labels[new] = body["name"]
            return {"id": new, "name": body["name"]}
        return {}

    monkeypatch.setattr(email_routes, "api_call", fake_api)
    monkeypatch.setattr(email_routes, "access_token", lambda: "tok")
    monkeypatch.setattr(email_routes, "_label_ids", {})
    return calls


def _modifies(calls):
    return [c["body"] for c in calls if c["url"].endswith("/modify")]


def test_archive_files_and_leaves_the_inbox(con, gmail):
    with con:
        _email(con, "m1")
    out = email_routes.post_action(
        "m1", email_routes.ActionIn(action="archive", label="Filed/Receipts"))
    assert out == {"ok": True, "performed": "archive", "label": "Filed/Receipts"}
    # The parent already existed; only the leaf was created.
    created = [c["body"]["name"] for c in gmail if c["method"] == "POST"
               and c["url"].endswith("/labels")]
    assert created == ["Filed/Receipts"]
    assert _modifies(gmail) == [{"addLabelIds": ["Label_2"],
                                 "removeLabelIds": ["INBOX", "UNREAD"]}]
    row = con.execute("SELECT user_action, user_label FROM email_triage"
                      " WHERE msg_id='m1'").fetchone()
    assert (row["user_action"], row["user_label"]) == ("archive", "Filed/Receipts")
    assert con.execute("SELECT in_inbox FROM emails WHERE msg_id='m1'"
                       ).fetchone()[0] == 0


def test_keep_files_too_and_undo_takes_the_label_back(con, gmail):
    with con:
        _email(con, "m2")
    email_routes.post_action(
        "m2", email_routes.ActionIn(action="keep", label="Courses/CSCI 103"))
    # A bare name nests under Filed/, parents created on the way down.
    created = [c["body"]["name"] for c in gmail if c["method"] == "POST"
               and c["url"].endswith("/labels")]
    assert created == ["Filed/Courses", "Filed/Courses/CSCI 103"]
    assert _modifies(gmail)[-1] == {"addLabelIds": ["Label_3"], "removeLabelIds": []}
    assert con.execute("SELECT in_inbox FROM emails WHERE msg_id='m2'"
                       ).fetchone()[0] == 1

    email_routes.post_undo("m2")
    # Keep never left the inbox, so undo only removes the label.
    assert _modifies(gmail)[-1] == {"addLabelIds": [], "removeLabelIds": ["Label_3"]}
    row = con.execute("SELECT user_action, user_label FROM email_triage"
                      " WHERE msg_id='m2'").fetchone()
    assert row["user_action"] is None and row["user_label"] is None


def test_retired_actions_are_refused(con, gmail):
    with con:
        _email(con, "m3")
    for action in ("delete", "bin"):
        with pytest.raises(email_routes.HTTPException) as e:
            email_routes.post_action("m3", email_routes.ActionIn(action=action))
        assert e.value.status_code == 400
    assert gmail == []


def test_unsubscribe_files_after_the_request(con, gmail, monkeypatch):
    sent = []
    monkeypatch.setattr(email_routes, "_one_click", sent.append)
    with con:
        _email(con, "m4", list_unsubscribe="<https://x.test/u>", one_click=1)
    out = email_routes.post_action(
        "m4", email_routes.ActionIn(action="unsubscribe", label="Filed/Newsletters"))
    assert sent == ["https://x.test/u"]
    assert out["performed"] == "one_click"
    assert _modifies(gmail)[-1]["removeLabelIds"] == ["INBOX", "UNREAD"]
    assert email_routes.post_undo("m4")["unsubscribe_sent"] is True


def test_filing_is_the_one_post_marked_safe_to_resend(con, gmail):
    """The modify call is the one Gmail has been seen to drop."""
    with con:
        _email(con, "m7")
    email_routes.post_action("m7", email_routes.ActionIn(action="keep",
                                                         label="Filed/Newsletters"))
    assert [c.get("idempotent") for c in gmail
            if c["url"].endswith("/modify")] == [True]


class _Reply:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return b"{}"


def test_a_dropped_connection_on_modify_is_retried(monkeypatch):
    """Through the real _api and json_request: urlopen is the only fake."""
    from app.providers import http as http_mod
    attempts = []

    def flaky(req, timeout=None):
        attempts.append(req.full_url)
        if len(attempts) == 1:
            raise http.client.RemoteDisconnected("closed")
        return _Reply()

    monkeypatch.setattr(http_mod.urllib.request, "urlopen", flaky)
    monkeypatch.setattr(http_mod.time, "sleep", lambda s: None)
    assert email_routes.api_call("https://gmail.test/modify", "tok", method="POST",
                             body={}, idempotent=True) == {}
    assert len(attempts) == 2


def test_a_lost_network_is_a_502_with_a_reason_not_a_bare_500(monkeypatch):
    from app.providers import http as http_mod

    def gone(req, timeout=None):
        raise http.client.RemoteDisconnected("closed")

    monkeypatch.setattr(http_mod.urllib.request, "urlopen", gone)
    monkeypatch.setattr(http_mod.time, "sleep", lambda s: None)
    with pytest.raises(email_routes.HTTPException) as e:
        email_routes.api_call("https://gmail.test/modify", "tok", method="POST",
                          body={}, idempotent=True)
    assert e.value.status_code == 502
    assert "gmail unreachable" in e.value.detail


def test_one_click_falls_back_to_mailto_when_refused(con, gmail, monkeypatch):
    """Consensus via loops.so: Cloudflare 403s the POST. The header carries a
    mailto too, and a refused POST reaches nobody, so that's next."""
    def refused(url):
        raise email_routes.HTTPException(502, "unsubscribe endpoint returned 403")
    mailed = []
    monkeypatch.setattr(email_routes, "_one_click", refused)
    monkeypatch.setattr(email_routes, "_mailto_unsubscribe",
                        lambda token, to: mailed.append(to))
    with con:
        _email(con, "m5", one_click=1,
               list_unsubscribe="<mailto:u@unsub.test?subject=unsubscribe>,"
                                "<https://x.test/u>")
        _email(con, "m6", one_click=1, list_unsubscribe="<https://x.test/u>")
    out = email_routes.post_action("m5", email_routes.ActionIn(action="unsubscribe"))
    assert out["performed"] == "mailto"
    assert mailed == ["mailto:u@unsub.test?subject=unsubscribe"]
    # No mailto to fall back on: the failure is the answer, and nothing filed.
    with pytest.raises(email_routes.HTTPException):
        email_routes.post_action("m6", email_routes.ActionIn(action="unsubscribe"))
    assert con.execute("SELECT in_inbox FROM emails WHERE msg_id='m6'").fetchone()[0] == 1


def test_one_click_sends_a_user_agent(monkeypatch):
    """urllib's default UA is on Cloudflare's banned list (error 1010)."""
    seen = []

    class _Resp:
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def fake(req, timeout):
        seen.append(req)
        return _Resp()
    monkeypatch.setattr(email_routes.urllib.request, "urlopen", fake)
    email_routes._one_click("https://x.test/u")
    ua = seen[0].get_header("User-agent")
    assert ua and "Python-urllib" not in ua
    assert seen[0].data == b"List-Unsubscribe=One-Click"


def test_the_queue_prefers_the_senders_record_over_the_model(con):
    with con:
        for i in range(2):
            _email(con, f"h{i}", addr="lyft@lyftmail.com", in_inbox=0)
            _triage(con, f"h{i}", user_action="archive",
                    user_action_at=f"2026-09-0{i + 1}T10:00:00",
                    user_label="Filed/Receipts")
        _email(con, "new", addr="lyft@lyftmail.com")
        _triage(con, "new", summary="A ride.", label="Filed/Travel", terms="")
        _email(con, "fresh", addr="someone@new.test")
        _triage(con, "fresh", summary="Hi.", label="Filed/Personal", terms="")
    cards = {c["id"]: c for c in email_routes._queue(con)["cards"]}
    assert cards["new"]["label"] == "Filed/Receipts"
    assert cards["new"]["label_basis"] == "sender"
    assert cards["fresh"]["label"] == "Filed/Personal"
    assert cards["fresh"]["label_basis"] == "model"


# ---------- search ----------

def test_search_finds_a_message_by_what_its_body_said(con):
    with con:
        _email(con, "r1", subject="Your receipt", name="USC Campus Dining",
               day="2026-09-01", in_inbox=0)
        _triage(con, "r1", summary="Dining receipt.", label="Filed/Receipts",
                terms="#124057035; $12.40")
        _email(con, "r2", subject="Weekly digest", day="2026-09-02", in_inbox=0)
        _triage(con, "r2", summary="Mentions receipts in passing.",
                label="Filed/Newsletters", terms="")
    # A term that exists only in what was pulled out of the body.
    hits = email_search.local(con, "124057035")
    assert [h["id"] for h in hits] == ["r1"]
    # Ranked, not just matched: a subject hit outranks a summary mention even
    # though the summary one is newer.
    assert [h["id"] for h in email_search.local(con, "receipt")] == ["r1", "r2"]
    # A half-typed last word already matches.
    assert [h["id"] for h in email_search.local(con, "dini")] == ["r1"]
    # Syntax characters are words, not operators.
    assert email_search.local(con, 'AND "(') == []


def test_label_scope_includes_sublabels_and_prefers_the_filed_one(con):
    with con:
        _email(con, "c1", subject="Midterm", in_inbox=0)
        _triage(con, "c1", label="Filed/Newsletters",
                user_label="Filed/Courses/CSCI 103", terms="")
        _email(con, "c2", subject="Midterm", in_inbox=0)
        _triage(con, "c2", label="Filed/Newsletters", terms="")
    got = email_search.local(con, "midterm", label="Filed/Courses")
    assert [h["id"] for h in got] == ["c1"]
    assert got[0]["label"] == "Filed/Courses/CSCI 103" and got[0]["filed"]


def test_the_index_follows_the_cache_without_being_told(con):
    with con:
        _email(con, "i1", subject="Boarding pass", in_inbox=0)
    assert [h["id"] for h in email_search.local(con, "boarding")] == ["i1"]
    assert email_search.ensure_index(con) is False      # nothing moved
    # A summarizer pass lands terms; nobody calls the index.
    with con:
        _triage(con, "i1", terms="AA 2231; LAX", summarized_at="2026-09-11T00:00:00")
    assert [h["id"] for h in email_search.local(con, "LAX")] == ["i1"]
    # And a dropped index is only a rebuild away.
    with con:
        con.execute("DELETE FROM email_fts")
        con.execute("DELETE FROM email_fts_state")
    assert [h["id"] for h in email_search.local(con, "LAX")] == ["i1"]


def test_fts_query_quotes_every_word():
    assert email_search.fts_query("CSCI 103 mid") == '"CSCI"* "103"* "mid"*'
    assert email_search.fts_query("  ") is None
