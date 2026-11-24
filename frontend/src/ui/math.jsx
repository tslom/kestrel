// TeX for the console ($…$ and $$…$$ from markdown.jsx). KaTeX from npm, not a CDN: renders offline.
import katex from "katex";
import "katex/dist/katex.min.css";
// A selection touching a formula copies its TeX whole (plain copy gave both KaTeX layers: "x2x^2").
// TeX, not unicode: \frac has no flat form. The handler is document-wide, installed once.
import "katex/contrib/copy-tex";

// A malformed formula must not lose the turn: KaTeX marks it in place (throwOnError off); the catch shows source.
// strict off: the model writes real-world TeX (\text with unicode, stray \newline).
function html(tex, display) {
  try {
    return katex.renderToString(tex, {
      displayMode: display,
      throwOnError: false,
      strict: false,
      trust: false,
      // Written into a style attribute, so the desk token follows light/dark.
      errorColor: "var(--bad)",
      // Default output (both layers): html-only would leave no accessible text. The <annotation> holds the TeX copy-tex reads.
    });
  } catch {
    return null;
  }
}

// Inline, in \displaystyle: TeX's text style shrinks an inline \frac to ~8px beside 13px mono. Taller line box is the price.
export function MathInline({ tex }) {
  // Our prefix stays out of the <annotation> or every copy would carry it.
  const out = html(`\\displaystyle ${tex}`, false)
    ?.replace('<annotation encoding="application/x-tex">\\displaystyle ',
              '<annotation encoding="application/x-tex">');
  if (out == null) return <code className="md-math-raw">{tex}</code>;
  return <span className="md-math-inline" dangerouslySetInnerHTML={{ __html: out }} />;
}

// Display: its own block; scrolls rather than widening the column.
export function MathBlock({ tex }) {
  const out = html(tex, true);
  if (out == null) return <pre className="md-math-raw">{tex}</pre>;
  return <div className="md-math" dangerouslySetInnerHTML={{ __html: out }} />;
}
