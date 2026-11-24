import Icon from "./icons.jsx";

// The refresh button every card and sheet carries; with `busy` (useLoad flag) it spins and goes inert.
export default function RefreshButton({ onClick, busy, title = "Refresh" }) {
  if (busy === undefined) {
    return <button onClick={onClick} title={title} aria-label={title}><Icon name="refresh" /></button>;
  }
  return (
    <button onClick={onClick} disabled={busy}
      className={busy ? "spin" : ""}
      title={busy ? "Refreshing…" : title} aria-label={busy ? "Refreshing…" : title}><Icon name="refresh" /></button>
  );
}
