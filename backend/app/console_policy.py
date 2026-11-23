"""The Bash safety policy for the console's auto-allow path.

`allowed_tools` bypasses the approval card entirely — the SDK auto-allows those
entries *without prompting*, so `can_use_tool` is never consulted for them.
`CONSOLE_ALLOWED_TOOLS` therefore carries no `Bash(...)` patterns at all, and
this module is what stands in their place: `is_safe_read()` sees the whole
command, not a prefix, and decides whether it may run unattended.

That makes this the boundary between injected text — an email body, a message,
an extracted attachment the console is summarizing — and an executed shell
command. It lives in its own module so that "what can this thing run without
asking?" is answerable by reading one file.
"""

import os.path
import re
import shlex

from .config import BACKEND_PORT

# Shell syntax that lets one command become two, or reach the filesystem.
# Their presence disqualifies a command outright — no attempt is made to
# reason about what the second half does.
_SHELL_BREAKOUT = (";", "&", "|", "`", "$(", ">", "<", "\n", "\r")

# The curl options a read may carry. An allowlist, because the blocklist this
# replaced was unsound: it tested `flag.startswith("-X")` per token, so a
# *bundled* short flag slipped past every entry — `-sX` does not start with
# `-X`, and `curl -sX DELETE http://localhost:8788/api/plaid/items/<id>` was
# auto-approved with no card. Anything not named here prompts, including
# option-attached values like `-m5` (write it as `-m 5`).
# `-L`/`--location` is allowed on one standing assumption: the host check below
# is a prefix test on the URL *as written*, and curl follows a 3xx wherever it
# points — so `-L` is only safe while the backend issues no redirects. It
# currently issues none (no RedirectResponse, no 30x anywhere in app/). If a
# route ever gains one, drop `-L` from this set, or a localhost read auto-
# approved here can be walked off-host by the response.
_CURL_SAFE_FLAGS = frozenset({
    "-s", "--silent", "-S", "--show-error", "-i", "--include", "-I", "--head",
    "-L", "--location", "-H", "--header", "-m", "--max-time", "--compressed",
    "--url", "--fail", "-w", "--write-out",
})

# Provider-CLI options that only read. argparse accepts unambiguous
# abbreviations, so this must be an allowlist too: `--ref` reaches --refresh,
# which re-pulls from an external API, and `--extract N` runs the attachment
# extractor. Both are side effects worth a click.
# `--body <id>`/`--chars` fetch one message from Gmail: a read, nothing
# written or stored, and the console's email_body tool is already allowed
# without a click — gating the identical CLI call would only add friction.
_PROVIDER_SAFE_FLAGS = frozenset({
    "--range", "--metric", "--query", "--format", "--list",
    "--body", "--chars",
})

_PROVIDER_PYTHONS = ("python", "python3", ".venv/bin/python")

_READ_HOSTS = (f"http://localhost:{BACKEND_PORT}/", f"http://127.0.0.1:{BACKEND_PORT}/")

# Commands a read may pipe *into*. The pipe itself was never the danger — the
# right-hand side is — so these are named individually and everything else
# still disqualifies the command. `sed` is absent on purpose (its `w` command
# writes files), as is any pager whose whole job is to produce output somewhere.
_PAGER_CMDS = frozenset({
    "head", "tail", "wc", "grep", "egrep", "sort", "uniq", "cut", "tr",
    "jq", "column", "nl", "rev", "tac",
})

# Flags a pager segment may carry. Allowlisted for the same reason curl's are:
# the file-taking options are the whole risk. `-o` (sort writes its output
# there), `-f` (grep reads patterns from a file) and jq's --rawfile family are
# excluded and therefore prompt. A bare numeric flag (`head -20`) is handled
# separately since it never appears in a fixed set.
_PAGER_SAFE_FLAGS = frozenset({
    "-n", "-c", "-l", "-w", "-i", "-v", "-r", "-E", "-e", "-A", "-B", "-C",
    "-u", "-t", "-d", "-k", "-s", "-q", "-h", "-b", "-x", "-m", "-g", "-a",
    "-R", "--color", "--raw-output", "--ignore-case", "--lines", "--reverse",
})

