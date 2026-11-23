"""What a tool call changed, and what auto mode may do without being asked.

Two questions share one table because they turn on the same fact: whether a
call writes, and whether what it wrote to is somewhere you can take it back
from.

`classify()` answers the first. Every tool call the session makes is run past
it and comes back either None — a read, which is most of them — or the row the
turn's change summary prints. The ledger is built from the *tool stream*
rather than from the approval path, which is what makes it mode-independent
and honest: a call that was denied, or that failed, never gets a result block,
so it never reaches the summary. Nothing here is asked of the model, either;
a summary the model wrote would be a claim about what it did, and this is
supposed to be the record you check that claim against.

`Change.guard` answers the second. Auto mode runs the whole turn unattended,
so the summary it produces arrives after the fact — and "you sent that email
an hour ago" is not a review. A guarded call still stops at a card even in
auto mode. The test is deliberately narrow: not "could this lose something"
(every write could) but "does this leave the machine, or leave nothing behind
to undo it with". A vault note is on disk and in the log; a task row can be
edited back; a sent mail, a trashed thread and a deleted calendar event are
gone. Roughly: guarding costs one click a week, and not guarding costs an
apology to someone who isn't you.

The tables are explicit rather than pattern-matched, for the reason registries
in this codebase usually are: a tool added to `console_tools.WRITE_TOOLS` and
not added here would be a write that no summary mentions. `test_console_
changes.py` fails on exactly that. The name-shaped fallback underneath is for
tools that are not ours to enumerate — the claude.ai connectors change without
telling us — and it is biased the way the rest of the file is: an unrecognized
name that *looks* destructive is treated as one.
"""

import os.path
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import sessions
from .config import VAULT_PATH
from .console_policy import (has_breakout, is_safe_read, shell_words,
                             strip_cd_prefix)
from .console_tools import SERVER_NAME

# ---------- the record ----------


@dataclass(frozen=True)
class Change:
    """One thing a turn did. `verb` and `scope` are the summary's two columns;
    `detail` is the same one-liner the tool log shows, so a row in the summary
    and the call it came from read alike."""

    verb: str            # created | edited | wrote | deleted | sent | ran
    scope: str           # file | task | email | calendar | db | shell | …
    detail: str
    tool: str
    guard: str | None = None
    topics: tuple[str, ...] = ()   # the cards it moved; see refresh_topics

    def as_dict(self) -> dict[str, Any]:
        return {"verb": self.verb, "scope": self.scope,
                "detail": self.detail, "tool": self.tool}


# ---------- the tables ----------

# (verb, scope, guard). A guard is a sentence, because it is shown on the card
# that interrupts auto mode: the user is being asked to approve one specific
# thing, and "auto mode paused" alone doesn't say what for. It renders as
# "auto mode paused — it would <guard>", so each one is written to finish that
# sentence and carries no dash of its own, which would read as a second break
# inside the first.

_BUILTIN: dict[str, tuple[str, str, str | None]] = {
    # Write's verb is decided per call — see _file_change. It is listed here so
    # the membership test finds it; the tuple's verb is the fallback.
    "Write": ("wrote", "file", None),
    "Edit": ("edited", "file", None),
    "NotebookEdit": ("edited", "notebook", None),
}

