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

// Desk order: the tiebreak within a rank band and the pre-report layout. The brief isn't here: it leads the day.
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

// Every card's data, fetched once at the top of the app: each widget exports `useXCard()` (fetch,
// refresh subscription, level, fold line) and its component is a view over it, so the desk can rank
// and list asks without mounting a card. Keyed by desk id; the constant object keeps hook order stable.
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

// Hooks run outside any ErrorBoundary: an unreadable payload would blank the page, console included.
// So each hook makes all its React calls first and derives after, and a throw is caught here.
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

// A card's rows in the needs-you list: none at any other level; a needs-you card with none built gets one opening row.
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

// { id: { level, summary} }. A card mid-fetch (null level) keeps its last word so it doesn't hop
// bands; idempotent, so writing during render is safe; unreported cards are absent.
export function useLastWord(cards) {
  const ref = useRef({});
  for (const id of DESK_IDS) {
    const { level, summary } = cards[id];
    if (level != null) ref.current[id] = { level, summary };
  }
  return ref.current;
}