_NUMERIC_FLAG = re.compile(r"^-\d+$")

# sqlite3 reads. The CLI is a general-purpose SQL shell, so three things are
# checked, not one: which database, which options, and what the SQL actually
# does. `-cmd`/`-init` are absent because both execute arbitrary SQL before the
# statement we inspected ever runs.
_SQLITE_SAFE_FLAGS = frozenset({
    "-readonly", "-json", "-header", "-noheader", "-line", "-list", "-csv",
    "-box", "-table", "-markdown", "-batch", "-echo", "-quote",
})

# app.db and index.db are ours; chat.db is Messages' and is read-only to us
# anyway. Matched on the basename so any spelling of the path works.
_SQLITE_READ_DBS = ("app.db", "index.db", "chat.db")

# Anything that can change a row, a schema, or which files are attached —
# plus the functions the sqlite3 CLI adds on top of the language. Those matter
# more than the DML keywords here: `SELECT writefile('/tmp/x', ...)` is a
# statement that starts with SELECT, contains no write keyword, and writes a
# file; readfile() and load_extension() are the same shape pointed the other
# way. A SELECT is only a read if nothing inside it reaches the filesystem.
_SQL_WRITE = re.compile(
    r"\b(insert|update|delete|drop|create|alter|attach|detach|replace|vacuum"
    r"|reindex|truncate|begin|commit|rollback|savepoint|release|trigger"
    r"|load_extension|writefile|readfile|edit|writable_schema"
    r"|fts3_tokenizer|zipfile|sqlar)\b",
    re.IGNORECASE)

# The only PRAGMA a read needs — and the one CLAUDE.md requires. Other pragmas
# are not all harmless (`writable_schema=ON` turns the schema into a table).
_SQL_PRAGMA_OK = re.compile(r"^pragma\s+busy_timeout\s*=\s*\d+$", re.IGNORECASE)

_SQL_READ_START = re.compile(r"^(select|with|explain)\b", re.IGNORECASE)


def shell_words(cmd: str) -> list[str] | None:
    """Tokenize like the shell would, or None if it can't be read confidently.

    shlex is used rather than str.split() so quoting is respected: a `|` or `;`
    inside a quoted URL or SQL string stays part of its token instead of
    looking like an operator. Real operators survive as tokens of their own,
    which is what the breakout scan needs. Unbalanced quotes return None —
    a command we can't parse is a command we don't approve.
    """
    try:
        words = shlex.split(cmd)
    except ValueError:
        return None
    return words or None


def strip_cd_prefix(words: list[str]) -> list[str]:
    """Drop a leading `cd <path> &&`.

    The provider CLI cannot run from anywhere else: the console's cwd is the
    vault, so `python -m app.providers` only resolves after a cd into the
    backend — which is exactly the form SYSTEM_APPEND prescribes. Refusing to
    look past that cd meant the allowlist rejected every provider read ever
    issued. Only one such prefix is removed, and only when the path is an
    ordinary word, so `cd x && cd y && rm` still has an `&&` left to trip on.
    """
    if (len(words) >= 4 and words[0] == "cd" and words[2] == "&&"
            and not has_breakout(words[1:2])):
        return words[3:]
    return words


def has_breakout(words: list[str]) -> bool:
    """Does any token carry shell syntax that could start a second command?"""
    return any(tok in w for w in words for tok in _SHELL_BREAKOUT)


def _pager_is_safe(seg: list[str]) -> bool:
    """A `| head -40` style tail segment: named command, no file-taking flags."""
    if not seg or seg[0] not in _PAGER_CMDS or has_breakout(seg):
        return False
    return all(f in _PAGER_SAFE_FLAGS or _NUMERIC_FLAG.match(f)
               for f in _flags(seg[1:]))


