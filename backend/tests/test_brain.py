"""The Brain: what survives the checks, what a verdict does, what a rebuild
keeps, and what the console is shown.

Mostly `apply_ops`, the only guard between a hallucinated fact and a page read every session:
the cited line is in the batch, the quote is in that line, a rejected claim never returns.
"""

import asyncio
import json
from datetime import datetime, timedelta

import frontmatter
import pytest

from app import brain, console_tools as ct, db
from app.brain import Item


def _items():
    its = [
        Item("message", "guid-1", datetime(2026, 7, 6, 14, 2),
             "yeah I'm moving into Maple Hall on the 17th", speaker="user",
             container="Texts with Jamie", label="texts with Jamie"),
        Item("chat", "sess-1", datetime(2026, 7, 7, 9, 0),
             "I hate em dashes, never use them in my emails", speaker="user",
             container="Chat with Kestrel", label="Kestrel chat",
             from_user_to_kestrel=True),
    ]
    brain.render_sources(its)   # assigns m1 / c1
    return its


def _apply(con, ops, items=None):
    with con:
        return brain.apply_ops(con, ops, items or _items(), "ingest")


def _facts(con):
    return [dict(r) for r in con.execute("SELECT * FROM brain_facts ORDER BY id")]


# ---------- the checks ----------

def test_a_quoted_fact_lands_pending_with_its_source(con):
    res = _apply(con, [{"op": "add", "page": "home", "claim": "Lives in Maple Hall",
                        "cite": "m1", "quote": "moving into Maple Hall"}])
    assert res.added == 1 and not res.dropped
    (f,) = _facts(con)
    assert f["status"] == "pending"
    assert f["as_of"] == "2026-07-06"          # the source's day, not the model's
    assert f["source_ref"] == "guid-1" and f["source_label"] == "texts with Jamie"


def test_what_the_user_told_kestrel_is_active(con):
    _apply(con, [{"op": "add", "page": "preferences", "claim": "No em dashes in writing",
                  "cite": "c1", "quote": "never use them"}])
    assert _facts(con)[0]["status"] == "active"


@pytest.mark.parametrize("op, why", [
    ({"cite": "m1", "quote": "moving into Birnkrant"}, "quote not found"),
    ({"cite": "m9", "quote": "Maple Hall"}, "cites no line"),
    ({"cite": "m1", "quote": ""}, "quote not found"),
    ({"cite": "m1", "quote": "Maple Hall", "page": "Home Stuff"}, "bad page slug"),
])
def test_a_proposal_that_fails_a_check_writes_nothing(con, op, why):
    res = _apply(con, [{"op": "add", "page": "home", "claim": "Lives in Birnkrant", **op}])
    assert res.added == 0 and why in res.dropped[0]["why"]
    assert _facts(con) == []


def test_the_quote_check_ignores_case_whitespace_and_curly_quotes(con):
    it = Item("message", "g", datetime(2026, 7, 1), "I’m  on the\nclub team",
              speaker="user", container="x")
    brain.render_sources([it])
    res = _apply(con, [{"op": "add", "page": "fitness", "claim": "Plays club tennis",
                        "cite": "m1", "quote": "i'm on the CLUB team"}], [it])
    assert res.added == 1


def test_a_quote_may_carry_the_speaker_it_was_shown_with(con):
    """The model sees '[m1] … user: yeah I'm moving…' and quotes what it saw."""
    res = _apply(con, [{"op": "add", "page": "home", "claim": "Lives in Maple Hall",
                        "cite": "m1", "quote": "user: yeah I'm moving into Maple"}])
    assert res.added == 1


def test_a_true_quote_under_the_wrong_id_lands_on_the_right_source(con):
    res = _apply(con, [{"op": "add", "page": "preferences", "claim": "No em dashes",
                        "cite": "m1", "quote": "hate em dashes"}])
    assert res.added == 1
    f = _facts(con)[0]
    assert f["source_ref"] == "sess-1" and f["status"] == "active"


