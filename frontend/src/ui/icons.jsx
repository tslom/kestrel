// One icon system for the whole desk. Every glyph the UI used to draw with a
// character — emoji (🎂 🚗 💬 📎 🔇 ⏰ 📋 🎉, the weather set) or a typographic
// stand-in (↻ ✕ ✓ ✎ ↩ ⊕ ★ ♫ ⤢ ▤ ▦) — resolves here instead. Emoji were the
// real problem: they arrive pre-colored from the system font, so they ignored
// both Flexoki and dark mode, and each one carries a different weight and
// baseline. These are all drawn on the same 24px grid with the same stroke, in
// `currentColor`, so an icon inherits the color and hover state of the button
// it sits in, exactly like the text next to it did.
//
// Adding one: keep the 24 grid and let the stroke do the work (no fills, no
// stroke-width overrides) — that shared weight is the only reason a car and a
// checkmark read as members of one set.

const S = { fill: "none", stroke: "currentColor", strokeWidth: 1.75,
  strokeLinecap: "round", strokeLinejoin: "round" };

// Precipitation icons need the cloud lifted to make room for what falls out of
// it, so they get a compact one rather than a scaled copy (scaling would thin
// the stroke and break the match).
const CLOUD = "M17.5 19H9a7 7 0 1 1 6.71-9h1.79a4.5 4.5 0 1 1 0 9z";
const CLOUD_SM = "M16.5 15H8.5a5.5 5.5 0 1 1 5.27-7h1.23a3.5 3.5 0 1 1 0 7z";

