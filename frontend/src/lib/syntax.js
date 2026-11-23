// Syntax colour for the diff pane (console/DiffView.jsx). Like shell.jsx and
// jscode.jsx, a tokenizer rather than a parser: it only has to know where
// strings, comments and words start and stop, and it hands back their sh-*
// kinds so a diff reads in the same palette as a run_script block. Unlike
// them it carries state from line to line (a docstring, a block comment, a
// template literal, a JSX tag over three lines, the text inside an element),
// which is why the pane feeds it the whole file when it can (GET
// /api/console/blob): a hunk that starts halfway down a docstring has no way
// to know its lines are prose.
//
// Kinds: op = keyword or tag, str = string, com = comment, var = number,
// cmd = a name being defined (or a heading), flag = key, property, decorator
// or attribute, "" = plain. Joined back up, a line's tokens are exactly the
// line; the pane checks that before trusting them.

const words = (s) => new Set(s.split(" "));

const PY_KW = words("and as assert async await break class continue def del elif else except "
  + "finally for from global if import in is lambda nonlocal not or pass raise return try while "
  + "with yield True False None");
const JS_KW = words("as async await break case catch class const continue debugger default delete "
  + "do else export extends finally for from function if import in instanceof let new of return "
  + "static super switch this throw try typeof var void while with yield null undefined true false");
const SH_KW = words("if then else elif fi for while until do done case esac in function return "
  + "local export readonly declare unset shift exit");
const SQL_KW = words("select from where and or not insert into values update set delete create "
  + "table index view drop alter add column primary key foreign references unique default null is "
  + "in like glob between join left right inner outer cross on as group by order having limit "
  + "offset union all distinct case when then else end exists begin commit rollback transaction "
  + "pragma if replace returning with recursive asc desc integer text real blob conflict do nothing");

const JS = { line: "//", block: ["/*", "*/"], quotes: "'\"", tmpl: true, regex: true, kw: JS_KW,
             defs: words("function class") };
const LANGS = {
  py: { line: "#", quotes: "'\"", triple: true, prefixes: true, kw: PY_KW, defs: words("def class"), deco: true },
  js: { ...JS, jsx: true },
  // TypeScript: `<T>(x)` is a generic, not a tag, outside a .tsx.
  ts: JS,
  css: { block: ["/*", "*/"], quotes: "'\"", css: true },
  json: { quotes: '"', kw: words("true false null"), jsonKeys: true },
  sh: { line: "#", quotes: "'\"", kw: SH_KW, vars: true, multiQuote: true },
  sql: { line: "--", block: ["/*", "*/"], quotes: "'", kw: SQL_KW, ci: true },
  conf: { line: "#", quotes: "'\"", triple: true, kw: words("true false yes no on off null"), keys: true },
  html: { block: ["<!--", "-->"], quotes: "'\"", tags: true },
  md: { md: true },
};

const EXT = {
  py: "py", pyi: "py", js: "js", jsx: "js", mjs: "js", cjs: "js", ts: "ts", mts: "ts", tsx: "js",
  css: "css", json: "json", sh: "sh", bash: "sh", zsh: "sh", sql: "sql", toml: "conf",
  yaml: "conf", yml: "conf", ini: "conf", cfg: "conf", env: "conf", html: "html", htm: "html",
  xml: "html", svg: "html", md: "md", markdown: "md",
};

/** The language family for a path, or null for one to leave plain. */
export function langOf(path) {
  const name = (path || "").split("/").pop().toLowerCase();
  if (name === ".env" || name.startsWith(".env.")) return "conf";
  const dot = name.lastIndexOf(".");
  return dot > 0 ? EXT[name.slice(dot + 1)] || null : null;
}

