// Which number on a card is worth your eye — the salience rule applied one
// level down.
//
// `lib/salience.js` decides which *cards* the desk shows. It stopped short of
// the thing that actually made the desk tiring: a card that earned its slot
// still rendered a fixed inventory. HealthWidget printed the same four metrics
// every day, the screen card (since removed) four numbers plus a top-app list
// plus a sparkline plus a median delta, and the ranking work — which is different
// today? — was left to the reader every single morning. Twelve cards doing
// that is a spreadsheet with a card border drawn round each column.
//
// So a card now leads with one reading, chosen here, and says nothing at all
// when nothing broke pattern. The rest of the numbers keep existing; they live
// in the sheet and on the end of a pin, which is where the console reads them.
//
// Two gates, and a reading has to clear BOTH:
//
//   statistical — is this outside the metric's own noise? (|z| ≥ Z_FLOOR,
//     robust z, see below)
//   practical   — is the change big enough to be worth a sentence? (`floor`,
//     declared by the widget in the metric's own units)
//
// Neither works alone, and the failure each one prevents is concrete. Resting
// HR sits in a 68–76 band: its spread is so tight that a purely statistical
// test calls +3 bpm a two-sigma event and the card cries wolf every other
// Tuesday. Steps swing 2.7k–22k: a purely practical test with any floor low
// enough to catch a real collapse fires on an ordinary Sunday. One gate is a
// noisy card, the other is a card that lies by omission; together they mean
// "unusual for this metric AND big enough to say out loud".

// Robust rather than mean/stdev, because the input is 28 days of human
// behaviour with outliers in it by construction. One 22k-step day drags a mean
// far enough that the genuinely quiet days after it read as the anomaly — the
// spike rewrites the baseline it should have been measured against. Median and
// MAD don't move.
const MAD_TO_SIGMA = 1.4826;   // makes MAD comparable to stdev on normal data

// Below this many observed days there is no baseline, so there is no such
// thing as unusual and the card says nothing. Deliberately not 2 or 3: with a
// handful of points the median is one of the points, MAD is often 0, and every
// reading looks significant. A card that starts making claims on day three of
// a new sensor is how §7 and §10 both got caught calling dead-source gaps a
// behaviour change.
const MIN_BASELINE = 8;

// How far outside the metric's own spread a reading has to sit. Calibrated by
// replaying 28 real days, not picked: at 1.5 the card led on 39% of them, and
// the separation in the data is unusually clean about where the line goes. The
// three step-count days that fired at 1.5 all sat at z ≈ 1.6–1.7, while every
// reading a person would actually want flagged — a 1.5-hour night, resting HR
// at the month's high, a 233-minute zone day — sat at 2.7 or beyond. Nothing
// real lives in between, so 2.0 splits them and takes the rate to 25%.
const Z_FLOOR = 2.0;

// The gate that turned out to matter most, added after replaying 28 real days
// through the first version of this file: it led on 46% of them. Every other
// morning is not "unusual", and the specific failures said why. Four separate
// ~21k-step days each got announced as a spike, and three ~230-minute zone
// days likewise — because MAD measures the spread of the *dense* part of the
// distribution, so on a bimodal metric (most days ~10k, a handful of long
// walking days at 21k) every member of the small mode reads as a tail event.
// It is a tail, statistically. It is also just a thing this person does four
// times a month, and a card that says so every time is a card you stop
// reading.
//
// So: a reading is notable only if the baseline itself rarely goes there.
// Count the past days at least this extreme in the same direction; if more
// than RARITY of them were, this is a mode, not news. That's an empirical
// p-value, which is the honest test on 28 points of non-normal human data —
// z stays on as a ranking key, not as the arbiter.
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
 * Score one reading against its own history.
 *
 * @param {object} r
 * @param {string} r.key            stable id, for React and for pin labels
 * @param {string} r.label          human name ("Resting HR")
 * @param {number|null} r.value     today's reading
 * @param {Array} r.series          [{day, value}] or [number]; nulls are gaps,
 *                                  never zeros — a day the sensor missed is not
 *                                  a day the number was 0, and treating it as
 *                                  one poisons every comparison after it
 * @param {number} r.floor          smallest deviation worth a sentence, in the
 *                                  metric's units. The widget declares it
 *                                  because the widget is what knows that 3 bpm
 *                                  is a lot and 3 steps is not
 * @param {"up"|"down"|null} r.worse which direction is the bad one, for tone
 * @param {(v:number)=>string} r.fmt how to render the value
 * @returns {object|null} the reading, annotated, or null if not notable
 */
