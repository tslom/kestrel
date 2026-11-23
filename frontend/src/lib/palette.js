// What ⌘K can reach. Kept out of App.jsx so the desk file stays about the
// desk, and so the list is one flat thing to read when you're asking "can I
// get there from the keyboard yet?".
//
// One command per subject, which is the rule that keeps this list short. The
// first draft offered "open the Calendar panel" and "jump to the Calendar
// card" as separate rows, and nine of the eleven panels picked up a
// near-duplicate that way — two rows with the same name, tying on score,
// where the panel is strictly the better answer. So a subject with a panel
// offers the panel, and only the cards without one offer the jump. The pairing
// is derived from DESK rather than listed again here, so a new card can't
// silently miss out.
//
// Deliberately not in here: the console's slash commands. Those are a
// different namespace with a different owner (backend `commands.py` scans the
// vault's .claude dirs and the SDK reports what a live session actually
// loaded), they're typed into the composer against its own autocomplete, and
// they address the agent rather than the dashboard. Two lists that look alike
// but resolve differently is the confusion worth avoiding — ⌘K drives the
// desk, `/` drives the agent.

import { useEffect } from "react";
import { commandRun } from "./usage";
import { isDark, setTheme } from "./theme";

// ⌘K / Ctrl+K toggles the palette. Free on both platforms here: the console's
// own hotkeys are Ctrl+Shift+Space (push-to-talk) and plain typing, and the
// permission card's global handler ignores anything with a modifier.
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

// Desk card id → the slide-over that supersedes it. `music` opens under the
// provider's own name, which is the one mismatch worth hard-coding.
export const PANEL_FOR = {
  calendar: "calendar", health: "health", trends: "trends", finance: "finance",
  email: "email", people: "people", music: "spotify",
  location: "location", tasks: "tasks", habits: "habits", brain: "brain",
  food: "food",
};

// Other names for a card, so the palette answers the word you reached for
// rather than the one on the label. Matched as substrings — see fuzzy.rank.
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
 * Build the command list.
 *
 * @param {Array<[string, string]>} desk  DESK from widgets/cards.js — [id, title] pairs, so
 *   the commands can't drift out of sync with what's actually on the desk.
 * @param {(key: string) => void} openPanel   a PANEL_FOR value, or a LOOSE_PANELS key.
 * @param {(id: string) => void} jumpTo       desk card id.
 * @param {() => void} refreshAll
 * @param {() => void} focusConsole
 * @returns {Array<{id, title, group, hint, keywords, accent?, run}>}
 */
export function buildCommands({ desk, openPanel, jumpTo, refreshAll, focusConsole, openLookLab }) {
  const dark = isDark();
  // Counting at the definition, not at the two call sites in CommandPalette:
  // a command added later can't forget to be counted, and Enter and click
  // can't drift apart. Same reasoning as deriving the card list from DESK.
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
    // Panels first: they hold more than the card does, so they're the more
    // likely destination when both would match.
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
      // Not a sync: this re-reads what app.db already has. Pulling fresh data
      // from the sources is the strip's sync button, which has the per-source
      // progress this list has nowhere to put.
      run: refreshAll,
    },
    {
      id: "act:console",
      title: "Focus the console",
      group: "Actions",
      hint: "write to the agent",
      keywords: "prompt ask chat claude composer message",
      // The one command that is a you-and-agent moment, so the one that takes
      // the amber thread. Everything else here just moves the desk around.
      accent: true,
      run: focusConsole,
    },
    {
      // Built fresh each time the palette opens (PlanApp memoises on it), so
      // it always offers the other one.
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
