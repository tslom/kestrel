// What ⌘K can reach: one flat list. One command per subject: a subject with a panel offers the panel, only
// panel-less cards offer a jump (derived from DESK, so a new card can't miss out). Console slash commands are
// deliberately absent: ⌘K drives the desk, `/` drives the agent.

import { useEffect } from "react";
import { commandRun } from "./usage";
import { isDark, setTheme } from "./theme";

// ⌘K / Ctrl+K toggles; free here (the console uses Ctrl+Shift+Space and plain typing, the permission card ignores modifiers).
export function usePaletteKey(setOpen) {
  useEffect(() => {
    const onKey = (e) => {
      if (e.key !== "k" || !(e.metaKey || e.ctrlKey) || e.altKey) return;
      e.preventDefault();
      setOpen((v) => !v);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [setOpen]);
}

// Desk card id → the slide-over that supersedes it (`music` opens as `spotify`).
export const PANEL_FOR = {
  calendar: "calendar", health: "health", trends: "trends", finance: "finance",
  email: "email", people: "people", music: "spotify",
  location: "location", tasks: "tasks", habits: "habits", brain: "brain",
  food: "food",
};

// Other names for a card, matched as substrings (fuzzy.rank).
const KEYWORDS = {
  calendar: "events schedule agenda meetings",
  health: "fitbit sleep steps azm heart",
  trends: "charts features correlations patterns",
  finance: "plaid spending accounts money",
  email: "gmail mail inbox unread",
  people: "crm contacts messages who unanswered",
  travel: "deliveries packages flights trips",
  music: "spotify listening tracks artists",
  location: "owntracks location places visits",
  habits: "checkins streak",
  food: "eat meals macros protein calories nutrition dining hall menu evk parkside village dining dollars bulk weight",
  tasks: "todo",
  debts: "owed lent borrowed",
  expenses: "dad reimburse claim wire spreadsheet export xlsx",
  lists: "shopping buy groceries packing checklist",
  generated: "notes agent vault",
  activity: "changes changelog undo automated self texts note to self away log",
  brain: "memory knows about me profile facts learned confirm people",
  watchlist: "films tv shows",
};

// Slide-overs with no card of their own.
const LOOSE_PANELS = [
  ["settings", "Settings", "preferences options account login sign in switch claude theme appearance model permissions update providers gemini chatgpt codex"],
  ["weather", "Weather", "forecast temperature rain uv wind"],
  ["athletics", "USC sports", "trojans athletics games schedule scores football basketball season"],
  ["status", "Sources & status", "auth freshness errors debug broken stale why empty"],
  ["review", "Email review queue", "triage reply queue"],
  ["usage", "Desk usage", "telemetry attention salience ranking dead cards unused"],
  ["tokens", "Model usage", "tokens cost spend limit 5h weekly window cache savings models tiering opus haiku fable tabs"],
  ["map", "Location map", "where map places dwell travel route owntracks gps basemap"],
  ["notes", "Vault notes", "obsidian vault notes search pinned bookmarks generated written"],
  ["brief", "Morning brief", "brief daily summary today plan overview"],
];

/**
 * Build the command list. `desk` is DESK from widgets/cards.js ([id, title] pairs, so commands can't drift);
 * `openPanel` takes a PANEL_FOR value or LOOSE_PANELS key; `jumpTo` a desk card id.
 * @returns {Array<{id, title, group, hint, keywords, accent?, run}>}
 */
export function buildCommands({ desk, openPanel, jumpTo, refreshAll, focusConsole, openLookLab }) {
  const dark = isDark();
  // Counted here so a new command can't forget it and Enter and click can't drift.
  const counted = (list) => list.map((c) => ({
    ...c, run: () => { commandRun(c.id); c.run(); },
  }));

  const cards = desk.map(([id, title]) => {
    const panel = PANEL_FOR[id];
    return {
      id: `card:${id}`,
      title,
      group: panel ? "Open" : "Jump to card",
      hint: panel ? "panel" : "card",
      keywords: KEYWORDS[id] || "",
      run: panel ? () => openPanel(panel) : () => jumpTo(id),
    };
  });

  return counted([
    // Panels first: they hold more than the card.
    ...cards.filter((c) => c.group === "Open"),
    ...LOOSE_PANELS.map(([key, title, keywords]) => ({
      id: `open:${key}`,
      title,
      group: "Open",
      hint: "panel",
      keywords,
      run: () => openPanel(key),
    })),
    ...cards.filter((c) => c.group !== "Open"),

    {
      id: "act:refresh",
      title: "Refresh every widget",
      group: "Actions",
      hint: "re-read the caches",
      keywords: "reload update refetch",
      // Not a sync: re-reads app.db; fetching from sources is the strip's sync button.
      run: refreshAll,
    },
    {
      id: "act:console",
      title: "Focus the console",
      group: "Actions",
      hint: "write to the agent",
      keywords: "prompt ask chat claude composer message",
      // The one you-and-agent command takes the amber thread.
      accent: true,
      run: focusConsole,
    },
    {
      // Built fresh each time the palette opens, so it offers the other theme.
      id: "act:theme",
      title: dark ? "Use the light theme" : "Use the dark theme",
      group: "Actions",
      hint: "overrides the system appearance",
      keywords: "theme dark light mode appearance night day colors",
      run: () => setTheme(dark ? "light" : "dark"),
    },
    ...(openLookLab ? [{
      id: "act:looklab",
      title: "Look lab",
      group: "Actions",
      hint: "try UI designs, flip A/B, vote",
      keywords: "look design redesign ui theme variant ab a/b compare apple style",
      run: openLookLab,
    }] : []),
  ]);
}
