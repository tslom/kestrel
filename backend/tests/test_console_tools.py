"""The console's in-process MCP tools (app/console_tools.py).

Each test names the console-session failure it exists to prevent — the
transcripts are the spec here, not a hypothetical.
"""

import asyncio
import time

import pytest

from app import console_tools as ct
from app import db


def call(t, **kw):
    """Run a tool handler and return (text, is_error)."""
    r = asyncio.run(t.handler(kw))
    return r["content"][0]["text"], bool(r.get("is_error"))


@pytest.fixture(autouse=True)
def _no_vault_write(monkeypatch):
    """regenerate_tasks_view() writes into the vault; tests must not."""
    from app import vault_views
    monkeypatch.setattr(vault_views, "regenerate_tasks_view", lambda: "stubbed")


# ---------- task_create / task_update ----------

def test_task_create_lands_in_app_db(dbfile):
    """Session 3077f49f filed 'Order move-in supplies' with the harness's own
    TaskCreate. It went to ~/.claude/tasks/<session>/1.json, app.db never saw
    it, and it was still sitting there unseen after its due date passed."""
    text, err = call(ct.task_create, title="Order move-in supplies",
                     due="2026-07-25")
    assert not err
    rows = db.list_tasks(status="open")
    assert [r["title"] for r in rows] == ["Order move-in supplies"]
    assert rows[0]["due"] == "2026-07-25"
    assert rows[0]["source"] == "console"
    assert str(rows[0]["id"]) in text


def test_task_create_requires_a_title(dbfile):
    text, err = call(ct.task_create, title="   ")
    assert err and "title is required" in text
    assert db.list_tasks(status="open") == []


def test_task_update_completes_without_raw_sql(dbfile):
    """Session 2d395a20 ran `UPDATE tasks SET status='done' WHERE id=47`
    against the wrong app.db, got an error, went looking for the right path
    with `find`, and ran it again."""
    made = db.create_task(title="Update room locations")
    text, err = call(ct.task_update, task_id=made["id"], status="done")
    assert not err and "done" in text
    assert db.list_tasks(status="open") == []


def test_task_update_can_set_fields_and_status_together(dbfile):
    made = db.create_task(title="old")
    _, err = call(ct.task_update, task_id=made["id"], title="new",
                  due="2026-08-04", status="waiting")
    assert not err
    row = db.list_tasks(status="waiting")[0]
    assert (row["title"], row["due"]) == ("new", "2026-08-04")


def test_task_update_reports_a_missing_task(dbfile):
    text, err = call(ct.task_update, task_id=9999, status="done")
    assert err and "no task 9999" in text


def test_task_update_needs_something_to_change(dbfile):
    made = db.create_task(title="x")
    text, err = call(ct.task_update, task_id=made["id"])
    assert err and "nothing to change" in text


def test_task_update_rejects_a_non_numeric_id(dbfile):
    text, err = call(ct.task_update, task_id="47; DROP TABLE tasks",
                     status="done")
    assert err and "must be an integer" in text


def test_task_update_takes_the_column_name_id_too(dbfile):
    """A db_query row calls it `id`; three sessions sent {"id": "130"} and the
    required task_id refused it before the handler ran."""
    assert "task_id" not in ct.task_update.input_schema["required"]
    made = db.create_task(title="ballot")
    _, err = call(ct.task_update, id=str(made["id"]), status="done")
    assert not err and db.list_tasks(status="open") == []
    text, err = call(ct.task_update, status="done")
    assert err and "must be an integer" in text


# ---------- db_query ----------

def test_db_query_reads(dbfile):
    db.create_task(title="findable")
    text, err = call(ct.db_query, sql="SELECT title FROM tasks")
    assert not err and "findable" in text


def test_db_query_takes_bound_params(dbfile):
    db.create_task(title="alpha")
    db.create_task(title="beta")
    text, err = call(ct.db_query, sql="SELECT title FROM tasks WHERE title=?",
                     params=["beta"])
    assert not err and "beta" in text and "alpha" not in text


@pytest.mark.parametrize("sql", [
    "UPDATE tasks SET status='done'",
    "DELETE FROM tasks",
    "DROP TABLE tasks",
    "INSERT INTO tasks (title) VALUES ('x')",
    # The Bash allowlist has to parse SQL to catch these two; this path does
    # not, because the connection itself is opened mode=ro.
    "SELECT writefile('/tmp/pwned', 'x')",
    "CREATE TABLE evil (a)",
])
def test_db_query_cannot_write(dbfile, sql):
    # Seed first so the database exists. Without it a read-only connection
    # fails to open at all, and these would pass on the wrong error.
    db.create_task(title="survivor")
    text, err = call(ct.db_query, sql=sql)
    assert err, f"{sql!r} was not refused"
    assert "cannot open" not in text, "refused for the wrong reason"
    assert [r["title"] for r in db.list_tasks(status="open")] == ["survivor"]


def test_db_query_reports_bad_sql_instead_of_raising(dbfile):
    db.create_task(title="seed")
    text, err = call(ct.db_query, sql="SELECT * FROM no_such_table")
    assert err and "no_such_table" in text


def test_db_query_caps_rows(dbfile):
    for i in range(10):
        db.create_task(title=f"t{i}")
    text, _ = call(ct.db_query, sql="SELECT title FROM tasks", limit=3)
    assert text.startswith("3 rows (first 3)")


def test_db_query_names_the_real_columns_after_a_guess(dbfile):
    """A `no such column` refusal (`li.text` for `name`, `messages.date` for
    `day`) gets answered by a schema probe or another guess, so the error
    carries the columns of every table the
    statement reads, aliases and all."""
    db.create_task(title="seed")
    text, err = call(ct.db_query, sql="SELECT i.text FROM lists l JOIN "
                     "list_items i ON i.list_id = l.id WHERE l.name LIKE '%x%'")
    assert err and text.startswith("error: no such column: i.text. Columns: ")
    assert "lists(id, name" in text and "list_items(id, list_id, name" in text
    # A CTE matches the FROM pattern too, and is no table, so it drops out.
    text, err = call(ct.db_query, sql="WITH t AS (SELECT * FROM tasks) "
                                      "SELECT course FROM t")
    assert err and "Columns: tasks(id, title" in text and "t(" not in text


def test_db_query_suggests_tables_with_near_names(dbfile):
    db.create_task(title="seed")
    text, err = call(ct.db_query, sql="SELECT * FROM list_item")
    assert err and "near names: list_items" in text
    text, err = call(ct.db_query, sql="SELECT 1 FROM qqqqqqqqqq")
    assert err and text == "error: no such table: qqqqqqqqqq"


def test_db_query_stops_a_runaway_query(dbfile, monkeypatch):
    """A correlated subquery per message row holds its call until the CLI's
    MCP timeout, 1,260s: the general tab sizes it for use_computer."""
    db.create_task(title="seed")
    monkeypatch.setattr(ct, "DB_QUERY_SECONDS", 0.2)
    started = time.monotonic()
    text, err = call(ct.db_query, sql=(
        "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM c "
        "WHERE x < 100000000) SELECT count(*) FROM c"))
    assert err and text.startswith("error: stopped after 0.2s")
    assert time.monotonic() - started < 5


# ---------- calendar_ids ----------

def test_calendar_ids_returns_the_full_suffixed_id(dbfile):
    """18 create_event calls in session 276ab09d 404'd in a row because the id
    was retyped without '@group.calendar.google.com'. The whole point of this
    tool is that the model never types one."""
    full = ("c_0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
            "@group.calendar.google.com")
    with db.session() as con, con:
        con.execute(
            "INSERT INTO events (day, event_id, calendar_id, calendar_name,"
            " summary, start_at, end_at, all_day, location)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            ("2026-07-30", "e1", full, "Chess Club", "Convention",
             "2026-07-30T11:00:00-04:00", "2026-07-30T13:00:00-04:00", 0, ""))
    text, err = call(ct.calendar_ids, name="chess")
    assert not err
    assert text == f"Chess Club = {full}"


def test_event_zone_follows_the_event_date_not_today(dbfile):
    """Session 7f73f8db spent ninety turns doing PT↔ET arithmetic over duplicate
    USC events, then heard 'lunch at 12:30' and wrote 12:30 America/Los_Angeles
    on a day the user was in North Carolina — three hours off. The zone is a
    property of the event's date, so the tool answers per date."""
    before, err = call(ct.event_zone, date="2026-08-16")   # day before RDU→LAX
    assert not err and "America/New_York" in before and "(ET)" in before
    after, _ = call(ct.event_zone, date="2026-08-17")      # the flight
    assert "America/Los_Angeles" in after and "(PT)" in after

    # The Thanksgiving window is a round trip, not a second permanent move.
    home, _ = call(ct.event_zone, date="2026-11-26")
    assert "America/New_York" in home
    back, _ = call(ct.event_zone, date="2026-11-29")
    assert "America/Los_Angeles" in back


def test_event_zone_defaults_to_the_day_key(dbfile):
    """Bare 'today' has to mean the 4am-rollover day-key, not the wall clock —
    between midnight and 4am those disagree, and so can their zones."""
    text, err = call(ct.event_zone)
    assert not err and db.day_key() in text


def test_event_zone_rejects_a_non_date(dbfile):
    """A malformed date must not silently fall back to today and hand back a
    confidently wrong zone."""
    text, err = call(ct.event_zone, date="next tuesday")
    assert err and "YYYY-MM-DD" in text


