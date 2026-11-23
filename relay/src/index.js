// Location relay — OwnTracks posts here; the dashboard pulls from here.
//
// Replaces the phone → Tailscale Funnel → Mac listener chain, which needed the
// Mac awake, Tailscale connected, Funnel configured and the listener running
// all at once, and went silently dark whenever any one of them wasn't. This
// Worker is always up, so the phone never waits on the Mac: points queue here
// until the backend's poller pulls them (providers/location.py `_pull_relay`),
// and are deleted once it acknowledges them. Nothing on the Mac is reachable
// from the internet any more.
//
//   POST /ingest   OwnTracks (HTTP mode). Basic auth, token in the password
//                  field (OwnTracks iOS can send nothing else); Bearer too.
//                  Stores `_type: location` messages verbatim. Replies `[]`.
//   GET  /pull     Backend. Bearer PULL_TOKEN. ?after=<id>&limit=<n> →
//                  {points: [{id, msg}], last_contact}
//   POST /ack      Backend. Bearer PULL_TOKEN. {upto: id} deletes id <= upto.
//
// Two tokens on purpose: the phone's can only add points, so a leaked phone
// config can't read your location history back out.
//
// Parsing and unit normalisation stay backend-side (location._row), so this
// file never needs redeploying when that logic changes.

const JSON_HEADERS = { "content-type": "application/json" };

function json(body, status = 200) {
  return new Response(JSON.stringify(body), { status, headers: JSON_HEADERS });
}

// Constant-time string compare — timingSafeEqual needs equal lengths, and a
// length mismatch is already a "no".
function same(a, b) {
  const enc = new TextEncoder();
  const x = enc.encode(a), y = enc.encode(b);
  if (x.length !== y.length) return false;
  return crypto.subtle.timingSafeEqual(x, y);
}

// The presented secret: Basic's password, or a Bearer token.
function presented(request) {
  const [scheme, value = ""] = (request.headers.get("authorization") || "").split(" ", 2);
  if (scheme?.toLowerCase() === "bearer") return value.trim();
  if (scheme?.toLowerCase() === "basic") {
    try {
      const decoded = atob(value);
      return decoded.slice(decoded.indexOf(":") + 1);
    } catch {
      return "";
    }
  }
  return "";
}

// 403, never 401: OwnTracks retries any non-2xx, and a 401 invites a client to
// pop a credential prompt nobody is there to answer.
function authorized(request, secret) {
  const got = presented(request);
  return Boolean(secret) && Boolean(got) && same(got, secret);
}

async function ingest(request, env) {
  if (!authorized(request, env.INGEST_TOKEN)) return json({ error: "forbidden" }, 403);
  let body;
  try {
    body = await request.json();
  } catch {
    return json([]);           // malformed: nothing to keep, and a retry won't fix it
  }
  const messages = (Array.isArray(body) ? body : [body])
    .filter((m) => m && typeof m === "object" && m._type === "location");
  const now = Date.now();
  const stmts = messages.map((m) =>
    env.DB.prepare("INSERT INTO points (msg, received_at) VALUES (?, ?)")
      .bind(JSON.stringify(m), now));
  // Any authorised request is contact — a status or ping message says the
  // app is alive even when there's no movement to report.
  stmts.push(env.DB.prepare(
    "INSERT INTO meta (key, value) VALUES ('last_contact', ?)"
    + " ON CONFLICT(key) DO UPDATE SET value = excluded.value").bind(String(now)));
  await env.DB.batch(stmts);
  return json([]);
}

async function pull(request, env) {
  if (!authorized(request, env.PULL_TOKEN)) return json({ error: "forbidden" }, 403);
  const url = new URL(request.url);
  const after = Math.max(0, parseInt(url.searchParams.get("after") || "0", 10) || 0);
  const limit = Math.min(1000, Math.max(1, parseInt(url.searchParams.get("limit") || "500", 10) || 500));
  const [rows, meta] = await env.DB.batch([
    env.DB.prepare("SELECT id, msg FROM points WHERE id > ? ORDER BY id LIMIT ?").bind(after, limit),
    env.DB.prepare("SELECT value FROM meta WHERE key = 'last_contact'"),
  ]);
  return json({
    points: rows.results.map((r) => ({ id: r.id, msg: JSON.parse(r.msg) })),
    last_contact: meta.results[0] ? Number(meta.results[0].value) : null,
  });
}

async function ack(request, env) {
  if (!authorized(request, env.PULL_TOKEN)) return json({ error: "forbidden" }, 403);
  let upto;
  try {
    upto = Number((await request.json()).upto);
  } catch {
    upto = NaN;
  }
  if (!Number.isInteger(upto) || upto < 0) return json({ error: "upto must be an integer id" }, 400);
  const res = await env.DB.prepare("DELETE FROM points WHERE id <= ?").bind(upto).run();
  return json({ deleted: res.meta.changes });
}

export default {
  async fetch(request, env) {
    const { pathname } = new URL(request.url);
    const route = `${request.method} ${pathname}`;
    if (route === "POST /ingest") return ingest(request, env);
    if (route === "GET /pull") return pull(request, env);
    if (route === "POST /ack") return ack(request, env);
    return json({ error: "not found" }, 404);
  },
};
