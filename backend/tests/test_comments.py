""".claude/hooks/comment_checks.py over the tree, and the hook that applies it to each edit.

Ratchets in comments_baseline.json (dated comments, long blocks, comment lines per file) fail
when a number goes up and ask for `--tighten` when one comes down.
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
    assert n == base, f"dated comments went down to {n}: run `{cc.TIGHTEN}` so it can't creep back."


def test_comment_lines_per_file_do_not_grow():
    base = json.loads(cc.BASELINE.read_text())
    over, loose, new = [], [], []
    for p in cc.tree_files():
        rel, text = p.relative_to(ROOT).as_posix(), p.read_text()
        n, ceiling = cc.comment_lines(p, text), cc.allowed_lines(rel, text, base)
        if n > ceiling:
            over.append(f"{rel}: {n} comment lines, ceiling {ceiling}")
        elif rel in base["comment_lines"] and n < ceiling:
            loose.append(rel)
        elif rel not in base["comment_lines"] and n:
            new.append(rel)
    assert not over, f"{cc.LENGTH_FIX}\n  " + "\n  ".join(over)
    gone = [r for r in base["comment_lines"] if not (ROOT / r).exists()]
    assert not (loose or new or gone), (
        f"comment counts moved down, or files came or went ({(loose + new + gone)[:5]}): "
        f"run `{cc.TIGHTEN}` so they can't creep back.")


def test_long_comment_blocks_do_not_grow():
    n, base = cc.count_long_blocks(), json.loads(cc.BASELINE.read_text())["long_blocks"]
    assert n <= base, (f"{n - base} more comment blocks over {cc.MAX_BLOCK} lines "
                       f"(baseline {base}): {cc.LENGTH_FIX}")
    assert n == base, f"long comment blocks went down to {n}: run `{cc.TIGHTEN}`."


@pytest.mark.parametrize("path, src, want", [
    ("x.py", '"""One.\n\nTwo.\n"""\nx = 1  # trailing, not a block\n# a\n# b\n', [(1, 4), (6, 2)]),
    ("x.js", "// a\n// b\nconst x = 1; // trailing\n/* c\n   d */\n", [(1, 2), (4, 2)]),
    ("x.jsx", "<div>\n  {/* a\n     b */}\n</div>\n", [(2, 2)]),
    ("x.css", "/* a\n   b\n   c */\n.x { color: red; } /* trailing */\n", [(1, 3)]),
])
def test_blocks_are_runs_of_comment_only_lines(path, src, want):
    assert [(ln, n) for ln, n, _ in cc.blocks(Path(path), src)] == want


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


def _hook_on(tmp_path, monkeypatch, head, now, rel="backend/app/x.py"):
    hook = _load_hook()
    monkeypatch.setattr(hook, "ROOT", tmp_path)
    monkeypatch.setattr(hook, "_at_head", lambda r: head)
    f = tmp_path / rel
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(now)
    return hook.check_comments(f)


LONG = "".join(f"# reason {i}\n" for i in range(cc.MAX_BLOCK + 1))


def test_the_hook_refuses_an_added_long_block(tmp_path, monkeypatch):
    monkeypatch.setattr(cc, "_base", lambda: {"comment_lines": {"backend/app/x.py": 99}})
    err = _hook_on(tmp_path, monkeypatch, "x = 1\n", LONG + "x = 1\n")
    assert f"over {cc.MAX_BLOCK} lines added" in err and "DESIGN.md" in err


def test_the_hook_lets_a_long_block_shrink(tmp_path, monkeypatch):
    monkeypatch.setattr(cc, "_base", lambda: {"comment_lines": {"backend/app/x.py": 99}})
    assert _hook_on(tmp_path, monkeypatch, "# old\n" * 12 + "x = 1\n", LONG + "x = 1\n") == ""


def test_the_hook_refuses_a_file_over_its_ceiling(tmp_path, monkeypatch):
    monkeypatch.setattr(cc, "_base", lambda: {"comment_lines": {"backend/app/x.py": 1}})
    err = _hook_on(tmp_path, monkeypatch, "# one\nx = 1\n", "# one\n# two\nx = 1\n")
    assert "ceiling is 1" in err


def test_tighten_never_raises_a_number(tmp_path, monkeypatch):
    (tmp_path / "backend" / "app").mkdir(parents=True)
    (tmp_path / "backend" / "app" / "x.py").write_text("# a\n# b\nx = 1\n")
    baseline = tmp_path / "b.json"
    baseline.write_text(json.dumps({"dated_comments": 0, "long_blocks": 0,
                                    "comment_lines": {"backend/app/x.py": 1}}))
    monkeypatch.setattr(cc, "BASELINE", baseline)
    assert cc.tighten(tmp_path)["comment_lines"] == {"backend/app/x.py": 1}
