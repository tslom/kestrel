"""What the change classifier records, and what it refuses to do unattended.

Two things are being pinned here. The first is coverage: a write tool that
`classify()` doesn't know is a change no summary mentions, and the registry
loop that adds one to `console_tools.WRITE_TOOLS` and forgets this table is
the bug this file exists to fail on.

The second is the guard list — which calls still stop for a click in auto
mode. Those cases are written as the sentences they defend: sending mail is
not undoable, `rm` does not move anything to the trash, a DELETE is not an
edit. Anything else is allowed to run unattended on purpose, so the tests for
the unguarded side matter as much: a guard list that grows by accident turns
auto mode back into the thing it replaced.
"""

import re
from pathlib import Path

import pytest

from app import console_changes, console_tools
from app.console_changes import classify


# ---------- coverage ----------


def test_every_write_tool_is_a_change():
    """Each tool on console_tools.WRITE_TOOLS records something — except
    file_read, which is on that list for its cost, not for writing."""
    missed = []
    for spec in console_tools.WRITE_TOOLS:
        name = f"mcp__{console_tools.SERVER_NAME}__{spec.name}"
        change = classify(name, {})
        if spec.name == "file_read":
            assert change is None, "file_read reads; it is not a change"
            continue
        if change is None:
            missed.append(spec.name)
    assert not missed, (
        "these write tools would make changes no summary mentions — add them "
        f"to console_changes._VAULT: {missed}")


def test_read_tools_are_not_changes():
    """Except theme_set, which is on that list for needing no card (colour
    roles only, undone from the Look lab), and still changes the window."""
    for spec in console_tools.READ_TOOLS:
        name = f"mcp__{console_tools.SERVER_NAME}__{spec.name}"
        if spec.name == "theme_set":
            change = classify(name, {})
            assert (change.verb, change.scope, change.guard) == ("edited", "look", None)
            assert console_changes.refresh_topics(change, {}) == ("looks",)
            continue
        assert classify(name, {}) is None, f"{spec.name} only reads"


@pytest.mark.parametrize("tool", ["Read", "Grep", "Glob", "WebSearch",
                                  "WebFetch", "ToolSearch", "ExitPlanMode",
                                  "AskUserQuestion"])
def test_builtin_reads_are_not_changes(tool):
    assert classify(tool, {"pattern": "x"}) is None


# ---------- files ----------


def test_write_says_whether_it_replaced_something(tmp_path):
    existing = tmp_path / "note.md"
    existing.write_text("already here", encoding="utf-8")
    assert classify("Write", {"file_path": str(existing)}).verb == "rewrote"
    assert classify("Write", {"file_path": str(tmp_path / "new.md")}).verb \
        == "created"


def test_a_file_edit_never_asks():
    """The vault is on disk and in the log; editing it is what auto mode is
    for. If this ever gains a guard, auto mode is worth nothing."""
    assert classify("Edit", {"file_path": "/x/y.md"}).guard is None
    assert classify("Write", {"file_path": "/x/y.md"}).guard is None


# ---------- mail and calendar ----------


def test_drafting_is_unattended_but_sending_is_not():
    assert classify("mcp__claude_ai_Gmail__create_draft", {}).guard is None
    assert classify("mcp__vault__email_draft", {}).guard is None
    for tool in ("send_message", "reply", "forward"):
        change = classify(f"mcp__claude_ai_Gmail__{tool}", {})
        assert change.verb == "sent"
        assert change.guard, f"{tool} puts mail in someone else's inbox"


def test_trashing_mail_asks_and_labelling_it_does_not():
    assert classify("mcp__claude_ai_Gmail__trash_thread", {}).guard
    assert classify("mcp__claude_ai_Gmail__label_thread", {}).guard is None
    # `untrash` contains `trash`; it is the undo, not the delete.
    untrash = classify("mcp__claude_ai_Gmail__untrash_message", {})
    assert untrash is not None and untrash.guard is None


def test_deleting_an_event_asks_but_moving_one_does_not():
    for tool in ("mcp__claude_ai_Google_Calendar__delete_event",
                 "mcp__vault__event_delete"):
        change = classify(tool, {})
        assert change.verb == "deleted" and change.guard

    for tool in ("mcp__claude_ai_Google_Calendar__update_event",
                 "mcp__vault__event_update", "mcp__vault__event_create"):
        assert classify(tool, {}).guard is None


