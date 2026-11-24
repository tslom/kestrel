import React from "react";
import { report } from "../lib/report";

// Fault isolation for one card: around each drawn card and full view (not the app), so one throw
// doesn't unmount the whole tree. A class: `getDerivedStateFromError` has no hook equivalent.
export default class ErrorBoundary extends React.Component {
  state = { error: null };

  static getDerivedStateFromError(error) {
    return { error };
  }

  // Stays tripped until what it draws changes; pass `resetKey` where that happens in place (a streaming entry).
  componentDidUpdate(prev) {
    if (this.state.error && prev.resetKey !== this.props.resetKey)
      this.setState({ error: null });
  }

  componentDidCatch(error, info) {
    // `label` is the card, not the message.
    report("render", this.props.label || "widget",
           error?.message || String(error),
           // Both stacks: component = which card; JS top frame = which line (failures.resolve_trace maps it to src/).
           [(info?.componentStack || "").trim(), error?.stack]
             .filter(Boolean).join("\n\n") || null);
  }

  render() {
    if (!this.state.error) return this.props.children;
    if (this.props.fallback) return this.props.fallback;
    // Plain on purpose, not `WidgetError`: that runs hooks, unsafe for a card whose render broke.
    return (
      <p className="widget-error" role="alert">
        {this.props.label ? `${this.props.label} couldn't render` : "couldn't render"}
        {" — "}
        {this.state.error?.message || String(this.state.error)}
      </p>
    );
  }
}
