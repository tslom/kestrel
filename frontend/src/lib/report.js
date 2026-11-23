// Client-side failure reporting. Everything here posts to /api/failures,
// which upserts by fingerprint — so a card throwing on every render costs one
// row with a large `count`, not a row per render.
//
// Deliberately not `fetchJSON`: a report must never travel through the helper
// whose failures it exists to record, or a backend that is down turns one
// error into an unhandled rejection, which the handler below reports, which
// fails, which rejects again. `fetch` directly, `.catch` swallowing, and a
// send-side guard against reporting our own endpoint.

const ENDPOINT = "/api/failures";

// Reporting is best-effort by construction: if it fails there is nowhere left
// to say so. The console.error stays because devtools is still the fastest
// read when you happen to have it open.
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
      // The page may be unloading when this fires (an error thrown during
      // navigation away). keepalive lets the request outlive the document.
      keepalive: true,
    }).catch(() => {});
  } catch {
    /* nothing left to report to */
  }
}

// Installed once from main.jsx. Covers the two things a React error boundary
// structurally cannot see: a throw from outside the render tree (an event
// handler, a timer, a websocket callback) and a rejected promise nobody
// awaited — which is what every un-caught `fetchJSON` in a `useEffect`
// becomes.
export function installGlobalHandlers() {
  window.addEventListener("error", (e) => {
    // Resource load failures (a missing <img>) fire here too, with no
    // `error` object. They are not what this is for and they arrive in
    // volume, so drop them rather than filling the log.
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
