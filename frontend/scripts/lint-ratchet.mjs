// `npm run lint:ratchet` (and backend/tests/test_lint.py, so pytest holds it):
// fails on any ESLint error, or if src/ has more useEffect calls than
// lint-baseline.json allows. The effect rules themselves are errors
// (eslint.config.js); the count is held too, so a new effect that does
// sync with something outside React still has to be argued for. Any
// warnings are printed as counts. When the count drops, lower the baseline
// in the same commit so it can't creep back.
// (setTimeout was ratcheted here too, from 34 on 11-22, until it reached 0
// the same day; it's an ESLint error now, eslint.config.js.)
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join, relative } from "node:path";
import { ESLint } from "eslint";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const baseline = JSON.parse(readFileSync(join(root, "lint-baseline.json"), "utf8"));

// Each ratcheted call, with what to do instead when its count goes up.
const RATCHETS = {
  useEffect: "Derive it during render, handle it in the event, or key the component "
    + "(react.dev/learn/you-might-not-need-an-effect). Raise lint-baseline.json only "
    + "for an effect that syncs with something outside React.",
};

// Counted off the AST by a throwaway rule, so a comment saying "useEffect("
// isn't an effect and `React.useEffect(` is.
const counter = {
  rules: {
    calls: {
      meta: { type: "problem", schema: [] },
      create: (context) => ({
        CallExpression(node) {
          const c = node.callee;
          const name = c.type === "Identifier" ? c.name
            : c.type === "MemberExpression" && !c.computed ? c.property.name : null;
          if (Object.hasOwn(RATCHETS, name)) context.report({ node, message: name });
        },
      }),
    },
  },
};

const eslint = new ESLint({
  cwd: root,
  overrideConfig: [{ files: ["src/**/*.{js,jsx}"], plugins: { count: counter },
                     rules: { "count/calls": "warn" } }],
});
const results = await eslint.lintFiles(["src"]);

const counts = Object.fromEntries(Object.keys(RATCHETS).map((k) => [k, 0]));
const errors = [];
const warnings = {};
for (const r of results) {
  for (const m of r.messages) {
    if (m.ruleId === "count/calls") { counts[m.message] += 1; continue; }
    if (m.severity === 2) errors.push(`${relative(root, r.filePath)}:${m.line} ${m.ruleId}: ${m.message}`);
    else warnings[m.ruleId] = (warnings[m.ruleId] || 0) + 1;
  }
}

const lines = Object.keys(RATCHETS).map((k) => `${k} calls: ${counts[k]} (baseline ${baseline[k]})`);
for (const [rule, n] of Object.entries(warnings).sort((a, b) => b[1] - a[1])) lines.push(`  warn ${n} ${rule}`);
let failed = false;
if (errors.length) {
  failed = true;
  lines.push(`${errors.length} ESLint error(s):`, ...errors.map((e) => `  ${e}`));
}
for (const [name, why] of Object.entries(RATCHETS)) {
  const n = counts[name], base = baseline[name];
  if (n > base) {
    failed = true;
    lines.push(`${name} went up by ${n - base}. ${why}`);
  } else if (n < base) {
    lines.push(`${name} went down: set "${name}": ${n} in lint-baseline.json.`);
  }
}
console.log(lines.join("\n"));
process.exit(failed ? 1 : 0);