# ---------- tools we don't own ----------


def test_an_unknown_connector_tool_is_read_by_its_name():
    delete = classify("mcp__claude_ai_Notion__delete_page", {})
    assert delete is not None and delete.guard, \
        "a tool we've never seen, named for removing something, asks"
    assert delete.scope == "Notion"

    write = classify("mcp__claude_ai_Notion__update_page", {})
    assert write is not None and write.guard is None

    assert classify("mcp__claude_ai_Notion__search_pages", {}) is None
    assert classify("mcp__claude_ai_Notion__get_page", {}) is None


# ---------- shell ----------


@pytest.mark.parametrize("command", [
    "ls -la 06-Generated",
    "cat notes/today.md",
    "git status",
    "git log --oneline -20",
    "grep -rn kestrel . | head -40",
    "wc -l app/console.py",
    'sqlite3 app.db "PRAGMA busy_timeout=5000; SELECT count(*) FROM tasks"',
    "curl -s http://localhost:8788/api/tasks",
])
def test_reads_stay_out_of_the_summary(command):
    assert classify("Bash", {"command": command}) is None


@pytest.mark.parametrize("command,why", [
    ("rm -rf 06-Generated/old", "rm deletes with no trash"),
    ("cd /tmp && rm -rf x", "the destructive word is not the first one"),
    ("git push origin main", "published history isn't ours to undo"),
    ("git reset --hard HEAD~1", "uncommitted work is gone"),
    ("osascript -e 'tell application \"Messages\" to send \"hi\"'",
     "this is how the box sends a message"),
    ("sudo launchctl bootout gui/501/kestrel", "root, and a service"),
    ('sqlite3 app.db "DELETE FROM tasks WHERE id=3"', "no undo on a delete"),
    ("curl -X POST https://api.example.com/things -d @-",
     "a write on someone else's machine"),
])
def test_these_still_ask_in_auto_mode(command, why):
    change = classify("Bash", {"command": command})
    assert change is not None, why
    assert change.guard, why


@pytest.mark.parametrize("command", [
    "npm run build",
    "python -m pytest -q",
    "mkdir -p 06-Generated/2026",
    'sqlite3 app.db "INSERT INTO tasks (title) VALUES (\'x\')"',
    "curl -X POST http://localhost:8788/api/tasks -d '{}'",
])
def test_writes_are_recorded_and_still_run_unattended(command):
    change = classify("Bash", {"command": command})
    assert change is not None, "a shell write belongs in the summary"
    assert change.guard is None, "local and reversible: no card"


def test_a_db_write_reads_as_db_not_shell():
    change = classify("Bash", {"command":
                               'sqlite3 app.db "UPDATE tasks SET done=1"'})
    assert change.scope == "db" and change.verb == "edited"


def test_an_unparseable_command_asks():
    """shlex can't read it, so neither can the guard scan. One card beats one
    unattended unknown."""
    change = classify("Bash", {"command": 'echo "unbalanced'})
    assert change is not None and change.guard


def test_an_environment_assignment_is_not_the_program():
    """`VAULT_PATH=/abs uvicorn …` runs uvicorn, not a program named `abs`."""
    change = classify("Bash", {"command": "VAULT_PATH=/tmp/v uvicorn app:x"})
    assert change is not None and change.guard is None


# ---------- the record itself ----------


def test_a_change_serializes_to_the_four_columns_the_card_reads():
    change = classify("mcp__vault__task_create", {"title": "pay tuition"})
    assert change.as_dict() == {
        "verb": "created", "scope": "task",
        "detail": "pay tuition", "tool": "mcp__vault__task_create",
    }
    assert "guard" not in change.as_dict(), \
        "the guard is a reason shown on a card, not part of the summary row"


def test_guard_reasons_are_sentences():
    """Each one is rendered as 'auto mode paused — it would <guard>', so it
    has to read as the tail of that sentence: lower case, no full stop, and
    no dash of its own — a second one inside the card's reads as a stutter,
    which is what 'delete files — nothing here moves them' did on screen."""
    guards = [g for table in (console_changes._VAULT, console_changes._GMAIL,
                              console_changes._CALENDAR)
              for row in table.values() if row for g in [row[2]] if g]
    guards += list(console_changes._SHELL_GUARDS.values())
    guards += list(console_changes._GIT_GUARDS.values())
    assert guards
    for guard in guards:
        assert guard[0].islower(), guard
        assert not guard.endswith("."), guard
        assert "—" not in guard, guard


