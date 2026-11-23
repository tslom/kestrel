// lib/paths.js under node's test runner: which spans of a reply get asked
// about as files. backend/tests/test_file_links.py runs it, so pytest holds it.
import { test } from "node:test";
import assert from "node:assert/strict";
import { FILE_PATH_SRC, looksLikePath, lineless, isVaultNote } from "../src/lib/paths.js";

const found = (text) => [...text.matchAll(new RegExp(FILE_PATH_SRC, "g"))].map((m) => m[1]);

test("the forms from 11-19's replies, in prose", () => {
  assert.deepEqual(found("Saved ~/Projects/acme-site/.visual/uiux-2026-11-19/after-desktop-street.png."),
    ["~/Projects/acme-site/.visual/uiux-2026-11-19/after-desktop-street.png"]);
  assert.deepEqual(found("see /Users/me/Projects/acme-shop/.claude/worktrees/tab-0a1b2c3d/.shots/home-after-1440.png, then"),
    ["/Users/me/Projects/acme-shop/.claude/worktrees/tab-0a1b2c3d/.shots/home-after-1440.png"]);
  assert.deepEqual(found("It's at ~/Desktop/Sam_Rivera_Resume_Nov2026.pdf"),
    ["~/Desktop/Sam_Rivera_Resume_Nov2026.pdf"]);
  assert.deepEqual(found("(01-Projects/internships-2027/tailored/acme-labs/cv.pdf)"),
    ["01-Projects/internships-2027/tailored/acme-labs/cv.pdf"]);
  assert.deepEqual(found("wrote out/POSTING-SCHEDULE.md!"), ["out/POSTING-SCHEDULE.md"]);
});

test("prose that only looks pathy stays prose", () => {
  for (const s of ["km/s", "and/or", "1.5/2.0", "a /etc thing", "https://x.com/a/b.png",
                   "TCP/IP", "w/ the", "~/../outside.txt"])
    assert.deepEqual(found(s), [], s);
});

test("whole spans: code and link targets", () => {
  for (const p of ["~/Desktop/Sam_Rivera_Resume_Nov2026.pdf", "/Users/me/x/y.png",
                   "01-Projects/My Folder/notes.pdf", "out/POSTING-SCHEDULE.md", "~/Projects/acme-site/",
                   "backend/app/files.py:42"])
    assert.ok(looksLikePath(p), p);
  for (const p of ["npm run dev", "/api", "-rf /", "https://a.b/c.png", "mailto:a@b.c",
                   "README", "a|b/c.txt", "foo.py"])
    assert.ok(!looksLikePath(p), p);
});

test("a line suffix is the reader's, and a bare .md outside a project is a note", () => {
  assert.equal(lineless("src/x.py:42:7"), "src/x.py");
  assert.ok(isVaultNote("01-Projects/plan.md", []));
  assert.ok(!isVaultNote("out/POSTING-SCHEDULE.md", ["/Users/me/Projects/x"]));
  assert.ok(!isVaultNote("~/notes/plan.md", []));
  assert.ok(!isVaultNote("01-Projects/cv.pdf", []));
});
