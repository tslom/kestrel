import TasksWidget from "./TasksWidget.jsx";
import CalendarWidget from "./CalendarWidget.jsx";
import HealthWidget from "./HealthWidget.jsx";
import HabitsWidget from "./HabitsWidget.jsx";
import GeneratedWidget from "./GeneratedWidget.jsx";
import ActivityWidget from "./ActivityWidget.jsx";
import BrainWidget, { BrainSheet } from "./BrainWidget.jsx";
import EmailWidget from "./EmailWidget.jsx";
import EmailReview from "./EmailReview.jsx";
import TrendsWidget from "./TrendsWidget.jsx";
import FinanceWidget from "./FinanceWidget.jsx";
import TrendsExpand from "./TrendsExpand.jsx";
import PeopleWidget from "./PeopleWidget.jsx";
import PeopleExpand from "./PeopleExpand.jsx";
import MusicWidget from "./MusicWidget.jsx";
import LocationWidget from "./LocationWidget.jsx";
import TravelWidget from "./TravelWidget.jsx";
import DebtsWidget from "./DebtsWidget.jsx";
import ExpensesWidget from "./ExpensesWidget.jsx";
import ListsWidget from "./ListsWidget.jsx";
import WatchlistWidget from "./WatchlistWidget.jsx";
import CoursesWidget from "./CoursesWidget.jsx";
import StatusPanel from "./StatusPanel.jsx";
import TasksExpand from "./TasksExpand.jsx";
import HabitsExpand from "./HabitsExpand.jsx";
import FoodWidget from "./FoodWidget.jsx";
import FoodSheet from "./FoodSheet.jsx";
import CalendarSheet from "./CalendarSheet.jsx";
import HealthSheet from "./HealthSheet.jsx";
import MoneySheet from "./MoneySheet.jsx";
import MusicSheet from "./MusicSheet.jsx";
import LocationSheet from "./LocationSheet.jsx";
import MapSheet from "./MapSheet.jsx";
import EmailSheet from "./EmailSheet.jsx";
import UsageSheet from "./UsageSheet.jsx";
import TokenUsageSheet from "./TokenUsageSheet.jsx";
import DaySheet from "./DaySheet.jsx";
import VaultNotes from "./VaultNotes.jsx";
import SettingsSheet from "./SettingsSheet.jsx";
import WeatherSheet from "./WeatherSheet.jsx";
import SportsSheet from "./SportsSheet.jsx";
import BriefWidget from "./BriefWidget.jsx";
import NoteTab from "./NoteTab.jsx";
import { fmtDay } from "../lib/time";
import { PANEL_FOR } from "../lib/palette";

// What every desk draws from a card id: the card (a view over its hook's result, widgets/cards.js)
// and its sheets. Shared by every layout.

// Sheet titles. Keys are what openSheet() takes (card expand buttons, ⌘K); for provider-backed
// sheets the key is the registry name, which is not a title ("spotify" is the database talking).
const SHEET_TITLE = {
  calendar: "Calendar",
  email: "Email",
  finance: "Money",
  spotify: "Music",
  map: "Location map",
  health: "Health",
  location: "Location",
  trends: "Trends",
  people: "People",
  review: "Review queue",
  status: "Sources",
  settings: "Settings",
  weather: "Weather",
  athletics: "USC sports",
  tasks: "All tasks",
  habits: "Habits",
  food: "Food",
  usage: "Desk usage",
  tokens: "Model usage",
  notes: "Vault notes",
  brief: "Morning brief",
  brain: "Brain",
};

// Keyed sheets: `day:YYYY-MM-DD` (day drill-down) and `note:<vault path>`. The key carries the
// argument so the stack's rules hold: different days are different sheets, re-opening reuses one.
export const sheetTitle = (key) =>
  key.startsWith("day:") ? fmtDay(key.slice(4))
    : key.startsWith("note:") ? key.slice(5).split("/").pop().replace(/\.md$/, "")
    : (SHEET_TITLE[key] || key);

// Sheets that write: closing one must refresh its card.
export const SHEET_TOPIC = { tasks: "tasks", habits: "habits", brain: "brain", food: "food" };

// Every sheet body is its own content only — the frame (title, close, fold or
// tab) belongs to whoever is showing it.
export function SheetBody({ id, onPin, onOpen }) {
  switch (id) {
    case "calendar": return <CalendarSheet onPin={onPin} />;
    case "finance": return <MoneySheet onPin={onPin} />;
    case "health": return <HealthSheet onPin={onPin} onOpen={onOpen} />;
    case "spotify": return <MusicSheet onPin={onPin} />;
    case "map": return <MapSheet onPin={onPin} />;
    case "location": return <LocationSheet onPin={onPin} />;
    case "email": return <EmailSheet onPin={onPin} />;
    case "trends": return <TrendsExpand onPin={onPin} />;
    case "review": return <EmailReview onPin={onPin} />;
    case "people": return <PeopleExpand onPin={onPin} />;
    case "settings": return <SettingsSheet onOpen={onOpen} />;
    case "weather": return <WeatherSheet onPin={onPin} />;
    case "athletics": return <SportsSheet onPin={onPin} />;
    case "status": return <StatusPanel onPin={onPin} />;
    case "tasks": return <TasksExpand onPin={onPin} />;
    case "habits": return <HabitsExpand onPin={onPin} />;
    case "food": return <FoodSheet onPin={onPin} />;
    case "brain": return <BrainSheet />;
    case "usage": return <UsageSheet />;
    case "tokens": return <TokenUsageSheet />;
    case "notes": return <VaultNotes onOpenNote={(p) => onOpen(`note:${p}`)} />;
    case "brief": return <BriefWidget onPin={onPin} />;
    default:
      if (id.startsWith("note:")) {
        return <NoteTab path={id.slice(5)} onPin={onPin} onOpenNote={(p) => onOpen(`note:${p}`)} />;
      }
      // No generic fallback: a new sheet gets a case here and a title in SHEET_TITLE.
      if (id.startsWith("day:")) return <DaySheet key={id} day={id.slice(4)} onPin={onPin} />;
      return null;
  }
}

const WIDGET = {
  calendar: CalendarWidget, health: HealthWidget, trends: TrendsWidget,
  finance: FinanceWidget, email: EmailWidget, people: PeopleWidget,
  travel: TravelWidget, music: MusicWidget, location: LocationWidget,
  habits: HabitsWidget, food: FoodWidget, tasks: TasksWidget, debts: DebtsWidget,
  expenses: ExpensesWidget, lists: ListsWidget, generated: GeneratedWidget,
  watchlist: WatchlistWidget, courses: CoursesWidget, brain: BrainWidget,
  activity: ActivityWidget,
};

// One card in the side pane. Its sheet is the pane's "Full view" (palette.js PANEL_FOR); `onExpand`
// is only for a body link that means the sheet (Tasks' "N more").
export function CardView({ id, card, onPin, openSheet }) {
  const Widget = WIDGET[id];
  const sheet = PANEL_FOR[id];
  return (
    <Widget card={card} onPin={onPin}
      onExpand={sheet ? () => openSheet(sheet) : undefined}
      onReview={id === "email" ? () => openSheet("review") : undefined} />
  );
}
