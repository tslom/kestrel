import { createRoot } from "react-dom/client";
import { fetchJSON } from "../lib/api.js";
import { writeThemes } from "../lib/look.js";
import "./specimen.css";

// The theme specimen: one page with every colour role in use, drawn from the
// app's own stylesheet. It is what the console's theme_set tool screenshots
// (backend app/looks.py → app/webshot.py) so the model sees a theme before it
// says anything about it, and what "Preview" in the Look lab opens.
//
// A specimen and not the desk, on purpose: the desk opens a console socket
// (a CLI per page load) and syncs providers on read, which a screenshot per
// colour change shouldn't cost, and its picture would carry the day's mail.
// Nothing here reads personal data.
//
//   /#specimen/<dark|light>/<theme id | baseline>

const PAIRS = [
  ["text", "surface"], ["text_secondary", "surface"], ["text_faint", "surface"],
  ["accent", "surface"], ["on_accent", "accent"], ["good", "surface"], ["bad", "surface"],
  ["console_text", "console"], ["console_secondary", "console"], ["accent_console", "console"],
];

function luminance(hex) {
  const lin = (c) => (c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4);
  const [r, g, b] = [1, 3, 5].map((i) => lin(parseInt(hex.slice(i, i + 2), 16) / 255));
  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
}
function ratio(a, b) {
  const [hi, lo] = [luminance(a), luminance(b)].sort((x, y) => y - x);
  return (hi + 0.05) / (lo + 0.05);
}

function Specimen({ name, scheme, roles, values, set }) {
  return (
    <div className="spec">
      <header className="spec-head">
        <b>{name}</b><span>{scheme} mode</span>
        <span className="spec-faint">theme specimen: every colour role in use, not the live desk</span>
      </header>
      <div className="spec-cols">
        <section className="spec-desk" aria-label="Desk">
          <div className="spec-label">Desk <code>desk</code> · cards <code>surface</code></div>
          <div className="spec-card">
            <div className="spec-title">Today</div>
            <p>Primary text on a card: three things want you before noon. <code>text</code></p>
            <p className="spec-mid">Secondary text: the seminar moved to 3pm, room 204. <code>text_secondary</code></p>
            <p className="spec-faint">Tertiary: synced 2 minutes ago · later <code>text_faint</code></p>
            <div className="spec-row">
              <button className="pbtn">Button</button>
              <button className="pbtn go">Wants you</button>
              <span className="spec-pill">Pinned to Kestrel</span>
              <input className="spec-input" defaultValue="Input boundary" readOnly />
            </div>
          </div>
          <div className="spec-card spec-needs">
            <div className="spec-title">Reply to Dana <span className="spec-faint">2h</span></div>
            <p className="spec-mid">An accent edge means this one wants you. <code>accent</code></p>
          </div>
          <div className="spec-card">
            <div className="spec-row spec-status">
              <span className="spec-good">● Calendar synced</span>
              <span className="spec-bad">● Gmail auth expired</span>
              <span className="spec-bars" aria-hidden="true">
                {[5, 9, 6, 12, 8, 14, 10, 7, 11, 13].map((h, i) => <i key={i} style={{ height: h * 2 }} />)}
              </span>
              <span className="spec-mid">data marks <code>data</code></span>
            </div>
            <hr className="spec-line" />
            <p className="spec-faint">divider above: <code>line</code> · button edges: <code>line_strong</code> · input: <code>boundary</code></p>
          </div>
        </section>
        <section className="spec-console" aria-label="Console">
          <div className="spec-label">Console <code>console</code></div>
          <p className="spec-you">move my 3pm to tomorrow</p>
          <p>Moved it to tomorrow at 3pm and told the room. <code>console_text</code></p>
          <p className="spec-cmid">Read 2 events, changed 1. <code>console_secondary</code></p>
          <div className="spec-tool">
            <div><span className="spec-link">event_update</span> Seminar → Tue 15:00</div>
            <div className="spec-cfaint">raised block: <code>console_raise</code>, edge <code>console_line</code>, faint <code>console_faint</code></div>
          </div>
          <div className="spec-perm">
            <div>Send the email to Dana?</div>
            <div className="spec-row"><button className="spec-yes">Yes</button><button className="spec-no">No</button></div>
          </div>
          <p className="spec-cmid">Links and marks in the console: <span className="spec-link">accent_console</span></p>
        </section>
      </div>
      <section className="spec-swatches" aria-label="Roles">
        {Object.keys(roles).map((role) => (
          <div key={role} className={set.has(role) ? "set" : ""}>
            <i style={{ background: `var(${roles[role]})` }} />
            <span>{role}{set.has(role) && " •"}</span>
            <code>{values[role]}</code>
          </div>
        ))}
      </section>
      <footer className="spec-foot">
        {PAIRS.map(([fg, bg]) => (
          <span key={fg + bg}>{fg}/{bg} <b>{ratio(values[fg], values[bg]).toFixed(1)}</b></span>
        ))}
        <span className="spec-faint">• = set by this theme; the rest are the defaults</span>
      </footer>
    </div>
  );
}

export const isSpecimen = () => location.hash.startsWith("#specimen");

export async function mountSpecimen(el) {
  const [, scheme = "dark", id = "baseline"] = location.hash.split("/");
  const root = document.documentElement;
  root.dataset.theme = scheme === "light" ? "light" : "dark";
  const state = await fetchJSON("/api/looks/themes");
  writeThemes(state);
  const theme = state.themes.find((t) => t.id === id);
  if (theme) root.dataset.look = theme.id; else delete root.dataset.look;
  // What the page actually resolved each role to, read back off the root:
  // the swatch hexes are the rendered values, not the request's.
  const css = getComputedStyle(root);
  const values = Object.fromEntries(Object.entries(state.roles)
    .map(([role, token]) => [role, css.getPropertyValue(token).trim().toUpperCase()]));
  createRoot(el).render(
    <Specimen name={theme ? theme.name : "Default colours"} scheme={root.dataset.theme}
      roles={state.roles} values={values} set={new Set(Object.keys(theme?.[root.dataset.theme] || {}))} />);
  await document.fonts.ready;
  // No requestAnimationFrame here: the screenshot's window is never shown,
  // and WebKit doesn't run frames for a page nobody can see. webshot waits a
  // beat after this flag before it snaps.
  root.dataset.specimen = "ready";
}
