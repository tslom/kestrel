import React from "react";
import { createRoot } from "react-dom/client";
import PlanApp from "./plan/PlanApp.jsx";
import ErrorBoundary from "./ui/ErrorBoundary.jsx";
import { installGlobalHandlers } from "./lib/report";
import { installUsage } from "./lib/usage";
import { applyStoredTheme } from "./lib/theme";
import { applyLook, installLookKeys, loadThemes } from "./lib/look";
import { onTopic } from "./lib/refresh";
import { isSpecimen, mountSpecimen } from "./ui/ThemeSpecimen.jsx";
import { installEscapeGuard } from "./lib/escape";
// Self-hosted: the desk must render with the network down. The variable cut: mono runs at 500 and 600 in many places.
import "@fontsource-variable/jetbrains-mono";
import "./styles.css";
// the rooms desk, after the base sheet (plan/Rooms.jsx)
import "./plan/desk.css";

// Before first render: catches throws during mount and from timers or unawaited promises that card boundaries can't see.
installGlobalHandlers();

// Flush-on-hide, so the last impressions aren't lost.
installUsage();

// Before first paint: the ⌘K light/dark choice, so the page doesn't flash the system theme.
applyStoredTheme();

// The look lab's variant, if one is on (lib/look.js), before first paint too.
applyLook();
installLookKeys();

// Escape must never take the desktop window out of fullscreen.
installEscapeGuard();

// Last line for whatever passes the card and entry boundaries: a throw otherwise leaves a blank window (the server and sessions are fine).
const pageDown = (
  <p className="widget-error" role="alert">
    Kestrel's page stopped rendering. Your sessions are still running.
    <button className="pbtn" onClick={() => location.reload()}>Reload</button>
  </p>
);

if (isSpecimen()) {
  // /#specimen/…: the theme specimen instead of the desk (ui/ThemeSpecimen.jsx),
  // for the theme tool's screenshot. No console, no cards, no sockets.
  mountSpecimen(document.getElementById("root"));
} else {
  // Themes Kestrel made (lib/look.js): from the server at load, and again whenever
  // a console turn sets one.
  loadThemes();
  onTopic("looks", loadThemes);

  createRoot(document.getElementById("root")).render(
    <React.StrictMode>
      <ErrorBoundary label="desk" fallback={pageDown}>
        <PlanApp />
      </ErrorBoundary>
    </React.StrictMode>
  );
}