# The vault MCP server: the dashboard's own data, all of it in app.db or the
# vault, all of it editable from the dashboard afterwards. Two exceptions
# reach outside and are guarded.
_VAULT: dict[str, tuple[str, str, str | None]] = {
    "task_create": ("created", "task", None),
    "task_update": ("edited", "task", None),
    "list_create": ("created", "list", None),
    "list_add": ("created", "list item", None),
    "list_item_done": ("edited", "list item", None),
    "debt_add": ("created", "debt", None),
    "debt_settle": ("edited", "debt", None),
    "debt_update": ("edited", "debt", None),
    "debt_remove": ("deleted", "debt", None),
    "beer_add": ("created", "beer", None),
    "beer_settle": ("edited", "beer", None),
    "watchlist_add": ("created", "watchlist item", None),
    "watchlist_watched": ("edited", "watchlist item", None),
    "expense_add": ("created", "expense", None),
    "expense_update": ("edited", "expense", None),
    "expense_remove": ("deleted", "expense", None),
    "expense_paid": ("edited", "expense batch", None),
    "habit_add": ("created", "habit", None),
    "habit_checkin": ("created", "habit check-in", None),
    "package_add": ("created", "package", None),
    "package_update": ("edited", "package", None),
    "package_delete": ("deleted", "package", None),
    "trip_add": ("created", "trip", None),
    "trip_delete": ("deleted", "trip", None),
    "contact_create": ("created", "contact", None),
    # The one write in this table that edits something a *person* owns: it
    # overwrites a card in the macOS address book, which syncs, and the old
    # number is not kept anywhere once it does.
    "contact_update": ("edited", "contact",
                       "overwrite a contact card; it syncs to iCloud and "
                       "the value it replaces isn't kept"),
    # A draft is the reviewable artifact, not the irreversible act. Drafting
    # unattended is most of what auto mode is for.
    "email_draft": ("created", "email draft", None),
    # Archiving moves mail, it doesn't lose it: the card's undo puts it back.
    "email_archive": ("edited", "email", None),
    # The other way round from email_draft: once approved, nobody presses
    # Send — the mail leaves at send_at with no further look. The card is
    # the last human check it gets, so it cards even in auto mode.
    "email_schedule": ("created", "scheduled email",
                       "schedule mail that sends by itself at the time given, "
                       "with no further check before it leaves"),
    "email_unschedule": ("edited", "scheduled email", None),
    "event_create": ("created", "calendar event", None),
    "event_update": ("edited", "calendar event", None),
    "event_delete": ("deleted", "calendar event",
                     "delete a real Google Calendar event; there is no undo "
                     "and guests are notified"),
    # An RSVP does mail the organizer, but it can be sent again with a
    # different answer, so it stays on the unattended side of the line.
    "event_rsvp": ("edited", "calendar event", None),
    "course_set": ("edited", "course", None),
    "course_hours": ("edited", "course", None),
    "course_except": ("edited", "course", None),
    # Appends to a vault note the user can edit or delete in Obsidian.
    "course_review_add": ("created", "review item", None),
    "course_review_mark": ("edited", "review item", None),
    # A fact is reviewable afterwards in the Brain card, and a ✗ undoes it.
    "brain_note": ("created", "brain fact", None),
    "brain_verdict": ("edited", "brain fact", None),
    "brain_page": ("edited", "brain page", None),
    # §25 food: all app.db, all editable or deletable from the food sheet.
    "food_log": ("created", "food entry", None),
    "food_entry": ("edited", "food entry", None),
    "food_create": ("created", "food", None),
    "food_fix": ("edited", "food", None),
    "food_close": ("edited", "food day", None),
    "food_weigh": ("created", "weigh-in", None),
    "food_profile": ("edited", "food profile", None),
    # file_read is in WRITE_TOOLS because the vision path costs money, not
    # because it writes. It reads; it is not a change.
    "file_read": None,
    # On READ_TOOLS (no card: colour roles only, checked, undone from the
    # Look lab), but it does change what the window shows.
    "theme_set": ("edited", "look", None),
}

_GMAIL: dict[str, tuple[str, str, str | None]] = {
    "create_draft": ("created", "email draft", None),
    "update_draft": ("edited", "email draft", None),
    "send_message": ("sent", "email",
                     "send mail, which leaves for its recipient immediately"),
    "reply": ("sent", "email",
              "send a reply, which leaves for its recipient immediately"),
    "forward": ("sent", "email",
                "forward a thread, and everything quoted in it goes too"),
    "trash_message": ("deleted", "email",
                      "trash a message; Gmail empties the trash on its own "
                      "after 30 days"),
    "trash_thread": ("deleted", "email thread",
                     "trash a whole thread; Gmail empties the trash on its "
                     "own after 30 days"),
    "mark_message_spam": ("deleted", "email",
                          "report spam, which moves the mail and teaches the "
                          "filter about the sender"),
    "mark_thread_spam": ("deleted", "email thread",
                         "report spam, which moves the thread and teaches the "
                         "filter about the sender"),
    "delete_label": ("deleted", "email label",
                     "delete a label, which comes off every thread that had it"),
    # Reversible by their own opposites, all of them.
    "untrash_message": ("edited", "email", None),
    "untrash_thread": ("edited", "email thread", None),
    "unmark_message_spam": ("edited", "email", None),
    "unmark_thread_spam": ("edited", "email thread", None),
    "label_message": ("edited", "email", None),
    "label_thread": ("edited", "email thread", None),
    "unlabel_message": ("edited", "email", None),
    "unlabel_thread": ("edited", "email thread", None),
    "update_message_labels": ("edited", "email", None),
    "apply_sensitive_message_label": ("edited", "email", None),
    "apply_sensitive_thread_label": ("edited", "email thread", None),
    "create_label": ("created", "email label", None),
    "update_label": ("edited", "email label", None),
}

