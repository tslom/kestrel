// Backend fetch helper. Error bodies aren't always JSON (an unhandled exception is plain-text "Internal Server Error"): parse defensively so JSON.parse's SyntaxError never masks the real failure.
async function request(url, opts) {
  const r = await fetch(url, opts);
  const text = await r.text();
  let body = null;
  try { body = JSON.parse(text); } catch { /* non-JSON body */ }
  if (!r.ok) throw new Error(body?.detail || text.trim() || r.statusText);
  return body;
}

// Coalesce identical in-flight bodiless GETs (the desk mounts cards that read the same endpoints together).
// Not a cache: the entry drops when the request settles. Writes are never shared.
const inflight = new Map();

export async function fetchJSON(url, opts) {
  const method = opts?.method || "GET";
  if (method !== "GET" || opts?.body) return request(url, opts);

  const pending = inflight.get(url);
  // Each sharer gets its own clone: one shared object would leak a widget's edits into another.
  if (pending) return structuredClone(await pending);

  const p = request(url, opts);
  inflight.set(url, p);
  const done = () => { if (inflight.get(url) === p) inflight.delete(url); };
  p.then(done, done);  // not .finally(): its derived promise would reject unhandled
  return p;
}

// The mutating half. FastAPI rejects a JSON body without Content-Type, which a hand-written call drops silently,
// so the header isn't a parameter; only `method` varies (PATCH). Bodiless calls use `fetchJSON(url, { method: "DELETE" })`.
export function postJSON(url, body, method = "POST") {
  return fetchJSON(url, {
    method,
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

// A provider read that couldn't reach its source answers 200 from the app.db cache with `stale: true` and why
// (providers/offline.py), not a 500 that broke the whole desk. Takes several payloads (a card is several reads);
// nulls pass through (still loading isn't stale).
export function staleReason(...payloads) {
  const why = payloads.flatMap((p) => p?.offline || []);
  return why.length ? [...new Set(why)].join("\n") : null;
}
