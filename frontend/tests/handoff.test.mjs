// lib/handoff.js under node's test runner; backend/tests/test_handoff_report.py
// runs it, so pytest holds it. `node --test frontend/tests/`.
import { test } from "node:test";
import assert from "node:assert/strict";
import { REPORT_LIMIT, briefVerdict, buildReport, clip, lastTurn } from "../src/lib/handoff.js";

const user = (text) => ({ kind: "user", text });
const say = (text) => ({ kind: "text", text });
const tool = () => ({ kind: "tool", name: "Bash" });
const result = (status) => ({ kind: "result", text: `${status} · 12.0s · 3k in` });
const diff = (path, a, d) => ({ kind: "diff", files: [{ path, additions: a, deletions: d }] });

test("an API error mid-work holds the report (11-17's DNS outage)", () => {
  const log = [user("brief"), say("Now wiring it into the console."), tool(), result("error")];
  assert.equal(briefVerdict(lastTurn(log)), "error");
});

test("stopped, queued and a background subagent all hold it", () => {
  const log = [user("brief"), say("Verifying in WebKit…"), result("stopped")];
  assert.equal(briefVerdict(lastTurn(log)), "stopped");
  const done = [user("brief"), say("Done."), result("done")];
  assert.equal(briefVerdict(lastTurn(done), { queued: true }), "queued");
  assert.equal(briefVerdict(lastTurn(done), { agents: true }), "agents");
});

test("a dropped socket (no result) holds it, even after an earlier result", () => {
  assert.equal(briefVerdict(lastTurn([user("brief"), say("Working"), tool()])), "cut off");
  // A woken turn has no user line: the earlier "done" doesn't close it.
  const woke = [user("brief"), say("Waiting on the agent."), result("done"),
                { kind: "status", text: "A background task finished" }, say("Merging"), tool()];
  assert.equal(briefVerdict(lastTurn(woke)), "cut off");
});

test("window lines after the result don't count as more work", () => {
  const log = [user("brief"), say("All done."), result("done"), { kind: "status", text: "Resumed" },
               { kind: "help" }];
  assert.equal(briefVerdict(lastTurn(log)), "report");
});

test("the report after a resume is the final summary, with every turn's files", () => {
  const log = [user("brief"), say("Now wiring it in."), diff("a.py", 400, 0), result("error"),
               user("continue"), say("Halfway."), diff("a.py", 5, 2), diff("b.py", 1, 1),
               say("Finished: wired in, 12 tests pass."), result("done")];
  assert.equal(briefVerdict(lastTurn(log)), "report");
  const r = buildReport(log, 0, { from: "t1", label: "dev · x" });
  assert.equal(r.status, "done");
  assert.ok(r.text.startsWith("Finished: wired in, 12 tests pass."));
  assert.ok(r.text.includes("a.py (+405 −2), b.py (+1 −1)"));
  assert.ok(!r.text.includes("Now wiring"));
});

test("a long summary is delivered whole up to the limit", () => {
  const long = "word ".repeat(1600).trim();   // 8k: past the old 4k cut
  const r = buildReport([user("b"), say(long), result("done")], 0, { from: "t1", label: "general" });
  assert.equal(r.text, long);
  assert.ok(REPORT_LIMIT >= 8000);
});

test("past the limit it cuts the middle at breaks, keeping both ends", () => {
  const para = (n) => `Paragraph ${n}. ${"x".repeat(900)}`;
  const text = "Done: shipped it.\n\n" + Array.from({ length: 20 }, (_, i) => para(i)).join("\n\n")
    + "\n\n## For you to decide\n- **Pick one.**";
  const out = clip(text, 5000, "the last message in tab t1");
  assert.ok(out.length < 5300, `${out.length}`);
  const [head, rest] = out.split("\n\n[… ");
  const tail = rest.split(".]\n\n")[1];
  assert.ok(head.startsWith("Done: shipped it.") && head.endsWith("x"), "head ends on a whole paragraph");
  assert.ok(text.startsWith(head) && text.endsWith(tail));
  assert.ok(tail.endsWith("## For you to decide\n- **Pick one.**"), "the decisions survive");
  assert.match(out, /\[… \d+ characters cut from the middle of a \d+-character message\. The full text is the last message in tab t1\.\]/);
  assert.equal(head.length + tail.length + Number(rest.match(/^\d+/)[0]) + 4 >= text.length - 4, true);
  // No paragraph breaks: still never inside a word.
  const words = clip("alpha ".repeat(2000).trim(), 1000, "x");
  const [wh, wr] = words.split("\n\n[… ");
  assert.ok(wh.endsWith("alpha") && wr.split(".]\n\n")[1].startsWith("alpha"));
});

test("the pointer names the session when there is one", () => {
  const r = buildReport([user("b"), say("y ".repeat(20000)), result("done")], 0,
                        { from: "t9", label: "general", sessionId: "abc-123" });
  assert.ok(r.text.includes("tab t9 (session abc-123)"));
  assert.ok(r.text.length < REPORT_LIMIT + 200);
});
