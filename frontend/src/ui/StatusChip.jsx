import { useState } from "react";
import { fetchJSON } from "../lib/api";
import { useVisiblePoll } from "../lib/poll.js";
import { useLoad } from "../lib/refresh";

// The glance strip's roll-up dot: bare green when fine, names the sources that need you otherwise.
// Re-reads on the refresh bus plus a slow timer (a token can expire untouched).

const POLL_MS = 5 * 60 * 1000;

export default function StatusChip({ onOpen }) {
  const [data, setData] = useState(null);
  const [reload] = useLoad(() => fetchJSON("/api/status")
    .then(setData)
    .catch(() => setData(null)));   // backend down: the chip just goes quiet

  // No ticks while hidden; the refresh bus re-reads on return.
  useVisiblePoll(reload, POLL_MS);

  if (!data) return null;

  const bad = data.sources.filter((s) => s.state === "broken" || s.state === "stale");
  const names = bad.slice(0, 2).map((s) => s.source).join(", ");
  const extra = bad.length > 2 ? ` +${bad.length - 2}` : "";
  const worst = data.worst;

  return (
    <button className={`glance-chip status-chip ${worst}`} onClick={onOpen}
      aria-label={bad.length ? `Sources: ${names}${extra} need attention` : "Sources: all fine"}
      title={bad.length
        ? `${bad.map((s) => `${s.source}: ${s.detail}`).join("\n")}\n\nClick for all sources`
        : "Every source is authorized and current — click for detail"}>
      <span className={`dot state-${worst}`} />
      {bad.length > 0 && <span className="k">{names}{extra}</span>}
    </button>
  );
}
