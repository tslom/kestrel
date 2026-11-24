// Client-side failure reporting to /api/failures (upserts by fingerprint: a card throwing every render is one row).
// Deliberately bare `fetch`, never `fetchJSON`: a down backend would turn one error into an unhandled rejection
// that reports, fails and rejects again. `.catch` swallows; best-effort by construction.

const ENDPOINT = "/api/failures";

// The console.error stays: devtools is the fastest read when open.
export function report(kind, label, detail, trace) {
  try {
    // eslint-disable-next-line no-restricted-syntax -- must not go through fetchJSON (above)
    fetch(ENDPOINT, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        kind,
        label: String(label || "unknown").slice(0, 200),
        detail: detail == null ? null : String(detail),
        trace: trace == null ? null : String(trace),
      }),
      // The page may be unloading: keepalive outlives the document.
      keepalive: true,
    }).catch(() => {});
  } catch {
    /* nothing left to report to */
  }
}

// Installed once from main.jsx: covers what an error boundary can't see (throws outside render, unawaited rejections).
export function installGlobalHandlers() {
  window.addEventListener("error", (e) => {
    // Resource load failures (a missing <img>) arrive in volume with no `error`: drop them.
    if (!e.error) return;
    report("unhandled", e.error.name || "Error",
           e.error.message || String(e.error), e.error.stack);
  });

  window.addEventListener("unhandledrejection", (e) => {
    const r = e.reason;
    report("promise", r?.name || "Rejection",
           r?.message || String(r), r?.stack);
  });
}