def test_a_quote_found_in_several_other_lines_proves_nothing(con):
    its = [Item("message", f"g{i}", datetime(2026, 7, 1), "lol same", speaker="Pal",
                container="x") for i in range(2)]
    its.append(Item("message", "g9", datetime(2026, 7, 1), "unrelated", speaker="Pal",
                    container="x"))
    brain.render_sources(its)
    res = _apply(con, [{"op": "add", "page": "me", "claim": "x", "cite": "m3",
                        "quote": "lol same"}], its)
    assert res.added == 0


@pytest.mark.parametrize("claim, why", [
    ("USC email address is student@usc.edu", "identifier"),
    ("Has a credit card ending in 4242", "identifier"),
    ("Lives at 100 Example St Apt 12", "identifier"),
    ("Phone number is 202-555-1234", "identifier"),
    ("Rowan is a sibling (or close family member) of the user", "hedged"),
    ("Is a member of or interested in Poker Club", "hedged"),
    ("Likely lives near Theo", "hedged"),
    ("Alex is in a group chat called 'hq' with Theo and Felix", "about a chat"),
])
def test_identifiers_and_hedges_are_refused_whatever_the_prompt_said(con, claim, why):
    res = _apply(con, [{"op": "add", "page": "me", "claim": claim,
                        "cite": "m1", "quote": "Maple Hall"}])
    assert res.added == 0 and why in res.dropped[0]["why"]


def test_a_year_in_a_claim_is_not_an_identifier(con):
    res = _apply(con, [{"op": "add", "page": "travel", "claim": "Aspen ski trip Dec 13–18, 2026",
                        "cite": "m1", "quote": "Maple Hall"}])
    assert res.added == 1


def test_a_malformed_reply_is_retried_then_the_batch_is_halved(con, monkeypatch):
    its = _items()
    calls = []

    async def fake_ask(prompt, **kw):
        calls.append(prompt.count("[m1]") + prompt.count("[c1]"))
        if len(calls) <= 2:
            return '{"ops": [ {"op": "add" "page": }'      # the missing comma
        return '{"ops": []}'

    monkeypatch.setattr(brain.agent, "ask", fake_ask)
    stats = {"batches": 0, "added": 0, "superseded": 0, "dropped": 0}
    asyncio.run(brain._ingest_batch(its, "2026-07-06", "2026-07-12", "ingest", None, stats))
    assert calls == [2, 2, 1, 1]          # twice whole, then each half once
    assert stats["batches"] == 2


def test_a_known_fact_is_not_added_twice(con):
    op = {"op": "add", "page": "home", "claim": "Lives in Maple Hall",
          "cite": "m1", "quote": "Maple Hall"}
    _apply(con, [op])
    res = _apply(con, [op])
    assert res.added == 0 and "already known" in res.dropped[0]["why"]


def test_a_rejected_fact_is_never_learned_again(con):
    op = {"op": "add", "page": "home", "claim": "Lives in Maple Hall",
          "cite": "m1", "quote": "Maple Hall"}
    _apply(con, [op])
    with con:
        brain.set_verdict(con, _facts(con)[0]["id"], "rejected")
    res = _apply(con, [op])
    assert res.added == 0 and "already rejected" in res.dropped[0]["why"]


# ---------- change over time ----------

def test_supersede_retires_the_old_fact(con):
    _apply(con, [{"op": "add", "page": "home", "claim": "Lives in Raleigh",
                  "cite": "m1", "quote": "moving"}])
    old = _facts(con)[0]["id"]
    res = _apply(con, [{"op": "supersede", "fact": old, "page": "home",
                        "claim": "Lives in Maple Hall", "cite": "m1", "quote": "Maple Hall"}])
    assert res.superseded == 1
    old_row, new_row = _facts(con)
    assert old_row["status"] == "superseded" and old_row["superseded_by"] == new_row["id"]
    assert [f["claim"] for f in brain.page_facts(con, "home")] == ["Lives in Maple Hall"]


def test_rejecting_a_replacement_puts_the_old_fact_back(con):
    with con:
        brain.ensure_page(con, "home")
        old = brain.add_fact(con, page="home", claim="Lives in Raleigh", as_of="2026-06-01",
                             source_kind="chat", origin="chat", status="active")
        new = brain.add_fact(con, page="home", claim="Lives in Austin", as_of="2026-07-01",
                             source_kind="message", origin="ingest", status="pending",
                             replaces=old["id"])
        brain.set_verdict(con, new["id"], "rejected")
    rows = {f["id"]: f for f in _facts(con)}
    assert rows[old["id"]]["status"] == "active"
    assert rows[new["id"]]["status"] == "rejected"


