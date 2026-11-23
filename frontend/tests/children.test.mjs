// lib/children.js under node's test runner; backend/tests/test_child_tabs.py
// runs it, so pytest holds it. `node --test frontend/tests/`.
import { test } from "node:test";
import assert from "node:assert/strict";
import { admit, busyCount, childState, landing, needsLine, nextQueued, queuePlace,
         summary, tabOrder } from "../src/lib/children.js";

const caps = { maxWorking: 5, maxTabs: 10 };
const tab = (id, extra = {}) => ({ id, title: null, profile: "dev", ...extra });

test("children group under their root, in the order they were opened", () => {
  const tabs = [tab("a"), tab("b"), tab("c1", { parent: "a" }), tab("d1", { parent: "b" }),
                tab("c2", { parent: "a" }), tab("g", { parent: "c1" }), tab("o", { parent: "gone" })];
  const order = tabOrder(tabs).map(({ tab: t, children }) => [t.id, children.map((c) => c.id)]);
  // A grandchild groups with the root; a closed parent's child is top level.
  assert.deepEqual(order, [["a", ["c1", "c2", "g"]], ["b", ["d1"]], ["o", []]]);
});

test("each child's state, and the parent's badge", () => {
  const kids = [tab("w"), tab("s", { brief: "go" }), tab("q", { queued: true }),
                tab("n"), tab("e", { held: "error" }), tab("d", { done: true }), tab("x", { needs: "conflict" })];
  const turns = { w: { running: true }, n: { waiting: { question: true } } };
  assert.deepEqual(kids.map((k) => childState(k, turns[k.id])),
    ["working", "working", "queued", "needs", "needs", "done", "needs"]);
  assert.equal(summary(kids, turns).text, "2 running · 3 need you · 1 queued");
  assert.equal(summary([tab("d", { done: true }), tab("e", { done: true })], {}).text, "2 done");
  assert.equal(summary([tab("n", { needs: "x" })], {}).text, "1 needs you");
  // A held "agents" wait is still work going on, not a dead turn.
  assert.equal(childState(tab("h", { held: "agents" }), {}), "working");
});

test("open_tab past the working cap queues, in order, and the queue is bounded", () => {
  const five = ["1", "2", "3", "4", "5"].map((id) => tab(id));
  const turns = Object.fromEntries(five.map((t) => [t.id, { running: true }]));
  // The asking tab doesn't count against the new one.
  assert.deepEqual(admit(five, turns, { from: "5", ...caps }), { start: true });
  const parent = tab("p");
  const all = [parent, ...five];
  assert.deepEqual(admit(all, turns, { from: "p", ...caps }), { queue: 1 });
  const withQ = [...all, tab("q1", { queued: true, queuedAt: 1 })];
  assert.deepEqual(admit(withQ, turns, { from: "p", ...caps }), { queue: 2 });
  // Something queued: a free slot still goes to it first, not the newcomer.
  const freed = { ...turns, 1: { running: false } };
  assert.deepEqual(admit(withQ, freed, { from: "p", ...caps }), { queue: 2 });
  const full = [...all, ...Array.from({ length: 8 }, (_, i) => tab(`q${i}`, { queued: true }))];
  assert.ok(admit(full, turns, { from: "p", ...caps }).refuse);
  // Ten tabs open is the old refusal: the model can close done ones.
  const ten = Array.from({ length: 10 }, (_, i) => tab(`t${i}`));
  assert.match(admit(ten, {}, { from: "t0", ...caps }).refuse, /10 tabs are open/);
});

test("a queued tab starts when a slot frees, first asked first", () => {
  const tabs = [tab("1"), tab("2"), tab("3"), tab("4"), tab("5"),
                tab("late", { queued: true, queuedAt: 20 }), tab("early", { queued: true, queuedAt: 10 })];
  const running = Object.fromEntries(["1", "2", "3", "4", "5"].map((id) => [id, { running: true }]));
  assert.equal(nextQueued(tabs, running, caps), null);
  assert.equal(nextQueued(tabs, { ...running, 3: { running: false } }, caps), "early");
  assert.equal(queuePlace(tabs, "late"), 2);
  // A tab about to start on its brief holds its slot, so two frees don't
  // start the same queue twice over.
  const starting = tabs.map((t) => (t.id === "3" ? { ...t, brief: "go" } : t));
  assert.equal(nextQueued(starting, { ...running, 3: { running: false } }, caps), null);
  assert.equal(busyCount(starting, { ...running, 3: { running: false } }), 5);
});

test("landing: merged and clean closes it, anything left or said keeps it open", () => {
  const clean = { ahead: 0, dirty: [], branch: "kestrel/tab-1", base: "main" };
  assert.deepEqual(landing({ text: "Done, merged and pushed.", status: clean }), { done: true, why: null });
  assert.deepEqual(landing({ text: "Done." }), { done: true, why: null });
  assert.equal(landing({ text: "x", status: { ...clean, ahead: 2 } }).why,
    "2 commits on kestrel/tab-1 not on main: it didn't land");
  assert.equal(landing({ text: "x", status: { ...clean, ahead: 1, dirty: ["a"] } }).why,
    "1 commit on kestrel/tab-1 not on main and 1 uncommitted file in its worktree: it didn't land");
  assert.equal(landing({ text: "All good.\n\n**Needs you:** the rebase conflicts in db.py", status: clean }).why,
    "the rebase conflicts in db.py");
});

test("needsLine reads the line SPAWNED_APPEND asks for, and nothing looser", () => {
  assert.equal(needsLine("Shipped.\nNeeds you: pick a colour"), "pick a colour");
  assert.equal(needsLine("- needs you: a failing check"), "a failing check");
  assert.equal(needsLine("Nobody needs you to do anything."), null);
  assert.equal(needsLine(""), null);
});

test("follow-ups go only to the tab's own children, and wait their turn", async () => {
  const { followUp } = await import("../src/lib/children.js");
  const tabs = [tab("p"), tab("c", { parent: "p" }), tab("d", { parent: "p", done: true }),
                tab("q", { parent: "p", queued: true }), tab("o", { parent: "x" }),
                tab("b", { parent: "p", brief: "pending" })];
  const at = (id, turns = {}) => followUp(tabs, turns, { id, from: "p", ...caps });
  assert.deepEqual(at("c"), { start: true });
  assert.deepEqual(at("c", { c: { running: true } }), { start: true });   // taken at its turn's end
  assert.deepEqual(at("d"), { start: true });                             // a done one reopens
  assert.match(at("o").refuse, /wasn't opened by this one/);
  assert.match(at("q").refuse, /still queued/);
  assert.match(at("b").refuse, /hasn't taken its last message/);
  assert.match(at("nope").refuse, /no tab nope/);
  const busy = Object.fromEntries(["w1", "w2", "w3", "w4", "w5"].map((id) => [id, { running: true }]));
  const full = [...tabs, ...Object.keys(busy).map((id) => tab(id))];
  assert.deepEqual(followUp(full, busy, { id: "d", from: "p", ...caps }), { queue: true });
});
