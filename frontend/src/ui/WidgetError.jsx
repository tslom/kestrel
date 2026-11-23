import { useState } from "react";
import { postJSON } from "../lib/api";
import { broadcastRefresh } from "../lib/refresh";
import { NO_TOKENS } from "../lib/salience";

// A provider that has lost its credential is by far the most common widget
// error, and `oauth.py` writes that message for a terminal: four lines naming
// a python module and a flag. Rendered verbatim at full card weight it was the
// loudest thing on the desk — and *twice over*, because health and trends both
// read the google account and both printed the same paragraph.
//
// So recognise that one shape and answer it with the fix instead: a line and
// the button that repairs it (the same POST /api/oauth/start the status panel
// uses). Everything else still prints verbatim — an unrecognised error is
// exactly the kind you want to read in full.
//
// The card's salience for an error is set in widgets/cards.js, which sees the
// hook's error before anything renders.
export default function WidgetError({ error }) {
  const [busy, setBusy] = useState(false);
  const [failed, setFailed] = useState(null);
  const m = NO_TOKENS.exec(error || "");

  if (!m) return <p className="widget-error">{error}</p>;
  const [, provider, account] = m;

  // The backend opens the consent tab (open_browser) — we can't. In the
  // desktop shell `window.open` returns null and opens nothing, so the old
  // open-a-blank-tab-then-set-its-location dance threw on the null instead of
  // connecting anything. See app/browser.py.
  const connect = async () => {
    setBusy(true);
    setFailed(null);
    try {
      await postJSON("/api/oauth/start", { provider, account, open_browser: true });
      // No poll here: the callback lands in the browser, and coming back to
      // this window fires the focus refresh that reloads every card anyway.
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