def test_an_edit_is_the_user_stating_it(con):
    with con:
        brain.ensure_page(con, "school")
        f = brain.add_fact(con, page="school", claim="Studies CS", as_of="2026-08-01",
                           source_kind="message", origin="ingest", status="pending")
        new = brain.edit_fact(con, f["id"], "Studies CS + business at USC")
    assert new["status"] == "active" and new["verdict"] == "confirmed"
    assert _facts(con)[0]["status"] == "superseded"


def test_confirming_a_page_confirms_only_its_pending_facts(con):
    with con:
        for page, claim in (("school", "a"), ("school", "b"), ("home", "c")):
            brain.ensure_page(con, page)
            brain.add_fact(con, page=page, claim=claim, as_of="2026-08-01",
                           source_kind="message", origin="ingest", status="pending")
        assert brain.confirm_page(con, "school") == 2
    assert [f["status"] for f in _facts(con)] == ["active", "active", "pending"]


# ---------- rebuild ----------

def test_a_rebuild_keeps_every_verdict_and_everything_told_directly(con):
    with con:
        brain.ensure_page(con, "home")
        keep_told = brain.add_fact(con, page="home", claim="told", as_of="2026-08-01",
                                   source_kind="chat", origin="chat", status="active")
        ruled = brain.add_fact(con, page="home", claim="ruled", as_of="2026-08-01",
                               source_kind="message", origin="backfill", status="pending")
        brain.set_verdict(con, ruled["id"], "rejected")
        brain.add_fact(con, page="home", claim="unruled", as_of="2026-08-01",
                       source_kind="message", origin="backfill", status="pending")
    assert brain.rebuild_reset() == 1
    assert {f["claim"] for f in _facts(con)} == {"told", "ruled"}
    assert keep_told


# ---------- the vault view ----------

def test_render_writes_read_only_pages_and_removes_only_its_own(con, tmp_path):
    with con:
        brain.ensure_page(con, "people/jamie-smith", "Jamie Smith")
        brain.add_fact(con, page="people/jamie-smith", claim="Roommate at USC",
                       as_of="2026-08-17", source_kind="message", origin="ingest",
                       status="pending", core=True)
    base = tmp_path / brain.BRAIN_DIR
    base.mkdir()
    (base / "stale.md").write_text("---\nbrain: true\n---\nold\n")
    (base / "mine.md").write_text("a note the user put here\n")
    out = brain.render(tmp_path)
    page = (base / "people" / "jamie-smith.md").read_text()
    assert "edits here are overwritten" in page
    assert "Roommate at USC *(unconfirmed)*" in page and "^f1" in page
    assert frontmatter.loads(page).metadata["brain"] is True
    assert "Jamie Smith" in (base / "_index.md").read_text()
    assert "Roommate at USC" in (base / "_profile.md").read_text()
    assert not (base / "stale.md").exists()
    assert (base / "mine.md").exists()
    assert brain.render(tmp_path)["written"] == 0     # nothing changed, nothing touched
    assert out["removed"] == 1


# ---------- what the console sees ----------

def test_the_prompt_block_carries_the_profile_and_flags_unconfirmed(con):
    with con:
        brain.ensure_page(con, "me", "Me")
        brain.add_fact(con, page="me", claim="Freshman at USC", as_of="2026-08-20",
                       source_kind="chat", origin="chat", status="active", core=True)
        brain.add_fact(con, page="me", claim="Studies CS", as_of="2026-08-20",
                       source_kind="message", origin="ingest", status="pending", core=True)
    block = brain.prompt_block()
    assert "- [Me] Freshman at USC\n" in block
    assert "- [Me] Studies CS (unconfirmed)" in block
    assert "- me: Me · 2 facts" in block


