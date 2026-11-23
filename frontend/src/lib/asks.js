// Asks: the rows of the plan view's "needs you" list.
//
// A card's level says whether it needs you; its asks say *what for*, one row
// per thing you could do something about. Each card hook builds its own
// (widgets/cards.js), because the hook is what knows the data, and the list
// only orders and draws them.
//
// An ask:
//   id     stable across reloads — the snooze key (`task:12`, `email:review`)
//   card   the desk id it belongs to (opening it, hovering it)
//   tier   what kind of claim it has on you; the list sorts on it
//   kind   "late" | "fix" | "blocking" | "" — the dot's colour: a clock or a
//          broken source is red, anything else is amber; "blocking" (a
//          console card waiting on you) tints the whole row
//   title  what, in a few words
//   why    the reason line, ending in how long it has been true
//   acts   buttons, first one is what Enter does:
//          { label, style?, run }       does it here
//          { label, style?, sheet }     opens that full view
//          { label, style?, open: true } opens the card's detail
//          { label, style?, draft: { text, pin } } puts a prompt (and a pill)
//                                       in the console's composer, sends nothing
//   note   optional { path, title }: the vault note it's about (a pill on the
//          row — click opens it, drag pins it); see noteFor
//   deadline  optional day key: "later" offers the morning before it
//   clock  true for a leave-by: "later" offers minutes, not days

export const BLOCKING = 0; // a console turn is waiting on you
export const CLOCK = 1;    // something with a time on it: leave-by, overdue
export const BROKEN = 2;   // a source that has stopped working
export const OWED = 3;     // a person or a decision waiting on you

// "10 h", "3 d", "20 min" — how long something has been true, for the end
// of a reason line. Elapsed wall-clock time, so the browser clock is right
// here; day keys are a different question (see dayGap).
export function age(iso, now = Date.now()) {
  if (!iso) return null;
  const min = Math.max(0, (now - new Date(iso).getTime()) / 60000);
  if (min < 1) return "just now";
  if (min < 60) return `${Math.round(min)} min`;
  if (min < 48 * 60) return `${Math.round(min / 60)} h`;
  return `${Math.round(min / 1440)} d`;
}

// Whole days from day key `a` to day key `b` — both from the server, so the
// 4am rollover is already in them and noon keeps DST out of the division.
export function dayGap(a, b) {
  if (!a || !b) return null;
  return Math.round((new Date(`${b}T12:00:00`) - new Date(`${a}T12:00:00`)) / 864e5);
}

// The vault note an ask is about, when one can be told from the ask itself.
// Nothing links a task or an email to a note yet, so this starts with the
// case that's unambiguous: a course code in the title (a task "Review CSCI
// 103 slides", an office-hours change for MATH 229) names that course's note,
// which the Office hours card already knows the path of. The title only: a
// reason line can quote something else — the review queue's names its oldest
// email, and "[CSCI 170 on Gradescope]" doesn't make the queue about CSCI 170.
const COURSE_CODE = /\b([A-Z]{2,5})[ -]?(\d{3}[A-Z]?)\b/g;
export function noteFor(ask, courses) {
  for (const m of ask.title.matchAll(COURSE_CODE)) {
    const key = `${m[1]}${m[2]}`;
    const c = courses.find((x) => x.path && x.code.replace(/[\s-]/g, "") === key);
    if (c) return { path: c.path, title: c.code };
  }
  return null;
}

// Every card that needs you gets at least one row: the hook's own, or this.
export function genericAsk(id, name, summary, error) {
  return {
    id, card: id, tier: error ? BROKEN : OWED, kind: error ? "fix" : "",
    title: name, why: summary || "", acts: [{ label: "Open", open: true }],
  };
}

// Blocking a turn, then clocks, then broken sources, then everything else in
// desk order — and within one card, the order the hook gave.
export function orderAsks(asks, deskOrder) {
  const at = (id) => { const i = deskOrder.indexOf(id); return i < 0 ? 99 : i; };
  return asks
    .map((a, i) => [a, i])
    .sort(([a, i], [b, j]) => a.tier - b.tier || at(a.card) - at(b.card) || i - j)
    .map(([a]) => a);
}
