"""The Bash safety policy for the console's auto-allow path.

`allowed_tools` auto-allows without prompting, so `CONSOLE_ALLOWED_TOOLS` holds no
`Bash(...)` patterns; `is_safe_read()` sees the whole command and decides. This is
the boundary between injected text (mail, messages, attachments) and a shell.
Everything here is an allowlist: unknown shapes prompt. Design: DESIGN.md → Console (`allowed_tools` bullet, Bash policy).
"""

import os.path
import re
import shlex

from .config import BACKEND_PORT

# Syntax that makes one command two or reaches the filesystem: disqualifies outright.
_SHELL_BREAKOUT = (";", "&", "|", "`", "$(", ">", "<", "\n", "\r")

# Allowlist (a blocklist missed bundled `-sX`). `-L` is safe only while the backend issues no 3xx.
_CURL_SAFE_FLAGS = frozenset({
    "-s", "--silent", "-S", "--show-error", "-i", "--include", "-I", "--head",
    "-L", "--location", "-H", "--header", "-m", "--max-time", "--compressed",
    "--url", "--fail", "-w", "--write-out",
})

# Read-only provider flags. argparse abbreviates (`--ref` is --refresh), so allowlist.
_PROVIDER_SAFE_FLAGS = frozenset({
    "--range", "--metric", "--query", "--format", "--list",
    "--body", "--chars",
})

_PROVIDER_PYTHONS = ("python", "python3", ".venv/bin/python")

_READ_HOSTS = (f"http://localhost:{BACKEND_PORT}/", f"http://127.0.0.1:{BACKEND_PORT}/")

# Commands a read may pipe into. No `sed` (its `w` writes files).
_PAGER_CMDS = frozenset({
    "head", "tail", "wc", "grep", "egrep", "sort", "uniq", "cut", "tr",
    "jq", "column", "nl", "rev", "tac",
})

# File-taking flags (`sort -o`, `grep -f`, jq --rawfile) are excluded. Bare `-20` is _NUMERIC_FLAG.
_PAGER_SAFE_FLAGS = frozenset({
    "-n", "-c", "-l", "-w", "-i", "-v", "-r", "-E", "-e", "-A", "-B", "-C",
    "-u", "-t", "-d", "-k", "-s", "-q", "-h", "-b", "-x", "-m", "-g", "-a",
    "-R", "--color", "--raw-output", "--ignore-case", "--lines", "--reverse",
})

_NUMERIC_FLAG = re.compile(r"^-\d+$")

# sqlite3 is checked three ways: database, options, SQL. No `-cmd`/`-init`: they run SQL first.
_SQLITE_SAFE_FLAGS = frozenset({
    "-readonly", "-json", "-header", "-noheader", "-line", "-list", "-csv",
    "-box", "-table", "-markdown", "-batch", "-echo", "-quote",
})

# Matched on basename.
_SQLITE_READ_DBS = ("app.db", "index.db", "chat.db")

# Writes, plus CLI functions: `SELECT writefile(...)` starts with SELECT and writes a file.
_SQL_WRITE = re.compile(
    r"\b(insert|update|delete|drop|create|alter|attach|detach|replace|vacuum"
    r"|reindex|truncate|begin|commit|rollback|savepoint|release|trigger"
    r"|load_extension|writefile|readfile|edit|writable_schema"
    r"|fts3_tokenizer|zipfile|sqlar)\b",
    re.IGNORECASE)

# The only PRAGMA allowed (`writable_schema=ON` is not harmless).
_SQL_PRAGMA_OK = re.compile(r"^pragma\s+busy_timeout\s*=\s*\d+$", re.IGNORECASE)

_SQL_READ_START = re.compile(r"^(select|with|explain)\b", re.IGNORECASE)


def shell_words(cmd: str) -> list[str] | None:
    """Tokenize like the shell (quoted `|`/`;` stay inside their token); None if unbalanced."""
    try:
        words = shlex.split(cmd)
    except ValueError:
        return None
    return words or None


def strip_cd_prefix(words: list[str]) -> list[str]:
    """Drop one leading `cd <path> &&` (the provider CLI needs it; cwd is the vault)."""
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
    """Split on `;` outside string literals (chat_guids contain `;`); None if a quote is unterminated."""
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
    """Every statement reads. A write keyword inside a string literal still rejects (costs a click, never grants one)."""
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

    Allowlist: unrecognized shapes return False and prompt (a false negative costs a
    click, a false positive is an unattended write). Accepted around the command: a
    leading `cd <path> &&`, a trailing `2>/dev/null`, a pipe into a pager.
    """
    words = shell_words(command)
    if words is None:
        return False
    words = strip_cd_prefix(words)

    # Only these two exact redirects; any other `>` disqualifies below.
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

    # sqlite3 first: its SQL legitimately holds `;`, so the breakout scan skips the SQL, judged as SQL.
    if head[0] == "sqlite3":
        opts = [a for a in head[1:] if a.startswith("-")]
        args = [a for a in head[1:] if not a.startswith("-")]
        # Not _flags(): it would split `-json` into -j -s -o -n.
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

    if (len(head) >= 3 and head[0] in _PROVIDER_PYTHONS
            and head[1] == "-m" and head[2] == "app.providers"):
        return all(f in _PROVIDER_SAFE_FLAGS for f in _flags(head[3:]))

    # Localhost GETs: read options and exactly one URL.
    if head[0] == "curl":
        if not all(f in _CURL_SAFE_FLAGS for f in _flags(head[1:])):
            return False
        urls = [p for p in head[1:] if "://" in p]
        if len(urls) != 1:
            return False
        return urls[0].startswith(_READ_HOSTS)

    return False


# Claude in Chrome acts with real logins from a session that reads untrusted mail:
# anything touching a page asks in every mode. Looking doesn't.
BROWSER_TOOL_PREFIX = "mcp__claude-in-chrome__"
BROWSER_GUARD = "act in your real Chrome, with your logins"
COMPUTER_GUARD = "take over your screen and control apps on your Mac"
_BROWSER_LOOKS = {"tabs_context_mcp", "tabs_create_mcp", "list_connected_browsers",
                  "get_page_text", "read_page", "find", "read_console_messages",
                  "read_network_requests", "shortcuts_list",
                  "resize_window"}
_BROWSER_LOOK_ACTIONS = {"screenshot", "zoom", "wait", "scroll", "scroll_to", "hover"}
# Never covered by a standing yes: page script and uploads each ask on their own.
_BROWSER_ALWAYS_ASK = {"javascript_tool", "file_upload", "upload_image"}


def browser_can_stand(tool_name: str) -> bool:
    """Whether a Claude in Chrome call may be covered by "don't ask again until this turn ends"."""
    return tool_name.removeprefix(BROWSER_TOOL_PREFIX) not in _BROWSER_ALWAYS_ASK
_BROWSER_LOCAL = re.compile(r"^http://(localhost|127\.0\.0\.1)(:\d+)?(/|$)")


def browser_needs_card(tool_name: str, tool_input: object) -> bool:
    """Whether a Claude in Chrome call has to be approved. Unknown tools do."""
    name = tool_name.removeprefix(BROWSER_TOOL_PREFIX)
    if name == "computer" and isinstance(tool_input, dict):
        return tool_input.get("action") not in _BROWSER_LOOK_ACTIONS
    # Localhost is the dev server (auto-verify): no login to act under.
    if name == "navigate" and isinstance(tool_input, dict):
        return not _BROWSER_LOCAL.match(str(tool_input.get("url") or ""))
    return name not in _BROWSER_LOOKS
