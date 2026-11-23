// lib/interrupted.js under node's test runner: what a tab a quit cut off is
// told when you click Resume. backend/tests/test_console_work.py runs it.
import { test } from "node:test";
import assert from "node:assert/strict";
import { cutWhat, resumeText, interruptedTitle } from "../src/lib/interrupted.js";

const row = {
  turn: true,
  agents: [
    { id: "a01c03abf930a5e47", description: "Time-boxed local AI video test", kind: "local_agent" },
    { id: "a6f8645ee2812b1bc", description: "Analyze reference sites", kind: "local_agent" },
    { id: "b8aukrd2b", description: "until grep -q FRAME 179", kind: "local_bash" },
  ],
};

test("the row says what was cut off", () => {
  assert.equal(cutWhat(row), "a turn and 2 background agents and 1 background command");
  assert.equal(cutWhat({ turn: true, agents: [] }), "a turn");
  assert.equal(interruptedTitle(3), "3 tabs were interrupted when Kestrel quit");
});

test("the resume names every stopped agent and command and says to check before redoing", () => {
  const t = resumeText(row);
  assert.match(t, /cut your last turn off/);
  assert.match(t, /- background agent "Time-boxed local AI video test" \(a01c03abf930a5e47\)/);
  assert.match(t, /- background command "until grep -q FRAME 179" \(b8aukrd2b\)/);
  assert.match(t, /Before redoing anything, check what already landed/);
  assert.match(t, /git status and diff/);
});

test("background work alone, with the turn already over", () => {
  const t = resumeText({ turn: false, agents: [row.agents[0]] });
  assert.match(t, /background work you started was still running/);
  assert.doesNotMatch(t, /cut your last turn/);
});