def test_calendar_ids_says_so_when_nothing_matches(dbfile):
    text, _ = call(ct.calendar_ids, name="nonexistent")
    assert "no matching calendar" in text


# ---------- provider ----------

def test_provider_rejects_an_unknown_source_with_the_valid_list(dbfile):
    text, err = call(ct.provider, source="bogus", range="today")
    assert err and "unknown source" in text and "calendar" in text


def test_provider_rejects_a_bad_range_with_the_grammar(dbfile):
    """Three different range separators were guessed across one week of
    sessions: `,` and `:` and `..`. Only the last is real."""
    text, err = call(ct.provider, source="calendar", range="2026-09-02:2026-09-09")
    assert err and "unknown range" in text


# ---------- reddit_search ----------

def _reddit_hit(**over):
    it = {"day": "2026-08-06", "subreddit": "onebag", "title": "Rain covers?",
          "author": "someone", "score": 85, "num_comments": 22,
          "url": "https://www.reddit.com/r/onebag/comments/abc/x/",
          "link": None, "created": "2026-08-06T09:00",
          "selftext": "I've  been\nusing a pack liner instead.",
          "truncated": False, "provisional": False}
    return it | over


def test_reddit_search_carries_body_and_link(monkeypatch):
    """The one read with no second hop: the console has no fetch tool and
    Reddit 403s anonymous clients, so whatever this returns is all the model
    will ever see of the thread. A list of titles can't answer a research
    question, and the permalink is what the user opens."""
    from app.providers import reddit
    monkeypatch.setattr(reddit, "search", lambda *a, **k: {
        "source": "reddit", "search": "rain cover", "subreddit": "onebag",
        "days": 90, "count": 1, "data": [_reddit_hit()]})
    text, err = call(ct.reddit_search, query="rain cover", subreddit="r/onebag",
                     days=90)
    assert not err
    assert "in r/onebag" in text and "newest first" in text
    assert "85pt" in text
    assert "using a pack liner instead." in text     # whitespace collapsed
    assert "https://www.reddit.com/r/onebag/comments/abc/x/" in text


def test_reddit_search_needs_a_subreddit(monkeypatch):
    """Arctic Shift refuses an unscoped query, so the schema requires one
    rather than letting the model spend a call learning that."""
    from app.providers import reddit
    monkeypatch.setattr(reddit, "search",
                        lambda *a, **k: pytest.fail("called unscoped"))
    text, err = call(ct.reddit_search, query="rain cover")
    assert err and "subreddit" in text
    assert ct.reddit_search.input_schema["required"] == ["query", "subreddit"]


def test_reddit_search_translates_a_busy_archive(monkeypatch):
    """The archive says "Timeout. Maybe slow down a bit", which reads as a
    rate limit and invites an immediate retry. "Narrow it" is the wrong
    advice: when the search is down every narrower retry 422s too. The tool
    points past the archive instead."""
    from app.providers import reddit

    def busy(*a, **k):
        raise reddit.ArchiveBusy("arctic-shift 422 on posts/search: Timeout.")
    monkeypatch.setattr(reddit, "search", busy)
    text, err = call(ct.reddit_search, query="cover", subreddit="r/onebag")
    assert err and "refusing searches" in text
    assert "don't rerun it narrowed or reworded" in text
    assert "`site:reddit.com/r/onebag`" in text


def test_reddit_search_says_when_it_only_scanned_the_newest_posts(monkeypatch):
    from app.providers import reddit
    post = {"day": "2026-11-17", "subreddit": "HiggsfieldAI", "title": "Refund?",
            "score": 3, "num_comments": 1, "provisional": True,
            "selftext": "", "url": "https://www.reddit.com/r/x/comments/a/"}

    def scanned(query, sub, days, limit, complete=False):
        return {"count": 1, "data": [post], "scan": {
            "posts": 300, "complete": complete, "back_to": "2026-11-12"}}
    monkeypatch.setattr(reddit, "search", scanned)
    text, err = call(ct.reddit_search, query="refund", subreddit="HiggsfieldAI",
                     days=45)
    head = text.splitlines()[0]
    assert not err and "newest 300 posts only (back to 2026-11-12)" in head
    assert "not the last 45d" in head and "Say the search was partial" in head

    monkeypatch.setattr(reddit, "search", lambda *a: scanned(*a, complete=True))
    text, _ = call(ct.reddit_search, query="refund", subreddit="HiggsfieldAI",
                   days=45)
    assert text.splitlines()[0] == ("reddit search 'refund' in r/HiggsfieldAI, "
                                    "last 45d (1 posts, newest first)")


def test_reddit_search_names_the_tracked_subs_in_its_description():
    """The tool's whole point is being the read the cache can't serve, which
    only lands if the description says which subs the cache does hold."""
    from app.config import REDDIT_SUBREDDITS
    for sub in REDDIT_SUBREDDITS:
        assert f"r/{sub}" in ct.reddit_search.description


# ---------- the allowlist split ----------

def test_only_reads_are_auto_approved():
    """Writers must not appear in the auto-approved list: entries there bypass
    can_use_tool entirely, so task_create would fire with no card."""
    assert set(ct.READ_TOOL_NAMES) == {
        "mcp__vault__provider", "mcp__vault__email_body",
        "mcp__vault__email_search", "mcp__vault__email_scheduled",
        "mcp__vault__reddit_search",
        "mcp__vault__venue_search", "mcp__vault__venue_details",
        "mcp__vault__db_query",
        "mcp__vault__calendar_ids", "mcp__vault__event_zone",
        "mcp__vault__free_time", "mcp__vault__chat_search",
        "mcp__vault__contact_find", "mcp__vault__message_files",
        "mcp__vault__brain_read", "mcp__vault__course_context",
        "mcp__vault__course_review", "mcp__vault__study_coverage",
        "mcp__vault__doc_pages", "mcp__vault__food_search", "mcp__vault__food_day",
        "mcp__vault__food_suggest", "mcp__vault__project_info",
        # Writes nothing: it asks the page for a tab, and the tab on screen
        # is the review — closing it is the undo.
        "mcp__vault__open_tab",
        # Closes only idle tabs, and the session stays resumable.
        "mcp__vault__tabs",
        # A message to a tab this one opened: on screen in that tab, and its
        # report comes back here.
        "mcp__vault__message_tab",
        # Other models on errands (app/delegates): what leaves is a scrubbed
        # brief to a sandboxed CLI, logged in delegate_runs
        # (test_delegate_boundary); shortlist only draws a card.
        "mcp__vault__delegate", "mcp__vault__fan_out", "mcp__vault__shortlist",
        # Colour roles only, each a checked #RRGGBB on a design token
        # (looks.problems); undone by picking another colour in the Look lab.
        "mcp__vault__theme_set",
        # Calls only the reads above, in a sandbox (console_code); every
        # write name in it throws.
        "mcp__vault__run_script"}
    # Derived from WRITE_TOOLS rather than re-listed, so a writer added later
    # is covered without anyone remembering to add it here too.
    for t in ct.WRITE_TOOLS:
        assert f"mcp__vault__{t.name}" not in ct.READ_TOOL_NAMES


def test_every_tool_is_registered():
    """A handler defined but left out of TOOLS is invisible to the model, which
    is indistinguishable from not having written it."""
    assert {t.name for t in ct.TOOLS} == {
        "provider", "email_body", "email_search", "reddit_search",
        "venue_search", "venue_details", "db_query", "calendar_ids",
        "event_zone", "free_time", "chat_search", "contact_find",
        "task_create", "task_update",
        "list_add", "list_create", "list_item_done", "debt_add", "debt_settle",
        "debt_update", "debt_remove", "beer_add", "beer_settle",
        "watchlist_add", "watchlist_watched",
        "expense_add", "expense_update", "expense_remove", "expense_paid",
        "habit_checkin", "habit_add",
        "package_add", "package_update", "package_delete", "trip_add",
        "trip_delete", "contact_update", "contact_create", "email_draft",
        "email_archive",
        "email_schedule", "email_unschedule", "email_scheduled",
        "event_create", "event_update", "event_delete", "event_rsvp",
        "course_hours", "course_except", "course_set",
        "course_context", "course_review", "course_review_add",
        "course_review_mark", "study_coverage",
        "message_files", "file_read",
        "brain_read", "brain_note", "brain_verdict", "brain_page",
        "doc_pages",
        "food_search", "food_day", "food_suggest", "food_log", "food_entry", "project_info",
        "food_create", "food_fix", "food_close", "food_weigh", "food_profile",
        "open_tab", "tabs", "message_tab", "run_script",
        "delegate", "fan_out", "shortlist", "theme_set"}
    # TOOLS is the concatenation and nothing else — a tool reachable by the
    # model but in neither list would be neither approved nor gated.
    assert ([t.name for t in ct.TOOLS]
            == [t.name for t in ct.READ_TOOLS + ct.WRITE_TOOLS])


# ---------- the prompt names what it can reach ----------

