// Backend fetch helper. Error bodies aren't always JSON — an unhandled
// backend exception arrives as plain-text "Internal Server Error" — so
// parse defensively: JSON.parse's own SyntaxError must never mask the real
// failure (the "Unexpected token 'I'" widget bug).
async function request(url, opts) {
  const r = await fetch(url, opts);
  const text = await r.text();
  let body = null;
  try { body = JSON.parse(text); } catch { /* non-JSON body */ }
  if (!r.ok) throw new Error(body?.detail || text.trim() || r.statusText);
  return body;
}

// Two cards asking the same question in the same tick shouldn't ask twice.
// The glance strip opens on /api/calendar, /api/tasks, /api/email/queue and
// /api/habits — the same four reads the cards below it make — and the desk
// mounts them all together, so every load and every sync sent each of those
// twice.
//
// A coalesce, not a cache: the entry is dropped the moment the request
// settles, so the next read still goes to the network and nothing here can
// hand back an answer from a previous moment. Only bodiless GETs qualify;
// a write is never something you want silently shared.
const inflight = new Map();

export async function fetchJSON(url, opts) {
  const method = opts?.method || "GET";
  if (method !== "GET" || opts?.body) return request(url, opts);

  const pending = inflight.get(url);
  // Each sharer gets its own copy of the body. Handing back one object would
  // make a widget that edits its own state visible inside another widget —
  // a bug worth far more than the microsecond the clone costs.
  if (pending) return structuredClone(await pending);

  const p = request(url, opts);
  inflight.set(url, p);
  const done = () => { if (inflight.get(url) === p) inflight.delete(url); };
  p.then(done, done);  // not .finally(): its derived promise would reject unhandled
  return p;
}

// The mutating half, through the same parser. Every write the desk makes is a
// JSON body, and FastAPI rejects one that arrives without the Content-Type
// header — which a hand-written call site drops silently, and only fails at
// runtime. So the header isn't a parameter: `method` is the only thing a caller
// ever varies (PATCH, for the edit forms). Bodiless calls keep spelling out
// `fetchJSON(url, { method: "DELETE" })` — nothing to serialize, no header to
// get wrong, and no reason to route them through here.
export function postJSON(url, body, method = "POST") {
  return fetchJSON(url, {
    method,
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

// A provider read that couldn't reach its source still answers 200, from the
// app.db cache, carrying `stale: true` and why (backend: providers/offline.py).
// That used to be a 500 per card — the whole desk breaking at once, which on a
// lid opened before wifi came back read as the backend being dead.
//
// Takes payloads rather than one, because a card is usually several reads (four
// health metrics, three trend series) and any one of them going stale makes the
// card stale. Nulls pass through: a payload still loading isn't stale.
export function staleReason(...payloads) {
  const why = payloads.flatMap((p) => p?.offline || []);
  return why.length ? [...new Set(why)].join("\n") : null;
}
