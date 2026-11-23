import Icon from "./icons.jsx";

// The refresh button every card and sheet carries. With `busy` (a card's
// useLoad flag) it spins and goes inert while the load is in flight, so a
// click visibly did something; without it, it's the plain button a sheet
// head has always shown.
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
