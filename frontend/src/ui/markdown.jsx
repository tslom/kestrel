// Stateless markdown renderer for console output (inline pass + block pass); BriefWidget uses the inline half.
// Design: DESIGN.md → `frontend/src/ui/markdown.jsx`.
import { lazy, Suspense } from "react";
import VaultLink from "./VaultLink.jsx";
import Cite from "./Cite.jsx";
import FactRef from "./FactRef.jsx";
import DocPage from "./DocPage.jsx";
import StudyBlock from "./StudyBlock.jsx";
import { report } from "../lib/report";
import PathLink from "./FileLink.jsx";
import { WIKILINK_SRC, NOTE_PATH_SRC } from "../lib/obsidian";
import { FILE_PATH_SRC, looksLikePath } from "../lib/paths";

// KaTeX loads on the first formula (a third of the page's JS); raw source shows until then.
// The load must not reject: lazy() caches a rejection and rethrows every render, blanking the desk.
// No retry: a stale chunk is gone from the server; only ⌘R brings it back.
const RAW_TITLE = "KaTeX didn't load — reload (⌘R) to typeset this";
const RAW_MATH = {
  MathInline: ({ tex }) => <code className="md-math-raw" title={RAW_TITLE}>{tex}</code>,
  MathBlock: ({ tex }) => <pre className="md-math-raw" title={RAW_TITLE}>{tex}</pre>,
};
let math = null;
function loadMath() {
  math ??= import("./math.jsx").catch((e) => {
    report("render", "math.jsx", e?.message || String(e), e?.stack);
    return RAW_MATH;
  });
  return math;
}
const LazyInline = lazy(() => loadMath().then((m) => ({ default: m.MathInline })));
const LazyBlock = lazy(() => loadMath().then((m) => ({ default: m.MathBlock })));

function MathInline({ tex }) {
  return (
    <Suspense fallback={<code className="md-math-raw">{tex}</code>}>
      <LazyInline tex={tex} />
    </Suspense>
  );
}

function MathBlock({ tex }) {
  return (
    <Suspense fallback={<pre className="md-math-raw">{tex}</pre>}>
      <LazyBlock tex={tex} />
    </Suspense>
  );
}