_CALENDAR: dict[str, tuple[str, str, str | None]] = {
    "create_event": ("created", "calendar event", None),
    "update_event": ("edited", "calendar event", None),
    "delete_event": ("deleted", "calendar event",
                     "delete a real Google Calendar event; there is no undo "
                     "and guests are notified"),
    "respond_to_event": ("edited", "calendar event", None),
}

_TABLE: dict[str, tuple[str, str, str | None] | None] = {
    **_BUILTIN,
    **{f"mcp__{SERVER_NAME}__{k}": v for k, v in _VAULT.items()},
    **{f"mcp__claude_ai_Gmail__{k}": v for k, v in _GMAIL.items()},
    **{f"mcp__claude_ai_Google_Calendar__{k}": v
       for k, v in _CALENDAR.items()},
}

# ---------- which cards a change moved ----------

# The refresh bus's topics (lib/refresh.js) per scope. A change's cards reload
# when its call comes back clean, and the "needs you" list follows, since each
# card's hook builds its own asks. Topics rather than a bare broadcast, which
# would also re-read the provider-backed cards nothing here touched.
#
# Keyed on scope so a connector write and a vault tool on the same thing land
# on the same card. `test_console_changes.py` fails on a scope missing here,
# for the same reason the tables above are explicit. Empty means it was looked
# at and no card shows it: drafts and scheduled mail live in Gmail and
# email_schedule, and neither is on the queue.
_NOTE_TOPICS = ("courses", "generated", "notes")
_CARD_TOPICS: dict[str, tuple[str, ...]] = {
    "task": ("tasks",),
    "list": ("lists",),
    "list item": ("lists",),
    "debt": ("debts",),
    "beer": ("debts",),            # the debts card draws them, in its own section
    "watchlist item": ("watchlist",),
    "expense": ("expenses",),
    "expense batch": ("expenses",),
    "habit": ("habits",),
    "habit check-in": ("habits",),
    "package": ("travel",),
    "trip": ("travel",),
    "contact": ("people",),
    "email": ("email",),
    "email thread": ("email",),
    "Gmail": ("email",),           # _server_label, for a tool we've never seen
    "email draft": (),
    "scheduled email": (),
    "email label": ("email",),
    "calendar event": ("calendar",),
    "Google Calendar": ("calendar",),
    "course": ("courses",),
    "review item": ("courses",),
    "brain fact": ("brain",),
    "brain page": ("brain",),
    "food entry": ("food",),
    "food": ("food",),
    "food day": ("food", "habits"),  # closing a day ticks the food habit
    "weigh-in": ("food",),
    "food profile": ("food",),
    "file": _NOTE_TOPICS,
    "notebook": (),
}

# SQL and scripts can touch any table, and all of these are local reads.
_EVERY_CARD = tuple(sorted({t for ts in _CARD_TOPICS.values() for t in ts}))
_CARD_TOPICS.update(db=_EVERY_CARD, shell=_EVERY_CARD)
# After _EVERY_CARD on purpose: the page's colours (lib/look.js) reload when a
# theme is set, not on every shell command.
_CARD_TOPICS["look"] = ("looks",)

_OWN_API = re.compile(r"\b(localhost|127\.0\.0\.1):8788\b")


