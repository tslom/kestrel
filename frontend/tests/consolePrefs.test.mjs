// lib/consolePrefs.js under node's test runner; backend/tests/test_console_models.py
// runs it, so pytest holds it. `node --test frontend/tests/`.
import { test, beforeEach } from "node:test";
import assert from "node:assert/strict";

const store = new Map();
globalThis.localStorage = {
  getItem: (k) => (store.has(k) ? store.get(k) : null),
  setItem: (k, v) => store.set(k, String(v)),
  removeItem: (k) => store.delete(k),
};
const { cachedDefault, cachedModels, effortsFor, modelChoices, modelLabel,
        prettyModel, rememberDefault, rememberModels } = await import("../src/lib/consolePrefs.js");
const { refusedWhy } = await import("../src/lib/models.js");

// /api/models, trimmed (backend model_catalog.get_models): the live catalog's
// picker rows, and what the account in use may not run.
const INFO = { claude: {
  choices: [
    { value: "fable", resolved: "claude-fable-5-2", label: "Fable 5.2", family: "fable", efforts: [] },
    { value: "opus", resolved: "claude-opus-5-5", label: "Opus 5.5", family: "opus", efforts: [] },
    { value: "claude-fable-5-2", resolved: "claude-fable-5-2", label: "Fable 5.2", family: "fable", efforts: [] },
  ],
  blocked: { fable: "Fable requires usage credits" } } };

// The shape console.session_models sends, trimmed from a real CLI (2.1.328).
const CLI = [
  { value: "default", resolved: "claude-opus-5-5", label: "Default (recommended)", efforts: ["low", "high"] },
  { value: "opus", resolved: "claude-opus-5-5", label: "Opus 5.5", efforts: ["low", "medium", "high", "xhigh", "max"] },
  { value: "fable", resolved: "claude-fable-5-2", label: "Fable 5.2", efforts: ["low", "medium", "high", "xhigh", "max"] },
  { value: "sonnet", resolved: "claude-sonnet-5-5", label: "Sonnet 5.5", efforts: ["low", "medium", "high", "xhigh", "max"] },
  { value: "haiku", resolved: "claude-haiku-5-5", label: "Haiku 5.5", efforts: ["low", "medium", "high", "xhigh", "max"] },
  { value: "claude-haiku-4-5-20251001", resolved: "claude-haiku-4-5-20251001", label: "Haiku 4.5", efforts: [] },
  { value: "claude-opus-4-6", resolved: "claude-opus-4-6", label: "Opus 4.6", efforts: ["low", "medium", "high", "max"] },
];

beforeEach(() => store.clear());

test("before any session has reported, the backend's live catalog stands in", () => {
  store.set("models_info", JSON.stringify(INFO));
  assert.deepEqual(cachedModels("dev").map((m) => m.value), ["fable", "opus", "claude-fable-5-2"]);
});

test("a model the account in use can't run is marked with why, whichever name it goes by", () => {
  assert.equal(refusedWhy(INFO, { value: "fable" }), "Fable requires usage credits");
  assert.equal(refusedWhy(INFO, { value: "claude-fable-5-1" }), "Fable requires usage credits");
  assert.equal(refusedWhy(INFO, { value: "opus", resolved: "claude-opus-5-5" }), null);
  assert.equal(refusedWhy(INFO, { value: "default", resolved: "claude-fable-5-2" }), null);
  assert.equal(refusedWhy(null, { value: "fable" }), null);
  const { main, pinned } = modelChoices(CLI, "opus", "default", INFO);
  assert.deepEqual(main.find(([v]) => v === "fable"), ["fable", "fable · Fable 5.2", "Fable requires usage credits"]);
  assert.equal(main.find(([v]) => v === "opus")[2], null);
  assert.ok(pinned.every(([, , why]) => why === null));   // Haiku 4.5, Opus 4.6
});

test("the list is cached per profile, and a profile with none borrows another's", () => {
  rememberModels("dev", CLI);
  assert.equal(cachedModels("dev").length, CLI.length);
  assert.equal(cachedModels("study").length, CLI.length);   // borrowed
  rememberModels("study", CLI.slice(0, 3));
  assert.equal(cachedModels("study").length, 3);
  rememberModels("dev", []);                                 // an empty report changes nothing
  assert.equal(cachedModels("dev").length, CLI.length);
});

test("choices: default named by what it resolves to, aliases, pinned apart, the CLI's own default left out", () => {
  rememberDefault("dev", "opus");
  const { main, pinned } = modelChoices(CLI, cachedDefault("dev"), "default");
  assert.deepEqual(main, [
    ["default", "default · Opus 5.5", null], ["opus", "opus · Opus 5.5", null],
    ["fable", "fable · Fable 5.2", null], ["sonnet", "sonnet · Sonnet 5.5", null],
    ["haiku", "haiku · Haiku 5.5", null]]);
  assert.deepEqual(pinned, [["claude-haiku-4-5-20251001", "Haiku 4.5", null],
                            ["claude-opus-4-6", "Opus 4.6", null]]);
});

test("a stored model the list doesn't have is still shown", () => {
  const { main } = modelChoices(CLI, "sonnet", "mythos");
  assert.deepEqual(main.at(-1), ["mythos", "mythos"]);
  assert.equal(modelLabel([], "fable"), "fable");
  assert.equal(modelLabel(CLI, "default"), "default");
});

test("effort levels follow the model, default through what it resolves to", () => {
  assert.deepEqual(effortsFor(CLI, "default", "sonnet"), ["low", "medium", "high", "xhigh", "max"]);
  assert.deepEqual(effortsFor(CLI, "claude-opus-4-6"), ["low", "medium", "high", "max"]);
  assert.deepEqual(effortsFor(CLI, "claude-haiku-4-5-20251001"), []);
  assert.deepEqual(effortsFor(INFO.claude.choices, "fable"), []);
});

test("an id the list doesn't name still reads as a model", () => {
  assert.equal(prettyModel("claude-fable-5-2"), "Fable 5.2");
  assert.equal(prettyModel("claude-sonnet-5"), "Sonnet 5");
  assert.equal(prettyModel("claude-haiku-4-5-20251001"), "Haiku 4.5");
  assert.equal(prettyModel("gpt-x"), "gpt-x");
});
