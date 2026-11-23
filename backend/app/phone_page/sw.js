// The phone page's service worker (app/phone.py serves it at /sw.js).
// Two jobs: show a push as a notification that opens its card, and keep the
// page's shell so a phone that can't reach the Mac still loads and says so.
// Nothing under /api is ever cached: an old list of cards must not look live.
const SHELL = "kestrel-shell-1";
const FILES = ["/", "/app.js", "/app.css", "/manifest.webmanifest", "/icon.png"];

self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(SHELL).then((c) => c.addAll(FILES)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (e) => {
  e.waitUntil(caches.keys()
    .then((keys) => Promise.all(keys.filter((k) => k !== SHELL).map((k) => caches.delete(k))))
    .then(() => self.clients.claim()));
});

self.addEventListener("fetch", (e) => {
  const url = new URL(e.request.url);
  if (e.request.method !== "GET" || url.origin !== location.origin || url.pathname.startsWith("/api/")) return;
  // Network first, so a fixed page arrives; the kept copy only when the Mac is out of reach.
  e.respondWith(fetch(e.request).then((r) => {
    if (r.ok) { const copy = r.clone(); caches.open(SHELL).then((c) => c.put(e.request, copy)); }
    return r;
  }).catch(() => caches.match(e.request, { ignoreSearch: true }).then((r) => r || Response.error())));
});

self.addEventListener("push", (e) => {
  let data = {};
  try { data = e.data ? e.data.json() : {}; } catch { /* a push with no readable body still shows */ }
  e.waitUntil(self.registration.showNotification(data.title || "Kestrel", {
    body: data.body || "Something is waiting on you.",
    tag: data.tag || "kestrel",
    icon: "/icon.png",
    data: { url: data.url || "/" },
  }));
});

self.addEventListener("notificationclick", (e) => {
  e.notification.close();
  const url = new URL(e.notification.data?.url || "/", location.origin).href;
  e.waitUntil(self.clients.matchAll({ type: "window", includeUncontrolled: true }).then((wins) => {
    const open = wins.find((w) => new URL(w.url).origin === location.origin);
    if (open) return open.navigate(url).catch(() => null).then(() => open.focus());
    return self.clients.openWindow(url);
  }));
});
