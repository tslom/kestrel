// The markdown renderer for console output, apart from Console.jsx: it holds
// no state and knows nothing about the component, and BriefWidget needs the
// inline half of it too.
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

// KaTeX was over a third of the page's JS, and only a turn with TeX in it
// needs it — so it loads on the first formula, not with the desk. Until it
// lands the source shows raw, in the same dress math.jsx gives a formula
// KaTeX can't parse.
//
// The load must not reject. lazy() caches a rejection and rethrows it on every
// render, so one failed import takes the page down for good: a window open
// across a rebuild asks for a chunk that build deleted, and the first formula
// blanks the desk until the app is restarted. A miss
// costs the typesetting and nothing else. There's no retry: this page's chunk
// is gone from the server, so only a reload (⌘R) brings KaTeX back.
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

// Console output is plain text, so URLs in it are dead unless we make them
// anchors. target="_blank" is what the native window hands off to the system
// browser, so links behave the same in the .app as in a tab.
const URL_RE = /(https?:\/\/[^\s<>()[\]{}"']+[^\s<>()[\]{}"'.,;:!?])/g;

// Claude answers in markdown, so the marks showed up raw in the transcript.
// Handle the inline ones that actually turn up — code, bold, italic, strike,
// [text](url). Order matters: code and explicit links come first so their
// innards aren't re-formatted, and the lookarounds keep snake_case and 3*4*5
// out of it. Block syntax is a separate pass, below.
//
// The three note forms (wikilink, [text](note.md), and a bare path) go here
// rather than in a pass of their own for the same reason: one matcher means a
// path inside a link's href, or inside a code span, is claimed by the outer
// construct and never linkified twice.
// Math goes in the same matcher for the same reason the note forms do: a
// formula is full of _, *, and \[ \], and every one of them is a mark this
// pass would otherwise chew on. Claimed whole here, its innards are handed to
// KaTeX untouched — and a backticked $x$ still loses to the code span, which
// is leftmost.
//
// $$ before $ so the display form wins at a shared index. The inline form
// wants both ends tight against non-space and no digit after the closer, or
// "$5 to $10" reads as math; \(…\) is the other spelling of the same thing.
const MATH_DISPLAY_SRC = "\\$\\$([^\\n]+?)\\$\\$";
const MATH_INLINE_SRC = "\\$(?!\\s)((?:\\\\.|[^$\\\\\\n])+?)(?<!\\s)\\$(?!\\d)";
const MATH_PAREN_SRC = "\\\\\\(([^\\n]+?)\\\\\\)";

// A citation the console wrote: `[cite:e7a3f1]`, resolved by ui/Cite.jsx.
//
// Deliberately NOT anchored to the three kind letters. It was, until a live
// session read an email with `brief=false`, found a row whose only handle was
// the raw Gmail id, and wrote `[cite:1a0c5286b39d182b]` — which the narrow
// pattern declined, so the raw syntax landed in the prose looking like the
// renderer had broken. The backend now stamps refs on that read too, but the
// general case stands: whatever the model puts in a `[cite:…]`, the reader
// should see a citation that doesn't resolve, never the markup. Cite.jsx
// renders anything unref-shaped as dead without asking the network.
//
// It sits after the code span and before everything else for the same reason
// the note forms do: claimed whole here, it can't be chewed on by the italic
// or link passes, and a ref is not something to re-format.
const CITE_SRC = "\\[cite:([^\\]\\s]{1,64})\\]";

// A citation into a document, and a page embed written mid-sentence (alone on
// its line it's a block — PAGE_LINE below). Both say where in the document:
// `[cite:d3f9a1#p12 "the quoted words"]`. Ahead of CITE_SRC, which would
// otherwise decline the space and leave the markup in the prose. The ref is
// left loose for the reason CITE_SRC's is; Cite.jsx judges it.
const DOC_CITE_SRC = '\\[(?:cite|page):([^\\]\\s#]{1,64})#p(\\d{1,4})(?:\\s+"([^"\\]\\n]{1,300})")?\\]';

// The constructs below the math were added after replaying every console
// reply on record through this renderer and
// listing what came out raw — plus the CommonMark/GFM forms the model is
// known to write that hadn't turned up yet. A bold run's body steps over
// code spans whole, or **`char**`** closes on the stars inside the code.
//
// The lookahead keeps the two alternatives disjoint, and it isn't optional.
// With both able to match a code span, every span is a coin flip the regex
// retries when the closer never comes, 2.6× per span: an unclosed ** with 22
// code spans on its line takes 10s on the main thread, and an
// unclosed ** is what every bold line looks like while it streams. A boundary
// can't catch a hang.
const SPAN = "(?:`[^`\\n]+`|(?!`[^`\\n]+`)[^\\n])+?";
// A Brain fact by id, as the console's <brain> block hands them out: f997 or
// [f997]. Last in the matcher, so a link or a path that contains one keeps it;
// the lookarounds leave a hex colour, f-150 and f997x alone.
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
  // [text](path): a note, or any file (lib/paths.js, FileLink.jsx). The bang
  // of an ![image](path) rides along; it's a link like the rest.
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
  // fresh matcher per call — bold/italic recurse into their own contents, and
  // a shared /g regex would have its lastIndex clobbered by the inner pass
  const re = new RegExp(INLINE_SRC, "g");
  for (let m; (m = re.exec(line)); ) {
    const [full, code2, code, docRef, docPage, docQuote, cite, wiki, wikiAlias, imgAlt, imgSrc,
           linkText, linkHref, noteText, noteHref,
           mathBig, mathTex, mathParen, escaped, tag, tagText, footnote, autolink,
           boldEm, bold, boldAlt, em, emAlt, strike, url, notePath, filePath, fact] = m;
    if (m.index > last) out.push(line.slice(last, m.index));
    const key = `${keyBase}-${m.index}`;
    if (code2 !== undefined) out.push(<code key={key}>{code2}</code>);
    // A backticked path is how the console names a file most of the time, so
    // the code span becomes the link rather than losing to it — the monospace
    // stays, which is what makes it read as a path in the first place.
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
      // The ref rides in the key: these keys are character offsets, and two
      // answers routinely put a marker at the same offset, which made React
      // reuse one Cite for a different citation. See the reset in Cite.jsx.
      out.push(<Cite key={`${key}-${cite}`} refId={cite} />);
    // ![[embed]] is Obsidian's embed; here it's a link like any other.
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
    // $$…$$ mid-sentence is rare — a standalone one is a block, below — but
    // when it happens the model meant the display form, so keep it.
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

// A prompt you wrote stays literal text, but the formula typed into it should
// read as one. Only what looks like math, since prompts are full of $HOME.
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

// `onEdit(span)`, when given, makes each formula a button — the composer's
// preview, where clicking a drawn formula opens it again in the math field.
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

// Block-level markdown. Monospace hid this for a while: a bullet list is
// legible as raw text, so leaving blocks alone looked fine until the model
// answered with a table, whose columns only line up if something lays them
// out. This walks the lines once, groups the runs that belong together, and
// leaves anything unrecognised as the pre-wrapped text it already was.
const FENCE = /^\s*(```|~~~)/;
const FENCE_LANG = /^\s*(?:```|~~~)\s*([\w-]+)/;
const STUDY_KINDS = new Set(["quiz", "cards"]);
// Only a real doc ref makes a block: anything looser falls through to the
// inline pass, where Cite renders it as a (dead) pill rather than a page
// that can never load.
const PAGE_LINE = /^\s*\[page:(d[0-9a-f]{5,10})#p(\d{1,4})(?:\s+"([^"\]\n]{1,300})")?\]\s*$/;
const TABLE_ROW = /^\s*\|.*\|\s*$/;
const TABLE_RULE = /^\s*\|?[\s:|-]*-[\s:|-]*\|?\s*$/;
const HR = /^\s*([-*_])[ \t]*(?:\1[ \t]*){2,}$/;   // --- and - - - both count
const LIST_ITEM = /^(\s*)(?:[-*+]|(\d+)[.)])\s+(.*)$/;
const HEADING = /^\s*(#{1,6})\s+(.*)$/;
const CHECKBOX = /^\[([ xX])\]\s+(.*)$/;
// A wrapped list item: the lines after the marker line, indented under it.
// Notes hard-wrap, so without this every bullet's second line broke out of
// the list as a stray paragraph.
const CONTINUATION = /^\s+\S/;
// Blockquotes, and Obsidian's callouts riding on them: `> [!important] Title`
// opens a titled box, and the rest of the quote is its body. The fold marker
// (+/-) is accepted and ignored — there's no collapsing here.
const QUOTE = /^\s*>/;
const CALLOUT = /^\[!([\w-]+)\][+-]?\s*(.*)$/;
const CALLOUT_TONE = {
  tip: "good", hint: "good", success: "good", check: "good", done: "good",
  important: "warm", warning: "warm", caution: "warm", attention: "warm", question: "warm",
  help: "warm", faq: "warm",
  danger: "bad", error: "bad", failure: "bad", fail: "bad", missing: "bad", bug: "bad",
};
// A display formula is a block, not a run of text: it gets its own line, and
// it may take several. Both openers the model uses, and both shapes — closed
// on the same line ($$…$$, which is what the proofs actually emit) or left
// open for the lines that follow.
const MATH_OPEN = /^\s*(\$\$|\\\[)/;
const MATH_CLOSE = { "$$": /\$\$\s*$/, "\\[": /\\\]\s*$/ };

// `\|` is a literal pipe — how Obsidian writes a [[note\|alias]] inside a
// table — so it neither splits the row nor survives into the cell.
const cells = (row) =>
  row.trim().replace(/^\|/, "").replace(/(?<!\\)\|$/, "").split(/(?<!\\)\|/)
    .map((c) => c.trim().replace(/\\\|/g, "|"));

function renderTable(rows, key) {
  // The first row is only a header if a rule follows it; the model sometimes
  // emits a bare grid, and that should still line up.
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

// Nested bullets are normal in these answers, so indentation has to survive:
// each deeper run becomes a list inside the item above it. `cur` is a shared
// cursor so the recursion and its caller agree on how much was consumed.
function renderList(items, cur, key) {
  const base = items[cur.i].indent;
  const ordered = items[cur.i].ordered != null;
  const start = ordered ? Number(items[cur.i].ordered) : undefined;
  const nodes = [];
  // A run ends when the marker switches at this level — "- a" then "1. b" is
  // two lists, not one, and merging them silently renumbered the second.
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

// The same text, the same elements. The console re-renders its whole log on
// every keystroke in the prompt and every streamed token, and in a long chat
// re-parsing each message (and re-typesetting its math) is the lag.
// Handing React back the identical element objects lets
// it skip those subtrees outright. Output depends on nothing but the text, so
// the cache is safe; bounded, oldest out first.
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
      // A quiz or a deck of cards: interactive, because a quiz written as
      // prose gives its answers away. Unclosed = still streaming.
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
      // An unclosed formula at the end of the text is a turn still streaming:
      // half a \frac is a parse error, and flashing KaTeX's red mark on every
      // token is worse than showing the source until the closer lands.
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

// A quiz question or card face is usually one line, but a code question
// carries its snippet as a ``` fence inside the JSON string — and through the
// inline pass alone that showed as backticks with the lines run together.
// Anything with a line break gets the block pass.
function renderRich(text, key) {
  return text.includes("\n")
    ? <div key={key} className="md-rich">{renderMarkdown(text)}</div>
    : renderInline(text, key);
}