def test_the_prompt_says_tools_carry_a_prefix():
    """Measured over the 88 sessions on record: 29 of 153 ToolSearch calls came
    back empty, and every one was for a vault tool — 15/15 `select:` calls
    using the bare name this prompt writes (`select:db_query`,
    `select:debt_add,task_update`) and 14/14 keyword searches. A qualified
    `select:mcp__vault__x` never failed. The prose can keep the short names
    only while it also says what the real ones look like.

    This used to pin the words "already loaded", which were false: the vault
    tools are deferred, and the prompt telling the model not to look them up
    was fighting the one step that makes them callable. What has to survive is
    the qualified `select:` form, not the claim about loading."""
    from app import console_prompt
    s = console_prompt.SYSTEM_APPEND
    assert "mcp__vault__" in s
    # The form itself, spelled out — an empty search reading as a missing
    # capability is what this line exists to stop.
    assert "select:mcp__vault__" in s


def test_the_prompt_names_every_tool():
    """A tool the prose never mentions is one the model reaches only if its own
    description happens to fire — which is how `email_body` went unnamed while
    the prompt sent email questions to `db_query` over a table holding nothing
    but snippets. New tool, new line here."""
    from app import console_prompt
    # open_tab is named by HANDOFF_APPEND, which goes out with exactly the
    # sessions that are given the tool (render_append).
    prose = console_prompt.SYSTEM_APPEND + console_prompt.HANDOFF_APPEND
    unnamed = [t.name for t in ct.TOOLS if t.name not in prose]
    assert unnamed == []


# ---------- list_add / list_create / list_item_done ----------

def _shopping(): return db.find_list("Shopping")["id"]


def test_list_add_lands_on_the_list_not_the_task_board(dbfile):
    """Session 83d02377 was asked to add six move-in items, said 'no dedicated
    shopping-list write tool is exposed to me — only tasks', and filed all six
    as tasks. The shopping card never saw them."""
    text, err = call(ct.list_add, name="Laundry detergent (pods)",
                     category="Dorm", note="Bag is covered by the pack, soap isn't")
    assert not err
    items = db.list_items_for(_shopping())
    assert [i["name"] for i in items] == ["Laundry detergent (pods)"]
    assert items[0]["category"] == "Dorm"
    assert str(items[0]["id"]) in text
    assert db.list_tasks(status="open") == []      # and nothing on the board


def test_list_add_defaults_to_shopping(dbfile):
    """The list the migration seeds is the one an unqualified `add milk` means.
    Without the default, every buy-list add would need the model to name a list
    it was never told about."""
    _, err = call(ct.list_add, name="milk")
    assert not err
    assert [i["name"] for i in db.list_items_for(_shopping())] == ["milk"]


def test_list_add_files_to_a_named_list(dbfile):
    """The whole point of the second list: 'add a desk lamp to my college
    packing list' has to land there and not on the buy list."""
    call(ct.list_create, name="College packing")
    _, err = call(ct.list_add, name="Desk lamp", list="college PACKING")
    assert not err
    packing = db.find_list("College packing")
    assert [i["name"] for i in db.list_items_for(packing["id"])] == ["Desk lamp"]
    assert db.list_items_for(_shopping()) == []


def test_list_add_on_an_unknown_list_names_the_real_ones(dbfile):
    """The model can't see the chips. A miss that just said 'no such list'
    invites the same guess again, or a fallback onto the task board — so the
    error carries the names it could have meant, and writes nothing."""
    text, err = call(ct.list_add, name="Streamers", list="Party")
    assert err and "Party" in text and "Shopping" in text
    assert "list_create" in text
    assert db.list_items_for(_shopping()) == []


def test_list_add_requires_a_name(dbfile):
    text, err = call(ct.list_add, name="  ")
    assert err and "name is required" in text
    assert db.list_items_for(_shopping()) == []


def test_list_add_takes_several_items_in_one_call(dbfile):
    call(ct.list_create, name="Party")
    text, err = call(ct.list_add, list="party", items=[
        {"name": "Streamers"}, {"name": "Cups", "category": "Paper"},
        {"name": "  "}, {"name": "Milk", "list": "Shopping"}])
    assert not err, text
    party = db.find_list("Party")["id"]
    assert sorted(i["name"] for i in db.list_items_for(party)) == ["Cups", "Streamers"]
    assert [i["name"] for i in db.list_items_for(_shopping())] == ["Milk"]
    assert "FAILED: '  ': name is required" in text


def test_batch_schemas_validate_through_the_server(dbfile):
    """`items` has to get past the MCP server's jsonschema check, and a
    misnamed field inside an item has to be refused like a top-level one."""
    from mcp.types import CallToolRequest, CallToolRequestParams
    srv = ct.build_server()["instance"]

    def served(**args):
        req = CallToolRequest(method="tools/call",
                              params=CallToolRequestParams(name="list_add", arguments=args))
        r = asyncio.run(srv.request_handlers[CallToolRequest](req)).root
        return r.content[0].text
    assert "added item" in served(items=[{"name": "Eggs"}, {"name": "Bread"}])
    assert "'title' was unexpected" in served(items=[{"title": "Eggs"}])


def test_list_create_rejects_a_duplicate_name(dbfile):
    """Case-insensitively — 'shopping' next to 'Shopping' would be two chips
    each holding half the list."""
    text, err = call(ct.list_create, name="shopping")
    assert err and "already exists" in text
    assert [l["name"] for l in db.all_lists()] == ["Shopping"]


def test_list_item_done_ticks_off_and_undoes(dbfile):
    item = db.create_list_item(_shopping(), name="Desk lamp")
    text, err = call(ct.list_item_done, item_id=item["id"])
    assert not err and "done" in text
    assert db.list_items_for(_shopping()) == []    # open list is empty
    text, err = call(ct.list_item_done, item_id=item["id"], done=False)
    assert not err and "back on the list" in text
    assert [i["name"] for i in db.list_items_for(_shopping())] == ["Desk lamp"]


def test_list_item_done_attaches_a_note_and_keeps_the_old_one(dbfile):
    """A research tab says where its report is when it ticks its item off."""
    item = db.create_list_item(_shopping(), name="Research X", note="from the user")
    text, err = call(ct.list_item_done, item_id=item["id"], note="Report: R/X.md")
    assert not err and "Report: R/X.md" in text
    row = db.get_list_item(item["id"])
    assert row["done"] == 1 and row["note"] == "from the user\nReport: R/X.md"
    # Same note again: kept once.
    call(ct.list_item_done, item_id=item["id"], note="Report: R/X.md")
    assert db.get_list_item(item["id"])["note"] == "from the user\nReport: R/X.md"


def test_a_note_on_a_done_item_leaves_when_it_was_done(dbfile):
    item = db.create_list_item(_shopping(), name="Research Y")
    with db.session() as con, con:
        con.execute("UPDATE list_items SET done=1, done_at='2026-11-16T10:00:00' WHERE id=?",
                    (item["id"],))
    _, err = call(ct.list_item_done, item_id=item["id"], note="Report: R/Y.md")
    assert not err
    row = db.get_list_item(item["id"])
    assert row["done_at"] == "2026-11-16T10:00:00" and row["note"] == "Report: R/Y.md"


def test_list_item_done_reports_a_missing_item(dbfile):
    text, err = call(ct.list_item_done, item_id=404)
    assert err and "no list item 404" in text


# ---------- expense_add / expense_update / expense_remove / expense_paid ----------

def test_expense_tools_claim_recategorize_and_drop(dbfile):
    """The console can claim for Dad, change the category and remove a claim,
    and a bad call comes back as an error rather than raising."""
    from app import expenses

    text, err = call(ct.expense_add, merchant="Venmo", amount="24.50",
                     category="Personal", note="haircut")
    assert not err and "$24.50" in text and "[Personal]" in text
    [row] = expenses.summary()["waiting"]

    text, err = call(ct.expense_update, expense_id=row["id"], category="Grooming")
    assert not err and "[Grooming]" in text
    assert db.list_rows("debts") == []              # not a debt

    _, err = call(ct.expense_add, transaction_id="nope")
    assert err
    _, err = call(ct.expense_paid, batch_id=99)
    assert err

    text, err = call(ct.expense_remove, expense_id=row["id"])
    assert not err and expenses.summary()["waiting"] == []


# ---------- debt_add / debt_settle ----------

def test_debt_add_lands_on_the_card_not_in_a_task_note(dbfile):
    """Session 137b2608: 'this is a debt tracker so add them to debts -> ana
    and ben already paid'. With no debt writer it globbed the vault for a file
    named something like debt, found none, and wrote all four balances into
    task 91's note — where the debts card cannot see them and no total counts
    them."""
    text, err = call(ct.debt_add, person="Nadia", amount=40,
                     reason="beach house")
    assert not err
    rows = db.list_rows("debts")
    assert [(r["person"], r["amount"], r["direction"]) for r in rows] == [
        ("Nadia", 40.0, "owed_to_me")]
    assert rows[0]["reason"] == "beach house"
    assert str(rows[0]["id"]) in text and "$40.00" in text
    assert db.list_tasks(status="open") == []       # and not on the board
    assert db.list_rows("list_items") == []         # nor the other drain


def test_debt_add_takes_the_other_direction(dbfile):
    text, err = call(ct.debt_add, person="Abel", amount=12.5, direction="i_owe")
    assert not err and "is owed" in text and "$12.50" in text
    assert db.list_rows("debts")[0]["direction"] == "i_owe"
    assert db.debt_summary()["net"] == -12.5


def test_debt_add_rejects_an_unknown_direction(dbfile):
    text, err = call(ct.debt_add, person="Rowan", amount=24, direction="owes_me")
    assert err and "owed_to_me or i_owe" in text
    assert db.list_rows("debts") == []