def test_a_message_naming_someone_pulls_their_page(con):
    with con:
        brain.ensure_page(con, "people/jamie-smith", "Jamie Smith", aliases=["Jay"])
        brain.add_fact(con, page="people/jamie-smith", claim="Roommate", as_of="2026-08-17",
                       source_kind="chat", origin="chat", status="active")
    assert brain.match_pages("is jamie free tonight") == ["people/jamie-smith"]
    assert brain.match_pages("ask Jay") == ["people/jamie-smith"]
    assert brain.match_pages("camera settings") == []
    assert "Roommate" in brain.page_context(["people/jamie-smith"])


# ---------- the console's wiring ----------

def _console_options():
    from app import console

    async def deny(*a, **k):
        return {"behavior": "deny", "message": "test"}

    async def noop(*a, **k):
        return {}

    return console.build_options(None, None, "default", deny, noop)


def test_console_sessions_have_one_memory_not_two():
    """Claude Code's auto-memory must stay off, or it drifts from the Brain (it held a stale timezone rule)."""
    assert _console_options().env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] == "1"


def test_every_message_is_matched_against_the_brain():
    hooks = _console_options().hooks["UserPromptSubmit"]
    from app import console
    assert any(console.brain_context in m.hooks for m in hooks)


def test_the_hook_attaches_a_named_page_and_ignores_pinned_context(con):
    from app import console
    with con:
        brain.ensure_page(con, "people/jamie-smith", "Jamie Smith")
        brain.add_fact(con, page="people/jamie-smith", claim="Roommate", as_of="2026-08-17",
                       source_kind="chat", origin="chat", status="active")
    out = asyncio.run(console.brain_context({"prompt": "what's Jamie up to"}, None, None))
    assert "Roommate" in out["hookSpecificOutput"]["additionalContext"]
    pinned = ("<attached_context><widget name=people>Jamie Smith …</widget>"
              "</attached_context>\nwhat's for dinner")
    assert asyncio.run(console.brain_context({"prompt": pinned}, None, None)) == {}


# ---------- the console's tools ----------

def _call(fn, **args):
    out = asyncio.run(fn.handler(args))
    return out["content"][0]["text"], out.get("is_error", False)


@pytest.fixture
def no_render(monkeypatch):
    monkeypatch.setattr(brain, "render", lambda root=None: {})


def test_brain_note_told_is_active_and_inferred_waits(con, no_render):
    text, err = _call(ct.brain_note, page="school", claim="Freshman at USC", told_by_user=True)
    assert not err and "noted f1 on school" in text
    text, err = _call(ct.brain_note, page="people/jamie-smith", claim="Plays tennis",
                      title="Jamie Smith")
    assert "unconfirmed" in text
    assert [f["status"] for f in _facts(con)] == ["active", "pending"]
    assert brain._page_row(con, "people/jamie-smith")["title"] == "Jamie Smith"


def test_brain_note_core_reaches_the_profile_and_plain_notes_dont(con, no_render):
    _call(ct.brain_note, page="preferences", claim="Class calendar is Lock In",
          told_by_user=True, core=True)
    _call(ct.brain_note, page="preferences", claim="Likes ocean scents", told_by_user=True)
    assert [f["claim"] for f in brain.profile_facts(con)] == ["Class calendar is Lock In"]


def test_brain_note_replaces_and_verdict_rejects(con, no_render):
    _call(ct.brain_note, page="home", claim="Lives in Raleigh", told_by_user=True)
    text, err = _call(ct.brain_note, page="home", claim="Lives in LA", told_by_user=True,
                      replaces=1)
    assert not err and "replacing f1" in text
    text, err = _call(ct.brain_verdict, fact_id=2, verdict="rejected")
    assert not err and "rejected" in text
    assert [f["claim"] for f in brain.page_facts(con, "home")] == ["Lives in Raleigh"]


def test_fact_ids_pass_the_schema_as_printed(con, no_render):
    """Every fact-id parameter takes "f815" and 815 (an integer-only schema refused brain_verdict before its handler ran)."""
    import jsonschema
    for fn, key, wrap in ((ct.brain_verdict, "fact_id", False),
                          (ct.brain_note, "replaces", False),
                          (ct.brain_page, "move_facts", True)):
        prop = fn.input_schema["properties"][key]
        prop = prop["items"] if wrap else prop
        for ok in (815, "f815", "815"):
            jsonschema.validate(ok, prop)
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate("the USC one", prop)

    _call(ct.brain_note, page="home", claim="Lives in Raleigh", told_by_user=True)
    text, err = _call(ct.brain_note, page="home", claim="Lives in LA",
                      told_by_user=True, replaces="f1")
    assert not err and "replacing f1" in text and "ff1" not in text
    text, err = _call(ct.brain_verdict, fact_id="f2", verdict="rejected")
    assert not err and "f2 is now rejected" in text
    _call(ct.brain_note, page="home", claim="Has a cat", told_by_user=True)
    text, err = _call(ct.brain_page, page="pets", move_facts=["f3"])
    assert not err and "moved f3 here" in text


