// lib/tabRest.js under node's test runner; backend/tests/test_tab_rest.py
// runs it, so pytest holds it.
import { test } from "node:test";
import assert from "node:assert/strict";
import { INACTIVE_MS, canRest, dueToWake, isResting, moveTab, pinnedFirst, restLine,
         snoozeChoices, stepTarget, toSettle } from "../src/lib/tabRest.js";
import { isLive } from "../src/lib/children.js";

const tab = (id, extra = {}) => ({ id, title: null, profile: "dev", lastActive: Date.now(), ...extra });
const ids = (list) => list.map((t) => t.id);

test("a resting tab isn't live, so it isn't mounted", () => {
  assert.equal(isLive(tab("a", { snoozedUntil: Date.now() + 1000 })), false);
  assert.equal(isLive(tab("a", { settled: true })), false);
  assert.equal(isLive(tab("a")), true);
  assert.equal(isResting(tab("a", { settled: true })), true);
});

test("a tab mid-turn or waiting on you can't rest", () => {
  assert.equal(canRest(tab("a"), { running: true }).ok, false);
  assert.equal(canRest(tab("a"), { waiting: { name: "Bash" } }).ok, false);
  assert.equal(canRest(tab("a", { queued: true }), null).ok, false);
  assert.equal(canRest(tab("a"), { running: false }).ok, true);
});

test("Later's choices: an hour, tonight before 8pm, tomorrow 8am, next Monday", () => {
  const wed = new Date(2026, 10, 18, 14, 30);   // a Wednesday afternoon
  const c = snoozeChoices(wed);
  assert.deepEqual(c.map((o) => o.label), ["1 hour", "Tonight", "Tomorrow", "Next week"]);
  assert.equal(new Date(c[1].until).getHours(), 20);
  const tom = new Date(c[2].until);
  assert.equal(tom.getDate(), 19); assert.equal(tom.getHours(), 8);
  const mon = new Date(c[3].until);
  assert.equal(mon.getDay(), 1); assert.equal(mon.getDate(), 23);
  // Late: no "Tonight". 2am is before the day's 4am rollover, so "tomorrow"
  // is this morning.
  assert.ok(!snoozeChoices(new Date(2026, 10, 18, 21)).some((o) => o.label === "Tonight"));
  const early = snoozeChoices(new Date(2026, 10, 19, 2)).find((o) => o.label === "Tomorrow");
  assert.equal(new Date(early.until).getDate(), 19);
  // Sunday: tomorrow is Monday, so "Next week" would say the same thing.
  assert.ok(!snoozeChoices(new Date(2026, 10, 22, 15)).some((o) => o.label === "Next week"));
});

test("snoozes wake when their time comes", () => {
  const now = 1_000_000;
  const tabs = [tab("a", { snoozedUntil: now - 1 }), tab("b", { snoozedUntil: now + 1 }), tab("c")];
  assert.deepEqual(dueToWake(tabs, now), ["a"]);
});

test("auto-settle: merged and looked away from, or a week idle", () => {
  const now = Date.now();
  const old = now - INACTIVE_MS - 1;
  const tabs = [
    tab("on", { merged: now, lastActive: old }),        // on screen
    tab("m", { merged: now }),
    tab("mu", { merged: now }),                          // unread: you haven't seen it land
    tab("idle", { lastActive: old }),
    tab("pin", { lastActive: old, pinned: true }),
    tab("busy", { merged: now }),
    tab("kid", { parent: "on", lastActive: old }),       // children close on their own
    tab("brief", { brief: "go", lastActive: old }),
    tab("fresh"),
    tab("done", { settled: true, lastActive: old }),
  ];
  const got = toSettle(tabs, { busy: { running: true } }, { active: "on", now, unread: new Set(["mu"]) });
  assert.deepEqual(got, [{ id: "m", why: "merged" }, { id: "idle", why: "inactive" }]);
});

test("pinned tabs come first and moves don't cross the pin line", () => {
  const tabs = [tab("p1", { pinned: true }), tab("p2", { pinned: true }), tab("a"), tab("b"), tab("c")];
  assert.deepEqual(ids(pinnedFirst([tab("x"), tab("p", { pinned: true })])), ["p", "x"]);
  assert.deepEqual(ids(moveTab(tabs, "c", "a")), ["p1", "p2", "c", "a", "b"]);
  assert.deepEqual(ids(moveTab(tabs, "a", null)), ["p1", "p2", "b", "c", "a"]);
  // An unpinned tab dropped before a pinned one stops at the line.
  assert.deepEqual(ids(moveTab(tabs, "b", "p1")), ["p1", "p2", "b", "a", "c"]);
  // A pinned one dropped among the rest stays last of the pinned.
  assert.deepEqual(ids(moveTab(tabs, "p1", "c")), ["p2", "p1", "a", "b", "c"]);
  assert.equal(moveTab(tabs, "a", "a"), tabs);
});

test("a step left or right is a `before` for moveTab", () => {
  const order = [tab("a"), tab("b"), tab("c")];
  assert.equal(stepTarget(order, "b", -1), "a");
  assert.equal(stepTarget(order, "a", -1), undefined);
  assert.equal(stepTarget(order, "a", 1), "c");
  assert.equal(stepTarget(order, "b", 1), null);
  assert.equal(stepTarget(order, "c", 1), undefined);
  assert.deepEqual(ids(moveTab(order, "a", stepTarget(order, "a", 1))), ["b", "a", "c"]);
  assert.deepEqual(ids(moveTab(order, "b", stepTarget(order, "b", 1))), ["a", "c", "b"]);
});

test("the rest list's line says when it's back, or why it settled", () => {
  const now = new Date(2026, 10, 18, 14).getTime();
  assert.match(restLine(tab("a", { snoozedUntil: new Date(2026, 10, 18, 20).getTime() }), now), /^back 8:00/);
  assert.equal(restLine(tab("a", { settled: true, settledWhy: "merged", settledAt: now }), now), "merged");
  assert.equal(restLine(tab("a", { settled: true, settledAt: now - 3 * 86400e3 }), now), "settled · 3d ago");
});