def test_debt_add_requires_a_person_and_an_amount(dbfile):
    """A debt with no amount is worse than no debt: it shows on the card and
    contributes nothing to the total. The tool asks rather than defaulting."""
    text, err = call(ct.debt_add, person="Nadia")
    assert err and "amount is required" in text
    text, err = call(ct.debt_add, person="Nadia", amount="forty-ish")
    assert err and "must be a number" in text
    text, err = call(ct.debt_add, person="  ", amount=40)
    assert err and "person is required" in text
    assert db.list_rows("debts") == []


def test_debt_add_rejects_a_signed_amount(dbfile):
    """`direction` carries the sign. A -40 i_owe would subtract from what the
    user is owed, which is the opposite of how the call reads."""
    text, err = call(ct.debt_add, person="Abel", amount=-40, direction="i_owe")
    assert err and "greater than zero" in text
    text, err = call(ct.debt_add, person="Abel", amount=0)
    assert err and "greater than zero" in text
    assert db.list_rows("debts") == []


def test_debt_settle_closes_and_reopens(dbfile):
    made = db.create_debt(person="Felix", amount=24, reason="pizza")
    text, err = call(ct.debt_settle, debt_id=made["id"])
    assert not err and "settled" in text
    assert db.list_rows("debts") == []              # off the open card
    assert db.debt_summary()["owed_to_me"] == 0
    text, err = call(ct.debt_settle, debt_id=made["id"], settled=False)
    assert not err and "open again" in text
    assert [r["person"] for r in db.list_rows("debts")] == ["Felix"]


def test_debt_settle_reports_a_missing_debt(dbfile):
    text, err = call(ct.debt_settle, debt_id=404)
    assert err and "no debt 404" in text


def test_debt_settle_rejects_a_non_numeric_id(dbfile):
    text, err = call(ct.debt_settle, debt_id="1; DROP TABLE debts")
    assert err and "must be an integer" in text


def test_debt_merge_goes_through_update_and_remove_not_settle(dbfile):
    """Session e9215d16: asked for one sum per person, it had no way to edit
    or delete a row, so it settled nine split rows and added a combined one
    for each person. The card's history then showed nine payments that never
    happened."""
    a = db.create_debt(person="Petra", amount=99.38, reason="KBBQ + rental")
    b = db.create_debt(person="Petra", amount=25.47, reason="trip true-up")
    text, err = call(ct.debt_update, debt_id=a["id"], amount=124.85,
                     reason="Bday total")
    assert not err and "$124.85" in text and "Bday total" in text
    text, err = call(ct.debt_remove, debt_id=b["id"])
    assert not err and "removed" in text and "$25.47" in text
    rows = db.list_rows("debts")
    assert [(r["person"], r["amount"]) for r in rows] == [("Petra", 124.85)]
    with db.session() as con:      # nothing marked paid along the way
        assert con.execute("SELECT COUNT(*) FROM debts WHERE settled=1"
                           ).fetchone()[0] == 0


def test_debt_update_validates_and_clears(dbfile):
    made = db.create_debt(person="Felix", amount=24, reason="pizza",
                          incurred_on="2026-10-02")
    text, err = call(ct.debt_update, debt_id=made["id"], amount=-5)
    assert err and "greater than zero" in text
    text, err = call(ct.debt_update, debt_id=made["id"], direction="owes_me")
    assert err and "owed_to_me or i_owe" in text
    text, err = call(ct.debt_update, debt_id=made["id"], person="  ")
    assert err and "blank" in text
    text, err = call(ct.debt_update, debt_id=made["id"], reason="",
                     incurred_on="")
    assert not err
    row = db.list_rows("debts")[0]
    assert (row["amount"], row["reason"], row["incurred_on"]) == (24.0, None, None)


def test_debt_update_and_remove_report_a_missing_debt(dbfile):
    text, err = call(ct.debt_update, debt_id=404, amount=5)
    assert err and "no debt 404" in text
    text, err = call(ct.debt_remove, debt_id=404)
    assert err and "no debt 404" in text
    text, err = call(ct.debt_remove, debt_id="1; DROP TABLE debts")
    assert err and "must be an integer" in text


# ---------- beer_add / beer_settle ----------

def test_beers_stay_out_of_the_money(dbfile):
    """'theo, soren, and petra owe me a beer' is a favor, not a $5 debt: it
    must not move the owed-to-you total or put anyone on the chase list."""
    for p in ("Theo", "Soren", "Petra"):
        text, err = call(ct.beer_add, person=p)
        assert not err and f"{p} owes you a beer" == text
    text, err = call(ct.beer_add, person="theo", count=2, reason="ski trip")
    assert not err and "2 beers" in text
    assert db.list_rows("debts") == []
    assert db.debt_summary()["owed_to_me"] == 0
    s = db.beer_summary()
    assert s["owed_to_me"] == 5
    assert [(g["person"], g["count"]) for g in s["beers"]] == [
        ("Theo", 3), ("Petra", 1), ("Soren", 1)]
    assert s["beers"][0]["reasons"] == ["ski trip"]


def test_beer_settle_takes_one_at_a_time(dbfile):
    db.add_beers("Theo", 2)
    text, err = call(ct.beer_settle, person="theo")
    assert not err and "1 still open" in text
    text, err = call(ct.beer_settle, person="Theo")
    assert not err and "0 still open" in text
    text, err = call(ct.beer_settle, person="Theo")
    assert err and "no open beer" in text
    assert db.beer_summary()["beers"] == []


def test_beer_add_validates(dbfile):
    text, err = call(ct.beer_add, person="  ")
    assert err and "person is required" in text
    text, err = call(ct.beer_add, person="Theo", direction="owes_me")
    assert err and "owed_to_me or i_owe" in text
    text, err = call(ct.beer_add, person="Theo", count=0)
    assert err and "between 1 and 24" in text
    text, err = call(ct.beer_add, person="Theo", direction="i_owe")
    assert not err and text == "you owe Theo a beer"


def test_the_whole_137b2608_instruction_now_completes(dbfile):
    """The second half failed as a consequence of the first: the session left
    task 91 open because the note it had just written was the only copy of the
    debt. With the debts in their own store the task can close."""
    task = db.create_task(title="Nadia Rowan Felix Abel")
    for person in ("Nadia", "Abel"):
        _, err = call(ct.debt_add, person=person, amount=40,
                      reason="beach house")
        assert not err
    _, err = call(ct.task_update, task_id=task["id"], status="done")
    assert not err
    assert db.list_tasks(status="open") == []
    assert sorted(r["person"] for r in db.list_rows("debts")) == ["Abel", "Nadia"]
    assert db.debt_summary()["owed_to_me"] == 80.0


# ---------- watchlist_add / watchlist_watched ----------

def test_watchlist_add_queues_instead_of_nagging_from_the_board(dbfile):
    """No session had to get this wrong first: the drain has been observed
    twice and the watchlist was the last store the console could read and not
    write. A film filed as a task sits on the board asking to be completed."""
    text, err = call(ct.watchlist_add, title="Heat", year=1995,
                     services="Netflix", note="Rowan's rec")
    assert not err
    rows = db.list_rows("watchlist")
    assert [(r["title"], r["year"], r["kind"]) for r in rows] == [
        ("Heat", 1995, "movie")]
    assert rows[0]["services"] == "Netflix"
    assert "Heat" in text and "1995" in text and "Netflix" in text
    assert db.list_tasks(status="open") == []       # and not on the board


def test_watchlist_add_takes_shows(dbfile):
    text, err = call(ct.watchlist_add, title="The Bear", kind="show")
    assert not err and "show" in text
    assert db.list_rows("watchlist")[0]["kind"] == "show"


def test_watchlist_add_rejects_an_unknown_kind(dbfile):
    text, err = call(ct.watchlist_add, title="Heat", kind="film")
    assert err and "movie or show" in text
    assert db.list_rows("watchlist") == []


def test_watchlist_add_requires_a_title_and_a_numeric_year(dbfile):
    text, err = call(ct.watchlist_add, title="  ")
    assert err and "title is required" in text
    text, err = call(ct.watchlist_add, title="Heat", year="nineties")
    assert err and "four-digit number" in text
    assert db.list_rows("watchlist") == []


def test_watchlist_add_survives_a_missing_year(dbfile):
    """Optional means omittable — the schema shorthand bug that made every
    parameter required is exactly what this guards for the newest tools."""
    _, err = call(ct.watchlist_add, title="Sinners")
    assert not err
    assert db.list_rows("watchlist")[0]["year"] is None


def test_watchlist_watched_ticks_off_and_undoes(dbfile):
    item = db.create_watch_item(title="Heat", year=1995)
    text, err = call(ct.watchlist_watched, item_id=item["id"])
    assert not err and "watched" in text
    assert db.list_rows("watchlist") == []          # off the card
    text, err = call(ct.watchlist_watched, item_id=item["id"], watched=False)
    assert not err and "back on the list" in text
    assert [r["title"] for r in db.list_rows("watchlist")] == ["Heat"]


def test_watchlist_watched_reports_a_missing_item(dbfile):
    text, err = call(ct.watchlist_watched, item_id=404)
    assert err and "no watchlist item 404" in text


