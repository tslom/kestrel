import { useState } from "react";
import { postJSON } from "../lib/api";
import { broadcastRefresh } from "../lib/refresh";
import { NO_TOKENS } from "../lib/salience";

// A lost provider credential is the commonest widget error and `oauth.py` words it for a terminal, so
// it's recognised and answered with a line plus the fix button (POST /api/oauth/start, as the status
// panel does). Anything else prints verbatim. Error salience is set in widgets/cards.js.
export default function WidgetError({ error }) {
  const [busy, setBusy] = useState(false);
  const [failed, setFailed] = useState(null);
  const m = NO_TOKENS.exec(error || "");

  if (!m) return <p className="widget-error">{error}</p>;
  const [, provider, account] = m;

  // The backend opens the consent tab (app/browser.py): in the desktop shell `window.open` returns null.
  const connect = async () => {
    setBusy(true);
    setFailed(null);
    try {
      await postJSON("/api/oauth/start", { provider, account, open_browser: true });
      // No poll: returning to this window fires the focus refresh.
      broadcastRefresh();
    } catch (e) {
      setFailed(e.message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <p className="widget-error compact" title={error}>
      <span className="widget-error-what">
        {provider}/{account} needs reconnecting
      </span>
      <button className="chip" onClick={connect} disabled={busy}>
        {busy ? "…" : "connect"}
      </button>
      {failed && <span className="widget-error-why">{failed}</span>}
    </p>
  );
}
