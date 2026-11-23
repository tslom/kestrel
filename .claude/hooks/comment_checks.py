"""Comment rules that no linter here has: shared by lint_on_edit.py (the file
just edited) and backend/tests/test_comments.py (the whole tree).

* A `# noqa` names its codes and says why: `# noqa: BLE001 — why`. Ruff's
  PGH004 and RUF100 (backend/ruff.toml) already refuse a blanket noqa and an
  unused one; the reason is checked here. The frontend's `-- reason` on an
  eslint-disable is an ESLint rule (eslint.config.js), not this.
* No new dated comments: "(MM-DD)", "MM-DD:", "since YYYY-MM-DD". A date in
  a comment is history, and history goes in DESIGN.md or the commit message,
  where it stays true. The ones already here are held by a ratchet
  (COMMENTS_BASELINE) until the comment-cleanup pass removes them; the hook
  refuses an edit that adds one to a file.
"""

from __future__ import annotations

import io
import json
import re
import tokenize
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BASELINE = ROOT / "backend" / "tests" / "comments_baseline.json"

# MM-DD on its own ("11-17", "(08-11)"), or a full YYYY-MM-DD.
DATE = re.compile(r"(?<![\w-])(?:20\d\d-)?(?:0[1-9]|1[0-2])-(?:0[1-9]|[12]\d|3[01])(?![\w-])")
NOQA = re.compile(r"#\s*noqa\b(?P<rest>.*)$", re.I)
NOQA_OK = re.compile(r"^:\s*[A-Z]+\d+(?:\s*,\s*[A-Z]+\d+)*\s*(?:—|--|-|:)\s*\S")

DATED_FIX = ("a date stamp in a comment is history: put it in the commit message (or "
             "DESIGN.md if a reader needs it) and say what's true now. An example date "
             "in a comment can be a placeholder (YYYY-MM-DD).")
NOQA_FIX = ("a noqa names its code and says why, e.g. `# noqa: TID251 — Open-Meteo's "
            "civil date, not a day key`. If nothing fires there any more, delete it.")


def _py_comments(text: str, docstrings: bool = True) -> list[tuple[int, str]]:
    """(line, text) for every comment, and every docstring unless told not."""
    out: list[tuple[int, str]] = []
    try:
        toks = list(tokenize.generate_tokens(io.StringIO(text).readline))
    except (tokenize.TokenError, SyntaxError):
        return out
    prev = tokenize.NEWLINE
    for t in toks:
        if t.type == tokenize.COMMENT:
            out.append((t.start[0], t.string))
        elif docstrings and t.type == tokenize.STRING and prev in (tokenize.NEWLINE, tokenize.NL,
                                                    tokenize.INDENT, tokenize.DEDENT):
            # A string standing as a statement: a docstring (or a bare one).
            for i, line in enumerate(t.string.splitlines()):
                out.append((t.start[0] + i, line))
        if t.type not in (tokenize.COMMENT, tokenize.NL):
            prev = t.type
    return out


_REGEX_BEFORE = set("(,=:[!&|?{};+-*%<>~^") | {""}


def _js_comments(text: str) -> list[tuple[int, str]]:
    """(line, text) for every // and /* */ comment, skipping strings, template
    literals and regex literals (by the usual previous-token guess)."""
    out: list[tuple[int, str]] = []
    i, n, line = 0, len(text), 1
    last = ""          # last significant character, for the regex guess
    word = ""          # last identifier, so `return /x/` reads as a regex
    while i < n:
        c = text[i]
        if c == "\n":
            line += 1
            i += 1
            continue
        if c.isspace():
            i += 1
            continue
        two = text[i:i + 2]
        if two == "//":
            j = text.find("\n", i)
            j = n if j < 0 else j
            out.append((line, text[i:j]))
            i = j
            continue
        if two == "/*":
            j = text.find("*/", i + 2)
            j = n if j < 0 else j + 2
            for k, part in enumerate(text[i:j].split("\n")):
                out.append((line + k, part))
            line += text.count("\n", i, j)
            i = j
            continue
        if c in "'\"`" or (c == "/" and (last in _REGEX_BEFORE or word in ("return", "typeof", "case"))):
            q, j, depth = c, i + 1, 0
            in_class = False
            while j < n:
                d = text[j]
                if d == "\\":
                    j += 2
                    continue
                if d == "\n":
                    line += 1
                    if q in "'\"/":
                        break
                if q == "`" and text.startswith("${", j):
                    depth += 1
                    j += 2
                    continue
                if q == "`" and depth and d == "}":
                    depth -= 1
                elif q == "/" and d == "[":
                    in_class = True
                elif q == "/" and d == "]":
                    in_class = False
                elif d == q and not depth and not in_class:
                    break
                j += 1
            i = j + 1
            last, word = "x", ""
            continue
        if c.isalnum() or c in "_$":
            j = i
            while j < n and (text[j].isalnum() or text[j] in "_$"):
                j += 1
            word, last = text[i:j], "x"
            i = j
            continue
        last, word = c, ""
        i += 1
    return out


def comments(path: Path, text: str) -> list[tuple[int, str]]:
    if path.suffix == ".py":
        return _py_comments(text)
    if path.suffix in (".js", ".jsx", ".mjs"):
        return _js_comments(text)
    return []


def dated(path: Path, text: str) -> list[tuple[int, str]]:
    """One (line, comment) per date found, so a second date on a line counts."""
    return [(ln, c.strip()) for ln, c in comments(path, text) for _ in DATE.finditer(c)]


def bad_noqa(path: Path, text: str) -> list[tuple[int, str]]:
    if path.suffix != ".py":
        return []
    out = []
    for ln, c in _py_comments(text, docstrings=False):   # a noqa only works in a comment
        m = NOQA.search(c)
        if m and not NOQA_OK.match(m.group("rest").strip()):
            out.append((ln, c.strip()))
    return out


# What the ratchet covers: the code, not the vendored, generated or data.
SCOPES = ("backend/app", "backend/scripts", "backend/evals", "backend/tests",
          "frontend/src", "frontend/scripts", ".claude/hooks")
TOP_FILES = ("backend/launch_app.py", "backend/desktop_shell.py", "frontend/vite.config.js",
             "frontend/eslint.config.js")


def in_scope(rel: str) -> bool:
    return rel in TOP_FILES or any(rel.startswith(s + "/") for s in SCOPES)


def tree_files(root: Path = ROOT):
    for rel in TOP_FILES:
        if (root / rel).is_file():
            yield root / rel
    for scope in SCOPES:
        for p in sorted((root / scope).rglob("*")):
            if p.suffix in (".py", ".js", ".jsx", ".mjs") and "__pycache__" not in p.parts \
                    and "node_modules" not in p.parts and "data" not in p.relative_to(root).parts[:2]:
                yield p


def count_dated(root: Path = ROOT) -> int:
    return sum(len(dated(p, p.read_text())) for p in tree_files(root))


def baseline() -> int:
    return json.loads(BASELINE.read_text())["dated_comments"]