def test_watchlist_watched_rejects_a_non_numeric_id(dbfile):
    text, err = call(ct.watchlist_watched, item_id="1; DROP TABLE watchlist")
    assert err and "must be an integer" in text


def test_every_card_backed_store_has_a_writer():
    """The drain closes only when there is nowhere left for a write to fall.
    Each small list the console can read now has an add and a done-flip."""
    names = {t.name for t in ct.WRITE_TOOLS}
    for add, flip in (("list_add", "list_item_done"),
                      ("debt_add", "debt_settle"),
                      ("watchlist_add", "watchlist_watched"),
                      ("habit_add", "habit_checkin")):
        assert {add, flip} <= names


# ---------- habit_checkin / habit_add ----------

def test_habit_checkin_lands_on_the_card_not_the_task_board(dbfile, frozen_day):
    """Habits were the last store behind a card the console could read and not
    write, while SYSTEM_APPEND pointed at it ('mood/workout journaling moved to
    habits'). The task board was the only writer in reach, and a habit closed
    as a task still leaves the day reading as missed on the card."""
    db.create_habit("Exercise")
    text, err = call(ct.habit_checkin, habit="Exercise", note="lifted")
    assert not err
    grid = {h["name"]: h for h in db.list_habits(frozen_day)}
    assert grid["Exercise"]["today"] == 1
    assert grid["Exercise"]["streak"] == 1
    assert "1-day streak" in text
    assert db.list_tasks(status="open") == []
    with db.session() as con:
        row = con.execute("SELECT day, source, note FROM habit_checkins").fetchone()
    assert (row["day"], row["source"], row["note"]) == (frozen_day, "console", "lifted")


def test_habit_checkin_matches_the_name_the_user_says(dbfile, frozen_day):
    """The console has what the user said, not the id the widget clicks. A
    match that needed the exact stored casing would fail on '$50 doordash'."""
    db.create_habit("$50 DoorDash")
    text, err = call(ct.habit_checkin, habit="  $50   doordash ")
    assert not err and "$50 DoorDash" in text
    assert db.list_habits(frozen_day)[0]["today"] == 1


def test_habit_checkin_names_the_real_habits_when_it_cannot_match(dbfile, frozen_day):
    """Recovery in one turn. Guessing is the worse failure: a check written to
    an invented habit is a row the user never sees and never asked for."""
    db.create_habit("Exercise")
    db.create_habit("Read")
    text, err = call(ct.habit_checkin, habit="Meditate")
    assert err
    assert "Exercise" in text and "Read" in text and "habit_add" in text
    with db.session() as con:
        assert con.execute("SELECT COUNT(*) c FROM habit_checkins").fetchone()["c"] == 0
        assert con.execute("SELECT COUNT(*) c FROM habits").fetchone()["c"] == 2


def test_habit_checkin_files_under_the_day_key_not_the_wall_clock(dbfile, monkeypatch):
    """The 4am rollover, which is the whole reason day_key exists. At 01:30 the
    civil date is already the 12th while the card is still filing under the
    11th, so a default of date.today() would check off a day that hasn't
    started and leave the day the user meant reading as missed."""
    monkeypatch.setattr(db, "day_key", lambda dt=None: "2026-08-11")
    db.create_habit("Sleep")
    text, err = call(ct.habit_checkin, habit="Sleep")
    assert not err
    with db.session() as con:
        assert con.execute("SELECT day FROM habit_checkins").fetchone()["day"] == "2026-08-11"


def test_habit_checkin_refuses_a_day_past_the_day_key(dbfile, frozen_day):
    """Same rollover, arriving the other way: a model that works the date out
    itself between midnight and 4am hands over tomorrow's. The refusal quotes
    the day-key so the retry is right, instead of parking a check on a day no
    streak counts."""
    db.create_habit("Exercise")
    text, err = call(ct.habit_checkin, habit="Exercise", day="2026-08-02")
    assert err and "2026-08-01" in text and "4am" in text
    with db.session() as con:
        assert con.execute("SELECT COUNT(*) c FROM habit_checkins").fetchone()["c"] == 0


def test_habit_checkin_takes_a_past_day_the_user_named(dbfile, frozen_day):
    db.create_habit("Exercise")
    text, err = call(ct.habit_checkin, habit="Exercise", day="2026-07-30")
    assert not err and "2026-07-30" in text
    with db.session() as con:
        assert con.execute("SELECT day FROM habit_checkins").fetchone()["day"] == "2026-07-30"


def test_habit_checkin_rejects_a_non_date(dbfile, frozen_day):
    db.create_habit("Exercise")
    text, err = call(ct.habit_checkin, habit="Exercise", day="yesterday")
    assert err and "YYYY-MM-DD" in text


def test_habit_checkin_zero_un_checks_a_day(dbfile, frozen_day):
    """value=0 is how a mis-logged day is corrected. It leaves a tombstone
    rather than deleting the row, so the auto pass can't refill it."""
    db.create_habit("Exercise")
    call(ct.habit_checkin, habit="Exercise")
    text, err = call(ct.habit_checkin, habit="Exercise", value=0)
    assert not err and "un-checked" in text
    assert db.list_habits(frozen_day)[0]["today"] == 0
    with db.session() as con:
        assert con.execute("SELECT COUNT(*) c FROM habit_checkins").fetchone()["c"] == 1


def test_habit_add_starts_tracking_and_is_checkable(dbfile, frozen_day):
    text, err = call(ct.habit_add, name="Meditate")
    assert not err and "Meditate" in text
    text, err = call(ct.habit_checkin, habit="Meditate")
    assert not err
    assert db.list_habits(frozen_day)[0]["today"] == 1


def test_habit_add_does_not_duplicate_an_active_habit(dbfile, frozen_day):
    """Two rows named Exercise split one streak in two, and the check-in then
    writes to whichever the resolver happens to reach first."""
    db.create_habit("Exercise")
    text, err = call(ct.habit_add, name="exercise")
    assert not err and "already tracking" in text
    with db.session() as con:
        assert con.execute("SELECT COUNT(*) c FROM habits").fetchone()["c"] == 1


def test_habit_add_resumes_an_archived_habit_rather_than_reopening_it(dbfile, frozen_day):
    """Archiving keeps the check-in history, so creating a second row by the
    same name would strand it on a habit nothing renders and restart the
    streak at zero — the user's record of having done the thing, lost to a
    write that looked like it worked."""
    old = db.create_habit("Exercise")
    db.checkin(old["id"], day="2026-07-31")
    db.update_habit(old["id"], archived=True)
    assert db.list_habits(frozen_day) == []

    text, err = call(ct.habit_add, name="Exercise")
    assert not err and "resumed" in text
    live = db.list_habits(frozen_day)
    assert len(live) == 1 and live[0]["id"] == old["id"]
    with db.session() as con:
        assert con.execute("SELECT COUNT(*) c FROM habit_checkins").fetchone()["c"] == 1


def test_habit_add_requires_a_name(dbfile, frozen_day):
    text, err = call(ct.habit_add, name="   ")
    assert err and "name is required" in text
    assert db.list_habits(frozen_day) == []


def test_habit_add_sets_nothing_the_card_cannot_render():
    """cadence/target/unit are columns nothing reads — the widget's own create
    form takes a name, and a 'weekly' habit would draw a daily streak that
    misses six days in seven. A parameter the model can set and the card can't
    honour is worse than an absent one, so the schema doesn't offer them."""
    props = ct.habit_add.input_schema["properties"]
    assert set(props) == {"name"}


# ---------- package_add ----------

def test_package_add_files_an_order_no_email_will_announce(dbfile):
    """Session 83d02377, end: the user pasted an Amazon orders page — Raven 20
    'Arriving Thursday', Crep Protect 'Arriving Friday'. Those orders sit on a
    parent's account, so nothing reaches this mailbox and the extractor can
    never see them. With deliveries write-only-by-amendment, the console put
    all three on the shopping list and ticked them bought; the dates survived
    only as prose in a note, on no card that shows dates."""
    text, err = call(ct.package_add, merchant="Amazon",
                     item_desc="Fjällräven Raven 20 Backpack, Navy",
                     eta_day="2026-08-06")
    assert not err and "2026-08-06" in text
    from app.providers import email_extract
    rows = email_extract.active_packages()
    assert [r["item_desc"] for r in rows] == ["Fjällräven Raven 20 Backpack, Navy"]
    assert rows[0]["status"] == "ordered"
    assert db.list_rows("list_items") == []        # and not on the buy list
    assert db.list_tasks(status="open") == []      # and not on the board


def test_package_add_requires_a_merchant(dbfile):
    text, err = call(ct.package_add, item_desc="a mystery box")
    assert err and "merchant is required" in text


def test_package_add_rejects_a_bad_date_and_status(dbfile):
    text, err = call(ct.package_add, merchant="Amazon", eta_day="Thursday")
    assert err and "YYYY-MM-DD" in text
    text, err = call(ct.package_add, merchant="Amazon", status="arriving")
    assert err and "out_for_delivery" in text


def test_package_add_then_update_addresses_the_same_row(dbfile):
    """The key package_add reports has to be the one package_update takes —
    it is all the console has to go on for a later change."""
    text, _ = call(ct.package_add, merchant="Amazon", item_desc="Raven 20")
    key = "Amazon:manual-1"
    assert key in text
    text, err = call(ct.package_update, order_key=key, status="delivered")
    assert not err and "delivered" in text


