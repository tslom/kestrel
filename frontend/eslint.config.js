// Not a style linter. Each rule is a correction that kept coming back, made
// into a check (11-17, after the "All Code Is Slop" report):
// - you-might-not-need-an-effect is an error: an effect that derives state,
//   chains setters or relays an event is the pattern the next tab copies.
//   Derive it in render, do it in the handler, key the component, or lift the
//   state; an effect that really syncs with something outside React (the
//   socket, a canvas, a timer) says so in its disable's reason. The count of
//   useEffect calls is also a ratchet: scripts/lint-ratchet.mjs fails if it
//   goes up (lint-baseline.json).
// - raw fetch() outside lib/api.js is an error: fetchJSON/postJSON are where
//   errors, JSON and the backend's {detail} get handled once.
// - raw setTimeout/clearTimeout outside lib/timers.js is an error (11-22):
//   a delay is how an agent papers over an ordering bug, so a wait on time
//   goes through a named helper, and a wait on anything else waits on that.
// - rules-of-hooks and exhaustive-deps are errors; a deps list that's
//   deliberately short says why in its disable.
// - an eslint-disable says why (`-- reason`), and one that disables nothing
//   is an error (reportUnusedDisableDirectives), so suppressions can't pile up
//   as unexplained clutter or outlive the code they excused.
// The lint-on-edit hook (.claude/hooks/lint_on_edit.py) runs this on every
// file a Claude Code session edits and blocks on errors, not warnings.
import globals from "globals";
import reactHooks from "eslint-plugin-react-hooks";
import noEffect from "eslint-plugin-react-you-might-not-need-an-effect";

const TIMER_FIX = "is banned outside lib/timers.js. If it waits for state, a fetch or a render to "
  + "land, wait on that instead (await it, act in the callback or effect that follows it, or "
  + "derive it). For a real wall-clock delay use lib/timers.js: useDebounce (search box), "
  + "useTimeout (flash, toast, backoff, poll), timer() outside React, batch (coalesce a burst), "
  + "withTimeout (a promise that may never settle).";
const timerGlobals = ["setTimeout", "clearTimeout"].map((name) => ({ name, message: `${name} ${TIMER_FIX}` }));
const timerProps = ["window", "globalThis", "self"].flatMap((object) =>
  ["setTimeout", "clearTimeout"].map((property) => ({ object, property, message: `${property} ${TIMER_FIX}` })));

// eslint-comments/require-description, without the dependency.
const local = {
  rules: {
    "disable-needs-reason": {
      meta: { type: "problem", schema: [] },
      create: (context) => ({
        Program() {
          for (const c of context.sourceCode.getAllComments()) {
            if (/^\s*eslint-disable(-next-line|-line)?\b/.test(c.value) && !/\s--\s*\S/.test(c.value)) {
              context.report({ loc: c.loc, message: "Say why after the rule: `eslint-disable-next-line rule -- reason`. "
                + "If the rule no longer fires here, delete the directive." });
            }
          }
        },
      }),
    },
  },
};

const effectRules = Object.fromEntries(
  Object.keys(noEffect.rules).map((r) => [`react-you-might-not-need-an-effect/${r}`, "error"]));

export default [
  { ignores: ["dist/**", "node_modules/**", "public/**"] },
  {
    files: ["src/**/*.{js,jsx}"],
    languageOptions: {
      ecmaVersion: "latest",
      sourceType: "module",
      parserOptions: { ecmaFeatures: { jsx: true } },
      globals: { ...globals.browser },
    },
    linterOptions: { reportUnusedDisableDirectives: "error" },
    plugins: {
      local,
      "react-hooks": reactHooks,
      "react-you-might-not-need-an-effect": noEffect,
    },
    rules: {
      ...effectRules,
      "local/disable-needs-reason": "error",
      "react-hooks/rules-of-hooks": "error",
      "react-hooks/exhaustive-deps": "error",
      "no-restricted-globals": ["error", ...timerGlobals],
      "no-restricted-properties": ["error", ...timerProps],
      "no-restricted-syntax": ["error",
        { selector: "CallExpression[callee.name='fetch']",
          message: "Use fetchJSON/postJSON from lib/api.js; raw fetch skips its error and {detail} handling." },
        { selector: "CallExpression[callee.object.name=/^(window|globalThis)$/][callee.property.name='fetch']",
          message: "Use fetchJSON/postJSON from lib/api.js; raw fetch skips its error and {detail} handling." },
      ],
    },
  },
  { files: ["src/lib/api.js"], rules: { "no-restricted-syntax": "off" } },
  { files: ["src/lib/timers.js"], rules: { "no-restricted-globals": "off", "no-restricted-properties": "off" } },
];
