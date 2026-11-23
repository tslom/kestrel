import { useRef } from "react";
import { useCalendarCard } from "./CalendarWidget.jsx";
import { useCoursesCard } from "./CoursesWidget.jsx";
import { useHealthCard } from "./HealthWidget.jsx";
import { useTrendsCard } from "./TrendsWidget.jsx";
import { useFinanceCard } from "./FinanceWidget.jsx";
import { useEmailCard } from "./EmailWidget.jsx";
import { usePeopleCard } from "./PeopleWidget.jsx";
import { useTravelCard } from "./TravelWidget.jsx";
import { useMusicCard } from "./MusicWidget.jsx";
import { useLocationCard } from "./LocationWidget.jsx";
import { useHabitsCard } from "./HabitsWidget.jsx";
import { useFoodCard } from "./FoodWidget.jsx";
import { useTasksCard } from "./TasksWidget.jsx";
import { useDebtsCard } from "./DebtsWidget.jsx";
import { useExpensesCard } from "./ExpensesWidget.jsx";
import { useListsCard } from "./ListsWidget.jsx";
import { useGeneratedCard } from "./GeneratedWidget.jsx";
import { useWatchlistCard } from "./WatchlistWidget.jsx";
import { useActivityCard } from "./ActivityWidget.jsx";
import { useBrainCard } from "./BrainWidget.jsx";
import { NEEDS, errorSummary } from "../lib/salience";
import { genericAsk } from "../lib/asks.js";
import { report } from "../lib/report";

// Desk order: the tiebreak within a rank band, and the layout everything
// falls back to before any card has reported. The brief isn't in here — it
// summarises everything below it, so it leads whatever the day holds.
export const DESK = [
  ["calendar", "Calendar"],
  ["courses", "Office hours"],
  ["activity", "While you were away"],
  ["health", "Health"],
  ["trends", "Trends"],
  ["finance", "Finance"],
  ["email", "Email"],
  ["people", "People"],
  ["travel", "Deliveries & travel"],
  ["music", "Music"],
  ["location", "Location"],
  ["habits", "Habits"],
  ["food", "Food"],
  ["tasks", "Tasks"],
  ["debts", "Debts"],
  ["expenses", "Expenses"],
  ["lists", "Lists"],
  ["generated", "Generated inbox"],
  ["brain", "Brain"],
  ["watchlist", "Watchlist"],
];
export const DESK_IDS = DESK.map(([id]) => id);
export const CARD_NAME = Object.fromEntries(DESK);

// Every card's data, fetched once at the top of the app (IDEAS: plan view).
// A widget used to fetch its own data and report its own salience from inside
// its render, which meant nothing could know what a card held without drawing
// it. Each widget file now exports a `useXCard()` — the fetch, the refresh
// subscription, the level and the fold line — and its component is a view
// over what that hook returns, so the desk can rank, summarise and later list
// a card's asks without mounting it, and opening a card never refetches.
//
// Keyed by desk id. The object is a constant, so the hooks run in the same
// order on every render.
const HOOKS = {
  calendar: useCalendarCard,
  courses: useCoursesCard,
  activity: useActivityCard,
  health: useHealthCard,
  trends: useTrendsCard,
  finance: useFinanceCard,
  email: useEmailCard,
  people: usePeopleCard,
  travel: useTravelCard,
  music: useMusicCard,
  location: useLocationCard,
  habits: useHabitsCard,
  food: useFoodCard,
  tasks: useTasksCard,
  debts: useDebtsCard,
  expenses: useExpensesCard,
  lists: useListsCard,
  generated: useGeneratedCard,
  brain: useBrainCard,
  watchlist: useWatchlistCard,
};

// Throws already reported, so a card that fails on every render posts once.
const reported = new Set();

// A hook runs in the app's render now, outside any card's ErrorBoundary, so a
// payload it can't read would blank the whole page — console included. Each
// hook therefore makes all of its React hook calls first and derives after;
// a throw can only come from the derivation, by which point the hook order is
// already complete, and catching it here keeps the rest of the app alive. The
// card then renders as an error, which is what the boundary used to show.
// (`cardHook`, not `use`: rules-of-hooks reads a bare `use()` as React's own.)
function guard(id, cardHook) {
  let card;
  try {
    card = cardHook();
    // A failed fetch outranks whatever the card would otherwise say.
    if (card.error) card = { ...card, level: NEEDS, summary: errorSummary(card.error) };
  } catch (e) {
    const msg = e?.message || String(e);
    if (!reported.has(`${id}:${msg}`)) {
      reported.add(`${id}:${msg}`);
      report("render", id, msg, e?.stack);
    }
    card = { error: `couldn't read this card's data — ${msg}`,
             level: NEEDS, summary: `couldn't render — ${msg}` };
  }
  return { ...card, asks: asksFor(id, card) };
}

// The list's rows for one card. The list is exactly the cards that need you,
// so a card at any other level has none, whatever its hook built; one that
// needs you but built none (or is broken) gets a single row that opens it.
function asksFor(id, card) {
  if (card.level !== NEEDS) return [];
  if (card.error) return [genericAsk(id, CARD_NAME[id], card.summary, true)];
  return card.asks?.length ? card.asks : [genericAsk(id, CARD_NAME[id], card.summary)];
}

export function useCards() {
  const cards = {};
  for (const [id, use] of Object.entries(HOOKS)) cards[id] = guard(id, use);
  return cards;
}

// What each card last had to say: { id: { level, summary } }. A card whose
// fetch is out has a null level; it keeps its last word rather than going
// blank, so a card that reloads (the calendar switching days clears its
// payload) doesn't hop bands and back. Idempotent, so writing during render
// is safe. A card that hasn't reported yet is simply absent.
export function useLastWord(cards) {
  const ref = useRef({});
  for (const id of DESK_IDS) {
    const { level, summary } = cards[id];
    if (level != null) ref.current[id] = { level, summary };
  }
  return ref.current;
}