# ---------- package_update ----------

def _campus_ready():
    """The real row as the extractor left it: no ETA, still 'ordered'."""
    from app.providers import email_extract
    con = db.connect()
    try:
        with con:
            email_extract._upsert_package(
                con, {"merchant": "Campus Ready", "order_id": "41415",
                      "item_desc": "College Twin XL Extra Comfort Pack",
                      "status": "ordered"},
                "19fc4ce4db759a8c", "2026-08-02T19:28:32")
    finally:
        con.close()
    return "Campus Ready:41415"


def test_package_update_writes_a_date_the_email_never_carried(dbfile):
    """Session 1f1aaa64: the user pinned the reply giving the Aug 18 pickup
    date and said 'i wanted to add to deliveries/travel not task'. The console
    had no write path into `packages`, so it filed — then dropped — a task."""
    key = _campus_ready()
    text, err = call(ct.package_update, order_key=key, eta_day="2026-08-18")
    assert not err and "2026-08-18" in text
    from app.providers import email_extract
    row = next(p for p in email_extract.active_packages()
               if p["order_key"] == key)
    assert row["eta_day"] == "2026-08-18"
    assert row["status"] == "ordered"              # untouched fields stay put
    assert row["item_desc"] == "College Twin XL Extra Comfort Pack"


def test_package_update_reports_an_unknown_order_key(dbfile):
    text, err = call(ct.package_update, order_key="Nike:nope", eta_day="2026-08-18")
    assert err and "no package" in text


def test_package_update_rejects_a_bad_date_and_status(dbfile):
    key = _campus_ready()
    text, err = call(ct.package_update, order_key=key, eta_day="Aug 18")
    assert err and "YYYY-MM-DD" in text
    text, err = call(ct.package_update, order_key=key, status="on the truck")
    assert err and "out_for_delivery" in text


def test_package_update_needs_something_to_change(dbfile):
    key = _campus_ready()
    text, err = call(ct.package_update, order_key=key)
    assert err and "nothing to change" in text


# ---------- package_delete ----------

def test_package_delete_removes_the_row(dbfile):
    key = _campus_ready()
    text, err = call(ct.package_delete, order_key=key)
    assert not err and key in text
    from app.providers import email_extract
    assert email_extract.active_packages() == []


def test_package_delete_reports_an_unknown_order_key(dbfile):
    text, err = call(ct.package_delete, order_key="Nike:nope")
    assert err and "no package" in text


def test_package_delete_requires_an_order_key(dbfile):
    text, err = call(ct.package_delete)
    assert err and "order_key is required" in text


# ---------- trip_add ----------
#
# A flight can be on the calendar and in no other store, so /api/trips leaves
# it out and the travel card's count means only the other trips. Nothing else
# in the system can add it when the booking predates the mail sync's history:
# there is no message to extract.

def test_trip_add_puts_a_flight_on_the_card_with_no_email_behind_it(dbfile):
    text, err = call(ct.trip_add, flight_no="2299",
                     dep_local="2026-08-17T15:00:00", pnr="FLZUNL",
                     carrier="AA", dep_airport="RDU", arr_airport="LAX")
    assert not err
    assert "AA2299" in text and "RDU->LAX" in text
    con = db.connect()
    try:
        rows = [dict(r) for r in con.execute("SELECT * FROM trips")]
    finally:
        con.close()
    assert len(rows) == 1
    assert (rows[0]["pnr"], rows[0]["dep_day"]) == ("FLZUNL", "2026-08-17")


def test_trip_add_reports_a_bare_date_instead_of_filing_it_at_midnight(dbfile):
    """A date-only dep_local parses fine and lands at 00:00, which would date
    the T-24h check-in task a day early and print '12:00 AM' on the card."""
    text, err = call(ct.trip_add, flight_no="2299", dep_local="2026-08-17")
    assert err
    assert "departure time" in text


def test_trip_add_reports_a_bad_airport_rather_than_raising(dbfile):
    text, err = call(ct.trip_add, flight_no="2299",
                     dep_local="2026-08-17T15:00:00", dep_airport="Raleigh")
    assert err
    assert "3-letter IATA" in text


# ---------- trip_delete ----------
#
# An exchanged flight: the airline's change email opens the new leg without
# ever closing the old, so the card shows both and the dead leg keeps its
# check-in task and alarm.

def _two_legs():
    for dep in ("2026-11-22T09:40:00", "2026-11-29T05:00:00"):
        _, err = call(ct.trip_add, flight_no="AA3042" if "29" in dep else "2308",
                      dep_local=dep, pnr="FLZUNL", carrier="AA")
        assert not err
    task = db.create_task(title="Check in AA3042", due="2026-11-29",
                          source="travel", source_kind="travel",
                          source_ref="FLZUNL:3042:2026-11-29")
    return task["id"]


def _trip_keys():
    con = db.connect()
    try:
        return {(r["pnr"], r["flight_no"], r["dep_day"])
                for r in con.execute("SELECT * FROM trips")}
    finally:
        con.close()


def test_trip_delete_removes_one_leg_and_its_checkin_task(dbfile):
    task_id = _two_legs()
    text, err = call(ct.trip_delete, pnr="flzunl", flight_no="AA3042",
                     dep_day="2026-11-29")
    assert not err and "AA3042" in text and f"#{task_id}" in text
    assert _trip_keys() == {("FLZUNL", "2308", "2026-11-22")}
    con = db.connect()
    try:
        status = con.execute("SELECT status FROM tasks WHERE id=?",
                             (task_id,)).fetchone()["status"]
    finally:
        con.close()
    assert status == "dropped"
    # Dropped, not deleted: the dedupe still refuses to re-open it.
    assert db.task_exists_for_source("travel", "FLZUNL:3042:2026-11-29")


def test_trip_delete_withdraws_the_derived_alarm(dbfile):
    """The alarm is derived from `trips` each reconcile, so the deleted leg
    simply stops being wanted."""
    from datetime import datetime, timedelta

    from app import alarms
    _two_legs()
    call(ct.trip_delete, pnr="FLZUNL", flight_no="3042", dep_day="2026-11-29")
    con = db.connect()
    try:
        now = datetime(2026, 11, 17, 12).astimezone()
        res = alarms.flight_alarms(con, now=now, horizon=now + timedelta(days=30))
    finally:
        con.close()
    assert [a.source_ref for a in res.alarms] == ["FLZUNL:2308:2026-11-22"]


def test_trip_delete_reports_an_unknown_leg(dbfile):
    _two_legs()
    text, err = call(ct.trip_delete, pnr="FLZUNL", flight_no="2308",
                     dep_day="2026-11-25")
    assert err and "no such trip" in text
    assert len(_trip_keys()) == 2


def test_trip_delete_requires_the_whole_key(dbfile):
    text, err = call(ct.trip_delete, pnr="FLZUNL", flight_no="2308")
    assert err and "required" in text


# ---------- email_archive ----------

def test_email_archive_archives_cached_and_uncached_ids(dbfile, monkeypatch):
    from app.routes import email as email_routes
    con = db.connect()
    try:
        with con:
            con.execute(
                "INSERT INTO emails (msg_id, thread_id, day, received_at,"
                " from_addr, subject, in_inbox, unread, refreshed_at)"
                " VALUES ('aaa1', 't1', '2026-11-17', '2026-11-17T09:00:00',"
                " 'x@example.com', 'hi', 1, 1, '2026-11-17T09:00:00')")
    finally:
        con.close()
    calls = []
    monkeypatch.setattr(email_routes, "access_token", lambda: "tok")
    monkeypatch.setattr(email_routes, "api_call",
                        lambda url, token, **kw: calls.append((url, kw)) or {})
    text, err = call(ct.email_archive, ids=["aaa1", "bbb2", "aaa1"])
    assert not err and "archived 2" in text and "bbb2 (not in" in text
    assert [u.rsplit("/", 2)[1] for u, _ in calls] == ["aaa1", "bbb2"]
    assert all(kw["body"]["removeLabelIds"] == ["INBOX", "UNREAD"]
               for _, kw in calls)
    assert email_routes.get_queue()["count"] == 0


def test_email_archive_reports_gmail_failures_per_id(dbfile, monkeypatch):
    from fastapi import HTTPException

    from app.routes import email as email_routes

    def boom(url, token, **kw):
        raise HTTPException(502, "gmail unreachable")
    monkeypatch.setattr(email_routes, "access_token", lambda: "tok")
    monkeypatch.setattr(email_routes, "api_call", boom)
    text, err = call(ct.email_archive, ids="ccc3")
    assert err and "ccc3: gmail unreachable" in text


# ---------- schemas ----------
#
# The SDK's shorthand ({"name": Annotated[str, ...]}) marks every key required.
# Session 5719a6e8 shows the cost: `provider` without a metric was refused
# outright, and the retry filled in every blank it could — metric="" plus
# brief=false, which replaced a one-line-per-item read with a week of raw JSON.

def test_optional_parameters_are_actually_optional():
    for t, required in ((ct.provider, {"source", "range"}),
                        (ct.db_query, {"sql"}),
                        (ct.task_create, {"title"}),
                        (ct.contact_update, {"contact_id"})):
        assert set(t.input_schema["required"]) == required, t.name


