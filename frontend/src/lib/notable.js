// Which number on a card is worth your eye: score a reading against its own history.
// A reading must clear a statistical gate (robust z) AND a practical one (`floor`).
// Design: DESIGN.md → Card salience (`lib/notable.js`).

// Median/MAD, not mean/stdev: one 22k-step day would drag a mean and hide the quiet days after it.
const MAD_TO_SIGMA = 1.4826;   // makes MAD comparable to stdev on normal data

// Below this many observed days there is no baseline (MAD is often 0, everything looks significant).
const MIN_BASELINE = 8;

// Calibrated on 28 real days: false steps firings sat at z 1.6-1.7, real events at 2.7+.
const Z_FLOOR = 2.0;

// Share of past days at least as extreme (same direction) above which the reading is a habit, not news.
const RARITY = 0.1;

const finite = (xs) => xs.filter((v) => typeof v === "number" && Number.isFinite(v));

export function median(xs) {
  const s = finite(xs).slice().sort((a, b) => a - b);
  if (!s.length) return null;
  const m = s.length >> 1;
  return s.length % 2 ? s[m] : (s[m - 1] + s[m]) / 2;
}

// Median absolute deviation, scaled to read like a standard deviation.
function spread(xs, med) {
  const mad = median(finite(xs).map((v) => Math.abs(v - med)));
  return mad == null ? null : mad * MAD_TO_SIGMA;
}

/**
 * Score one reading against its own history; null if not notable.
 * r: {key, label, value, series: [{day,value}]|[number] (null = gap, never 0),
 * floor (smallest worthwhile deviation, metric units), worse: "up"|"down"|null, fmt}.
 */
export function score(r) {
  if (r.value == null || !Number.isFinite(r.value)) return null;

  // Baseline excludes today (by day key, else the last point); today in it hides its own deviation.
  const pts = (r.series || []).map(
    (p) => (typeof p === "number" ? { day: null, value: p } : p || {}));
  const past = finite(
    r.day ? pts.filter((p) => p.day !== r.day).map((p) => p.value)
          : pts.slice(0, -1).map((p) => p.value));
  if (past.length < MIN_BASELINE) return null;

  const med = median(past);
  const sigma = spread(past, med);
  const delta = r.value - med;

  // Practical gate first: cheap, and the one that fires.
  if (Math.abs(delta) < r.floor) return null;

  // Flat baseline (MAD 0): z is Infinity on purpose; `floor` already cleared.
  const z = sigma ? delta / sigma : Infinity;
  if (Math.abs(z) < Z_FLOOR) return null;

  // Rarity gate, same direction only: 2k-step days must not make a 21k day ordinary.
  const asFar = past.filter((v) => (delta > 0 ? v >= r.value : v <= r.value)).length;
  if (asFar / past.length > RARITY) return null;

  return {
    ...r,
    median: med,
    delta,
    z,
    n: past.length,
    // Days in the window at least this far: "first time in 28 days".
    asFar,
    // Tone, not direction; null where neither direction is bad (distance, minutes listened).
    tone: r.worse == null ? null : (delta > 0) === (r.worse === "up") ? "bad" : "good",
  };
}

/** The most unusual reading by |z| (units differ, so no fixed favourite metric), or null. */
export function lead(readings) {
  const scored = readings.map(score).filter(Boolean);
  scored.sort((a, b) => Math.abs(b.z) - Math.abs(a.z));
  return scored[0] || null;
}

/** Muted line: every reading with a value, in widget order; also what `Slot` shows folded. */
export function restLine(readings) {
  return readings
    .filter((r) => r.value != null && Number.isFinite(r.value))
    .map((r) => `${r.label.toLowerCase()} ${r.fmt(r.value)}`)
    .join(" · ");
}

// Signed delta vs a median that names its day count: "17-day median" is auditable, "typical" is not.
export function deltaLine(hit) {
  const sign = hit.delta > 0 ? "↑" : "↓";
  return `${sign}${hit.fmt(Math.abs(hit.delta))} vs your ${hit.n}-day median of ${hit.fmt(hit.median)}`;
}

export { MIN_BASELINE, Z_FLOOR, RARITY };