def test_scheduling_a_send_cards_even_in_auto_mode():
    """email_schedule is the one vault tool that makes mail leave with no
    further human look, so its approval is the last check it gets."""
    from app import console_changes
    c = console_changes.classify("mcp__vault__email_schedule",
                                 {"send_at": "2026-10-24T08:00", "body": "x"})
    assert c is not None and c.guard
    assert console_changes.classify("mcp__vault__email_unschedule",
                                    {"id": 1}).guard is None


# ---------- which cards a change moves ----------


def test_every_scope_says_which_cards_it_moves():
    """A tool in the tables with no row in _CARD_TOPICS is a write whose card
    stays stale until the next poll, with nothing to say why."""
    scopes = {row[1] for row in console_changes._TABLE.values() if row}
    missed = scopes - console_changes._CARD_TOPICS.keys()
    assert not missed, f"add these to console_changes._CARD_TOPICS: {missed}"


def test_every_topic_has_something_listening():
    """The two sides share only a string, so a typo on either is a refresh
    that quietly goes nowhere. A source's sheet subscribes under its own name."""
    src = Path(__file__).parents[2] / "frontend" / "src"
    code = "\n".join(p.read_text() for p in src.rglob("*.js*"))
    for topic in console_changes._EVERY_CARD:
        assert re.search(rf'"{topic}"\]?\)|TOPIC = "{topic}"', code), \
            f"nothing in frontend/src subscribes to {topic!r}"


@pytest.mark.parametrize("tool,topics", [
    ("mcp__vault__task_update", ("tasks",)),
    ("mcp__vault__beer_add", ("debts",)),
    ("mcp__vault__package_update", ("travel",)),
    ("mcp__vault__food_close", ("food", "habits")),
    ("mcp__vault__event_create", ("calendar",)),
    ("mcp__claude_ai_Google_Calendar__update_event", ("calendar",)),
    ("mcp__claude_ai_Gmail__trash_thread", ("email",)),
    ("mcp__claude_ai_Gmail__snooze_thread_set", ("email",)),   # unknown to us
    ("mcp__vault__email_draft", ()),
])
def test_a_tool_moves_the_card_that_shows_it(tool, topics):
    change = classify(tool, {})
    assert console_changes.refresh_topics(change, {}) == topics


def test_only_a_note_in_the_vault_moves_a_card(tmp_path, monkeypatch):
    monkeypatch.setattr(console_changes, "VAULT_PATH", tmp_path)
    inside = {"file_path": str(tmp_path / "06-Generated" / "x.md")}
    outside = {"file_path": "/tmp/elsewhere/x.md"}
    assert "generated" in console_changes.refresh_topics(classify("Write", inside), inside)
    assert console_changes.refresh_topics(classify("Write", outside), outside) == ()


def test_a_script_could_have_touched_anything_but_not_in_a_dev_tab():
    run = classify("Bash", {"command": "python scripts/fix.py"})
    assert console_changes.refresh_topics(run, {}) == console_changes._EVERY_CARD
    assert console_changes.refresh_topics(run, {}, dev=True) == ()


def test_a_dev_tab_moves_the_desk_only_through_the_desks_own_data():
    sql = classify("Bash", {"command": "sqlite3 backend/data/app.db 'UPDATE tasks SET x=1'"})
    api = classify("Bash", {"command": "curl -X POST localhost:8788/api/list-items/30/done"})
    other = classify("Bash", {"command": "curl -X POST localhost:9000/x"})
    assert console_changes.refresh_topics(sql, {}, dev=True) == console_changes._EVERY_CARD
    assert console_changes.refresh_topics(api, {}, dev=True) == console_changes._EVERY_CARD
    assert console_changes.refresh_topics(other, {}, dev=True) == ()


def test_topics_stay_out_of_the_summary_row():
    change = classify("mcp__vault__task_create", {"title": "x"})
    assert "topics" not in change.as_dict()