def refresh_topics(change: "Change", tool_input: Any, dev: bool = False) -> tuple[str, ...]:
    """The bus topics this change should reload. A dev tab is working on code,
    so its files and scripts say nothing about the desk: there only a call on
    app.db or the dashboard's own API counts."""
    if change.scope == "file":
        data = tool_input if isinstance(tool_input, dict) else {}
        if not _in_vault(data.get("file_path")):
            return ()
    if dev and change.scope == "shell" and not _OWN_API.search(change.detail):
        return ()
    return _CARD_TOPICS.get(change.scope, ())


def _in_vault(path: Any) -> bool:
    try:
        return Path(str(path or "")).resolve().is_relative_to(VAULT_PATH.resolve())
    except OSError:
        return False


# ---------- the fallback, for tools we don't own ----------

# Connector tool sets are versioned by someone else. A name we've never seen
# is read by its verb: destructive-looking ones are recorded *and* guarded,
# write-looking ones are recorded, and everything else is assumed to read —
# which is what get/list/search/find/query/suggest all are.
_UNKNOWN_DESTRUCTIVE = re.compile(
    r"(^|_)(delete|destroy|remove|trash|purge|send|reply|forward|revoke"
    r"|cancel|archive)(_|$)")
_UNKNOWN_WRITE = re.compile(
    r"(^|_)(create|add|new|update|edit|set|write|draft|insert|append|move"
    r"|rename|upload|post|put|patch|apply|mark|label|assign|complete|star"
    r"|schedule|invite)(_|$)")

# ---------- shell ----------

# Programs whose whole job is to read. A Bash call that is one of these, or a
# pipeline of them, is not a change and stays out of the summary. `sed` and
# `awk` are absent on purpose — both write files — and so is anything that
# starts something (`open`, `npm`, `python`), which may do anything at all.
#
# `sqlite3` and `curl` are pointedly absent, though most of their use here is
# reading: the reads among them are the ones `is_safe_read` already
# recognized and returned above, so what reaches this set is a sqlite3 that
# isn't a plain SELECT and a curl that isn't a localhost GET. Those are the
# db writes and the outbound requests the summary exists to show.
_SHELL_READS = frozenset({
    "ls", "cat", "bat", "head", "tail", "grep", "egrep", "fgrep", "rg",
    "find", "fd", "wc", "stat", "file", "du", "df", "tree", "pwd", "whoami",
    "which", "type", "date", "echo", "printf", "diff", "cmp", "jq", "sort",
    "uniq", "cut", "tr", "nl", "rev", "tac", "column", "basename", "dirname",
    "realpath", "uname", "env", "ps", "git",
})

# git subcommands that only look.
_GIT_READS = frozenset({
    "status", "log", "diff", "show", "branch", "remote", "rev-parse",
    "blame", "describe", "ls-files", "shortlog", "stash",
})

# The shell's share of the guard list. Deleting files, rewriting history and
# anything that talks to another machine — plus `osascript`, which is how this
# box sends an iMessage.
_SHELL_GUARDS: dict[str, str] = {
    "rm": "delete files, and nothing here moves them to the trash first",
    "rmdir": "delete a directory",
    "shred": "overwrite a file past recovery",
    "srm": "overwrite a file past recovery",
    "dd": "write a raw block device",
    "mkfs": "format a filesystem",
    "truncate": "truncate a file in place",
    "osascript": "drive another app by AppleScript, which is how a message "
                 "or a mail actually gets sent from this machine",
    "sendmail": "send mail",
    "mail": "send mail",
    "mailx": "send mail",
    "msmtp": "send mail",
    "ssh": "run a command on another machine",
    "scp": "copy files to or from another machine",
    "rsync": "sync files, possibly onto another machine",
    "gh": "act on GitHub, where issues, PRs and releases are public",
    "sudo": "run as root",
    "launchctl": "load or unload a background service",
    "crontab": "rewrite the crontab",
    "killall": "kill processes by name",
    "pkill": "kill processes by name",
}

_GIT_GUARDS: dict[str, str] = {
    "push": "push to the remote, and published history is not yours to undo",
    "reset": "reset the working tree, losing uncommitted work for good",
    "clean": "delete untracked files",
    "filter-branch": "rewrite history",
}

