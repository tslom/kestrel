import { useState } from "react";
import { fetchJSON, postJSON } from "./api";
import { useVisiblePoll } from "./poll.js";
import { useLoad } from "./refresh.js";

// Saved Claude accounts (app/claude_accounts.py): which one is in use, every
// account's 5h and weekly usage, and the rules. The backend polls the API
// itself (60s per account, longer after a 429); this only reads its answer,
// so polling it often costs nothing upstream.
//
// `onData` runs on every answer, polled or from a write: the console bar's
// chip uses it to reconnect an idle tab onto a newly active account.

const URL = "/api/console/accounts";
const POLL_MS = 30_000;

export function useAccounts({ onData } = {}) {
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);
  const got = (d) => { setData(d); setError(null); onData?.(d); };
  const [load] = useLoad(() => fetchJSON(URL).then(got).catch((e) => setError(e.message)));
  useVisiblePoll(load, POLL_MS, { catchUp: true });
  // Every write answers with the whole state, so the view never re-asks.
  const act = (path, body, method = "POST") =>
    postJSON(`${URL}${path}`, body ?? {}, method).then(got).catch((e) => setError(e.message));
  return { data, error, setError, act };
}

export const windowOf = (acct, key) => acct.windows.find((w) => w.key === key);

// "62%", or "—" once the window has rolled over (its number is void).
export function pctOf(w) {
  if (!w || w.percent == null) return "—";
  if (w.resets_at != null && w.resets_at * 1000 <= Date.now()) return "0%";
  return `${Math.round(w.percent)}%`;
}

// The account's name, short enough for the bar: an email's local part.
export const shortName = (label) => (label || "").split("@")[0];
