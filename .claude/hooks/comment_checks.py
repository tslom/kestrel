"""Comment rules no linter here has, shared by lint_on_edit.py (one file) and test_comments.py (the tree).

A `# noqa` names its code and why; no dated comments; a file's comment lines only go down
(comments_baseline.json) and an edit adds no block over MAX_BLOCK lines. Long reasoning goes in DESIGN.md.
"""

from __future__ import annotations

import io
import json
import re
import sys
import tokenize
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BASELINE = ROOT / "backend" / "tests" / "comments_baseline.json"

# MM-DD on its own ("11-17", "(08-11)"), or a full YYYY-MM-DD.
DATE = re.compile(r"(?<![\w-])(?:20\d\d-)?(?:0[1-9]|1[0-2])-(?:0[1-9]|[12]\d|3[01])(?![\w-])")
NOQA = re.compile(r"#\s*noqa\b(?P<rest>.*)$", re.I)
NOQA_OK = re.compile(r"^:\s*[A-Z]+\d+(?:\s*,\s*[A-Z]+\d+)*\s*(?:—|--|-|:)\s*\S")
CSS_COMMENT = re.compile(r"/\*.*?\*/", re.S)

MAX_BLOCK = 6          # lines, quotes included: a 4-line module docstring plus its pointer
NEW_FILE_SHARE = 0.2   # of non-blank lines, for a file the baseline doesn't know yet

DATED_FIX = ("a date stamp in a comment is history: put it in the commit message (or "
             "DESIGN.md if a reader needs it) and say what's true now. An example date "
             "in a comment can be a placeholder (YYYY-MM-DD).")
NOQA_FIX = ("a noqa names its code and says why, e.g. `# noqa: TID251 — Open-Meteo's "
            "civil date, not a day key`. If nothing fires there any more, delete it.")
LENGTH_FIX = ("a comment is one line of why (two at most); a docstring is one line unless "
              "it states a contract. Long reasoning goes in DESIGN.md, in the module's "
              "section, and the code points there by section name.")
TIGHTEN = "python3 .claude/hooks/comment_checks.py --tighten"


def _py_scan(text: str, docstrings: bool = True) -> list[tuple[int, str, bool]]:
    """(line, text, alone-on-its-line) for every comment and docstring line."""
    out: list[tuple[int, str, bool]] = []
    try:
        toks = list(tokenize.generate_tokens(io.StringIO(text).readline))
    except (tokenize.TokenError, SyntaxError):
        return out
    prev = tokenize.NEWLINE
    for t in toks:
        if t.type == tokenize.COMMENT:
            out.append((t.start[0], t.string, not t.line[:t.start[1]].strip()))
        elif docstrings and t.type == tokenize.STRING and prev in (tokenize.NEWLINE, tokenize.NL,
                                                    tokenize.INDENT, tokenize.DEDENT):
            # A string standing as a statement: a docstring (or a bare one).
            for i, line in enumerate(t.string.splitlines()):
                out.append((t.start[0] + i, line, True))
        if t.type not in (tokenize.COMMENT, tokenize.NL):
            prev = t.type
    return out


def _py_comments(text: str, docstrings: bool = True) -> list[tuple[int, str]]:
    return [(ln, c) for ln, c, _ in _py_scan(text, docstrings)]


_REGEX_BEFORE = set("(,=:[!&|?{};+-*%<>~^") | {""}


def _js_scan(text: str) -> list[tuple[int, str, bool]]:
    """Every // and /* */ comment line, skipping strings, template literals and
    regex literals (by the usual previous-token guess)."""
    out: list[tuple[int, str, bool]] = []
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
        if two in ("//", "/*"):
            alone = not text[text.rfind("\n", 0, i) + 1:i].strip(" \t{")
            if two == "//":
                j = text.find("\n", i)
                j = n if j < 0 else j
            else:
                j = text.find("*/", i + 2)
                j = n if j < 0 else j + 2
            for k, part in enumerate(text[i:j].split("\n")):
                out.append((line + k, part, alone))
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


def _js_comments(text: str) -> list[tuple[int, str]]:
    return [(ln, c) for ln, c, _ in _js_scan(text)]