const IDENT = /[A-Za-z_$][\w$]*/y;
const ATTR = /[A-Za-z_$][\w$:-]*/y;      // aria-label, xlink:href
const CSS_IDENT = /-?-?[A-Za-z_][\w-]*/y;
const NUMBER = /(?:0[xX][\da-fA-F_]+|\d[\d_]*(?:\.\d+)?(?:[eE][+-]?\d+)?)n?/y;
const CSS_NUMBER = /-?(?:\d+\.?\d*|\.\d+)(?:%|[a-zA-Z]+)?/y;
const HEX_COLOUR = /#[\da-fA-F]{3,8}\b/y;
const PY_PREFIX = /[rRbBuUfF]{1,2}(?=['"])/y;
const SH_VAR = /\$(?:\{[^}]*\}?|[A-Za-z_]\w*|[0-9@#?$!*-])/y;
const TAG = /<\/?[A-Za-z][\w.:-]*/y;
const AT_RULE = /@[\w-]+/y;
const DECORATOR = /@[\w.]+/y;
const WORD_CHAR = /[\w$]/;
// After one of these a JSX tag or a regex may start; after a name, a number
// or a `)` it's a less-than or a divide.
const EXPR_START = "(,=?:{}[&|>;!";

function at(re, line, i) {
  re.lastIndex = i;
  const m = re.exec(line);
  return m ? m[0] : null;
}

// Past the end of a string or comment that ends with `end`, or -1.
function close(line, from, { end, esc }) {
  for (let k = from; k < line.length; k++) {
    if (esc && line[k] === "\\") { k++; continue; }
    if (line.startsWith(end, k)) return k + end.length;
  }
  return -1;
}

// A template literal from `k`, with `st.depth` the ${…} braces it's inside
// (0: its text). Past the closing backtick, or -1 with `st` left for the
// next line. A ${…} can hold strings and templates of its own.
function template(line, k, st) {
  while (k < line.length) {
    const ch = line[k];
    if (st.depth === 0) {
      if (ch === "\\") k += 2;
      else if (ch === "`") return k + 1;
      else if (ch === "$" && line[k + 1] === "{") { st.depth = 1; k += 2; }
      else k++;
      continue;
    }
    if (ch === "'" || ch === '"') {
      const e = close(line, k + 1, { end: ch, esc: true });
      k = e < 0 ? k + 1 : e;
    } else if (ch === "`") {
      const e = template(line, k + 1, { depth: 0 });
      k = e < 0 ? line.length : e;
    } else {
      if (ch === "{") st.depth++;
      else if (ch === "}") st.depth--;
      k++;
    }
  }
  return -1;
}

function markdown(lines) {
  let fence = null;
  return lines.map((line) => {
    const toks = [];
    const push = (t, s) => { if (s) toks.push({ t, s }); };
    const lead = /^\s*(`{3,}|~{3,})/.exec(line);
    if (fence) {
      if (lead && lead[1][0] === fence[0] && lead[1].length >= fence.length) fence = null;
      push(fence ? "" : "op", line);
      return toks;
    }
    if (lead) { fence = lead[1]; push("op", line); return toks; }
    if (/^#{1,6}\s/.test(line)) { push("cmd", line); return toks; }
    const bullet = /^\s*(?:[-*+]|\d+[.)])\s/.exec(line);
    let i = 0;
    if (bullet) { push("op", bullet[0]); i = bullet[0].length; }
    let plain = "";
    const flush = () => { push("", plain); plain = ""; };
    while (i < line.length) {
      if (line[i] === "`") {
        const ticks = at(/`+/y, line, i);
        const end = line.indexOf(ticks, i + ticks.length);
        if (end > 0) { flush(); push("str", line.slice(i, end + ticks.length)); i = end + ticks.length; continue; }
      }
      if (line.startsWith("**", i)) {
        const end = line.indexOf("**", i + 2);
        if (end > i + 2) { flush(); push("cmd", line.slice(i, end + 2)); i = end + 2; continue; }
      }
      plain += line[i];
      i++;
    }
    flush();
    return toks;
  });
}

/**
 * `text` as lines of tokens, [[{ t, s }, …], …], one entry per "\n"-split
 * line. An unknown `lang` comes back plain.
 */
export function highlight(text, lang) {
  const spec = LANGS[lang];
  const lines = text.split("\n");
  if (!spec) return lines.map((l) => (l ? [{ t: "", s: l }] : []));
  if (spec.md) return markdown(lines);
  const markup = spec.jsx || spec.tags;
  const out = [];
  // A string or comment still running: { t, end, esc }, or { t, tmpl } for
  // a template literal.
  let open = null;
  let depth = 0;        // CSS: inside a rule's braces
  // JSX/HTML: where we are, innermost last. "el": an element's children,
  // prose (quotes and keywords mean nothing there). { braces }: a {…} in
  // them, code. { tag, braces, closing }: inside a tag, at its attributes
  // (braces 0) or in one's {…} (code, and may hold whole elements).
  const stack = [];
  const top = () => stack[stack.length - 1];
  const inText = () => (spec.jsx ? top() === "el" : !top());
  const inTag = () => (top()?.tag ? top() : null);
  const inAttrs = () => inTag()?.braces === 0;
  for (const line of lines) {
    const toks = [];
    let lastChar = "";  // the last non-space character so far on this line
    let lastKind = null;
    let prevWord = "";
    const push = (t, s) => {
      if (!s) return;
      const last = toks[toks.length - 1];
      if (last && last.t === t) last.s += s; else toks.push({ t, s });
      const trimmed = s.trimEnd();
      if (trimmed) { lastChar = trimmed[trimmed.length - 1]; lastKind = t; }
    };
    let i = 0;
    if (open) {
      const end = open.tmpl ? template(line, 0, open.tmpl) : close(line, 0, open);
      if (end < 0) { push(open.t, line); out.push(toks); continue; }
      push(open.t, line.slice(0, end));
      i = end;
      open = null;
    }
    while (i < line.length) {
      const c = line[i];
      if (c === " " || c === "\t") { push("", c); i++; continue; }
      // Prose: an element's children in JSX, anything outside a tag in HTML.
      if (markup && c !== "<" && inText()) {
        if (c === "{") stack.push({ braces: 1 });
        push("", c);
        i++;
        continue;
      }
      if (spec.block && line.startsWith(spec.block[0], i)) {
        const end = close(line, i + spec.block[0].length, { end: spec.block[1] });
        if (end < 0) { push("com", line.slice(i)); open = { t: "com", end: spec.block[1] }; break; }
        push("com", line.slice(i, end));
        i = end;
        continue;
      }
      if (spec.line && line.startsWith(spec.line, i) && !inAttrs()
          && (spec.line !== "#" || lang === "py" || i === 0 || /\s/.test(line[i - 1]))) {
        push("com", line.slice(i));
        break;
      }
      // A regex literal, where an expression may start (else it's a divide),
      // read whole so a quote or a backtick inside one opens nothing.
      if (spec.regex && c === "/" && !inAttrs() && (lastKind === "op" || EXPR_START.includes(lastChar))) {
        let k = i + 1;
        for (let cls = false; k < line.length; k++) {
          if (line[k] === "\\") k++;
          else if (cls) cls = line[k] !== "]";
          else if (line[k] === "[") cls = true;
          else if (line[k] === "/") break;
        }
        if (k > i + 1 && k < line.length) {
          const end = k + 1 + at(/[a-z]*/y, line, k + 1).length;
          push("str", line.slice(i, end));
          i = end;
          continue;
        }
      }
      if (spec.tmpl && c === "`") {
        const st = { depth: 0 };
        const end = template(line, i + 1, st);
        if (end < 0) { push("str", line.slice(i)); open = { t: "str", tmpl: st }; break; }
        push("str", line.slice(i, end));
        i = end;
        continue;
      }
      const prefix = spec.prefixes && !WORD_CHAR.test(line[i - 1] || "") ? at(PY_PREFIX, line, i) : null;
      const q = prefix ? line[i + prefix.length] : c;
      if (spec.quotes.includes(q)) {
        const from = i + (prefix ? prefix.length : 0);
        const triple = spec.triple && line.startsWith(q.repeat(3), from);
        const delim = triple ? q.repeat(3) : q;
        const end = close(line, from + delim.length, { end: delim, esc: true });
        if (end < 0) {
          if (triple || spec.multiQuote) {
            push("str", line.slice(i));
            open = { t: "str", end: delim, esc: true };
            break;
          }
          // A one-line string can't run on, so this quote isn't one (an
          // apostrophe in prose, say): plain, and keep reading.
          push("", line.slice(i, from + 1));
          i = from + 1;
          continue;
        }
        push(spec.jsonKeys && /^\s*:/.test(line.slice(end)) ? "flag" : "str", line.slice(i, end));
        i = end;
        continue;
      }
      if (spec.vars && c === "$") {
        const v = at(SH_VAR, line, i);
        if (v) { push("var", v); i += v.length; continue; }
      }
      if (markup && c === "<" && !inAttrs()) {
        if (spec.jsx && (line.startsWith("<>", i) || line.startsWith("</>", i))) {
          const s = line.startsWith("<>", i) ? "<>" : "</>";
          if (s === "<>") stack.push("el"); else if (top() === "el") stack.pop();
          push("op", s);
          i += s.length;
          continue;
        }
        const name = at(TAG, line, i);
        if (name && (!spec.jsx || inText() || lastKind === "op" || EXPR_START.includes(lastChar))) {
          push("op", name);
          i += name.length;
          stack.push({ tag: true, braces: 0, closing: name.startsWith("</") });
          continue;
        }
      }
      if (inAttrs()) {
        const tag = top();
        if (c === ">" || line.startsWith("/>", i)) {
          const s = c === ">" ? ">" : "/>";
          stack.pop();
          if (tag.closing) { if (top() === "el") stack.pop(); }
          else if (s === ">" && spec.jsx) stack.push("el");
          push("op", s);
          i += s.length;
          continue;
        }
        if (c === "{") { tag.braces = 1; push("", c); i++; continue; }
      }
      if (spec.css) {
        const hex = c === "#" && depth > 0 ? at(HEX_COLOUR, line, i) : null;
        if (hex) { push("var", hex); i += hex.length; continue; }
        if (/[\d.-]/.test(c) && !/[\w-]/.test(line[i - 1] || "")) {
          const n = at(CSS_NUMBER, line, i);
          if (n && /\d/.test(n)) { push("var", n); i += n.length; continue; }
        }
        const rule = c === "@" ? at(AT_RULE, line, i) : null;
        if (rule) { push("op", rule); i += rule.length; continue; }
        const w = /[\w-]/.test(line[i - 1] || "") ? null : at(CSS_IDENT, line, i);
        if (w) {
          const prop = depth > 0 && /^\s*:(?!:)/.test(line.slice(i + w.length));
          push(prop ? "flag" : "", w);
          i += w.length;
          continue;
        }
        if (c === "{") depth++;
        else if (c === "}") depth = Math.max(0, depth - 1);
        push("", c);
        i++;
        continue;
      }
      if (/\d/.test(c) && !WORD_CHAR.test(line[i - 1] || "")) {
        const n = at(NUMBER, line, i);
        if (n) { push("var", n); i += n.length; continue; }
      }
      if (spec.deco && c === "@" && !line.slice(0, i).trim()) {
        const d = at(DECORATOR, line, i);
        if (d) { push("flag", d); i += d.length; continue; }
      }
      const w = at(inAttrs() ? ATTR : IDENT, line, i);
      if (w) {
        const after = line.slice(i + w.length);
        const member = line[i - 1] === "." && line[i - 2] !== ".";
        let t = "";
        if (inAttrs()) t = "flag";
        else if (spec.keys && !line.slice(0, i).replace(/^\s*-\s/, "").trim() && /^\s*[:=]/.test(after)) t = "flag";
        else if (spec.defs?.has(prevWord)) t = "cmd";
        else if (!member && spec.kw?.has(spec.ci ? w.toLowerCase() : w)
                 && !(spec.jsx && /^\s*:/.test(after) && lastChar && "{,".includes(lastChar))) t = "op";
        push(t, w);
        prevWord = w;
        i += w.length;
        continue;
      }
      if (spec.keys && c === "[" && !line.slice(0, i).trim()) { push("cmd", line.slice(i)); break; }
      // Braces that close what JSX opened: a {…} in children, or in a tag.
      const ctx = markup ? top() : null;
      if (ctx && ctx !== "el" && ctx.braces > 0 && (c === "{" || c === "}")) {
        ctx.braces += c === "{" ? 1 : -1;
        if (ctx.braces === 0 && !ctx.tag) stack.pop();
      }
      push("", c);
      i++;
    }
    out.push(toks);
  }
  return out;
}
