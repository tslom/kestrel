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
// Self-hosted, not a CDN link: the desk has to render the same with the
// network down, which is most of what a localhost dashboard is for. The
// variable cut is the one to take — the mono runs at 500 and 600 in a dozen
// places, and two static files would cost more than the whole axis does.
import "@fontsource-variable/jetbrains-mono";
import "./styles.css";
// the rooms desk, after the base sheet (plan/Rooms.jsx)
import "./plan/desk.css";

// Before the first render: a throw during mount is exactly the kind this
// should catch, and the per-card boundaries can't see one from a timer or an
// unawaited promise.
installGlobalHandlers();

// Flush-on-hide, so the last few impressions of a session aren't lost with
// the tab.
installUsage();

// A light/dark choice made from ⌘K, before first paint so the page doesn't
// flash the system's theme first.
applyStoredTheme();

// The look lab's variant, if one is on (lib/look.js), before first paint too.
applyLook();
installLookKeys();

// Escape must never take the desktop window out of fullscreen.
installEscapeGuard();

// The last line, for whatever gets past the card and console-entry boundaries:
// a throw here otherwise unmounts everything and leaves a blank window with no
// hint that a reload would fix it. The server and every console turn are fine
// in that state; only the page died.
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
  // Themes Kestrel made (lib/look.js): from the server now, and again whenever
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
