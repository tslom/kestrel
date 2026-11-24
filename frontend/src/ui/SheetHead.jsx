import { dragPin } from "../lib/drag.js";
import Icon from "./icons.jsx";
import RefreshButton from "./RefreshButton.jsx";

// A sheet's sticky toolbar: data age, refresh, and the pin for the whole sheet. Providers cache, so
// the age is stated; pass a string/count as `refreshedAt`, or `false` to drop the slot (CSS reads that
// as an empty left). Every control is optional (no `onRefresh` = no dead button); `children` go ahead of the two.
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