def test_rejecting_a_replacement_says_what_came_back(con, no_render):
    _call(ct.brain_note, page="home", claim="Lives in Raleigh", told_by_user=True)
    _call(ct.brain_note, page="home", claim="Lives in LA", told_by_user=True, replaces=1)
    text, _ = _call(ct.brain_verdict, fact_id=2, verdict="rejected")
    assert "f1 is back as active" in text and "Lives in Raleigh" in text
    text, _ = _call(ct.brain_verdict, fact_id=1, verdict="rejected")
    assert "is back" not in text


def test_confirming_a_rejected_fact_brings_it_back(con, no_render):
    _call(ct.brain_note, page="people/nora-okafor", claim="Rushing a new house",
          title="Nora Okafor")
    _call(ct.brain_verdict, fact_id=1, verdict="rejected")
    text, err = _call(ct.brain_verdict, fact_id=1, verdict="confirmed")
    assert not err and "f1 is now active" in text
    assert [f["claim"] for f in brain.page_facts(con, "people/nora-okafor")] == ["Rushing a new house"]


def test_brain_page_moves_an_alias_to_the_right_person(con, no_render):
    with con:
        brain.ensure_page(con, "people/nora-okafor", "Nora Okafor", aliases=["Noni", "Nora"])
    text, err = _call(ct.brain_page, page="people/noelle-lund", title="Noelle Lund",
                      add_aliases=["Noni"])
    assert err and "already names people/nora-okafor" in text
    text, err = _call(ct.brain_page, page="people/nora-okafor", remove_aliases=["noni"])
    assert not err and "aliases: Nora" in text
    text, err = _call(ct.brain_page, page="people/noelle-lund", add_aliases=["Noni"])
    assert not err and "aliases: Noni" in text


def test_a_learned_alias_never_lands_on_a_second_page(con):
    with con:
        brain.ensure_page(con, "people/noelle-lund", "Noelle Lund", aliases=["Noni"])
        brain.ensure_page(con, "people/nora-okafor", "Nora Okafor", aliases=["Noni", "Nora"])
        brain.ensure_page(con, "people/theo-marsh", "Theo Marsh", aliases=["Noelle"])
    assert json.loads(brain._page_row(con, "people/nora-okafor")["aliases"]) == ["Nora"]
    assert json.loads(brain._page_row(con, "people/theo-marsh")["aliases"]) == []


def test_brain_page_moves_a_fact_as_it_is(con, no_render):
    _call(ct.brain_note, page="fitness", claim="Paid $100 in Chess Club dues", told_by_user=True)
    text, err = _call(ct.brain_page, page="chess-club", title="Chess Club", move_facts=[1])
    assert not err and "moved f1 here" in text
    (f,) = brain.page_facts(con, "chess-club")
    assert (f["id"], f["status"], f["source_label"]) == (1, "active", "Kestrel chat")
    assert brain.page_facts(con, "fitness") == []


def test_brain_page_refuses_to_remove_an_alias_it_lacks(con, no_render):
    _call(ct.brain_page, page="people/theo-marsh", title="Theo Marsh")
    text, err = _call(ct.brain_page, page="people/theo-marsh", remove_aliases=["Teddy"])
    assert err and "no alias Teddy".lower() in text.lower()


def test_brain_note_refuses_a_bad_slug(con, no_render):
    text, err = _call(ct.brain_note, page="My Friends!", claim="x")
    assert err and "bad page slug" in text