def test_no_tool_requires_every_parameter():
    """The shape the shorthand produces, named so a tool that regresses to it
    fails here rather than in a session."""
    # Every field is the key: trips' composite primary key, and a delete
    # shouldn't guess at a missing part of it (FLZUNL/2308 has two days).
    every_field_is_the_key = {"trip_delete"}
    # A message with no recipient, or a recipient with no message, is nothing.
    every_field_is_the_key |= {"message_tab"}
    for t in ct.TOOLS:
        if t.name in every_field_is_the_key:
            continue
        props, required = t.input_schema["properties"], t.input_schema["required"]
        assert not (len(props) > 1 and set(required) == set(props)), t.name


def test_every_schema_is_a_full_json_schema():
    """create_sdk_mcp_server only passes a schema through untouched when it has
    both "type" and "properties"; anything else goes down the shorthand path."""
    for t in ct.TOOLS:
        s = t.input_schema
        assert s["type"] == "object" and "properties" in s, t.name
        for name, prop in s["properties"].items():
            assert prop.get("description"), f"{t.name}.{name}"


def served(name, **args):
    """Call a tool through the real SDK MCP server — schema validation and
    all, which a direct handler() call skips. Returns (text, is_error)."""
    from mcp.types import CallToolRequest, CallToolRequestParams
    srv = ct.build_server()["instance"]
    req = CallToolRequest(method="tools/call",
                          params=CallToolRequestParams(name=name, arguments=args))
    r = asyncio.run(srv.request_handlers[CallToolRequest](req)).root
    return r.content[0].text, bool(r.isError)


def test_a_refusal_reaches_the_cli_as_an_error(dbfile):
    """The SDK server doesn't read `isError`, so a refusal sent that way goes
    out as a success: a green card, and a failed write counted in the turn's
    change summary."""
    db.create_task(title="seed")
    text, err = served("db_query", sql="SELECT * FROM no_such_table")
    assert err and text.startswith("error: no such table")
    text, err = served("db_query", sql="SELECT title FROM tasks")
    assert not err and "seed" in text


def test_every_schema_is_closed_to_unknown_keys():
    for t in ct.TOOLS:
        assert t.input_schema["additionalProperties"] is False, t.name


def test_a_misnamed_argument_is_refused_by_name(monkeypatch):
    """`brain_read {"slug": "school"}` (the param is `page`) used to reach the
    handler with the slug silently dropped; it answered "pass a page slug or a
    query" and Kestrel told the user it didn't know their USC ID while that
    fact was active. The refusal has to name the stray key, and the handler
    must not run on what's left."""
    ran = []
    monkeypatch.setattr(ct.db, "session", lambda: ran.append(1))
    text, err = served("brain_read", slug="school")
    assert err and "'slug' was unexpected" in text
    assert not ran
    text, err = served("task_update", task_id=3, due_time="17:00")
    assert err and "'due_time' was unexpected" in text


def test_brain_read_with_no_arguments_names_its_parameters():
    text, err = call(ct.brain_read)
    assert err and "`page`" in text and "`query`" in text


def test_email_body_takes_id_past_the_schema(monkeypatch):
    """15 blind calls sent `{"id": ...}`; the handler took it, the schema's
    required msg_id refused it first."""
    from app.providers import email as email_provider
    seen = []

    def fake_body(msg_id, chars):
        seen.append(msg_id)
        raise ValueError("stop here")

    monkeypatch.setattr(email_provider, "body", fake_body)
    text, err = served("email_body", id="1a00ad13fcc2d430")
    assert seen == ["1a00ad13fcc2d430"] and "stop here" in text
    text, err = served("email_body", thread_id="1a004433")
    assert err and "'thread_id' was unexpected" in text


def test_email_body_reads_by_the_ref_an_email_line_ends_in(dbfile, monkeypatch):
    """A `provider source=email` line carries its ref and no id, so the ref is
    what the model passes; read only by msg_id it comes back "no cached
    message 'e12f01'" for an email that was cached all along."""
    from app import citations
    from app.providers import email as email_provider
    with db.session() as con, con:
        con.execute("INSERT INTO emails (msg_id, thread_id, day, received_at,"
                    " from_name, from_addr, subject, refreshed_at)"
                    " VALUES (?,?,?,?,?,?,?,?)",
                    ("1a1e3d5c12e66039", "t1", "2026-11-17", "2026-11-17T09:00",
                     "Rajdeep", "r@x.com", "August rent", "2026-11-17T09:05"))
    email_ref = citations.remember("email", "1a1e3d5c12e66039",
                                   "Rajdeep: August rent", "2026-11-17")
    text_ref = citations.remember("message", "6D32BCC1", "Ortho: appointment "
                                  "on Thu Nov 19", "2026-11-17")
    seen = []

    def fake_body(msg_id, chars):
        seen.append(msg_id)
        raise ValueError("stop here")

    monkeypatch.setattr(email_provider, "body", fake_body)
    text, err = call(ct.email_body, msg_id=f"[{email_ref}]")
    assert seen == ["1a1e3d5c12e66039"] and "stop here" in text
    text, err = call(ct.email_body, msg_id=text_ref.upper())
    assert err and "a text message, not an email: Ortho" in text
    text, err = call(ct.email_body, msg_id="e00000")
    assert err and "no read handed out the ref" in text
    assert seen == ["1a1e3d5c12e66039"]


def test_provider_defaults_hold_when_the_optionals_are_omitted(monkeypatch):
    """What the model gets for the call it could not previously make."""
    seen = {}

    def fake_get(source, rng, metric, query):
        seen.update(source=source, rng=rng, metric=metric, query=query)
        return {"range": rng, "count": 0, "data": []}

    monkeypatch.setattr(ct, "provider_get", fake_get)
    text, err = call(ct.provider, source="messages", range="this-week")
    assert not err
    assert seen == {"source": "messages", "rng": "this-week",
                    "metric": None, "query": None}
    # brief stayed on: the one-line header, not the indented JSON dump the
    # session's brief=false recovery produced.
    assert text.startswith("messages this-week") and "{" not in text


# ---------- contact_find / contact_update / contact_create ----------
#
# Session 5719a6e8 was asked to update a contact's phone number and said there
# was no write path. app/contacts.py had landed thirteen minutes earlier; the
# console just had no tool for it, and told the user to go type it into
# Contacts.app by hand.

@pytest.fixture
def contacts_stub(monkeypatch):
    """The card store, without osascript or a real AddressBook."""
    from app import contacts

    calls = []
    card = {"id": "CARD-1:ABPerson", "name": "Nadia Fenwick",
            "identifiers": ["2025550178"]}

    def fake_update(contact_id, **kw):
        calls.append(("update", contact_id, kw))
        return {"id": contact_id, "before": card, "after": card,
                "changed": ["+phone 2025550142"], "applied": True}

    def fake_create(**kw):
        calls.append(("create", kw))
        return {"id": "NEW-1:ABPerson", "source": "iCloud",
                "name": "Nadia Fenwick", "phones": kw.get("phones") or [],
                "emails": []}

    monkeypatch.setattr(contacts, "update", fake_update)
    monkeypatch.setattr(contacts, "create", fake_create)
    monkeypatch.setattr(contacts, "find",
                        lambda q: [card] if "nadi" in q.lower() else [])
    monkeypatch.setattr(ct, "_resync_identities", lambda: None)
    return calls


def test_contact_find_gives_the_id_an_edit_needs(contacts_stub):
    text, err = call(ct.contact_find, query="Nadia")
    assert not err
    assert "CARD-1:ABPerson" in text and "2025550178" in text


def test_contact_find_says_so_rather_than_guessing(contacts_stub):
    text, err = call(ct.contact_find, query="Nobody")
    assert not err and "no Contacts card matches" in text


def test_contact_update_adds_the_number_to_the_real_card(contacts_stub):
    text, err = call(ct.contact_update, contact_id="CARD-1:ABPerson",
                     add_phones=["+1 (202) 555-0142"])
    assert not err
    _, cid, kw = contacts_stub[0]
    assert cid == "CARD-1:ABPerson"
    assert kw["add_phones"] == ["+1 (202) 555-0142"]
    assert "iCloud" in text


def test_contact_update_leaves_unnamed_fields_alone(contacts_stub):
    """contacts.update() reads a field it is *given* as "" as "clear this".
    Under the old all-required schema the model had to send every field on
    every call, so adding a phone number would have blanked the name, the org
    and the note — and synced that to the phone."""
    call(ct.contact_update, contact_id="CARD-1:ABPerson",
         add_phones=["2025550142"])
    _, _, kw = contacts_stub[0]
    assert "first" not in kw and "last" not in kw
    assert "org" not in kw and "note" not in kw


def test_contact_update_still_lets_a_field_be_cleared(contacts_stub):
    call(ct.contact_update, contact_id="CARD-1:ABPerson", org="")
    _, _, kw = contacts_stub[0]
    assert kw["org"] == ""


def test_contact_update_takes_a_bare_string_for_one_number(contacts_stub):
    call(ct.contact_update, contact_id="CARD-1:ABPerson",
         add_phones="2025550142")
    _, _, kw = contacts_stub[0]
    assert kw["add_phones"] == ["2025550142"]


def test_contact_update_refuses_a_no_op(contacts_stub):
    text, err = call(ct.contact_update, contact_id="CARD-1:ABPerson")
    assert err and "nothing to change" in text
    assert contacts_stub == []


