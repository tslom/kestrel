// The app's mark (same Malar as the Dock icon and favicon: scripts/gen-icon.py, public/favicon.svg):
// yellow-ringed eye and tear stripe on a rufous tile. The one place amber fills a whole shape.
// Not in icons.jsx, and its colors are literals: it must look identical in any theme.
const RUFOUS = "#BC5215";      // Flexoki orange-600, the paper-side accent
const INK = "#100F0F";
const PAPER = "#F2F0E5";
const CERE = "#D0A215";        // Flexoki yellow-400, the bare ring round the eye

// Superellipse |x|^5 + |y|^5 = 1 (a rounded rect looks wrong beside real app icons).
const TILE = "M100 50C100 99.3 99.3 100 50 100C.7 100 0 99.3 0 50C0 .7 .7 0 50 0C99.3 0 100 .7 100 50Z";

// Tapers and leans back; upright or flat-cut it reads as an "i" at 20px.
const STRIPE = "M53 55C51 66 47 77 42 91C50 79 56 67 60 56Z";

export default function Mark({ size = 20, title }) {
  return (
    <svg width={size} height={size} viewBox="0 0 100 100" aria-hidden={!title}
      role={title ? "img" : undefined}>
      {title && <title>{title}</title>}
      <defs>
        {/* Fixed id: instances share identical geometry, so a collision is harmless. */}
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