def _sql_statements(sql: str) -> list[str] | None:
    """Split on statement separators, ignoring `;` inside string literals.

    Splitting naively would be safe but useless: a chat_guid is literally
    `any;+;chat5831...`, so a plain str.split(';') tears one WHERE clause into
    three fragments, two of which look nothing like a SELECT. Quote tracking
    keeps the literal intact. Unterminated quotes return None — malformed SQL
    is not SQL we approve.
    """
    out: list[str] = []
    buf: list[str] = []
    quote: str | None = None
    i = 0
    while i < len(sql):
        ch = sql[i]
        if quote:
            buf.append(ch)
            if ch == quote:
                if i + 1 < len(sql) and sql[i + 1] == quote:
                    buf.append(sql[i + 1])   # '' / "" is an escaped quote
                    i += 1
                else:
                    quote = None
        elif ch in ("'", '"'):
            quote = ch
            buf.append(ch)
        elif ch == ";":
            out.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
        i += 1
    if quote:
        return None
    out.append("".join(buf))
    return [s.strip() for s in out if s.strip()]


def _sql_is_read_only(sql: str) -> bool:
    """Every statement in `sql` reads and nothing else.

    Note the direction of the remaining imprecision: a write keyword sitting
    harmlessly inside a string literal still rejects the command. That costs a
    click and never grants one, which is the trade this module makes
    everywhere else too.
    """
    statements = _sql_statements(sql)
    if not statements:
        return False
    for stmt in statements:
        if _SQL_PRAGMA_OK.match(stmt):
            continue
        if _SQL_WRITE.search(stmt) or not _SQL_READ_START.match(stmt):
            return False
    return True


def _flags(parts: list[str]) -> list[str]:
    """Option tokens, with bundled short flags split out (`-sSL` -> -s -S -L).
    A token that mixes letters with anything else (`-m5`, `-o/tmp/x`) stays
    whole, so it fails the allowlist rather than being half-understood."""
    out: list[str] = []
    for p in parts:
        if not p.startswith("-") or p == "-" or p == "--":
            continue
        if p.startswith("--") or len(p) == 2:
            out.append(p.split("=", 1)[0])
        elif p[1:].isalpha():
            out += [f"-{c}" for c in p[1:]]
        else:
            out.append(p)
    return out


def is_safe_read(command: str) -> bool:
    """Is this Bash command a read that can run without asking?

    Replaces the prefix wildcards that used to live in CONSOLE_ALLOWED_TOOLS.
    Those auto-approved anything *starting* with a known prefix, which meant a
    curl to a GET endpoint and a curl that POSTed to it were indistinguishable
    — same prefix, opposite consequences. Reading the whole command is the only
    way to tell those apart.

    Allowlist, not blocklist: unrecognized shapes return False and prompt. The
    cost of a false negative is one click; the cost of a false positive is an
    unattended write, so that asymmetry decides every judgment call here.

    This matters most for injected instructions. Text from an email, a message,
    or an extracted attachment can end up asking the console to run something;
    when it does, the request has to arrive as a card the user sees rather than
    as a command that already ran.

    Three shapes are accepted around the command proper, because refusing them
    made this function unreachable in practice — every read the console
    actually issued carried at least one, and so prompted: a leading
    `cd <path> &&`, a trailing `2>/dev/null`, and a pipe into a pager. None of
    them changes what the command reads; they only decide where its noise goes.
    """
    words = shell_words(command)
    if words is None:
        return False
    words = strip_cd_prefix(words)

    # Discarding stderr says nothing about what the command does, and the model
    # appends it reflexively. Only these two exact forms — any other redirect
    # still contains a `>` and disqualifies below.
    words = [w for w in words if w not in ("2>/dev/null", "2>&1")]

    # Split on pipes; the head is the read, every tail must be a pager.
    segments: list[list[str]] = [[]]
    for w in words:
        if w == "|":
            segments.append([])
        else:
            segments[-1].append(w)
    head, tails = segments[0], segments[1:]
    if not head or not all(_pager_is_safe(t) for t in tails):
        return False

    # sqlite3 comes first because it is the one read whose argument is *meant*
    # to contain shell metacharacters: `PRAGMA busy_timeout=5000; SELECT ...`
    # is the form CLAUDE.md mandates, and shlex has already stripped the quotes
    # that made its `;` inert. So the breakout scan runs on everything except
    # the SQL, and the SQL is judged as SQL instead.
    if head[0] == "sqlite3":
        opts = [a for a in head[1:] if a.startswith("-")]
        args = [a for a in head[1:] if not a.startswith("-")]
        # Not _flags(): that bundles short options (`-json` -> -j -s -o -n),
        # which is right for curl and wrong for sqlite3's single-dash words.
        if not all(o in _SQLITE_SAFE_FLAGS for o in opts):
            return False
        if len(args) < 2 or has_breakout(opts + args[:1]):
            return False
        db, sql = args[0], " ; ".join(args[1:])
        if os.path.basename(db) not in _SQLITE_READ_DBS:
            return False
        return _sql_is_read_only(sql)

    if has_breakout(head):
        return False

    # Provider CLI reads: `python -m app.providers <source> [flags]`.
    if (len(head) >= 3 and head[0] in _PROVIDER_PYTHONS
            and head[1] == "-m" and head[2] == "app.providers"):
        return all(f in _PROVIDER_SAFE_FLAGS for f in _flags(head[3:]))

    # Localhost GETs against our own API. Only read options, and exactly one
    # URL — a second URL, or an option we don't recognize, prompts.
    if head[0] == "curl":
        if not all(f in _CURL_SAFE_FLAGS for f in _flags(head[1:])):
            return False
        urls = [p for p in head[1:] if "://" in p]
        if len(urls) != 1:
            return False
        return urls[0].startswith(_READ_HOSTS)

    return False


