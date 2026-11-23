import { useState } from "react";
import { fetchJSON } from "../lib/api";
import { useVisiblePoll } from "../lib/poll.js";
import { useLoad } from "../lib/refresh";

// The roll-up: one dot in the glance strip, the whole status panel behind it.
// Quiet when everything's fine (a bare green dot — a row of per-source chips
// is exactly the noise that makes you stop reading them), and names the
// sources that need you when they don't.
//
// Re-reads on the refresh bus (so a sync updates it) plus its own slow timer,
// since a token can expire with nobody touching the page.

const POLL_MS = 5 * 60 * 1000;

export default function StatusChip({ onOpen }) {
  const [data, setData] = useState(null);
  const [reload] = useLoad(() => fetchJSON("/api/status")
    .then(setData)
    .catch(() => setData(null)));   // backend down: the chip just goes quiet

  // No ticks while the tab is hidden, and no catch-up on return: this is on
  // the refresh bus, which already re-reads it when you come back from away.
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