def test_brain_read_by_page_and_by_query(con, no_render):
    _call(ct.brain_note, page="people/jamie-smith", claim="Roommate at USC",
          told_by_user=True, title="Jamie Smith")
    text, _ = _call(ct.brain_read, page="people/jamie-smith")
    assert "[f1] people/jamie-smith: Roommate at USC" in text
    text, _ = _call(ct.brain_read, query="roommate")
    assert "Roommate" in text
    text, err = _call(ct.brain_read)
    assert err


# ---------- sources ----------

def _msg(con, guid, chat, mine, day="2026-07-06", text="hi"):
    con.execute("INSERT INTO messages (guid, day, ts, chat_guid, handle, contact_name,"
                " is_from_me, text, refreshed_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (guid, day, f"{day}T12:00:00-04:00", chat, "h", "Pal", mine, text, "x"))


def test_a_group_counts_only_where_the_user_talks(con):
    with con:
        con.execute("INSERT INTO chats VALUES ('g-talk', 'hq', 1, 'x')")
        con.execute("INSERT INTO chats VALUES ('g-lurk', 'club list', 1, 'x')")
        con.execute("INSERT INTO chats VALUES ('dm', NULL, 0, 'x')")
        for i in range(brain.BRAIN_GROUP_MIN_MINE):
            _msg(con, f"t{i}", "g-talk", 1)
            _msg(con, f"l{i}", "g-lurk", 0)
        _msg(con, "d", "dm", 0)
    got = brain._included_chats(con, "2026-01-01")
    assert got == {"g-talk": "Group texts · hq", "dm": "Texts with Pal"}


def test_message_times_keep_the_zone_they_were_sent_in(con):
    with con:
        con.execute("INSERT INTO chats VALUES ('dm', NULL, 0, 'x')")
        con.execute("INSERT INTO messages (guid, day, ts, chat_guid, handle, contact_name,"
                    " is_from_me, text, refreshed_at) VALUES"
                    " ('late', '2026-09-01', '2026-09-01T23:30:00-07:00', 'dm', 'h', 'Pal', 1,"
                    " 'going to bed', 'x')")
    items = brain._message_items(con, *brain._day_bounds("2026-09-01", "2026-09-01"),
                                 "2026-01-01")
    assert [i.ts for i in items] == [datetime(2026, 9, 1, 23, 30)]


def test_batches_split_on_the_budget_and_keep_order():
    its = [Item("message", str(i), datetime(2026, 7, 1) + timedelta(minutes=i), "x" * 60)
           for i in range(10)]
    got = brain.batches(its, budget=250)
    assert [len(b) for b in got] == [2, 2, 2, 2, 2]
    assert [i.ref for b in got for i in b] == [str(i) for i in range(10)]


def test_weeks_cover_the_range_exactly():
    assert brain.weeks("2026-07-01", "2026-07-16") == [
        ("2026-07-01", "2026-07-07"), ("2026-07-08", "2026-07-14"),
        ("2026-07-15", "2026-07-16")]


def test_parse_ops_takes_the_json_out_of_prose():
    assert brain.parse_ops('Sure:\n```json\n{"ops": [{"op": "add"}]}\n```') == [{"op": "add"}]
    with pytest.raises(ValueError):
        brain.parse_ops("nothing to add")


# ---------- lint ----------

def test_lint_merges_but_never_retires_a_confirmed_fact_for_a_guess(con):
    with con:
        brain.ensure_page(con, "school")
        a = brain.add_fact(con, page="school", claim="At USC", as_of="2026-08-01",
                           source_kind="chat", origin="chat", status="active")
        brain.set_verdict(con, a["id"], "confirmed")
        b = brain.add_fact(con, page="school", claim="Goes to USC", as_of="2026-08-02",
                           source_kind="message", origin="ingest", status="pending")
        c = brain.add_fact(con, page="school", claim="Attends USC", as_of="2026-08-03",
                           source_kind="message", origin="ingest", status="pending")
        stats = brain.apply_lint(con, {"pages": [{
            "slug": "school", "summary": "Where the user studies.",
            "merge": [{"keep": b["id"], "drop": [a["id"]]}, {"keep": a["id"], "drop": [c["id"]]}],
            "core": [a["id"], 999]}]})
    rows = {f["id"]: f for f in _facts(con)}
    assert rows[a["id"]]["status"] == "active" and rows[a["id"]]["core"] == 1
    assert rows[c["id"]]["status"] == "superseded"
    assert stats == {"merged": 1, "superseded": 0, "core": 1, "summaries": 1, "dropped": 1}
    assert brain._page_row(con, "school")["summary"] == "Where the user studies."


# ---------- the year of texts ----------

def test_the_message_backfill_reaches_back_without_capturing_or_moving_the_watermark(
        dbfile, monkeypatch):
    import sqlite3
    from app.providers import messages

    chat = sqlite3.connect(":memory:")
    chat.row_factory = sqlite3.Row
    chat.executescript("""
        CREATE TABLE message (ROWID INTEGER PRIMARY KEY, guid TEXT, date INTEGER, text TEXT,
          attributedBody BLOB, is_from_me INTEGER, handle_id INTEGER,
          associated_message_type INTEGER DEFAULT 0, item_type INTEGER DEFAULT 0,
          is_read INTEGER DEFAULT 0, date_read INTEGER DEFAULT 0,
          is_delivered INTEGER DEFAULT 0, date_delivered INTEGER DEFAULT 0);
        CREATE TABLE handle (ROWID INTEGER PRIMARY KEY, id TEXT);
        CREATE TABLE chat (ROWID INTEGER PRIMARY KEY, guid TEXT, display_name TEXT, style INTEGER);
        CREATE TABLE chat_message_join (chat_id INTEGER, message_id INTEGER);
        CREATE TABLE attachment (ROWID INTEGER PRIMARY KEY, mime_type TEXT, uti TEXT);
        CREATE TABLE message_attachment_join (message_id INTEGER, attachment_id INTEGER);
        INSERT INTO handle VALUES (1, '+12025551234');
        INSERT INTO chat VALUES (1, 'any;-;+12025551234', NULL, 45);
    """)

    def add(rowid, days_ago, text):
        ts = datetime.now() - timedelta(days=days_ago)
        chat.execute("INSERT INTO message (ROWID, guid, date, text, attributedBody,"
                     " is_from_me, handle_id, associated_message_type, item_type)"
                     " VALUES (?,?,?,?,NULL,1,1,0,0)",
                     (rowid, f"g{rowid}", int((ts.timestamp() - messages.APPLE_EPOCH) * 1e9), text))
        chat.execute("INSERT INTO chat_message_join VALUES (1, ?)", (rowid,))

    add(1, 400, "too old")
    add(2, 200, "note to self: buy stamps")
    add(3, 10, "already cached")
    with db.session() as app, app:
        app.execute("INSERT INTO messages (guid, day, ts, chat_guid, is_from_me, text,"
                    " refreshed_at) VALUES ('g3', ?, ?, 'any;-;+12025551234', 1,"
                    " 'already cached', 'x')",
                    ((datetime.now() - timedelta(days=10)).date().isoformat(),
                     (datetime.now() - timedelta(days=10)).isoformat(timespec="seconds")))
    monkeypatch.setattr(messages, "_ro", lambda path: chat)
    monkeypatch.setattr(messages, "_load_contacts", lambda: {})

    assert messages.backfill_older(365)["inserted"] == 1
    with db.session() as app:
        texts = [r[0] for r in app.execute("SELECT text FROM messages ORDER BY ts")]
        wm = app.execute("SELECT * FROM sync_state").fetchall()
    assert texts == ["note to self: buy stamps", "already cached"]
    assert wm == []


def test_a_fact_id_can_be_looked_up_and_an_invented_one_cant(router_client, con):
    now = "2026-11-05T00:00:00"
    with con:
        con.execute("INSERT OR IGNORE INTO brain_pages (slug,title,created_at,updated_at)"
                    " VALUES ('school','School',?,?)", (now, now))
        fid = con.execute(
            "INSERT INTO brain_facts (page,claim,as_of,source_kind,origin,status,"
            "source_label,created_at,updated_at) VALUES"
            " ('school','Takes MATH 226','2026-09-01','chat','chat','active','a chat',?,?)",
            (now, now)).lastrowid
    client = router_client(brain.router)
    got = client.get(f"/api/brain/fact/{fid}").json()
    assert got["claim"] == "Takes MATH 226" and got["page_title"] == "School"
    assert client.get("/api/brain/fact/999999").status_code == 404
