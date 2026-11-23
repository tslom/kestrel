import { useEffect, useState } from "react";
import { fetchJSON } from "./api";

// Google's web apps pick an account from the browser session unless the URL
// says otherwise, so a bare mail.google.com/... link lands in whichever
// account happens to be first — not the one this dashboard is reading.
// `authuser=<address>` pins it. The addresses come from the backend (it's the
// side that holds the tokens); we fetch once per page load and cache.

let addresses = null;                  // { gmail, calendar } once resolved
const waiters = new Set();

const ready = fetchJSON("/api/oauth/addresses")
  .then((a) => { addresses = a; })
  .catch(() => { addresses = {}; })    // unauthorized/offline: plain links
  .finally(() => { waiters.forEach((fn) => fn()); waiters.clear(); });

function authuser(kind) {
  const addr = addresses?.[kind];
  return addr ? `authuser=${encodeURIComponent(addr)}` : "";
}

export const GMAIL_URL = (id) => {
  const q = authuser("gmail");
  return `https://mail.google.com/mail/u/0/${q ? `?${q}` : ""}#all/${id}`;
};

export const CALENDAR_URL = () => {
  const q = authuser("calendar");
  return `https://calendar.google.com/calendar/r${q ? `?${q}` : ""}`;
};

/** Re-render once the addresses land, so hrefs rendered early get fixed up. */
export function useGoogleAddresses() {
  const [, bump] = useState(0);
  useEffect(() => {
    if (addresses) return;
    const fn = () => bump((n) => n + 1);
    waiters.add(fn);
    return () => waiters.delete(fn);
  }, []);
  return addresses || {};
}

export { ready as googleAddressesReady };
