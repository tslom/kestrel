// Not a style linter: each rule is a recurring correction made into a check.
// - you-might-not-need-an-effect is an error (derive in render, act in the handler, key, lift; a real sync says
//   so in its disable). useEffect count is a ratchet (scripts/lint-ratchet.mjs, lint-baseline.json).
// - raw fetch() outside lib/api.js: fetchJSON/postJSON handle errors, JSON and {detail} once.
// - raw setTimeout/clearTimeout outside lib/timers.js: a delay papers over an ordering bug.
// - rules-of-hooks and exhaustive-deps are errors; an eslint-disable says why, and an unused one is an error.
// The lint-on-edit hook (.claude/hooks/lint_on_edit.py) runs this on edited files and blocks on errors.
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