const PATHS = {
  // ---- chrome: the verbs on every card
  refresh: ["M21 12a9 9 0 0 0-9-9 9.75 9.75 0 0 0-6.74 2.74L3 8", "M3 3v5h5",
    "M3 12a9 9 0 0 0 9 9 9.75 9.75 0 0 0 6.74-2.74L21 16", "M21 21v-5h-5"],
  close: ["M18 6 6 18", "M6 6l12 12"],
  plus: ["M12 5v14", "M5 12h14"],
  arrowUp: ["M12 19V5", "M5 12l7-7 7 7"],
  sliders: ["M4 7h9", "M19 7h1", "M16 5a2 2 0 1 0 0 4 2 2 0 0 0 0-4z",
    "M4 17h1", "M11 17h9", "M8 15a2 2 0 1 0 0 4 2 2 0 0 0 0-4z"],
  // `close` already means "delete" on cards that only delete (lists, list
  // items), so a card offering both a reversible and a permanent removal —
  // habits archive *and* delete — needs the permanent one to look different
  // or the two buttons read as the same button twice.
  trash: ["M4 7h16", "M10 11v6", "M14 11v6",
    "M5 7l1 12a2 2 0 0 0 2 2h8a2 2 0 0 0 2-2l1-12", "M9 7V4h6v3"],
  check: ["M20 6 9 17l-5-5"],
  expand: ["M15 3h6v6", "M9 21H3v-6", "M21 3l-7 7", "M3 21l7-7"],
  pin: ["M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18z", "M12 8v8", "M8 12h8"],
  edit: ["M12 20h9", "M16.5 3.5a2.121 2.121 0 0 1 3 3L7 19l-4 1 1-4z"],
  undo: ["M9 14 4 9l5-5", "M4 9h11a5 5 0 0 1 0 10h-3"],
  external: ["M7 17 17 7", "M8 7h9v9"],
  star: ["m12 3.5 2.7 5.6 6 .9-4.35 4.3 1.03 6.2L12 17.6 6.62 20.5l1.03-6.2L3.3 10l6-.9z"],
  clock: ["M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18z", "M12 7.5V12l3 1.8"],
  // A pushpin, for a pinned console tab ("pin" above is pin-to-console).
  tack: ["M9 3.5h6l-1 5.5 3 3H7l3-3z", "M12 12v8.5"],
  listCheck: ["M9 3h6a1 1 0 0 1 1 1v1a1 1 0 0 1-1 1H9a1 1 0 0 1-1-1V4a1 1 0 0 1 1-1z",
    "M16 4.5h2a2 2 0 0 1 2 2V20a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V6.5a2 2 0 0 1 2-2h2",
    "m9.5 14 1.8 1.8 3.7-3.7"],
  mute: ["M8.7 3.2A6 6 0 0 1 18 8c0 2 .2 3.6.5 4.8", "M17 17H3s3-2 3-9a5.9 5.9 0 0 1 .3-1.8",
    "M10.3 21a2 2 0 0 0 3.4 0", "m2.5 2.5 19 19"],
  checkCircle: ["M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18z", "m8.2 12.2 2.6 2.6 5-5"],
  // ---- voice: the console's ears and mouth
  mic: ["M12 3a3 3 0 0 0-3 3v6a3 3 0 0 0 6 0V6a3 3 0 0 0-3-3z",
    "M18 11v1a6 6 0 0 1-12 0v-1", "M12 18v3", "M9 21h6"],
  volume: ["M11 5 6.5 9H3v6h3.5L11 19z", "M15.5 8.5a5 5 0 0 1 0 7",
    "M18.5 5.5a9 9 0 0 1 0 13"],
  volumeOff: ["M11 5 6.5 9H3v6h3.5L11 19z", "m16 9.5 5 5", "m21 9.5-5 5"],
  arrowLeft: ["M19 12H5", "m12 19-7-7 7-7"],
  arrowRight: ["M5 12h14", "m12 5 7 7-7 7"],
  chevronDown: ["m6 9 6 6 6-6"],
  chevronRight: ["m9 6 6 6-6 6"],

  // ---- nouns: what a row is about
  // The frosting scallop is what separates a cake from a rounded rectangle,
  // and the flame is a dot rather than a teardrop — at 12px a filled teardrop
  // is a smudge, while a dot on a stick still reads as a lit candle.
  cake: ["M20 21v-8a2 2 0 0 0-2-2H6a2 2 0 0 0-2 2v8",
    "M4 16s.5-1 2-1 2.5 2 4 2 2.5-2 4-2 2.5 2 4 2 2-1 2-1",
    "M2 21h20", "M12 8v3", "M12 5.2h.01"],
  message: ["M20.5 12a8 8 0 0 1-11.7 7.1L4 20.5l1.4-4.8A8 8 0 1 1 20.5 12z"],
  // The citation set. An envelope needs its flap to be an envelope at 11px,
  // and the calendar needs its two rings — the header rule alone reads as a
  // plain framed box, which is what `expand` already looks like.
  mail: ["M4 6h16a1 1 0 0 1 1 1v10a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1z",
    "m3.6 7.2 8.4 5.9 8.4-5.9"],
  // Email filing: the label a message goes under, and finding it again.
  tag: ["M20.6 13.4 13.4 20.6a2 2 0 0 1-2.8 0L3 13V3h10l7.6 7.6a2 2 0 0 1 0 2.8z",
    "M7.5 7.5h.01"],
  search: ["M11 4a7 7 0 1 0 0 14 7 7 0 0 0 0-14z", "m20 20-4-4"],
  calendar: ["M5 5h14a1 1 0 0 1 1 1v13a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V6a1 1 0 0 1 1-1z",
    "M4 10h16", "M8 3v4", "M16 3v4"],
  paperclip: ["M20.9 11.1 12.3 19.7a5.2 5.2 0 0 1-7.4-7.4l8.6-8.6a3.5 3.5 0 0 1 4.9 4.9l-8.6 8.6a1.7 1.7 0 0 1-2.5-2.5l7.9-7.9"],
  // a page with its corner folded: a document citation
  file: ["M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z", "M14 3v5h5"],
  folder: ["M3 7a2 2 0 0 1 2-2h4l2 2.5h8a2 2 0 0 1 2 2V17a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"],
  music: ["M9 17.5V4.8L20 3v12.5", "M6 20.5a3 3 0 1 0 0-6 3 3 0 0 0 0 6z",
    "M17 18.5a3 3 0 1 0 0-6 3 3 0 0 0 0 6z"],
  car: ["M19 17h2a1 1 0 0 0 1-1v-3c0-.9-.7-1.7-1.5-1.9l-1.7-4.5A2 2 0 0 0 17 5.3H7a2 2 0 0 0-1.8 1.3L3.5 11.1C2.7 11.3 2 12.1 2 13v3a1 1 0 0 0 1 1h2",
    "M9 17h6", "M7 19a2 2 0 1 0 0-4 2 2 0 0 0 0 4z", "M17 19a2 2 0 1 0 0-4 2 2 0 0 0 0 4z"],

  // ---- weather (WMO buckets, day/night aware)
  sun: ["M12 8a4 4 0 1 0 0 8 4 4 0 0 0 0-8z",
    "M12 2v2", "M12 20v2", "M4.9 4.9l1.4 1.4", "M17.7 17.7l1.4 1.4",
    "M2 12h2", "M20 12h2", "M4.9 19.1l1.4-1.4", "M17.7 6.3l1.4-1.4"],
  moon: ["M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z"],
  cloud: [CLOUD],
  cloudSun: ["M12 2v2", "m4.93 4.93 1.41 1.41", "M20 12h2", "m19.07 4.93-1.41 1.41",
    "M15.95 12.65a4 4 0 0 0-5.93-4.13", "M13 22H7a5 5 0 1 1 4.9-6H13a3 3 0 0 1 0 6z"],
  cloudMoon: ["M10.19 8.5A6 6 0 0 1 16 4a1 1 0 0 0 6 6 6 6 0 0 1-3 5.2",
    "M13 22H7a5 5 0 1 1 4.9-6H13a3 3 0 0 1 0 6z"],
  fog: [CLOUD_SM, "M5 19h7", "M15 19h4", "M7 22h5", "M15 22h2"],
  rain: [CLOUD_SM, "M9 18.5 8 22", "M13 18.5 12 22", "M17 18.5 16 22"],
  snow: [CLOUD_SM, "M9 19h.01", "M13 19h.01", "M17 19h.01", "M11 22h.01", "M15 22h.01"],
  storm: [CLOUD_SM, "M13.5 17 10 21.5h3.5L11 24.5"],
  thermometer: ["M14 14.76V4.5a2.5 2.5 0 0 0-5 0v10.26a4.5 4.5 0 1 0 5 0z"],

  // ---- a dev tab's project scripts (.kestrel/project.json) and its PR
  play: ["M7 4.5v15a1 1 0 0 0 1.5.86l12-7.5a1 1 0 0 0 0-1.72l-12-7.5A1 1 0 0 0 7 4.5z"],
  stop: ["M7 6h10a1 1 0 0 1 1 1v10a1 1 0 0 1-1 1H7a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1z"],
  flask: ["M9 3h6", "M10 3v6.5L4.6 18.4A2 2 0 0 0 6.3 21.5h11.4a2 2 0 0 0 1.7-3.1L14 9.5V3", "M7.5 15h9"],
  sparkle: ["M12 3l1.9 5.6L19.5 10.5l-5.6 1.9L12 18l-1.9-5.6L4.5 10.5l5.6-1.9z", "M19 17v4", "M17 19h4"],
  hammer: ["M15 12l-8.5 8.5a2.12 2.12 0 0 1-3-3L12 9", "M17.64 15 22 10.64",
    "M20.91 11.7l-1.25-1.25a3.2 3.2 0 0 1-.93-2.25v-.86L16 4.6a5.56 5.56 0 0 0-3.94-1.64H9l.92.82A6.18 6.18 0 0 1 12 8.4V10l2 2h2.47l2.26 1.91"],
  pullRequest: ["M18 15a3 3 0 1 0 0 6 3 3 0 0 0 0-6z", "M6 3a3 3 0 1 0 0 6 3 3 0 0 0 0-6z",
    "M13 6h3a2 2 0 0 1 2 2v7", "M6 9v12"],
};

// `size` is a number of px, not em: these sit in 10–13px UI text, and letting
// them track font-size would make the small ones illegible.
export default function Icon({ name, size = 14, className = "", ...rest }) {
  const d = PATHS[name];
  if (!d) return null;
  return (
    <svg className={`icon${className ? ` ${className}` : ""}`}
      width={size} height={size} viewBox="0 0 24 24"
      aria-hidden="true" focusable="false" {...S} {...rest}>
      {d.map((p) => <path key={p} d={p} />)}
    </svg>
  );
}
