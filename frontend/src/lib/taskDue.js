// Quick-add's due parser: "draft the memo @ 5pm" -> title + due date with an hour. A trailing `@ ...` is the whole
// grammar; anything unrecognised stays in the title, and what was recognised is echoed before commit.
// `today` is the backend's day key (4am rollover), never a browser date: at 1am they differ, exactly when "@ 2am" is typed.

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
    // A bare small hour is an evening ("@ 5" is 5pm); 8-12 stay as written.
    h += 12;
  }
  return `${String(h).padStart(2, "0")}:${String(min).padStart(2, "0")}`;
}

function addDays(day, n) {
  // Noon, so a DST jump can't move the date.
  const d = new Date(`${day}T12:00:00`);
  d.setDate(d.getDate() + n);
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}

// Day part of a `@ ...` phrase against the server's day key; null if unclaimed.
function parseDay(raw, today) {
  const word = raw.toLowerCase();
  if (word in WORD_DAYS) return addDays(today, WORD_DAYS[word]);
  // "9/25[/26]", month/day; a past date isn't rolled forward (likelier a typo than next September).
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

// Split "title @ when" into { title, due, due_time, hint }. `hint` echoes what was understood (null with no `@`);
// an unparseable `@` returns the whole string as title with `unparsed` set, so the widget can say so.
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

  // No day named: the next time that clock comes round, compared as instants through the backend's rollover rule
  // (db.day_instant): at 1am "@ 2am" is an hour away and still today's day key.
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
