// TeX for the console. Homework questions arrive as a screenshot of a
// textbook and come back as $…$ and $$…$$, which the markdown pass had no
// case for — the answer to a distance-to-a-line proof rendered as literal
// \frac{|ax_1 + by_1 + c|}{\sqrt{a^2 + b^2}} (09-03).
//
// KaTeX from npm, not a CDN, for the reason the fonts are: the desk renders
// the same with the network down.
import katex from "katex";
import "katex/dist/katex.min.css";
// A selection touching a formula copies its TeX, $…$ or $$…$$, whole: the plain
// copy gave both of KaTeX's layers glyph by glyph (x2x^2 for x²). TeX rather
// than a unicode flattening because \frac and \sum have no flat form, and this
// is the notation the console and Obsidian both already read. The handler is
// document-wide, once, so anything drawn by this file gets it.
import "katex/contrib/copy-tex";

// A malformed formula is the model's typo, not a reason to lose the turn:
// KaTeX marks what it couldn't parse in place (throwOnError off) and the
// catch below is for the rest, where showing the source beats an empty span.
// strict off because the model writes real-world TeX — \text with unicode in
// it, a stray \newline — and warnings are noise on a localhost desk.
function html(tex, display) {
  try {
    return katex.renderToString(tex, {
      displayMode: display,
      throwOnError: false,
      strict: false,
      trust: false,
      // KaTeX writes this straight into a style attribute, so the desk's own
      // token goes through and the mark follows the light/dark swap.
      errorColor: "var(--bad)",
      // Default output, both layers. KaTeX marks the visual one aria-hidden
      // either way, so html-only would leave the formula with no accessible
      // text at all. The TeX source is in the <annotation>, which is what
      // copy-tex reads.
    });
  } catch {
    return null;
  }
}

// Inline: sits in the text run. Set in \displaystyle, not TeX's default text
// style — that shrinks an inline \frac to script size (~8px next to 13px mono)
// and crams \sum limits beside the sigma, which is where homework math lives
// (10-27). The taller line box is the price; a readable fraction is worth it.
export function MathInline({ tex }) {
  // The prefix is ours, not the formula's, so it stays out of the <annotation>
  // or every copied inline formula would paste with it.
  const out = html(`\\displaystyle ${tex}`, false)
    ?.replace('<annotation encoding="application/x-tex">\\displaystyle ',
              '<annotation encoding="application/x-tex">');
  if (out == null) return <code className="md-math-raw">{tex}</code>;
  return <span className="md-math-inline" dangerouslySetInnerHTML={{ __html: out }} />;
}

// Display: its own block, and it scrolls rather than widening the column —
// a long derivation step is exactly the thing that overflows the console.
export function MathBlock({ tex }) {
  const out = html(tex, true);
  if (out == null) return <pre className="md-math-raw">{tex}</pre>;
  return <div className="md-math" dangerouslySetInnerHTML={{ __html: out }} />;
}
