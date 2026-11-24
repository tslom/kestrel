// Picking a tab back up after the usage limit cut it off (console/LimitBanner.jsx); plain functions (tests/autoResume.test.mjs).
// Once the shut window resets, wait a per-tab offset (cut-off tabs don't fire at once), then send Resume's message.
// A refused resume waits for the next reset; attempts are at least MIN_GAP_MS apart, doubling. Stopped or opted-out tabs are left alone.

export const AUTO_KEY = "console_auto_resume";          // "0" = off; on by default
export const MIN_GAP_MS = 5 * 60_000;
const MAX_GAP_MS = 60 * 60_000;
// Shut, but no reset time anywhere: look again this long after the cut.
const UNKNOWN_RESET_MS = 60 * 60_000;

export const autoResumeOn = () => {
  try { return localStorage.getItem(AUTO_KEY) !== "0"; } catch { return true; }
};

// Per tab: { last, tries } — when the last automatic resume went, and how
// many have gone since a turn last finished clean. Survives a reload.
const autoKey = (tab) => `console_autoresume:${tab}`;
export function readAuto(tab) {
  try { return JSON.parse(localStorage.getItem(autoKey(tab))) || { last: 0, tries: 0 }; }
  catch { return { last: 0, tries: 0 }; }
}
export function writeAuto(tab, v) {
  try {
    if (v) localStorage.setItem(autoKey(tab), JSON.stringify(v));
    else localStorage.removeItem(autoKey(tab));
  } catch { /* a nicety */ }
}

// The tab's own rate_limit events as windows: they arrive the moment a window shuts; the poll is minutes behind (and pauses when hidden).
export function eventWindows(rateLimits) {
  return Object.entries(rateLimits || {}).map(([key, ev]) => ({
    key, status: ev.status, resets_at: ev.resets_at ?? null,
    percent: ev.utilization != null ? ev.utilization * 100 : null,
  }));
}

// A per-model week ("seven_day_fable") only binds a tab running that model.
function binds(w, model) {
  const m = /^seven_day_(.+)$/.exec(w.key || "");
  if (!m || !model) return true;
  return String(model).toLowerCase().includes(m[1].toLowerCase());
}

// { open, resetsAt }: whether every window that binds this tab is open, and
// when the last shut one resets (ms; null when none says). "allowed_warning"
// is still open; a window past its reset is open whatever it last said.
export function windowState(windows, { model = null, now = Date.now() } = {}) {
  const shut = (windows || []).filter((w) => binds(w, model)
    && (w.status === "rejected" || (w.percent ?? 0) >= 100));
  if (!shut.length) return { open: true, resetsAt: null };
  const times = shut.map((w) => (w.resets_at || 0) * 1000);
  if (times.some((t) => !t)) {
    // One of them doesn't say when; only a known future reset counts as shut.
    const known = times.filter(Boolean);
    const latest = known.length ? Math.max(...known) : null;
    return { open: false, resetsAt: latest && latest > now ? latest : null };
  }
  const resetsAt = Math.max(...times);
  return { open: resetsAt <= now, resetsAt };
}

// 20s to 2m20s after the reset, the same for a tab every time.
export function jitterMs(tab) {
  let h = 0;
  for (const c of String(tab)) h = (h * 31 + c.charCodeAt(0)) >>> 0;
  return 20_000 + (h % 120) * 1000;
}

export const gapMs = (tries) => (tries > 0 ? Math.min(MAX_GAP_MS, MIN_GAP_MS * 2 ** (tries - 1)) : 0);

// When to resume, or null when it shouldn't: { at, resetsAt } (ms).
// `limited`: the tab's record ({ at, manual }); `auto`: readAuto(tab).
export function planResume({ limited, windows, model = null, tab, auto = { last: 0, tries: 0 },
                             now = Date.now(), on = true }) {
  if (!on || !limited || limited.manual) return null;
  const { open, resetsAt } = windowState(windows, { model, now });
  // Anchored to fixed times (reset, cut, last try), never `now`, or the moment would run away.
  let at;
  if (resetsAt) at = resetsAt + jitterMs(tab);
  // Open, with no reset known: the readings predate the cut. Give it the gap.
  else if (open) at = (limited.at || now) + MIN_GAP_MS + jitterMs(tab);
  else at = (limited.at || now) + UNKNOWN_RESET_MS;
  // Never sooner than the gap after the last try (doubling as tries mount).
  if (auto.last) at = Math.max(at, auto.last + Math.max(MIN_GAP_MS, gapMs(auto.tries)));
  return { at, resetsAt };
}
