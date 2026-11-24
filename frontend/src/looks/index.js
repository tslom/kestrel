// The look lab's registry: variants per redesign round; verdicts go to app.db (app/looks.py).
// Four axes, each an attribute on <html>, combining freely:
//   LOOKS        colour/type systems     data-look="<id>"    (rounds 1–3)
//   LAYOUTS      the desk: rooms/classic data-layout="<id>"  (rounds 4–5)
//   GROUNDS      what the map is drawn on data-map="<id>"    (round 4)
//   AGENT_STYLES the agent visualizer    data-agents="<id>"  (round 5)
// A vote is stored under the variant's id, prefixed for layout/ground (`layout-rooms`, `ground-dots`).
// Design: DESIGN.md → Themes Kestrel makes (the look lab). Retired round-1 looks: `git show 914683d:frontend/src/looks/`.
import "./flexoki.css";
import "./amber.css";
import "./accents.css";
import "./map-narrow.css";
import "./grounds.css";
import "./lab.css";

// The hybrid (round 5) is the desk (plan/Rooms.jsx, plan/desk.css; Needs you inline per room, most
// urgent on the top bar via plan/FocusLine.jsx); Classic stays as fallback. Round-4 concepts:
// `git show c1423ce:frontend/src/plan/Concepts.jsx`. The agent visualizer is a setting, Orbs default.
export const ROUND = 5;

export const LOOKS = [
  { id: "baseline", name: "Blue (default)",
    question: "Neutral greys, blue accent, system sans, one type and spacing scale." },
  { id: "violet", name: "Violet",
    question: "Violet accent: clear of every status colour." },
  { id: "rose", name: "Rose",
    question: "Rose accent. Close to \"bad\" red: warm without reading as an error?" },
  { id: "amber", name: "Amber",
    question: "Same system with the old orange as the accent." },
  { id: "green", name: "Green",
    question: "Green accent. It shares its hue with \"good\": does a primary button still read as an action?" },
  { id: "map-narrow", name: "Map narrow",
    question: "The map as a fixed 360px column; the console gets the rest of the width." },
  { id: "flexoki", name: "Flexoki (before)",
    question: "The look before round 2: warm paper, orange, mono UI." },
];

// Each layout says what it cuts and where that went: every function must stay reachable.
export const LAYOUTS = [
  { id: "hybrid", name: "Rooms (default)",
    question: "A sidebar of rooms, a page per room, Today as home; one floating bar with the day as a sentence and the most urgent ask; the console a centred ask bar until ⌘J.",
    cuts: "No single screen holds everything: the map is a room of its own, and Needs you lives in each room (all of it on Today), its first ask on the top bar." },
  { id: "classic", name: "Classic (the old desk)",
    question: "Glance strip, brief, console + map side by side, Needs you on the right.",
    cuts: "Nothing: the desk before round 5, kept as a fallback." },
];

export const GROUNDS = [
  { id: "contours", name: "Contours (now)",
    question: "The current ground: contour lines over a height field, hills where cards need you." },
  { id: "dots", name: "Dot matrix",
    question: "A halftone grid: each dot as big as the ground is high, like a stadium board. Ripples are dots swelling." },
  { id: "grid", name: "Gravity grid",
    question: "A fine square grid that the cards bend, like a mass bending space. Flat when the day is clear." },
  { id: "flow", name: "Flow",
    question: "Drifting particles that stream around each card's hill. A turn speeds them up." },
  { id: "lights", name: "Night lights",
    question: "A dark ground seen from orbit: each card a little city, brighter the more it needs you." },
  { id: "aurora", name: "Aurora glass",
    question: "Soft colour fields under frosted glass, like a macOS wallpaper; what needs you glows warm." },
];

// Round 5: the agent visualizer, drawn at what each working tab's (or a replay's) current call touches.
// Orbs first = the default: it draws only while something works.
export const AGENT_STYLES = [
  { id: "orbs", name: "Orbs",
    question: "A glowing orb per agent glides to what it's touching, with a comet trail and a label; a write rings the card it's changing." },
  { id: "cursors", name: "Cursors",
    question: "A pointer per agent with a name tag, like people in a shared doc: snappier, more literal." },
  { id: "off", name: "Off", question: "No agents drawn on the UI." },
];

const by = (list) => (id) => list.find((l) => l.id === id) || list[0];
export const lookById = by(LOOKS);
export const layoutById = by(LAYOUTS);
export const groundById = by(GROUNDS);
export const agentsById = by(AGENT_STYLES);