# Claude in Chrome acts in the user's real Chrome with their real logins, from
# a session that also reads untrusted mail, so what touches a page asks in every
# mode, never-ask included. Looking doesn't, and a new tab starts empty.
BROWSER_TOOL_PREFIX = "mcp__claude-in-chrome__"
BROWSER_GUARD = "act in your real Chrome, with your logins"
COMPUTER_GUARD = "take over your screen and control apps on your Mac"
_BROWSER_LOOKS = {"tabs_context_mcp", "tabs_create_mcp", "list_connected_browsers",
                  "get_page_text", "read_page", "find", "read_console_messages",
                  "read_network_requests", "shortcuts_list",
                  # the window's size, not the page: nothing to act under
                  "resize_window"}
_BROWSER_LOOK_ACTIONS = {"screenshot", "zoom", "wait", "scroll", "scroll_to", "hover"}
# Never covered by a standing yes, however the card was answered: script run
# inside a logged-in page can do anything the page can, and an upload sends a
# local file to whoever the page is. Each asks on its own (New Features #55).
_BROWSER_ALWAYS_ASK = {"javascript_tool", "file_upload", "upload_image"}


def browser_can_stand(tool_name: str) -> bool:
    """Whether a Claude in Chrome call may be covered by "don't ask again
    until this turn ends". One web task was a card per click before."""
    return tool_name.removeprefix(BROWSER_TOOL_PREFIX) not in _BROWSER_ALWAYS_ASK
_BROWSER_LOCAL = re.compile(r"^http://(localhost|127\.0\.0\.1)(:\d+)?(/|$)")


def browser_needs_card(tool_name: str, tool_input: object) -> bool:
    """Whether a Claude in Chrome call has to be approved. Unknown tools do."""
    name = tool_name.removeprefix(BROWSER_TOOL_PREFIX)
    if name == "computer" and isinstance(tool_input, dict):
        return tool_input.get("action") not in _BROWSER_LOOK_ACTIONS
    # Your own dev server: what auto-verify opens, with no login to act under.
    if name == "navigate" and isinstance(tool_input, dict):
        return not _BROWSER_LOCAL.match(str(tool_input.get("url") or ""))
    return name not in _BROWSER_LOOKS
