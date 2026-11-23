import { dragPin } from "../lib/drag.js";
import Icon from "./icons.jsx";
import RefreshButton from "./RefreshButton.jsx";

// The toolbar every sheet opens with: how old the data is, a refresh, and the
// pin that sends the whole sheet to the console. Sticky, so the pin stays
// reachable from the bottom of a long scroll.
//
// Providers cache, so the age is stated rather than implied — a sheet that
// says nothing about when it last synced is claiming "now" on the cache's
// behalf. A sheet with no age to state (or a count in its place) passes that
// as `refreshedAt`; `false` drops the slot entirely, which the CSS reads as a
// head with nothing on its left.
//
// Every control is optional: no `onRefresh`, no refresh button (a dead one is
// worse than none); no `onPin`, no pin. `busy` is forwarded to the refresh
// button, and `children` are extra actions ahead of the standard two.
export default function SheetHead({
  refreshedAt, label, data, onPin, pinTitle = "Attach this slice to your next console message",
  onRefresh, refreshTitle, busy, children,
}) {
  return (
    <header className="expand-head" {...dragPin(label, data)}>
      {refreshedAt !== false && <span className="expand-refreshed">{refreshedAt}</span>}
      <span className="expand-spacer" />
      <div className="widget-actions">
        {children}
        {onRefresh && <RefreshButton onClick={onRefresh} busy={busy} title={refreshTitle} />}
        {onPin && (
          <button className="pin" disabled={!data}
            onClick={() => onPin(label, data)}
            title={pinTitle}><Icon name="pin" /> pin</button>
        )}
      </div>
    </header>
  );
}
