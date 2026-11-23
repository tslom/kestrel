// lib/autoResume.js under node; backend/tests/test_console_models.py runs it.
import { test, beforeEach } from "node:test";
import assert from "node:assert/strict";

const store = new Map();
globalThis.localStorage = {
  getItem: (k) => (store.has(k) ? store.get(k) : null),
  setItem: (k, v) => store.set(k, String(v)),
  removeItem: (k) => store.delete(k),
};
const { AUTO_KEY, MIN_GAP_MS, autoResumeOn, eventWindows, gapMs, jitterMs, planResume, readAuto,
        windowState, writeAuto } = await import("../src/lib/autoResume.js");

beforeEach(() => store.clear());

const NOW = 1_800_000_000_000;
const min = 60_000;
const at = (ms) => ms / 1000;   // windows carry unix seconds
const shut5h = (resetMs) => ({ key: "five_hour", status: "rejected", resets_at: at(resetMs) });

test("on by default, off only when Settings says so", () => {
  assert.equal(autoResumeOn(), true);
  localStorage.setItem(AUTO_KEY, "0");
  assert.equal(autoResumeOn(), false);
  localStorage.setItem(AUTO_KEY, "1");
  assert.equal(autoResumeOn(), true);
});

test("a shut window resumes a tab just after its reset, each tab at its own offset", () => {
  const p = planResume({ limited: { at: NOW - 30 * min }, windows: [shut5h(NOW + 20 * min)], tab: "abc", now: NOW });
  assert.equal(p.resetsAt, NOW + 20 * min);
  assert.equal(p.at, NOW + 20 * min + jitterMs("abc"));
  assert.ok(jitterMs("abc") >= 20_000 && jitterMs("abc") < 140_000);
  assert.notEqual(jitterMs("abc"), jitterMs("abd"));
});

test("the plan doesn't move as the clock reaches it", () => {
  const args = { limited: { at: NOW }, windows: [], tab: "t1" };   // open, no reset known
  const a = planResume({ ...args, now: NOW }).at;
  const b = planResume({ ...args, now: a + 1000 }).at;
  assert.equal(a, b);
  assert.equal(a, NOW + MIN_GAP_MS + jitterMs("t1"));
});

test("never: off, not limited, or the user said don't (or stopped it)", () => {
  const windows = [shut5h(NOW + min)];
  assert.equal(planResume({ limited: { at: NOW }, windows, tab: "t", now: NOW, on: false }), null);
  assert.equal(planResume({ limited: null, windows, tab: "t", now: NOW }), null);
  assert.equal(planResume({ limited: { at: NOW, manual: true }, windows, tab: "t", now: NOW }), null);
});

test("refused again: waits for the next reset, never sooner than a doubling gap", () => {
  // tried 2 minutes ago, cut off again; the window says it reset a minute ago
  const auto = { last: NOW - 2 * min, tries: 1 };
  const p = planResume({ limited: { at: NOW - min }, windows: [shut5h(NOW - min)], tab: "t", auto, now: NOW });
  assert.equal(p.at, NOW - 2 * min + MIN_GAP_MS);
  assert.equal(gapMs(1), 5 * min);
  assert.equal(gapMs(2), 10 * min);
  assert.equal(gapMs(3), 20 * min);
  assert.equal(gapMs(9), 60 * min);
  const p3 = planResume({ limited: { at: NOW }, windows: [], tab: "t", auto: { last: NOW, tries: 3 }, now: NOW });
  assert.equal(p3.at, NOW + 20 * min);
});

test("the tab's own rate_limit events count, and a per-model week binds only that model", () => {
  const events = eventWindows({ seven_day_fable: { status: "rejected", resets_at: at(NOW + 3 * 86400_000) },
                               five_hour: { status: "allowed", utilization: 0.4, resets_at: at(NOW + min) } });
  assert.deepEqual(windowState(events, { model: "claude-sonnet-5-5", now: NOW }), { open: true, resetsAt: null });
  assert.equal(windowState(events, { model: "claude-fable-5-2", now: NOW }).open, false);
  // a window past its reset is open, whatever it last said
  assert.equal(windowState([shut5h(NOW - 1000)], { now: NOW }).open, true);
  // shut with no time said: not open, reset unknown
  assert.deepEqual(windowState([{ key: "five_hour", status: "rejected" }], { now: NOW }), { open: false, resetsAt: null });
});

test("shut with no reset known: try an hour after the cut", () => {
  const p = planResume({ limited: { at: NOW }, windows: [{ key: "five_hour", status: "rejected" }], tab: "t", now: NOW });
  assert.equal(p.at, NOW + 60 * min);
});

test("the per-tab record survives a reload and clears", () => {
  assert.deepEqual(readAuto("t"), { last: 0, tries: 0 });
  writeAuto("t", { last: NOW, tries: 2 });
  assert.deepEqual(readAuto("t"), { last: NOW, tries: 2 });
  writeAuto("t", null);
  assert.deepEqual(readAuto("t"), { last: 0, tries: 0 });
});