def _css_scan(text: str) -> list[tuple[int, str, bool]]:
    out = []
    for m in CSS_COMMENT.finditer(text):
        start = text.count("\n", 0, m.start()) + 1
        alone = not text[text.rfind("\n", 0, m.start()) + 1:m.start()].strip()
        out += [(start + k, part, alone) for k, part in enumerate(m.group(0).split("\n"))]
    return out


def scan(path: Path, text: str) -> list[tuple[int, str, bool]]:
    if path.suffix == ".py":
        return _py_scan(text)
    if path.suffix in (".js", ".jsx", ".mjs"):
        return _js_scan(text)
    if path.suffix == ".css":
        return _css_scan(text)
    return []


def comments(path: Path, text: str) -> list[tuple[int, str]]:
    return [(ln, c) for ln, c, _ in scan(path, text)]


def comment_lines(path: Path, text: str) -> int:
    """Lines holding a comment or docstring (a line with two counts once)."""
    return len({ln for ln, _, _ in scan(path, text)})


def blocks(path: Path, text: str) -> list[tuple[int, int, str]]:
    """(first line, length, first text) per run of comment-only lines; a
    trailing comment on a code line isn't part of a block."""
    lines = sorted({ln: c for ln, c, alone in scan(path, text) if alone}.items())
    out: list[list] = []
    for ln, c in lines:
        if out and ln == out[-1][0] + out[-1][1]:
            out[-1][1] += 1
        else:
            out.append([ln, 1, c.strip()])
    return [tuple(b) for b in out]


def long_blocks(path: Path, text: str) -> list[tuple[int, int, str]]:
    return [b for b in blocks(path, text) if b[1] > MAX_BLOCK]


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
SUFFIXES = (".py", ".js", ".jsx", ".mjs", ".css")


def in_scope(rel: str) -> bool:
    return rel in TOP_FILES or any(rel.startswith(s + "/") for s in SCOPES)


def tree_files(root: Path = ROOT):
    for rel in TOP_FILES:
        if (root / rel).is_file():
            yield root / rel
    for scope in SCOPES:
        for p in sorted((root / scope).rglob("*")):
            if p.suffix in SUFFIXES and "__pycache__" not in p.parts \
                    and "node_modules" not in p.parts and "data" not in p.relative_to(root).parts[:2]:
                yield p


def count_dated(root: Path = ROOT) -> int:
    return sum(len(dated(p, p.read_text())) for p in tree_files(root))


def count_long_blocks(root: Path = ROOT) -> int:
    return sum(len(long_blocks(p, p.read_text())) for p in tree_files(root))


def tree_comment_lines(root: Path = ROOT) -> dict[str, int]:
    return {p.relative_to(root).as_posix(): comment_lines(p, p.read_text()) for p in tree_files(root)}


def _base() -> dict:
    return json.loads(BASELINE.read_text())


def baseline() -> int:
    return _base()["dated_comments"]


def allowed_lines(rel: str, text: str, base: dict | None = None) -> int:
    """The file's ceiling: its baseline, or a share of its lines if it's new."""
    known = (base or _base()).get("comment_lines", {})
    if rel in known:
        return known[rel]
    return max(2, int(NEW_FILE_SHARE * sum(1 for ln in text.splitlines() if ln.strip())))


def tighten(root: Path = ROOT) -> dict:
    """Lower every number in the baseline to what the tree has now; never raise one."""
    base = _base()
    now = tree_comment_lines(root)
    old = base.get("comment_lines", {})
    base["comment_lines"] = {rel: min(n, old.get(rel, n)) for rel, n in sorted(now.items()) if n}
    base["dated_comments"] = min(base["dated_comments"], count_dated(root))
    base["long_blocks"] = min(base.get("long_blocks", 10**9), count_long_blocks(root))
    BASELINE.write_text(json.dumps(base, indent=1) + "\n")
    return base


if __name__ == "__main__":
    if sys.argv[1:] == ["--tighten"]:
        b = tighten()
        print(f"comments_baseline.json: {sum(b['comment_lines'].values())} comment lines in "
              f"{len(b['comment_lines'])} files, {b['long_blocks']} long blocks, "
              f"{b['dated_comments']} dated")
    else:
        print(f"usage: {TIGHTEN}", file=sys.stderr)
        sys.exit(2)
