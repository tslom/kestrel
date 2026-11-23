// Quick-add's due parser: "draft the memo @ 5pm" -> a title and a due date
// with an hour on it.
//
// The widget's one input is the only place a task gets created from the
// dashboard, so without this there is no way to give a task the time a
// countdown needs — the API takes one, but only the console was reaching it.
// A trailing `@ …` is the whole grammar. It is deliberately small: anything it
// doesn't recognise stays part of the title rather than being guessed at, and
// what it *did* recognise is echoed back under the input before you commit, so
// it never quietly reschedules something you meant to name.
//
// `today` is the backend's day key, never a browser date. The 4am rollover is
// the server's call (db.day_key), and at 1am the two disagree by a day — which
// is exactly when "@ 2am" is the thing being typed.

const WORD_DAYS = {
  today: 0, tonight: 0, tmr: 1, tom: 1, tomorrow: 1,
};

// "5pm", "5:30pm", "17:00", "5" — to 'HH:MM', or null.
function parseClock(raw) {
  const m = /^(\d{1,2})(?::(\d{2}))?\s*(am|pm)?$/i.exec(raw.trim());
  if (!m) return null;
  let h = Number(m[1]);
  const min = m[2] ? Number(m[2]) : 0;
  const mer = m[3] && m[3].toLowerCase();
  if (min > 59) return null;
  if (mer) {
    if (h < 1 || h > 12) return null;
    h = (h % 12) + (mer === "pm" ? 12 : 0);
  } else if (h > 23) {
    return null;
  } else if (!m[2] && h >= 1 && h <= 7) {
    // A bare small hour is an evening: "@ 5" is 5pm, not dawn. 8–12 stay as
    // written, where morning is at least as likely as night.
    h += 12;
  }
  return `${String(h).padStart(2, "0")}:${String(min).padStart(2, "0")}`;
}

function addDays(day, n) {
  // Noon, so a DST jump can't move the date out from under the arithmetic.
  const d = new Date(`${day}T12:00:00`);
  d.setDate(d.getDate() + n);
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}

// Resolve the day part of a `@ …` phrase against the server's day key.
// Returns null for a phrase this parser doesn't claim.
function parseDay(raw, today) {
  const word = raw.toLowerCase();
  if (word in WORD_DAYS) return addDays(today, WORD_DAYS[word]);
  // "9/25" or "9/25/26" — month/day in the local convention, since that's what
  // gets typed here. A date already past this year is not rolled forward; it's
  // more likely a typo than a plan for next September.
  const md = /^(\d{1,2})\/(\d{1,2})(?:\/(\d{2,4}))?$/.exec(word);
  if (md) {
    const year = md[3]
      ? Number(md[3].length === 2 ? `20${md[3]}` : md[3])
      : Number(today.slice(0, 4));
    return `${year}-${String(Number(md[1])).padStart(2, "0")}-${String(Number(md[2])).padStart(2, "0")}`;
  }
  // "2026-09-25" as typed.
  if (/^\d{4}-\d{2}-\d{2}$/.test(word)) return word;
  return null;
}

// Split "title @ when" into { title, due, due_time, hint }.
//
// `hint` is the human echo of what was understood ("tomorrow 5:00 PM"), or null
// when there was no `@` at all. When an `@` is present but unparseable, the
// whole string comes back as the title with `unparsed` set, so the widget can
// say so rather than silently filing "call mum @ hlaf 5".
export function parseQuickDue(input, today) {
  const at = input.lastIndexOf("@");
  if (at === -1 || !today) return { title: input.trim(), due: null, due_time: null, hint: null };

  const title = input.slice(0, at).trim();
  const phrase = input.slice(at + 1).trim();
  const fail = { title: input.trim(), due: null, due_time: null, hint: null, unparsed: true };
  if (!title || !phrase) return fail;

  // At most two words: an optional day, then a clock time.
  const parts = phrase.split(/\s+/);
  let day = null, clock = null;
  if (parts.length === 1) {
    clock = parseClock(parts[0]);
    if (!clock) {
      // "@ tomorrow" with no hour is still a due date, just an all-day one.
      day = parseDay(parts[0], today);
      if (!day) return fail;
    }
  } else if (parts.length === 2) {
    day = parseDay(parts[0], today);
    clock = parseClock(parts[1]);
    if (!day || !clock) return fail;
  } else {
    return fail;
  }

  // A time with no day named means the next time that clock comes round: later
  // today, or tomorrow if it has already gone. Compared as instants, and
  // through the same rollover rule the backend uses (db.day_instant) — at 1am,
  // "@ 2am" is an hour away and still belongs to today's day key.
  if (!day) {
    day = today;
    const at2am = Number(clock.slice(0, 2)) < 4 ? addDays(day, 1) : day;
    if (new Date(`${at2am}T${clock}`).getTime() <= Date.now()) day = addDays(day, 1);
  }

  const hint = clock
    ? `${day} ${fmtHint(clock)}`
    : `${day}`;
  return { title, due: day, due_time: clock, hint };
}

function fmtHint(clock) {
  const h = Number(clock.slice(0, 2));
  return `${h % 12 || 12}:${clock.slice(3)} ${h < 12 ? "AM" : "PM"}`;
}
