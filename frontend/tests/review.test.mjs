// lib/syntax.js and lib/review.js under node's test runner — the diff pane's
// colour and its comments. backend/tests/test_review_frontend.py runs it, so
// pytest holds it. `node --test frontend/tests/`.
import { test } from "node:test";
import assert from "node:assert/strict";
import { readdirSync, readFileSync, statSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { highlight, langOf } from "../src/lib/syntax.js";
import { anchor, commitMessage, conflictMessage, lines, numbered, place, reviewMessage,
         where } from "../src/lib/review.js";

const ROOT = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const has = (toks, t, s) => toks.some((x) => x.t === t && x.s === s);
const kinds = (toks) => toks.map((x) => `${x.t}:${x.s}`);

function walk(dir, out = []) {
  for (const name of readdirSync(dir)) {
    if (["node_modules", "dist", "__pycache__", "data", "food_data"].includes(name)) continue;
    const p = join(dir, name);
    if (statSync(p).isDirectory()) walk(p, out);
    else if (langOf(p)) out.push(p);
  }
  return out;
}

test("tokens join back to exactly the line, over this repo's own files", () => {
  const files = [...walk(join(ROOT, "frontend", "src")), ...walk(join(ROOT, "backend", "app"))];
  assert.ok(files.length > 100, `only ${files.length} files found`);
  for (const f of files) {
    const text = readFileSync(f, "utf8");
    const src = text.split("\n");
    const toks = highlight(text, langOf(f));
    assert.equal(toks.length, src.length, f);
    toks.forEach((line, i) => assert.equal(line.map((t) => t.s).join(""), src[i], `${f}:${i + 1}`));
  }
});

test("a docstring's middle is string, and the code after it isn't", () => {
  const t = highlight('def f(x):\n    """Return x if it is in the list.\n\n    for and not\n    """\n    return x  # done', "py");
  assert.ok(has(t[0], "op", "def") && has(t[0], "cmd", "f"));
  assert.deepEqual(t[3].map((x) => x.t), ["str"]);
  assert.ok(has(t[5], "op", "return") && has(t[5], "com", "# done"));
  const p = highlight('x = rb"\\x00" + f"{y}"\n@app.get("/x")\nclass A(B):', "py");
  assert.ok(has(p[0], "str", 'rb"\\x00"') && has(p[0], "str", 'f"{y}"'));
  assert.ok(has(p[1], "flag", "@app.get") && has(p[2], "cmd", "A"));
});

test("JSX: prose stays prose, a tag carries over lines, attributes are flags", () => {
  const t = highlight([
    "return (",
    '  <p className="status">Don\'t do it if you can</p>',
    "  <DiffPane files={a} icon={<Icon name=\"x\" />}",
    "    onClose={() => x > 1} aria-label=\"y\" />",
    ");",
    "const y = a < b && c > d;",
  ].join("\n"), "js");
  assert.ok(!t[1].some((x) => x.t === "str" && x.s.includes("Don")), kinds(t[1]).join(" "));
  assert.ok(!has(t[1], "op", "if"));
  assert.ok(has(t[1], "flag", "className") && has(t[1], "str", '"status"'));
  assert.ok(has(t[2], "op", "<Icon") && has(t[2], "flag", "icon"), kinds(t[2]).join(" "));
  assert.ok(has(t[3], "flag", "onClose") && has(t[3], "flag", "aria-label") && has(t[3], "op", "/>"));
  assert.ok(!t[5].some((x) => x.t === "op" && x.s.startsWith("<")), kinds(t[5]).join(" "));
});

test("a quote or backtick in a regex, comment or ${} opens nothing", () => {
  const t = highlight([
    "const re = /[`'\"]/g; const z = `a ${q ? `b` : \"c\"} d`;",
    "onKeyDown={(e) => {",
    "  // `\\` then Enter: a new line",
    "  x();",
    "}}",
    "const SPLIT = new RegExp(`(${[",
    "  /```[^\\n]*\\n[\\s\\S]*?```/.source,",
    "].join(\"|\")})`);",
    "export function F() {}",
  ].join("\n"), "js");
  assert.ok(has(t[0], "str", "/[`'\"]/g"), kinds(t[0]).join(" "));
  assert.ok(has(t[2], "com", "// `\\` then Enter: a new line"));
  assert.ok(has(t[3], "", "  x();") || t[3].every((x) => x.t !== "str"));
  assert.ok(has(t[8], "op", "export") && has(t[8], "cmd", "F"), kinds(t[8]).join(" "));
  // The real case: shell.jsx's regex holds a backtick.
  const shell = readFileSync(join(ROOT, "frontend/src/lib/shell.jsx"), "utf8");
  const n = shell.split("\n").findIndex((l) => l.startsWith("export function ShellCode"));
  assert.ok(n > 0 && has(highlight(shell, "js")[n], "op", "export"));
});

test("CSS, JSON, markdown, shell, config and TypeScript", () => {
  const c = highlight("/* a\n b */\n@media (max-width: 640px) {\n  .dp-body { padding: 4px 8px; color: #B9DDB8; --x: var(--y); }\n}", "css");
  assert.deepEqual(c[1].map((x) => x.t), ["com"]);
  assert.ok(has(c[2], "op", "@media"));
  assert.ok(has(c[3], "flag", "padding") && has(c[3], "var", "4px") && has(c[3], "var", "#B9DDB8") && has(c[3], "flag", "--x"));
  assert.ok(!has(c[3], "flag", "dp-body"));
  const j = highlight('{"a": "b", "n": 1, "t": true}', "json")[0];
  assert.ok(has(j, "flag", '"a"') && has(j, "str", '"b"') && has(j, "var", "1") && has(j, "op", "true"));
  const m = highlight("# Title\n- **bold** and `code`\n```py\nx = 1 # not a heading\n```\nafter", "md");
  assert.ok(has(m[0], "cmd", "# Title") && has(m[1], "cmd", "**bold**") && has(m[1], "str", "`code`"));
  assert.ok(!m[3].some((x) => x.t === "cmd") && m[5].every((x) => x.t === ""));
  const s = highlight('if [ -n "$X" ]; then echo ${Y:-z} # hi', "sh")[0];
  assert.ok(has(s, "op", "if") && has(s, "str", '"$X"') && has(s, "var", "${Y:-z}") && has(s, "com", "# hi"));
  const k = highlight("[tool.ruff]\nline-length = 100\nname: x # c", "conf");
  assert.ok(has(k[0], "cmd", "[tool.ruff]") && has(k[2], "flag", "name") && has(k[2], "com", "# c"));
  const ts = highlight("const f = <T,>(x: T) => x; const y = 1", "ts")[0];
  assert.ok(!ts.some((x) => x.t === "op" && x.s.startsWith("<")) && has(ts, "var", "1"));
  assert.equal(langOf("a/b/Foo.JSX"), "js");
  assert.equal(langOf("backend/.env"), "conf");
  assert.equal(langOf("Makefile"), null);
  assert.deepEqual(highlight("plain\n", null), [[{ t: "", s: "plain" }], []]);
});

const DIFF = ["@@ -8,4 +8,5 @@", " import re", "-LIMIT = 40", "+LIMIT = 50", "+WIDTH = 100", " ", "\\ No newline at end of file"];

test("rows are numbered off the hunk header", () => {
  const rows = numbered(DIFF);
  assert.deepEqual(rows.map((r) => [r.kind, r.o ?? null, r.n ?? null]),
    [["hunk", null, null], ["ctx", 8, 8], ["del", 9, null], ["add", null, 9], ["add", null, 10],
     ["ctx", 10, 11], ["meta", null, null]]);
});

test("a comment anchors to its rows and finds them again, or says it can't", () => {
  const rows = numbered(DIFF);
  const one = anchor(rows, 4, 4);
  assert.deepEqual(one, { side: "new", start: 10, line: 10, quote: [{ kind: "add", text: "WIDTH = 100" }] });
  const span = anchor(rows, 4, 2);                       // either order, across a removal
  assert.equal(span.side, "new");
  assert.deepEqual([span.start, span.line], [9, 10]);
  assert.equal(span.quote.length, 3);
  const gone = anchor(rows, 2, 2);
  assert.deepEqual([gone.side, gone.line], ["old", 9]);
  assert.equal(anchor(rows, 0, 0), null);                // a hunk header isn't a line

  const c = { id: "1", path: "a.py", ...one, body: "Name it MAX_WIDTH." };
  assert.deepEqual([...place(rows, [c]).at.keys()], [4]);
  const moved = numbered(["@@ -8,2 +8,2 @@", " import re", "+WIDTH = 120"]);
  assert.deepEqual(place(moved, [c]).outdated, [c]);    // the line changed: outdated, not misplaced
});

test("the message names each line, quotes it, and carries the note", () => {
  const rows = numbered(DIFF);
  const c1 = { id: "1", path: "b/x.py", ...anchor(rows, 4, 4), body: "Name it MAX_WIDTH." };
  const c2 = { id: "2", path: "a/y.py", ...anchor(rows, 2, 2), body: "Why lower?" };
  const c3 = { id: "3", path: "b/x.py", ...anchor(rows, 2, 4), body: "Range." };
  assert.equal(where(c1), "b/x.py:10");
  assert.equal(where(c2), "a/y.py (removed, was line 9)");
  assert.equal(where(c3), "b/x.py:9-10");
  assert.equal(lines(c3), "lines 9-10");
  const msg = reviewMessage([c1, c2, c3], { note: " Otherwise good. ", branch: "kestrel/tab-0123abcd" });
  assert.equal(msg, [
    "Review comments on kestrel/tab-0123abcd: 3 comments. Each quotes the lines it's about.",
    "a/y.py (removed, was line 9)\n> -LIMIT = 40\nWhy lower?",
    "b/x.py:9-10\n> -LIMIT = 40\n> +LIMIT = 50\n> +WIDTH = 100\nRange.",
    "b/x.py:10\n> +WIDTH = 100\nName it MAX_WIDTH.",
    "Overall: Otherwise good.",
  ].join("\n\n"));
  assert.ok(reviewMessage([c1]).startsWith("Review comments on the diff: 1 comment."));
  const long = { ...c1, quote: Array.from({ length: 9 }, (_, i) => ({ kind: "ctx", text: `l${i}` })) };
  assert.ok(reviewMessage([long]).includes("> … 3 more"));
});

test("asking the tab to resolve or to commit", () => {
  const ask = conflictMessage([{ path: "a.py", kind: "contents" }, { path: "d.txt", kind: "modify/delete" }],
    { branch: "kestrel/tab-0123abcd", base: "main" });
  assert.ok(ask.startsWith("main has moved on and conflicts with kestrel/tab-0123abcd in a.py (contents), d.txt (modify/delete)."));
  assert.ok(ask.includes("`git rebase main`"));
  const many = Array.from({ length: 10 }, (_, i) => `f${i}`);
  assert.ok(commitMessage(many).includes("f0, f1, f2, f3, f4, f5, f6, f7 and 2 more"));
});