# curl/wget options that turn a fetch into a write. A plain GET of a web page
# is a read wherever it points; a POST is a write on someone else's machine.
_HTTP_WRITE_FLAGS = frozenset({
    "-X", "--request", "-d", "--data", "--data-raw", "--data-binary",
    "--data-urlencode", "-F", "--form", "-T", "--upload-file",
})

_LOCAL_HOSTS = ("http://localhost", "http://127.0.0.1")

# An INSERT can be re-edited; a DROP or a DELETE leaves nothing to edit. Only
# the second kind interrupts auto mode. (`console_policy._SQL_WRITE` is the
# wider net — it decides what may run unattended at all, and every write is on
# it. This is the subset that can't be walked back.)
_SQL_DESTRUCTIVE = re.compile(r"\b(drop|delete\s+from|truncate)\b",
                              re.IGNORECASE)

_STDERR_REDIRECTS = ("2>/dev/null", "2>&1")


def _segments(words: list[str]) -> list[list[str]]:
    """Split a token list on pipes."""
    out: list[list[str]] = [[]]
    for w in words:
        if w == "|":
            out.append([])
        else:
            out[-1].append(w)
    return [seg for seg in out if seg]


_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


def _program(seg: list[str]) -> str:
    """The program a segment runs, by basename — `/usr/bin/rm` is `rm`.

    A leading `VAR=value` is an assignment, not the program: the same trap
    `_always_rule` fell into, where `VAULT_PATH=/abs uvicorn …` got named
    after a word the command never ran.
    """
    for word in seg:
        if word.startswith("-") or _ASSIGNMENT.match(word):
            continue
        return os.path.basename(word)
    return ""


def _http_writes(seg: list[str]) -> bool:
    """A curl/wget that sends a body or names a method, anywhere but here."""
    if _program(seg) not in ("curl", "wget"):
        return False
    if not any(w.split("=", 1)[0] in _HTTP_WRITE_FLAGS for w in seg[1:]):
        return False
    urls = [w for w in seg if "://" in w]
    return not all(u.startswith(_LOCAL_HOSTS) for u in urls) or not urls


# What a dev tab in auto mode runs without a card that other tabs stop for.
# Deleting build leftovers, killing a stuck dev server, `gh` and pushing
# (CLAUDE.md: commit and push once verified) are the work there, not
# exceptions to it — a dev tab that cards each one is a tab you have to sit
# and watch, which is what picking auto was meant to avoid (10-29). Mail,
# AppleScript, other machines, sudo, launchctl and history rewrites still stop.
_DEV_WAIVED_SHELL = frozenset({"rm", "rmdir", "gh", "pkill", "killall"})
_DEV_WAIVED_GIT = frozenset({"push"})


def _shell_guard(words: list[str], dev: bool = False) -> str | None:
    """Why this command may not run unattended, or None.

    Every token is read, not just the first: `cd /tmp && rm -rf x` runs `rm`
    no matter which word it starts with, and the console writes commands in
    exactly that shape. A word that only happens to be named `rm` costs one
    card, which is the direction this file errs in everywhere.
    """
    names = {os.path.basename(w) for w in words}
    for name in sorted(names):
        if name in _SHELL_GUARDS and not (dev and name in _DEV_WAIVED_SHELL):
            return _SHELL_GUARDS[name]
    if "git" in names:
        for sub, why in _GIT_GUARDS.items():
            if sub in words and not (dev and sub in _DEV_WAIVED_GIT):
                return why
    for seg in _segments(words):
        if _http_writes(seg):
            return "send a request with a body to another machine"
    return None