// target="_blank" is what the native window hands off to the system browser.
const URL_RE = /(https?:\/\/[^\s<>()[\]{}"']+[^\s<>()[\]{}"'.,;:!?])/g;

// One inline matcher (order matters: code and explicit links first, so their innards aren't
// re-formatted; the lookarounds keep snake_case and 3*4*5 out). Notes, paths and math live in it
// so the outer construct claims them whole. Blocks are a separate pass, below.
// $$ before $; the inline form needs tight non-space ends and no digit after the closer ("$5 to $10").
const MATH_DISPLAY_SRC = "\\$\\$([^\\n]+?)\\$\\$";
const MATH_INLINE_SRC = "\\$(?!\\s)((?:\\\\.|[^$\\\\\\n])+?)(?<!\\s)\\$(?!\\d)";
const MATH_PAREN_SRC = "\\\\\\(([^\\n]+?)\\\\\\)";

// `[cite:e7a3f1]`, resolved by ui/Cite.jsx. Deliberately loose (not anchored to the kind letters):
// whatever the model puts in a `[cite:…]` must render as a dead citation, never raw markup.
const CITE_SRC = "\\[cite:([^\\]\\s]{1,64})\\]";

// `[cite:d3f9a1#p12 "quote"]` or a mid-sentence page embed (alone on a line it's PAGE_LINE).
// Ahead of CITE_SRC, which would decline the space; the ref is loose and Cite.jsx judges it.
const DOC_CITE_SRC = '\\[(?:cite|page):([^\\]\\s#]{1,64})#p(\\d{1,4})(?:\\s+"([^"\\]\\n]{1,300})")?\\]';

// A bold/strike body steps over code spans whole (**`char**`** must not close inside the code).
// The lookahead keeps the alternatives disjoint: if both match a code span the regex backtracks
// 2.6x per span (22 spans on an unclosed ** = 10s), and every streaming bold line is unclosed.
const SPAN = "(?:`[^`\\n]+`|(?!`[^`\\n]+`)[^\\n])+?";
// A Brain fact id (f997 or [f997]); last in the matcher; lookarounds skip hex colours, f-150, f997x.
const FACT_SRC = "(?<![\\w/#.\\-])(\\[f\\d{3,4}\\]|f\\d{3,4})(?![\\w\\-])";
const FACT_CODE = /^\[?f(\d{3,4})\]?$/;
const factId = (ref) => Number(ref.replace(/\D/g, ""));

const INLINE_SRC = [
  "``\\s?([^\\n]+?)\\s?``",
  "`([^`\\n]+)`",
  DOC_CITE_SRC,
  CITE_SRC,
  `!?${WIKILINK_SRC}`,
  "!\\[([^\\]\\n]*)\\]\\((https?:\\/\\/[^)\\s]+)\\)",
  "\\[([^\\]\\n]+)\\]\\((https?:\\/\\/[^)\\s]+)\\)",
  // [text](path): a note or any file (lib/paths.js, FileLink.jsx); an ![image](path) bang rides along.
  "!?\\[([^\\]\\n]+)\\]\\((?!https?:)([^)\\s]+)\\)",
  MATH_DISPLAY_SRC,
  MATH_INLINE_SRC,
  MATH_PAREN_SRC,
  // a backslash escape, after \( so that one stays math
  "\\\\([!-/:-@[-`{-~])",
  "<br\\s*\\/?>",
  "<(sub|sup|kbd|mark|b|i|u|s)>([^<\\n]+?)<\\/(?:sub|sup|kbd|mark|b|i|u|s)>",
  "\\[\\^([\\w-]{1,20})\\]",
  "<(https?:\\/\\/[^>\\s]+)>",
  `\\*\\*\\*(${SPAN})\\*\\*\\*`,
  `\\*\\*(${SPAN})\\*\\*`,
  `__(${SPAN})__`,
  "(?<![\\w*])\\*(?!\\s)([^*\\n]+?)(?<!\\s)\\*(?![\\w*])",
  "(?<![\\w_])_(?!\\s)([^_\\n]+?)(?<!\\s)_(?![\\w_])",
  `~~(${SPAN})~~`,
  URL_RE.source,
  NOTE_PATH_SRC,
  FILE_PATH_SRC,
  FACT_SRC,
].join("|");

const HTML_TAG = { b: "strong", i: "em", u: "u", s: "del", sub: "sub", sup: "sup", kbd: "kbd", mark: "mark" };

export function renderInline(line, keyBase) {
  const out = [];
  let last = 0;
  // fresh matcher per call: bold/italic recurse, which would clobber a shared /g lastIndex
  const re = new RegExp(INLINE_SRC, "g");
  for (let m; (m = re.exec(line)); ) {
    const [full, code2, code, docRef, docPage, docQuote, cite, wiki, wikiAlias, imgAlt, imgSrc,
           linkText, linkHref, noteText, noteHref,
           mathBig, mathTex, mathParen, escaped, tag, tagText, footnote, autolink,
           boldEm, bold, boldAlt, em, emAlt, strike, url, notePath, filePath, fact] = m;
    if (m.index > last) out.push(line.slice(last, m.index));
    const key = `${keyBase}-${m.index}`;
    if (code2 !== undefined) out.push(<code key={key}>{code2}</code>);
    // A backticked path or fact id becomes the link, keeping its monospace.
    else if (code !== undefined)
      out.push(FACT_CODE.test(code)
        ? <FactRef key={`${key}-${code}`} id={factId(code)}><code>{code}</code></FactRef>
        : looksLikePath(code)
        ? <PathLink key={`${key}-${code}`} path={code}><code>{code}</code></PathLink>
        : <code key={key}>{code}</code>);
    else if (docRef !== undefined)
      out.push(<Cite key={`${key}-${docRef}-${docPage}`} refId={docRef}
                     page={Number(docPage)} quote={docQuote} />);
    else if (cite !== undefined)
      // The ref rides in the key: offsets collide across answers and React reused one Cite (see Cite.jsx).
      out.push(<Cite key={`${key}-${cite}`} refId={cite} />);
    // ![[embed]] renders as a plain link.
    else if (wiki !== undefined)
      out.push(<VaultLink key={key} target={wiki}>{wikiAlias ?? wiki}</VaultLink>);
    else if (imgSrc !== undefined)
      out.push(<a key={key} href={imgSrc} target="_blank" rel="noreferrer">
        <img className="md-img" src={imgSrc} alt={imgAlt} /></a>);
    else if (linkText !== undefined)
      out.push(<a key={key} href={linkHref} target="_blank" rel="noreferrer">{linkText}</a>);
    else if (noteText !== undefined)
      out.push(looksLikePath(noteHref) || noteHref.endsWith(".md")
        ? <PathLink key={`${key}-${noteHref}`} path={noteHref}>{noteText}</PathLink>
        : noteText);
    // $$…$$ mid-sentence means the display form (a standalone one is a block, below).
    else if (mathBig !== undefined) out.push(<MathBlock key={key} tex={mathBig} />);
    else if (mathTex ?? mathParen)
      out.push(<MathInline key={key} tex={mathTex ?? mathParen} />);
    else if (escaped !== undefined) out.push(escaped);
    else if (tag !== undefined) {
      const Tag = HTML_TAG[tag];
      out.push(<Tag key={key}>{renderInline(tagText, key)}</Tag>);
    }
    else if (footnote !== undefined) out.push(<sup key={key} className="md-fn">{footnote}</sup>);
    else if (autolink !== undefined)
      out.push(<a key={key} href={autolink} target="_blank" rel="noreferrer">{autolink}</a>);
    else if (full.startsWith("<")) out.push(<br key={key} />);
    else if (boldEm !== undefined)
      out.push(<strong key={key}><em>{renderInline(boldEm, key)}</em></strong>);
    else if (bold ?? boldAlt) out.push(<strong key={key}>{renderInline(bold ?? boldAlt, key)}</strong>);
    else if (em ?? emAlt) out.push(<em key={key}>{renderInline(em ?? emAlt, key)}</em>);
    else if (strike !== undefined) out.push(<del key={key}>{renderInline(strike, key)}</del>);
    else if (url !== undefined)
      out.push(<a key={key} href={url} target="_blank" rel="noreferrer">{url}</a>);
    else if ((notePath ?? filePath) !== undefined)
      out.push(<PathLink key={`${key}-${notePath ?? filePath}`} path={notePath ?? filePath}>
        {notePath ?? filePath}</PathLink>);
    else out.push(<FactRef key={`${key}-${fact}`} id={factId(fact)}>{fact}</FactRef>);
    last = m.index + full.length;
  }
  if (last < line.length) out.push(line.slice(last));
  return out;
}

// A user prompt stays literal text except formulas; only math-looking spans count ($HOME isn't).
const MATHISH = /[\\^{}=+]/;

// `known`: formulas the math field wrote, which count however plain ($y$).
export function userMathSpans(text, known) {
  const spans = [];
  const re = new RegExp(MATH_INLINE_SRC, "g");
  for (let m; (m = re.exec(text)); )
    if (MATHISH.test(m[1]) || known?.has(m[1]))
      spans.push({ start: m.index, end: m.index + m[0].length, tex: m[1] });
  return spans;
}

// `onEdit(span)`, when given, makes each formula a button (the composer preview reopens it in the math field).
export function renderUserMath(text, onEdit, known) {
  const spans = userMathSpans(text, known);
  if (!spans.length) return text;
  const out = [];
  let last = 0;
  for (const s of spans) {
    if (s.start > last) out.push(text.slice(last, s.start));
    out.push(onEdit
      ? <button key={s.start} type="button" className="math-edit" title="Edit this formula"
                onClick={() => onEdit(s)}><MathInline tex={s.tex} /></button>
      : <MathInline key={s.start} tex={s.tex} />);
    last = s.end;
  }
  if (last < text.length) out.push(text.slice(last));
  return out;
}

// Block-level pass: walks the lines once, groups runs, leaves anything unrecognised as pre-wrapped text.
const FENCE = /^\s*(```|~~~)/;
const FENCE_LANG = /^\s*(?:```|~~~)\s*([\w-]+)/;
const STUDY_KINDS = new Set(["quiz", "cards"]);
// Only a real doc ref makes a block; looser falls through to the inline pass as a dead Cite pill.
const PAGE_LINE = /^\s*\[page:(d[0-9a-f]{5,10})#p(\d{1,4})(?:\s+"([^"\]\n]{1,300})")?\]\s*$/;
const TABLE_ROW = /^\s*\|.*\|\s*$/;
const TABLE_RULE = /^\s*\|?[\s:|-]*-[\s:|-]*\|?\s*$/;
const HR = /^\s*([-*_])[ \t]*(?:\1[ \t]*){2,}$/;   // --- and - - - both count
const LIST_ITEM = /^(\s*)(?:[-*+]|(\d+)[.)])\s+(.*)$/;
const HEADING = /^\s*(#{1,6})\s+(.*)$/;
const CHECKBOX = /^\[([ xX])\]\s+(.*)$/;
// A wrapped list item (indented lines after the marker); notes hard-wrap.
const CONTINUATION = /^\s+\S/;
// Blockquotes and Obsidian callouts (`> [!important] Title`); the fold marker (+/-) is ignored.
const QUOTE = /^\s*>/;
const CALLOUT = /^\[!([\w-]+)\][+-]?\s*(.*)$/;
const CALLOUT_TONE = {
  tip: "good", hint: "good", success: "good", check: "good", done: "good",
  important: "warm", warning: "warm", caution: "warm", attention: "warm", question: "warm",
  help: "warm", faq: "warm",
  danger: "bad", error: "bad", failure: "bad", fail: "bad", missing: "bad", bug: "bad",
};
// A display formula is a block: both openers, closed on one line or left open over several.
const MATH_OPEN = /^\s*(\$\$|\\\[)/;
const MATH_CLOSE = { "$$": /\$\$\s*$/, "\\[": /\\\]\s*$/ };

// `\|` is a literal pipe ([[note\|alias]] in a table): it neither splits the row nor survives.
const cells = (row) =>
  row.trim().replace(/^\|/, "").replace(/(?<!\\)\|$/, "").split(/(?<!\\)\|/)
    .map((c) => c.trim().replace(/\\\|/g, "|"));

function renderTable(rows, key) {
  // The first row is a header only if a rule follows; a bare grid still lines up.
  const ruled = rows.length > 1 && TABLE_RULE.test(rows[1]);
  const align = ruled
    ? cells(rows[1]).map((c) =>
        c.endsWith(":") ? (c.startsWith(":") ? "center" : "right") : undefined)
    : [];
  const body = rows.slice(ruled ? 2 : 0).map(cells);
  return (
    <div className="md-table-wrap" key={key}>
      <table className="md-table">
        {ruled && (
          <thead><tr>{cells(rows[0]).map((c, i) => (
            <th key={i} style={{ textAlign: align[i] }}>{renderInline(c, `${key}h${i}`)}</th>
          ))}</tr></thead>
        )}
        <tbody>{body.map((r, ri) => (
          <tr key={ri}>{r.map((c, ci) => (
            <td key={ci} style={{ textAlign: align[ci] }}>{renderInline(c, `${key}-${ri}-${ci}`)}</td>
          ))}</tr>
        ))}</tbody>
      </table>
    </div>
  );
}

// Deeper indentation becomes a nested list; `cur` is a shared cursor so recursion and caller agree on consumption.
function renderList(items, cur, key) {
  const base = items[cur.i].indent;
  const ordered = items[cur.i].ordered != null;
  const start = ordered ? Number(items[cur.i].ordered) : undefined;
  const nodes = [];
  // A run ends when the marker kind switches ("- a" then "1. b" is two lists; merging renumbered the second).
  const mine = (it) => it.indent > base || (it.ordered != null) === ordered;
  while (cur.i < items.length && items[cur.i].indent >= base && mine(items[cur.i])) {
    if (items[cur.i].indent > base) {
      nodes[nodes.length - 1].sub = renderList(items, cur, `${key}s${cur.i}`);
      continue;
    }
    nodes.push({ item: items[cur.i++], sub: null });
  }
  const Tag = ordered ? "ol" : "ul";
  return (
    <Tag key={key} className="md-list" start={start !== 1 ? start : undefined}>
      {nodes.map(({ item, sub }, n) => {
        const box = CHECKBOX.exec(item.text);
        return (
          <li key={n} className={box ? "md-task" : undefined}>
            {box && <span className="md-check">{box[1] === " " ? "☐" : "☑"}</span>}
            {renderInline(box ? box[2] : item.text, `${key}-${n}`)}
            {sub}
          </li>
        );
      })}
    </Tag>
  );
}

// Same text, same element objects, so React skips the subtree (the console re-renders its log per keystroke/token).
// Output depends only on the text, so the cache is safe; bounded, oldest out first.
const MD_CACHE_MAX = 600;
const mdCache = new Map();
export function renderMarkdownCached(text) {
  if (!text) return text;
  const hit = mdCache.get(text);
  if (hit) {
    mdCache.delete(text);
    mdCache.set(text, hit);
    return hit;
  }
  const out = renderMarkdown(text);
  mdCache.set(text, out);
  if (mdCache.size > MD_CACHE_MAX) mdCache.delete(mdCache.keys().next().value);
  return out;
}

export function renderMarkdown(text) {
  if (!text) return text;
  const lines = text.split("\n");
  const blocks = [];
  let para = [];
  const flush = () => {
    while (para.length && !para[para.length - 1].trim()) para.pop();
    if (!para.length) return;
    const k = blocks.length;
    const run = para;
    para = [];
    blocks.push(
      <pre key={`p${k}`} className="md-p">
        {run.flatMap((l, n) => [...(n ? ["\n"] : []), ...renderInline(l, `${k}-${n}`)])}
      </pre>
    );
  };

  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    const fence = FENCE.exec(line);
    if (fence) {
      flush();
      const lang = (FENCE_LANG.exec(line)?.[1] || "").toLowerCase();
      const body = [];
      // closed only by the same marker: a ``` inside a ~~~ block is content
      for (i++; i < lines.length && FENCE.exec(lines[i])?.[1] !== fence[1]; i++) body.push(lines[i]);
      // Quiz/cards are interactive (prose would give the answers away); unclosed = still streaming.
      if (STUDY_KINDS.has(lang))
        blocks.push(<StudyBlock key={`c${i}`} kind={lang} source={body.join("\n")}
                                closed={i < lines.length} renderInline={renderRich} />);
      else blocks.push(<pre key={`c${i}`} className="md-code">{body.join("\n")}</pre>);
      continue;
    }
    // A page shown in the reply — `[page:d3f9a1#p12]` alone on its line.
    const shown = PAGE_LINE.exec(line);
    if (shown) {
      flush();
      blocks.push(<DocPage key={`g${i}-${shown[1]}-${shown[2]}`} docRef={shown[1]}
                           page={Number(shown[2])} quote={shown[3]} width={520} embed />);
      continue;
    }
    const open = MATH_OPEN.exec(line);
    if (open) {
      flush();
      const close = MATH_CLOSE[open[1]];
      const first = line.trim().slice(open[1].length);
      const body = [];
      let closed = close.test(first) && first.trim();
      if (closed) {
        body.push(first.replace(close, ""));
      } else {
        body.push(first);
        for (i++; i < lines.length && !close.test(lines[i]); i++) body.push(lines[i]);
        closed = i < lines.length;
        if (closed) body.push(lines[i].replace(close, ""));
      }
      const tex = body.join("\n").trim();
      // Unclosed = still streaming: show source rather than flash KaTeX's red parse mark per token.
      blocks.push(closed
        ? <MathBlock key={`m${i}`} tex={tex} />
        : <pre key={`m${i}`} className="md-math-raw">{tex}</pre>);
      continue;
    }
    if (QUOTE.test(line)) {
      flush();
      const inner = [];
      for (; i < lines.length && QUOTE.test(lines[i]); i++) inner.push(lines[i].replace(/^\s*> ?/, ""));
      i--;
      const call = CALLOUT.exec(inner[0].trim());
      if (call) {
        const kind = call[1].toLowerCase();
        blocks.push(
          <aside key={`q${i}`} className={`md-callout ${CALLOUT_TONE[kind] ?? ""}`}>
            <div className="md-callout-title">
              {call[2] ? renderInline(call[2], `q${i}t`) : kind[0].toUpperCase() + kind.slice(1)}
            </div>
            {renderMarkdown(inner.slice(1).join("\n"))}
          </aside>
        );
      } else {
        blocks.push(<blockquote key={`q${i}`} className="md-quote">{renderMarkdown(inner.join("\n"))}</blockquote>);
      }
      continue;
    }
    if (TABLE_ROW.test(line)) {
      flush();
      const rows = [];
      for (; i < lines.length && TABLE_ROW.test(lines[i]); i++) rows.push(lines[i]);
      blocks.push(renderTable(rows, `t${i}`));
      i--;
      continue;
    }
    if (HR.test(line)) { flush(); blocks.push(<hr key={`r${i}`} className="md-hr" />); continue; }
    if (LIST_ITEM.test(line)) {
      flush();
      const items = [];
      for (; i < lines.length; i++) {
        const m = LIST_ITEM.exec(lines[i]);
        if (m) items.push({ indent: m[1].length, ordered: m[2], text: m[3] });
        else if (CONTINUATION.test(lines[i]) && !FENCE.test(lines[i]) && !TABLE_ROW.test(lines[i]))
          items[items.length - 1].text += " " + lines[i].trim();
        else break;
      }
      const cur = { i: 0 };
      while (cur.i < items.length) blocks.push(renderList(items, cur, `l${i}-${cur.i}`));
      i--;
      continue;
    }
    const h = HEADING.exec(line);
    if (h) {
      flush();
      blocks.push(<strong key={`h${i}`} className={`md-head md-h${h[1].length}`}>{renderInline(h[2], i)}</strong>);
      continue;
    }
    if (line.trim() || para.length) para.push(line);  // no blank run after a block
  }
  flush();
  return blocks;
}

// Quiz/card text with a line break (a ``` snippet in the JSON string) needs the block pass.
function renderRich(text, key) {
  return text.includes("\n")
    ? <div key={key} className="md-rich">{renderMarkdown(text)}</div>
    : renderInline(text, key);
}