export function score(r) {
  if (r.value == null || !Number.isFinite(r.value)) return null;

  // The baseline is the history *without* today in it. Leaving today's point
  // in makes a reading part of the distribution it's being tested against,
  // which drags the median toward it and shrinks the very deviation being
  // measured — the more extreme the day, the more it hides itself.
  // Accepts either shape the desk already has lying around: the {day, value}
  // series the providers return, or a bare number array. With day keys the
  // reading is excluded by key; without them the last point is assumed to be
  // it, which is what a sparkline array means by convention here.
  const pts = (r.series || []).map(
    (p) => (typeof p === "number" ? { day: null, value: p } : p || {}));
  const past = finite(
    r.day ? pts.filter((p) => p.day !== r.day).map((p) => p.value)
          : pts.slice(0, -1).map((p) => p.value));
  if (past.length < MIN_BASELINE) return null;

  const med = median(past);
  const sigma = spread(past, med);
  const delta = r.value - med;

  // Practical gate first — it's the cheap one, and it's the one that fires.
  if (Math.abs(delta) < r.floor) return null;

  // A flat baseline (MAD 0) means every past day read the same. That happens
  // on integer-ish metrics with a narrow range, and dividing by it would make
  // any deviation infinitely significant. Having already cleared `floor`,
  // treat it as notable rather than as a divide-by-zero.
  const z = sigma ? delta / sigma : Infinity;
  if (Math.abs(z) < Z_FLOOR) return null;

  // Rarity gate. Same direction only: a 21k-step day is not made ordinary by
  // the existence of 2k-step days, and folding both tails together would let
  // a metric that swings hard both ways silence itself.
  const asFar = past.filter((v) => (delta > 0 ? v >= r.value : v <= r.value)).length;
  if (asFar / past.length > RARITY) return null;

  return {
    ...r,
    median: med,
    delta,
    z,
    n: past.length,
    // How many days in the window went at least this far. Surfaced because
    // "first time in 28 days" is the sentence the number is actually making,
    // and a card that can say it doesn't have to be believed on trust.
    asFar,
    // Tone, not direction: `up`/`down` is arithmetic the caller can already do.
    // This says whether to render it as a concern, and stays null for the
    // metrics where neither direction is bad (distance travelled, minutes
    // listened) so nothing gets a red number for having a Saturday.
    tone: r.worse == null ? null : (delta > 0) === (r.worse === "up") ? "bad" : "good",
  };
}

/**
 * The card's lede: the most unusual reading, or null on an ordinary day.
 *
 * Ranked by |z| rather than by |delta| or by the order the widget listed them:
 * the whole point is that the card doesn't have a favourite metric. Steps
 * being 8k down and resting HR being 4 bpm up are not comparable in their own
 * units, and "sleep first because sleep is listed first" is the fixed-order
 * desk all over again, one level in.
 */
export function lead(readings) {
  const scored = readings.map(score).filter(Boolean);
  scored.sort((a, b) => Math.abs(b.z) - Math.abs(a.z));
  return scored[0] || null;
}

/**
 * The muted line under the lede — every reading that has a value, in the order
 * the widget listed them, at one glance's worth of detail.
 *
 * This is also what `Slot` shows when the card folds, and that is on purpose:
 * a folded card and an open quiet card should say the same thing, or folding
 * would be hiding something. It reads live data, never an empty state — the
 * rule DESIGN.md records about collapsed summaries, which only started
 * mattering once cards holding real numbers began collapsing too.
 */
export function restLine(readings) {
  return readings
    .filter((r) => r.value != null && Number.isFinite(r.value))
    .map((r) => `${r.label.toLowerCase()} ${r.fmt(r.value)}`)
    .join(" · ");
}

// How the delta reads in the lede's second line. Signed in the metric's own
// units, against a median that names how many days it stands on — "vs your
// 17-day median" is checkable, "vs typical" is a claim you can't audit, and
// the n moves around here because a watch worn 17 nights out of 28 should not
// be quoted as though it were 28.
export function deltaLine(hit) {
  const sign = hit.delta > 0 ? "↑" : "↓";
  return `${sign}${hit.fmt(Math.abs(hit.delta))} vs your ${hit.n}-day median of ${hit.fmt(hit.median)}`;
}

export { MIN_BASELINE, Z_FLOOR, RARITY };
