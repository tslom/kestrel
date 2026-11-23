"""PreToolUse hook on Bash: refuse a command that can't work here, and say what does.

Exit 2 blocks the call and hands stderr back to the model. Each rule is a
failure class mined from 30 days of transcripts, and
backend/tests/test_bash_check.py replays the real commands:

- `timeout` / `gtimeout`: macOS has neither (no coreutils). 46 calls in 37
  sessions used one, and every one failed with "command not found". In 23 a
  pipe or a later command returned 0, so the call didn't even show as
  failed, and a bounded pytest run looked as if it had run. The rule checks
  PATH, so it goes quiet if coreutils is ever installed.
- A sqlite3 dot-command inside a SQL argument: sqlite3 reads each argument as
  SQL or as one dot-command, never both, so "PRAGMA busy_timeout=5000;
  .schema list_items" (CLAUDE.md's PRAGMA rule, applied to a dot-command) is a
  syntax error. 15 calls in 13 sessions, 12 of them masked the same way.

Not a rule: a missing busy timeout. 25 calls skipped it, all reads or
`.backup`, and none failed (no "database is locked" in 30 days).

Parsing is best effort (shlex, no expansion; heredoc bodies are skipped). A
command it can't parse passes: this guards, it never gets in the way.
"""

from __future__ import annotations   # hooks run on PATH's python3, maybe macOS's 3.9

import json
import re
import shlex
import shutil
import sys

SEPARATOR = re.compile(r"[;&|()\n]+")            # a token made only of these ends a command
REDIRECT = re.compile(r"&?[<>][<>&]*")           # <, >, >>, <<, <<<, >&, &> (an fd digit stays a word)
ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=")
HEREDOC = re.compile(r"(?<!<)<<-?(?!<)[ \t]*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1")
# Words that leave the next word in command position.
PREFIXES = {"if", "then", "else", "elif", "do", "while", "until", "!", "{", "time",
            "exec", "command", "nohup", "nice", "caffeinate", "sudo", "xargs"}
ENV_WITH_ARG = {"-u", "--unset", "-C", "--chdir", "-S", "--split-string"}
SQLITE_WITH_ARG = {"-cmd", "-init", "-separator", "-newline", "-nullvalue", "-vfs",
                   "-mmap", "-maxsize", "-escape"}
DOT_AFTER_SQL = re.compile(r"[;\n]\s*\.[A-Za-z]")
FIX_DB = "sqlite3 -cmd '.timeout 5000' backend/data/app.db '.schema list_items' 'SELECT …'"

TIMEOUT_MSG = (
    "Blocked: macOS has no `{word}` (coreutils isn't installed), so this would fail "
    "with \"command not found\", and after a `;` the failure is easy to miss. "
    "A scratch node script should end with process.exit(0) instead (CLAUDE.md, "
    "Tool use). To bound any command: perl -e 'alarm shift; exec @ARGV' 30 node x.mjs "
    "(it is killed by SIGALRM, exit 142).")
DOT_MSG = (
    "Blocked: sqlite3 reads each argument as SQL or as one dot-command, never both, "
    "so {arg!r} is a syntax error. Set the busy timeout with -cmd and give each "
    f"dot-command its own argument: {FIX_DB}")


def strip_heredocs(command: str) -> str:
    """The command without heredoc bodies: what's in one is data (a script
    being written, SQL on stdin), not commands this shell runs."""
    out, pos = [], 0
    for m in HEREDOC.finditer(command):
        if m.start() < pos:
            continue
        body = command.find("\n", m.end())
        if body < 0:
            break
        end = re.compile(rf"^\t*{m.group(2)}[ \t]*$", re.M).search(command, body + 1)
        out.append(command[pos:body + 1])
        pos = end.end() if end else len(command)
    out.append(command[pos:])
    return "".join(out)


def tokens(command: str) -> list[str] | None:
    lex = shlex.shlex(strip_heredocs(command), posix=True, punctuation_chars=";&|()<>\n")
    lex.whitespace = " \t\r"
    lex.whitespace_split = True
    lex.commenters = ""
    try:
        return list(lex)
    except ValueError:
        return None


def commands(toks: list[str]):
    """(word, args) for each simple command: leading assignments, prefix
    words and `env` options skipped, redirections and their targets dropped."""
    i, n = 0, len(toks)
    while i < n:
        while i < n and SEPARATOR.fullmatch(toks[i]):
            i += 1
        while i < n and (ASSIGNMENT.match(toks[i]) or toks[i] in PREFIXES):
            i += 1
        if i < n and toks[i] == "env":
            i += 1
            while i < n and (toks[i].startswith("-") or ASSIGNMENT.match(toks[i])):
                i += 2 if toks[i] in ENV_WITH_ARG else 1
        if i >= n:
            return
        word, args = toks[i], []
        i += 1
        while i < n and not SEPARATOR.fullmatch(toks[i]):
            if REDIRECT.fullmatch(toks[i]):
                i += 2
                continue
            args.append(toks[i])
            i += 1
        yield word, args


def check_sqlite(args: list[str]) -> str | None:
    """A SQL argument (or -cmd value) that goes on into a dot-command."""
    sql, i, seen_db = [], 0, False
    while i < len(args):
        a = args[i]
        if a in SQLITE_WITH_ARG:
            sql.append(args[i + 1] if i + 1 < len(args) else "")
            i += 2
            continue
        if not a.startswith("-"):
            if seen_db:
                sql.append(a)
            seen_db = True
        i += 1
    for arg in sql:
        if not arg.lstrip().startswith(".") and DOT_AFTER_SQL.search(arg):
            return DOT_MSG.format(arg=arg[:80])
    return None


def check(command: str) -> str | None:
    toks = tokens(command)
    if toks is None:
        return None
    for word, args in commands(toks):
        base = word.rsplit("/", 1)[-1]
        if base in ("timeout", "gtimeout") and shutil.which(base) is None:
            return TIMEOUT_MSG.format(word=base)
        if base == "sqlite3":
            msg = check_sqlite(args)
            if msg:
                return msg
    return None


def main() -> int:
    try:
        event = json.load(sys.stdin)
    except ValueError:
        return 0
    if event.get("tool_name") != "Bash":
        return 0
    msg = check((event.get("tool_input") or {}).get("command") or "")
    if msg:
        print(msg, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