def _shell_change(command: str, dev: bool = False) -> Change | None:
    """Classify one Bash call.

    Note what this is *not*: `is_safe_read` decides what may run without a
    card, and this decides what goes in the summary. They overlap but are not
    the same question — `ls` is not safe enough for the first (it isn't on the
    allowlist) and is not interesting enough for the second.
    """
    detail = " ".join(command.split())
    words = shell_words(command)
    if words is None:
        # An unparseable command is one whose contents are unknown, so it is
        # recorded as a change and it interrupts auto mode.
        return Change("ran", "shell", detail, "Bash",
                      "run a command that can't be parsed, so what it does "
                      "is unknown")
    guard = _shell_guard(words, dev)
    if guard:
        return Change("ran", "shell", detail, "Bash", guard)
    if is_safe_read(command):
        return None
    body = [w for w in strip_cd_prefix(words) if w not in _STDERR_REDIRECTS]
    segs = _segments(body)
    if segs and _program(segs[0]) == "sqlite3":
        # Past is_safe_read, so this is SQL that writes (or SQL we couldn't
        # read as a pure SELECT). It says "db", not "shell", because that is
        # what the row means to someone reading the summary.
        destructive = bool(_SQL_DESTRUCTIVE.search(command))
        return Change("edited", "db", detail, "Bash",
                      "run SQL that drops or deletes rows in app.db, and there "
                      "is no undo short of a backup" if destructive else None)
    if segs and not has_breakout([w for seg in segs for w in seg]):
        if all(_program(seg) in _SHELL_READS
               and (_program(seg) != "git" or _git_reads(seg))
               for seg in segs):
            return None
    return Change("ran", "shell", detail, "Bash")


def _git_reads(seg: list[str]) -> bool:
    args = [w for w in seg[1:] if not w.startswith("-")]
    return bool(args) and args[0] in _GIT_READS


# ---------- files ----------


def _file_change(tool: str, tool_input: dict[str, Any]) -> Change:
    """Write says whether it made the file or replaced one. The distinction is
    the whole point of a summary line — "wrote 3 notes" reads very differently
    once you know one of them already existed."""
    verb, scope, guard = _BUILTIN[tool]
    if tool == "Write":
        try:
            verb = ("rewrote" if os.path.exists(str(tool_input.get("file_path", "")))
                    else "created")
        except OSError:
            pass
    return Change(verb, scope, sessions.tool_detail(tool_input), tool, guard)


# ---------- the entry point ----------


def dev_guard(change: Change | None, tool_name: str, tool_input: Any) -> str | None:
    """`change.guard` as a dev tab reads it: the same, less `_DEV_WAIVED_*`.

    Only Bash's guards are re-read; an MCP send or delete stops in a dev tab
    exactly as it does anywhere else. The summary row is unchanged — a waived
    push still lists as a push.
    """
    if change is None or change.guard is None or tool_name != "Bash":
        return change.guard if change is not None else None
    dev = _shell_change(str((tool_input or {}).get("command", "")), dev=True)
    return dev.guard if dev is not None else None


def classify(tool_name: str, tool_input: Any) -> Change | None:
    """The change this call makes, or None if it only reads."""
    name = str(tool_name or "")
    data = tool_input if isinstance(tool_input, dict) else {}

    if name == "Bash":
        return _shell_change(str(data.get("command", "")))
    if name in _BUILTIN:
        return _file_change(name, data)

    if name in _TABLE:
        row = _TABLE[name]
        if row is None:                     # named, and named as a read
            return None
        verb, scope, guard = row
        return Change(verb, scope, sessions.tool_detail(data), name, guard)

    if not name.startswith("mcp__"):
        # Built-ins we didn't list are reads: Read, Grep, Glob, WebSearch,
        # WebFetch, ToolSearch, AskUserQuestion, ExitPlanMode.
        return None

    bare = name.rsplit("__", 1)[-1]
    if _UNKNOWN_DESTRUCTIVE.search(bare):
        return Change("deleted", _server_label(name),
                      sessions.tool_detail(data), name,
                      f"run {bare}, a tool this dashboard doesn't know, "
                      "whose name says it removes or sends something")
    if _UNKNOWN_WRITE.search(bare):
        return Change("edited", _server_label(name),
                      sessions.tool_detail(data), name)
    return None


def _server_label(name: str) -> str:
    """`mcp__claude_ai_Gmail__send_message` -> `Gmail`, for the scope column."""
    parts = name.split("__")
    server = parts[1] if len(parts) > 2 else "tool"
    return server.replace("claude_ai_", "").replace("_", " ")
