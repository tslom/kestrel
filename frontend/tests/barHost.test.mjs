// lib/barHost.js's fold under node, against a fake bar whose width is the sum
// of what isn't folded (node has no layout). backend/tests/test_console_models.py runs it.
import { test } from "node:test";
import assert from "node:assert/strict";
import { CUT_ORDER, fitBar, folded } from "../src/lib/barHost.js";

// items: { key: px it frees when folded }; base: what never folds.
function fakeBar(width, base, items, strip = { scrollLeft: 30 }) {
  let cut = "";
  return {
    clientWidth: width,
    get scrollWidth() {
      const off = cut.split(" ");
      return base + Object.entries(items).filter(([k]) => !off.includes(k)).reduce((a, [, w]) => a + w, 0);
    },
    setAttribute(_, v) { cut = v; if (!v) strip.scrollLeft = 0; },
    querySelector: (sel) => (Object.keys(items).some((k) => sel === `[data-cut="${k}"]`) ? {} : null),
    querySelectorAll: () => [strip],
    strip,
  };
}

test("nothing folds when it fits", () => {
  assert.equal(fitBar(fakeBar(600, 300, { timeline: 60, full: 24 })), "");
});

test("folds lowest priority first, and only as far as it needs", () => {
  const bar = fakeBar(430, 250, { timeline: 60, view: 40, full: 24, review: 50, merge: 60 });
  // 484 wide: timeline alone frees enough
  assert.equal(fitBar(bar), "timeline");
  const tight = fakeBar(300, 250, { timeline: 60, view: 40, full: 24, review: 50, merge: 60 });
  // 184 to free: everything up to merge (the strip narrowing on the way)
  assert.equal(fitBar(tight), "timeline view full tabs review merge");
});

test("keys with nothing on the bar are skipped; layout keys always count", () => {
  const bar = fakeBar(100, 250, { mode: 10 });
  assert.equal(fitBar(bar), "tabs mode squeeze");
  assert.deepEqual(CUT_ORDER.slice(-2), ["mode", "squeeze"]);
});

test("the tab strip keeps its scroll through the measuring", () => {
  const bar = fakeBar(400, 250, { timeline: 60, view: 200 });
  fitBar(bar);
  assert.equal(bar.strip.scrollLeft, 30);
});

test("compactions never reach the menu; folded controls do", () => {
  assert.equal(folded("reset model-short tabs", "reset"), false);
  assert.equal(folded("reset review", "review"), true);
  assert.equal(folded("", "review"), false);
});
