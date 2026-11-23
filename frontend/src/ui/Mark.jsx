// The app's mark, as markup. Same Malar the Dock icon and the favicon draw
// (scripts/gen-icon.py, public/favicon.svg): a kestrel's face cut down to its
// two tells, the yellow-ringed eye and the black tear stripe under it, on a
// rufous tile. The dashboard is the thing that hangs over the vault and
// watches it.
//
// The tile is the one place amber fills a whole shape. Everywhere else in the
// UI amber is a thread (context moving to the agent); here it is the bird's
// back, and it is what finds the window in a Dock full of other apps.
//
// Deliberately not part of icons.jsx: that set is one stroke weight in
// `currentColor` precisely so a glyph takes the color of whatever it sits in.
// The mark is the opposite — it carries its own colors and has to look
// identical wherever it lands, which is what makes it a mark and not an icon.
//
// Colors are literals rather than var(--accent)/var(--ink) for the same
// reason: the mark must not invert or shift with the theme.
const RUFOUS = "#BC5215";      // Flexoki orange-600, the paper-side accent
const INK = "#100F0F";
const PAPER = "#F2F0E5";
const CERE = "#D0A215";        // Flexoki yellow-400, the bare ring round the eye

// Superellipse |x|^5 + |y|^5 = 1 — four cubics with their controls at the
// corners. A rounded rect is visibly the wrong shape next to real app icons.
const TILE = "M100 50C100 99.3 99.3 100 50 100C.7 100 0 99.3 0 50C0 .7 .7 0 50 0C99.3 0 100 .7 100 50Z";

// Tapers to a point as it falls and leans back from the eye; upright, or cut
// off flat, it reads as an "i" at 20px.
const STRIPE = "M53 55C51 66 47 77 42 91C50 79 56 67 60 56Z";

export default function Mark({ size = 20, title }) {
  return (
    <svg width={size} height={size} viewBox="0 0 100 100" aria-hidden={!title}
      role={title ? "img" : undefined}>
      {title && <title>{title}</title>}
      <defs>
        {/* Fixed id: the mark renders once, and two instances would collide
            onto identical geometry anyway, so uniquifying buys nothing. */}
        <clipPath id="mark-tile"><path d={TILE} /></clipPath>
      </defs>
      <g clipPath="url(#mark-tile)">
        <rect width="100" height="100" fill={RUFOUS} />
        <path d={STRIPE} fill={INK} />
        <circle cx="59" cy="39" r="15" fill="none" stroke={CERE} strokeWidth="4.5" />
        <circle cx="59" cy="39" r="11.5" fill={INK} />
        <circle cx="63" cy="35" r="3.2" fill={PAPER} />
      </g>
    </svg>
  );
}
