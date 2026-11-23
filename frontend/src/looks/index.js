// The look lab's registry: UI design variants for the redesign rounds.
//
// The redesign is a loop, not one rewrite (user, 11-13): a round ships a few
// variants, you live with them and flip A/B, each verdict goes to app.db
// (backend app/looks.py), and the next round keeps what won and tries the
// next variable.
//
// Four axes, each its own attribute on <html>, so they combine freely:
//   LOOKS        colour/type systems     data-look="<id>"    (rounds 1–3)
//   LAYOUTS      the desk: rooms/classic data-layout="<id>"  (rounds 4–5)
//   GROUNDS      what the map is drawn on data-map="<id>"    (round 4)
//   AGENT_STYLES the agent visualizer    data-agents="<id>"  (round 5)
// A vote is stored under the variant's id, prefixed for the two new axes
// (`layout-rooms`, `ground-dots`), so `look_votes` reads as one history.
//
// Round 1 (11-13): Cupertino beat Studio twice ("likes the colors from the
// Cupertino"); its palette became the baseline in round 2, along with the
// token system in docs/ui-overhaul.md. `git show 914683d:frontend/src/looks/`
// has the retired round-1 looks.
//
// Round 3 (11-14): the accent opened up (accents.css). Votes by 11-20: violet
// and rose one yes each, nothing else voted.
//
// Round 4 (11-20): "more radical changes, like new designs for the dashboard,
// solutions to fix clutter while keeping functionality." Round 3 only moved a
// hue, so this round moves the layout itself: four different answers to
// clutter (Today, Rooms, Tiles, Canvas; retired 11-22), each keeping every card, sheet,
// ask and the console reachable. And the map's contour ground, which he
// wanted changed ("map is kinda cool", #28 and #46), gets five alternatives
// (plan/mapGrounds.js).
import "./flexoki.css";
import "./amber.css";
import "./accents.css";
import "./map-narrow.css";
import "./grounds.css";
import "./lab.css";

// Round 5 (11-22): the user's round-4 verdict. Keep Rooms' sidebar as the
// structure, Today's sentence-like glance as its header, and Canvas's
// centered ask bar and floating glass top bar: one hybrid layout, plus three
// takes on Needs you and an agent visualizer.
//
// Picked 11-22: "i like inline layout and then focus line header". Hybrid is
// the desk now (plan/Rooms.jsx, plan/desk.css), Needs you is inline in each
// room with the most urgent ask on the top bar (plan/FocusLine.jsx), and the
// round-4 concepts and the other two Needs-you takes are gone
// (`git show c1423ce:frontend/src/plan/Concepts.jsx` has them). Classic stays
// here as the fallback. The agent visualizer is a setting, Orbs by default.
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

// Each layout says what it cuts and where everything it cut went, because
// "keeps every function reachable" is the bar a concept has to clear.
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

// Round 5: the agent visualizer. Each working tab (and a replay of a past
// session) is drawn on the UI itself, at the card, room or machine its
// current tool call touches.
// Orbs first, so the default: it only draws while something is working.
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
