import React from "react";
import { report } from "../lib/report";

// Fault isolation for one card.
//
// Without a boundary, any widget throwing during render unmounts the whole
// tree: twenty-eight working cards replaced by a blank page, and the only
// trace in a devtools console nobody has open. That is the opposite of how the provider layer behaves one floor
// down, where a source that breaks reports four states and keeps the others
// running.
//
// The boundary goes around each drawn card and full view rather than around
// the app, because the point is that the *rest* of it survives. A card that
// cannot render is one card's worth of bad news.
//
// Still a class: `getDerivedStateFromError` has no hook equivalent.
export default class ErrorBoundary extends React.Component {
  state = { error: null };

  static getDerivedStateFromError(error) {
    return { error };
  }

  // A boundary stays tripped until something changes what it's drawing. Give it
  // a `resetKey` where that happens in place — a console entry's text grows
  // token by token, and a failure on one token says nothing about the next.
  componentDidUpdate(prev) {
    if (this.state.error && prev.resetKey !== this.props.resetKey)
      this.setState({ error: null });
  }

  componentDidCatch(error, info) {
    // `label` is the card, not the message — the failing widget is what you
    // go and look at, and it is what makes two rows worth telling apart.
    report("render", this.props.label || "widget",
           error?.message || String(error),
           // Both stacks: the component stack says which card, the JS stack's
           // top frame says which line threw — and since the backend maps
           // bundle frames back to src/ (failures.resolve_trace), that frame
           // is no longer just minified noise.
           [(info?.componentStack || "").trim(), error?.stack]
             .filter(Boolean).join("\n\n") || null);
  }

  render() {
    if (!this.state.error) return this.props.children;
    if (this.props.fallback) return this.props.fallback;
    // Deliberately plain, and deliberately not `WidgetError` — that one runs
    // hooks and offers a reconnect button, neither of which is safe or
    // meaningful for a card whose render is the thing that broke.
    return (
      <p className="widget-error" role="alert">
        {this.props.label ? `${this.props.label} couldn't render` : "couldn't render"}
        {" — "}
        {this.state.error?.message || String(this.state.error)}
      </p>
    );
  }
}
