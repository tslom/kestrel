""".claude/hooks/comment_checks.py over the tree, and the hook that applies it
to each edit.

- Every `# noqa` names its code and says why. Ruff (PGH004, RUF100) refuses
  a blanket or unused one; the reason is checked here.
- Dated comments are a ratchet: comments_baseline.json holds the count, the
  suite fails if it goes up, and asks for the baseline to come down with it.
"""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
HOOKS = ROOT / ".claude" / "hooks"
sys.path.insert(0, str(HOOKS))
import comment_checks as cc  # the hook's own module, found through the path set just above


def _load_hook():
    spec = importlib.util.spec_from_file_location("lint_on_edit", HOOKS / "lint_on_edit.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_every_noqa_says_why():
    bad = [f"{p.relative_to(ROOT)}:{ln} {c}" for p in cc.tree_files()
           for ln, c in cc.bad_noqa(p, p.read_text())]
    assert not bad, cc.NOQA_FIX + "\n  " + "\n  ".join(bad)


def test_dated_comments_do_not_grow():
    n, base = cc.count_dated(), cc.baseline()
    assert n <= base, (
        f"dated comments went up by {n - base} (baseline {base}): {cc.DATED_FIX} "
        "Run the lint-on-edit hook's check on the file you touched to see which.")
    assert n == base, (f"dated comments went down to {n}: set \"dated_comments\": {n} in "
                       "backend/tests/comments_baseline.json so it can't creep back.")


@pytest.mark.parametrize("line, ok", [
    ("x = 1  # noqa: F401 — re-exported", True),
    ("x = 1  # noqa: F401, TID251 -- both, see above", True),
    ("x = 1  # noqa: F401", False),
    ("x = 1  # noqa", False),
    ("x = 1  # noqa: F401 —", False),
])
def test_a_noqa_needs_code_and_reason(line, ok):
    assert (not cc.bad_noqa(Path("x.py"), line + "\n")) is ok


@pytest.mark.parametrize("src, n", [
    ("# changed 11-17: x\n", 1),
    ("# (08-11) and again 2026-10-29\n", 2),
    ('"""Docstring, seen 10-03."""\n', 1),
    ('KEY = "2026-07-01"  # a day key\n', 0),          # data in a string isn't a comment
    ("# 12-345 isn't a date, nor 13-01, nor 3-14\n", 0),
])
def test_dates_in_python_comments(src, n):
    assert len(cc.dated(Path("x.py"), src)) == n


@pytest.mark.parametrize("src, n", [
    ("// since 11-03\nconst a = 1;\n", 1),
    ("/* one 10-29\n   two 11-17 */\n", 2),
    ('const u = "http://x/11-17"; // a url\n', 0),     # `//` inside a string
    ("const t = `${a}-11-17 // no`;\n", 0),             # nor in a template literal
    ("const r = /\\/\\/ 11-17/; f(1); // ok\n", 0),     # nor in a regex literal
    ("x = a / b; // ratio, 11-20\n", 1),                # division isn't a regex
])
def test_dates_in_js_comments(src, n):
    assert len(cc.dated(Path("x.jsx"), src)) == n


def test_the_hook_blocks_an_added_date_and_a_bare_noqa(tmp_path, monkeypatch, capsys):
    hook = _load_hook()
    monkeypatch.setattr(hook, "ROOT", tmp_path)        # not a git repo: HEAD is empty
    monkeypatch.setattr(hook, "lint", lambda p: (0, ""))
    f = tmp_path / "backend" / "app" / "x.py"
    f.parent.mkdir(parents=True)
    f.write_text("import os  # noqa\n# fixed 11-22: it works now\n")
    monkeypatch.setattr(sys, "stdin", __import__("io").StringIO(json.dumps(
        {"tool_input": {"file_path": str(f)}})))
    assert hook.main() == 2
    err = capsys.readouterr().err
    assert "noqa without a code and reason" in err and "dated comment added" in err
    assert "commit message" in err                     # the error names the fix


def test_the_hook_lets_an_old_date_stay(tmp_path, monkeypatch):
    hook = _load_hook()
    monkeypatch.setattr(hook, "ROOT", tmp_path)
    monkeypatch.setattr(hook, "_at_head", lambda rel: "// seen 11-03\nconst a = 0;\n")
    f = tmp_path / "frontend" / "src" / "x.js"
    f.parent.mkdir(parents=True)
    f.write_text("// seen 11-03\nconst a = 1;\n")
    assert hook.check_comments(f) == ""