def test_contact_update_needs_a_card_id(contacts_stub):
    text, err = call(ct.contact_update, add_phones=["2025550142"])
    assert err and "contact_find" in text


def test_a_duplicate_number_points_at_the_existing_card(contacts_stub,
                                                        monkeypatch):
    """Two cards sharing a number splits one person's history in the CRM."""
    from app import contacts

    def boom(**kw):
        raise contacts.DuplicateContact([{"name": "Rowan Tate"}])

    monkeypatch.setattr(contacts, "create", boom)
    text, err = call(ct.contact_create, first="Nadia", last="Fenwick",
                     phones=["2025550133"])
    assert err
    assert "Rowan Tate" in text and "contact_update" in text


def test_a_missing_automation_permission_says_which_prompt(contacts_stub,
                                                           monkeypatch):
    """The osascript failure is opaque, and the console's fallback advice
    ("update it in Contacts yourself") is wrong for a permission problem."""
    from app import contacts

    def boom(contact_id, **kw):
        raise contacts.ContactError("execution error: Not authorized (-1743)")

    monkeypatch.setattr(contacts, "update", boom)
    text, err = call(ct.contact_update, contact_id="CARD-1:ABPerson",
                     add_phones=["2025550142"])
    assert err and "Automation" in text


def test_contact_create_names_where_the_card_landed(contacts_stub):
    """`make new person` lands in whichever account Contacts calls default,
    and only some of the five reach the phone."""
    text, err = call(ct.contact_create, first="Nadia", last="Fenwick",
                     phones=["2025550142"])
    assert not err
    assert "iCloud" in text and "Nadia Fenwick" in text


def test_contact_update_takes_a_number_that_arrived_as_a_number(contacts_stub):
    """A model that emits 2025550142 unquoted used to reach `for v in value`
    on an int and raise out of the tool."""
    call(ct.contact_update, contact_id="CARD-1:ABPerson", add_phones=2025550142)
    _, _, kw = contacts_stub[0]
    assert kw["add_phones"] == ["2025550142"]


# ---------- message_files / file_read ----------

VCARD = ("BEGIN:VCARD\r\nVERSION:3.0\r\nN:Pryor;Linus;;;\r\n"
         "FN:Linus Pryor\r\nTEL;type=pref:+17085550199\r\nEND:VCARD\r\n")


@pytest.fixture
def thread(dbfile, tmp_path):
    """The exact shape session 18517f8d could not read: a typed message
    carrying a contact card, a second card whose message text is nothing but
    the "(contact card)" placeholder, and a tapback in between."""
    card = tmp_path / "Linus Pryor.vcf"
    card.write_text(VCARD)
    with db.session() as con:
        with con:
            con.executemany(
                "INSERT INTO messages (guid, day, ts, chat_guid, handle,"
                " contact_name, is_from_me, text, refreshed_at)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                [("M1", "2026-09-01", "2026-09-01T20:16:44-07:00",
                  "any;-;+15055550123", "5055550123", "Omar", 0,
                  "Here's the club president", "2026-09-01T21:00:00"),
                 ("M2", "2026-09-01", "2026-09-01T20:17:03-07:00",
                  "any;-;+15055550123", "5055550123", "Omar", 0,
                  "(contact card)", "2026-09-01T21:00:00"),
                 ("M3", "2026-09-01", "2026-09-01T20:17:30-07:00",
                  "any;-;+15055550123", "5055550123", "Omar", 0,
                  "Liked a message", "2026-09-01T21:00:00")])
            con.executemany(
                "INSERT INTO attachments (id, source, source_ref, ord, day,"
                " filename, mime, size_bytes, locator, is_sticker, seen_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                [(800, "message", "M1", 0, "2026-09-01", "Linus Pryor.vcf",
                  "text/vcard", card.stat().st_size, str(card), 0,
                  "2026-09-01T21:00:00"),
                 # iCloud-offloaded: the row exists, the bytes do not.
                 (801, "message", "M2", 0, "2026-09-01", "Reese Morgan.vcf",
                  "text/vcard", 151, None, 0, "2026-09-01T21:00:00"),
                 (802, "message", "M3", 0, "2026-09-01", "tapback.png",
                  "image/png", 900, "/nope.png", 1, "2026-09-01T21:00:00"),
                 (803, "message", "M3", 1, "2026-09-01", None,
                  "dyn.age81a5dzq7y066dbtf0g82peqf4hk2pdrb00n5xy", 40,
                  "/nope.bin", 0, "2026-09-01T21:00:00")])
    return card


def test_message_files_names_the_card_the_placeholder_hid(thread):
    """Session 18517f8d asked `messages` who Omar had sent, saw one row
    reading "(contact card)" and no names, and told the user the contact had
    never come through — while both vCards sat in `attachments`, named after
    the people on them. It also read "Linus Pryor" as two people."""
    text, err = call(ct.message_files, who="Omar")
    assert not err
    assert "Linus Pryor.vcf" in text and "Reese Morgan.vcf" in text
    assert "#800" in text
    # The typed message that says *who* the card is lives on a different row
    # than the file, so the listing has to carry it across.
    assert "club president" in text


def test_message_files_takes_a_chat_guid_straight_from_a_widget(thread):
    """`waiting`/`unanswered` widgets hand the model a chat_guid, not a name —
    if that doesn't match, the model retypes it as a person and may miss."""
    text, err = call(ct.message_files, who="any;-;+15055550123")
    assert not err and "2 file(s)" in text


def test_message_files_matches_underscores_literally(thread):
    """`_` is a LIKE wildcard: unescaped, a search for "Linus_Pryor" also
    matched the file named "Linus Pryor.vcf"."""
    text, _ = call(ct.message_files, who="Omar", query="Linus_Pryor")
    assert "Linus Pryor.vcf" not in text
    text, err = call(ct.message_files, who="Omar", query="Linus Pryor")
    assert not err and "Linus Pryor.vcf" in text


def test_message_files_leaves_out_tapbacks_and_link_payloads(thread):
    """A tapback image and a rich-link blob are not files anyone chose to
    send, and there are enough of them to bury the ones that are."""
    text, _ = call(ct.message_files)
    assert "tapback.png" not in text and "dyn.age" not in text


def test_message_files_reports_no_match_instead_of_erroring(thread):
    text, err = call(ct.message_files, who="Nobody")
    assert not err and "no files" in text


def test_file_read_gives_up_the_number_on_the_card(thread):
    """The point of the pair: the filename names the person, this gives the
    number the user was asking for, without opening Messages."""
    text, err = call(ct.file_read, id=800)
    assert not err
    assert "+17085550199" in text
    # Fenced, because a vCard's fields are whatever the sender typed.
    assert "<untrusted_attachment" in text


def test_file_read_says_the_bytes_are_offloaded_rather_than_empty(thread):
    """A card iCloud never downloaded reads as no text at all; saying "there's
    nothing in it" would be the same wrong answer in a new place."""
    text, err = call(ct.file_read, id=801)
    assert not err and "iCloud" in text


def test_message_files_inlines_a_short_card_it_has_already_read(thread):
    """Extraction is cached, so once a 150-byte vCard has been read there is
    no reason to spend a second call re-fetching it."""
    call(ct.file_read, id=800)
    text, err = call(ct.message_files, who="Omar")
    assert not err and "+17085550199" in text


def test_file_read_names_the_tool_that_hands_out_ids(thread):
    text, err = call(ct.file_read, id=9999)
    assert err and "message_files" in text


# ---------- blocking work stays off the event loop ----------

def test_slow_reads_and_writes_run_off_the_event_loop(dbfile, monkeypatch):
    """These handlers run on uvicorn's loop. A provider backfill or a 30s
    osascript called inline froze every other request and the console's own
    websocket for as long as it took."""
    import threading

    from app import contacts

    main = threading.current_thread()
    seen = {}

    def fake_get(source, rng, metric, query):
        seen["provider"] = threading.current_thread()
        return {"range": rng, "count": 0, "data": []}

    def fake_update(contact_id, **kw):
        seen["contacts"] = threading.current_thread()
        card = {"name": "Ada", "identifiers": []}
        return {"before": card, "after": card, "changed": ["note"]}

    monkeypatch.setattr(ct, "provider_get", fake_get)
    monkeypatch.setattr(contacts, "update", fake_update)
    monkeypatch.setattr(ct, "_resync_identities",
                        lambda: seen.setdefault("resync",
                                                threading.current_thread()))
    call(ct.provider, source="weather", range="today")
    call(ct.contact_update, contact_id="A:ABPerson", note="x")
    assert set(seen) == {"provider", "contacts", "resync"}
    assert all(t is not main for t in seen.values())


def test_a_failed_calendar_write_keeps_its_own_wording(monkeypatch):
    from app.routes import calendar as calendar_routes

    def boom(**kw):
        raise RuntimeError("Google said 503")

    monkeypatch.setattr(calendar_routes, "delete_event", boom)
    text, err = call(ct.event_delete, match="Dentist")
    assert err and text == "error: couldn't delete the event: Google said 503"


def test_a_bad_repeat_count_is_an_error_not_a_crash():
    """_as_int refuses with an HTTPException while the call's arguments are
    being built; that has to come back as a tool error like any refusal."""
    text, err = call(ct.event_create, summary="Gym", start="2026-08-20T07:00",
                     repeat="weekly", repeat_count="six")
    assert err and "whole number" in text
