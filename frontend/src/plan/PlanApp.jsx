import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { broadcastRefresh, watchDayRollover } from "../lib/refresh";
import CommandPalette from "../console/CommandPalette.jsx";
import { buildCommands, usePaletteKey } from "../lib/palette";
import BriefLine from "./BriefLine.jsx";
import LookLab, { LookChip } from "../ui/LookLab.jsx";
import GlanceStrip from "../widgets/GlanceStrip.jsx";
import { DESK, DESK_IDS, useCards, useLastWord } from "../widgets/cards.js";
import { SheetBody, SHEET_TOPIC } from "../widgets/views.jsx";
import { orderAsks } from "../lib/asks.js";
import { usePills } from "../lib/pills";
import { useStackEscape } from "../lib/escape";
import { sheetOpened, opened, seen } from "../lib/usage";
import { NEEDS, HAS } from "../lib/salience";
import { ConsolePane } from "../console/Console.jsx";
import { useWidgetData } from "../lib/refresh";
import { useTicker, useVisiblePoll } from "../lib/poll";
import { canRest, dueToWake, toSettle } from "../lib/tabRest.js";
import { useTurnTrace, answerConsole, draftConsole, reportConsole, resumeConsole, resumeInterruptedConsole, workingTabs } from "../lib/turn.js";
import { cutWhat, interruptedTitle, resumeText } from "../lib/interrupted.js";
import { HELD } from "../lib/handoff.js";
import { useUsageWindow } from "../console/LimitBanner.jsx";
import { useConsoleTabs, MAX_TABS, MAX_WORKING } from "../lib/tabs.js";
import { BarHostContext } from "../lib/barHost.js";
import { autoResumeOn } from "../lib/autoResume.js";
import { admit, followUp, isLive, landing, needsLine, nextQueued, queuePlace } from "../lib/children.js";
import ModePicker from "../console/ModePicker.jsx";
import ConsoleTabs, { NewTabButton, barOrder } from "./ConsoleTabs.jsx";
import OrphansPanel, { orphanLine } from "./OrphansPanel.jsx";
import { DiffPane } from "../console/DiffView.jsx";
import { createPortal } from "react-dom";
import { BLOCKING, BROKEN, CLOCK, OWED, noteFor } from "../lib/asks.js";
import { fetchJSON, postJSON } from "../lib/api";
import { useDebounce } from "../lib/timers.js";
import { offerUndo, useClearedToday } from "../lib/undo";
import { fmtTime } from "../lib/time";
import UndoToast from "./UndoToast.jsx";
import HabitNudge from "./HabitNudge.jsx";
import Icon from "../ui/icons.jsx";
import { PIN_MIME, pinDropEffect, readPin } from "../lib/drag.js";
import StatusBar, { toolLabel } from "./StatusBar.jsx";
import Timeline from "./Timeline.jsx";
import { PANEL_FOR } from "../lib/palette";
import PlanMap from "./PlanMap.jsx";
import ViewPane from "./ViewPane.jsx";
import NeedsList from "./NeedsList.jsx";
import CardDetail from "./CardDetail.jsx";
import FullView from "./FullView.jsx";
import Rooms, { TodaySentence, roomOf } from "./Rooms.jsx";
import FocusLine from "./FocusLine.jsx";
import AgentLayer from "./AgentLayer.jsx";
import { currentLayout, useLook } from "../lib/look.js";
import { useScheduledPrompts } from "../lib/schedules.js";

// The plan view: strip and brief, the stage (console + map), the "needs you" list.
// Design: DESIGN.md → Layout: the plan view (`plan/PlanApp.jsx`).
const clip = (s, n) => (s.length > n ? `${s.slice(0, n - 1)}…` : s);
const minutesFrom = (n) => new Date(Date.now() + n * 60000).toISOString();
// Local noon, read back in local parts: DST-safe, and toISOString is a day off east of UTC.
const dayBefore = (key) => {
  const d = new Date(`${key}T12:00:00`);
  d.setDate(d.getDate() - 1);
  const pad = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
};
const SIDE_HIDDEN = "plan_side_hidden";
// Keep in step with the @media in styles.css.
const NARROW = "(max-width: 1200px)";
const weekday = (key) => new Date(`${key}T12:00:00`).toLocaleDateString([], { weekday: "long" });

export default function PlanApp() {
  const { pills, pinContext, removePill, clearPills, addPill, updatePill } = usePills();
  const [paletteOpen, setPaletteOpen] = useState(false);
  usePaletteKey(setPaletteOpen);
  const focusComposer = useRef(null);

  // The side column: the list, one card, or Sources.
  const [side, setSide] = useState({ view: "list" });
  // Narrow: closed until asked for, then takes the map's place. Wide: can be put away (per browser).
  const [sideOpen, setSideOpen] = useState(false);
  const [sideHidden, setSideHidden] = useState(() => {
    try { return localStorage.getItem(SIDE_HIDDEN) === "1"; } catch { return false; }
  });
  const hideSide = useCallback(() => {
    setSideOpen(false);
    // Narrow: × only closes; saving it as "put away" would hide the column on widening.
    if (window.matchMedia(NARROW).matches) return;
    setSideHidden(true);
    try { localStorage.setItem(SIDE_HIDDEN, "1"); } catch { /* per-viewer nicety */ }
  }, []);
  const dockSide = useCallback(() => {
    setSideOpen(false);
    setSideHidden(false);
    try { localStorage.removeItem(SIDE_HIDDEN); } catch { /* fine */ }
  }, []);
  // Narrowing closes the column; the chip brings it back.
  useEffect(() => {
    const mq = window.matchMedia(NARROW);
    const onChange = (e) => { if (e.matches) setSideOpen(false); };
    mq.addEventListener("change", onChange);
    return () => mq.removeEventListener("change", onChange);
  }, []);
  // Full views, in the order you opened them, and the one showing.
  const [stack, setStack] = useState([]);
  const [showing, setShowing] = useState(null);

  const cards = useCards();
  const sal = useLastWord(cards);
  useEffect(() => { watchDayRollover(); }, []);

  // Map inputs beyond the cards: source syncs, console activity, and the hovered card (shared with the list).
  const { data: status, reload: reloadStatus } = useWidgetData("/api/status");
  useVisiblePoll(reloadStatus, 60000);
  const sources = useMemo(() => Object.fromEntries(
    (status?.sources || []).map((s) => [s.source, s])), [status]);
  const ctabs = useConsoleTabs();
  const trace = useTurnTrace(ctabs.active);
  const agents = useMemo(() => workingTabs(ctabs.tabs, trace.tabs), [ctabs.tabs, trace.tabs]);
  const [hot, setHot] = useState(null);
  const [keyed, setKeyed] = useState(false);
  // Claude's view takes the map's place; a new picture brings it up unless closed this turn.
  const [viewing, setViewing] = useState(false);
  const closedTurn = useRef(null);
  const newestView = trace.views.at(-1)?.src;
  useEffect(() => {
    if (newestView && trace.running && closedTurn.current !== trace.startedAt) setViewing(true);
  }, [newestView]); // eslint-disable-line react-hooks/exhaustive-deps -- fires on a new picture only; running/startedAt are read at that moment, not watched
  // A computer-use job from any tab; one asking for an answer opens even a closed pane.
  const job = Object.values(trace.tabs).find((t) => t.computer)?.computer || null;
  useEffect(() => { if (job?.show) setViewing(true); }, [job?.show, job?.id]);
  const closeView = () => { closedTurn.current = trace.startedAt; setViewing(false); setViewOn(null); };
  // A new preview ask from the tab on screen brings the pane up; switching tabs isn't one.
  const previewAsk = { tab: ctabs.active, n: trace.preview?.n || 0 };
  const [seenAsk, setSeenAsk] = useState(previewAsk);
  // The pane's first tab when an ask is what opened it.
  const [viewOn, setViewOn] = useState(null);
  if (seenAsk.tab !== previewAsk.tab || seenAsk.n !== previewAsk.n) {
    setSeenAsk(previewAsk);
    if (seenAsk.tab === previewAsk.tab && previewAsk.n > seenAsk.n) {
      if (!viewing) setViewOn("preview");
      setViewing(true);
    }
  }

  // Console state: "read" (beside the map), "peek" (folded to its bar), "full" (takes the window).
  // ⌘J toggles read/peek; ⌘F toggles full (shadowing the browser's find). Hybrid opens peeked.
  const [sheet, setSheet] = useState(() => (currentLayout() === "hybrid" ? "peek" : "read"));
  const [labOpen, setLabOpen] = useState(false);   // the look lab (ui/LookLab.jsx)
  // Layout ("hybrid" rooms desk or "classic") and the room you're in; switching layout resets both.
  const { layout } = useLook();
  const [cview, setCview] = useState(null);
  const [cfocus, setCfocus] = useState(null);
  const [cLayout, setCLayout] = useState(layout);
  if (cLayout !== layout) {
    setCLayout(layout); setCview(null); setCfocus(null);
    if (layout === "hybrid") setSheet((s) => (s === "read" ? "peek" : s));
  }
  const layoutRef = useRef(layout);
  layoutRef.current = layout;
  // The floating top bar's height as --top-h on .plan (one line, two, or wrapped); a ResizeObserver, not an effect.
  const topObs = useRef(null);
  const measureTop = useCallback((el) => {
    topObs.current?.disconnect();
    topObs.current = null;
    if (!el || typeof ResizeObserver === "undefined") return;
    const plan = el.closest(".plan");
    topObs.current = new ResizeObserver(() => plan?.style.setProperty("--top-h", `${Math.ceil(el.offsetHeight)}px`));
    topObs.current.observe(el);
  }, []);
  // The folded console's height (bar + growing composer) as --dock-h, so a room's foot clears the dock.
  const dockObs = useRef(null);
  const measureDock = useCallback((stage) => {
    dockObs.current?.disconnect();
    dockObs.current = null;
    if (!stage || typeof ResizeObserver === "undefined") return;
    const plan = stage.closest(".plan");
    const sheetEl = stage.querySelector(":scope > .stage-sheet");
    const set = () => {
      if (!sheetEl) return;
      const h = stage.getBoundingClientRect().bottom - sheetEl.getBoundingClientRect().top;
      plan?.style.setProperty("--dock-h", `${Math.max(0, Math.ceil(h))}px`);
    };
    dockObs.current = new ResizeObserver(set);
    stage.querySelectorAll(":scope > :is(.stage-sheet, .prompt-bar)").forEach((el) => dockObs.current.observe(el));
  }, []);
  const toggleSheet = useCallback(() => setSheet((s) => (s === "peek" ? "read" : "peek")), []);
  const raiseSheet = useCallback(() => setSheet((s) => (s === "peek" ? "read" : s)), []);
  // Going to a tab is going to its conversation.
  const showTab = useCallback((id) => {
    ctabs.show(id);
    setSheet((s) => (s === "peek" ? "read" : s));
  }, [ctabs.show]);   // eslint-disable-line react-hooks/exhaustive-deps -- ctabs is a new object each render; its methods are stable useCallbacks
  // ⌘J/⌘F, and `kestrel:notification` (desktop_shell.py: a clicked banner naming its tab).
  useEffect(() => {
    const onKey = (e) => {
      if (!(e.metaKey || e.ctrlKey) || e.altKey || e.shiftKey) return;
      if (e.key === "j") { e.preventDefault(); toggleSheet(); }
      else if (e.key === "f") { e.preventDefault(); setSheet((s) => (s === "full" ? "read" : "full")); }
    };
    // show() ignores a tab closed since the banner went out.
    const onNotification = (e) => { if (e.detail?.tab) showTab(e.detail.tab); };
    window.addEventListener("keydown", onKey);
    window.addEventListener("kestrel:notification", onNotification);
    return () => {
      window.removeEventListener("keydown", onKey);
      window.removeEventListener("kestrel:notification", onNotification);
    };
  }, [toggleSheet, showTab]);
  // A turn ending out of sight (peeked sheet, background tab) marks that tab unread until opened.
  const [unreadTabs, setUnreadTabs] = useState(() => new Set());
  const wasRunning = useRef({});
  const wasAsking = useRef({});
  // Turn ends arrive over Console's socket as trace.tabs changing: no handler to move this into.
  useEffect(() => {
    const ended = [], started = [];
    for (const [id, t] of Object.entries(trace.tabs)) {
      // eslint-disable-next-line react-you-might-not-need-an-effect/no-event-handler -- reacts to trace.tabs, which Console's socket drives; no event here to handle (above)
      if (wasRunning.current[id] && !t.running && (id !== ctabs.active || sheet === "peek")) ended.push(id);
      // Notify only your own tabs (#79): a spawned one reports to its parent unless you've typed in it.
      // at_desk() in app/notify.py gates the rest.
      const tab = ctabs.tabs.find((x) => x.id === id);
      if (wasRunning.current[id] && !t.running && (!tab?.spawned || tab.engaged)
          && (id !== ctabs.active || document.hidden || !document.hasFocus())) {
        postJSON("/api/console/notify", { id, tab: id, at: String(Date.now()), title: "Kestrel finished",
                                          body: tab?.title || "A tab finished its turn" }).catch(() => {});
      }
      if (t.running) started.push(id);
      // A child stopped on a question: tell its parent, once per question.
      const q = t.waiting?.question ? t.waiting.requestId : null;
      if (q && wasAsking.current[id] !== q) {
        const tab = ctabs.tabs.find((x) => x.id === id);
        if (tab?.spawned && tab.parent && !tab.engaged)
          tellParent(tab.parent, { from: id, status: "needs you",
            label: tab.profile === "dev" ? `dev · ${(tab.project || "").split("/").pop()}` : tab.profile,
            text: `It stopped on a question for the user${t.waiting.detail ? `: ${t.waiting.detail}` : ""}`
                  .replace(/([^.?!])$/, "$1.") + " "
                  + "It waits in its tab until they answer there." });
      }
    }
    wasAsking.current = Object.fromEntries(Object.entries(trace.tabs)
      .map(([id, t]) => [id, t.waiting?.question ? t.waiting.requestId : null]));
    wasRunning.current = Object.fromEntries(Object.entries(trace.tabs).map(([id, t]) => [id, t.running]));
    if (!ended.length && !started.some((id) => unreadTabs.has(id))) return;
    setUnreadTabs((u) => {
      const next = new Set(u);
      started.forEach((id) => next.delete(id));
      ended.forEach((id) => next.add(id));
      return next;
    });
  }, [trace.tabs]);   // eslint-disable-line react-hooks/exhaustive-deps -- runs per change of trace.tabs; unreadTabs is only read to skip a no-op update
  // Seeing a conversation reads it; adjusted in render so the mark clears in the same paint.
  const readNow = [...unreadTabs].filter((id) => !ctabs.tabs.some((t) => t.id === id)
                                                  || (id === ctabs.active && sheet !== "peek"));
  if (readNow.length) setUnreadTabs((u) => new Set([...u].filter((id) => !readNow.includes(id))));
  const unread = unreadTabs.has(ctabs.active);
  // A question card is answered in the conversation, so it raises the sheet; a permission can use the bar.
  useEffect(() => {
    if (trace.waiting?.question) setSheet((s) => (s === "peek" ? "read" : s));
  }, [trace.waiting]);

  const openTab = useCallback(() => {
    ctabs.open();
    setSheet((s) => (s === "peek" ? "read" : s));
  }, [ctabs.open]);   // eslint-disable-line react-hooks/exhaustive-deps -- ctabs is a new object each render; its methods are stable useCallbacks
  // A tab mid-turn doesn't close: unmounting would cut the turn off.
  const closeTab = useCallback((id) => {
    const t = trace.tabs[id];
    if (t?.running || t?.waiting) return;
    // Read before the close drops the tab.
    const tree = t?.worktree?.path || ctabs.tabs.find((x) => x.id === id)?.worktree;
    ctabs.close(id);
    // What the socket's release (console_worktree.release) left is the left-behind list's.
    if (tree) releasedRef.current?.(tree);
  }, [trace.tabs, ctabs.tabs, ctabs.close]);   // eslint-disable-line react-hooks/exhaustive-deps -- ctabs is a new object each render; its methods are stable useCallbacks
  const releasedRef = useRef(null);
  // Snooze (`until`) or settle a tab (#91): refused mid-turn like closing; its worktree stays.
  const restTab = useCallback((id, { until = null } = {}) => {
    const t = ctabs.tabs.find((x) => x.id === id);
    if (!t || !canRest(t, trace.tabs[id]).ok) return;
    ctabs.rest(id, { until });
  }, [trace.tabs, ctabs.tabs, ctabs.rest]);   // eslint-disable-line react-hooks/exhaustive-deps -- ctabs is a new object each render; its methods are stable useCallbacks
  // Retitle from the whole session (#102). Resolves to an error line, or null on success.
  const sessionOf = (id) => { try { return localStorage.getItem(`console_session:${id}`); } catch { return null; } };
  const canRetitle = useCallback((id) => !!sessionOf(id), []);
  const retitleTab = useCallback(async (id) => {
    const t = ctabs.tabs.find((x) => x.id === id);
    const sid = sessionOf(id);
    if (!t || !sid) return "nothing to read yet";
    try {
      const q = t.project ? `?project=${encodeURIComponent(t.project)}` : "";
      const r = await postJSON(`/api/sessions/${encodeURIComponent(sid)}/title${q}`, {});
      ctabs.rename(id, r.title);
      return null;
    } catch (err) {
      return `Couldn't name it: ${err.message}`;
    }
  }, [ctabs.tabs, ctabs.rename]);   // eslint-disable-line react-hooks/exhaustive-deps -- ctabs is a new object each render; its methods are stable useCallbacks
  // Each minute, hidden or not: wake due snoozes; settle tabs (lib/tabRest.js).
  useTicker(() => {
    const { tabs, active, turns } = capsRef.current;
    const due = dueToWake(tabs);
    if (due.length) ctabs.wake(due);
    for (const { id, why } of toSettle(tabs, turns, { active, unread: unreadRef.current })) ctabs.rest(id, { why });
  }, 60000, { whenHidden: true });
  const unreadRef = useRef(unreadTabs);
  unreadRef.current = unreadTabs;
  // open_tab handoffs (console_tools.open_tab → onOpenTab) are admitted here: only the window knows the caps.
  // Read through a ref so the callback stays stable and memoised panes don't re-render per turn event.
  const capsRef = useRef({});
  capsRef.current = { tabs: ctabs.tabs, active: ctabs.active, turns: trace.tabs,
                      working: Object.values(trace.tabs).filter((t) => t.running).length };
  // Past MAX_WORKING the brief queues (nextQueued, below); past MAX_TABS it is refused.
  const handOff = useCallback(({ mode, project, brief, from, model = null, effort = null }) => {
    const { tabs, turns } = capsRef.current;
    const verdict = admit(tabs, turns, { from, maxWorking: MAX_WORKING, maxTabs: MAX_TABS });
    if (verdict.refuse) return { ok: false, reason: verdict.refuse };
    // The id is made here so the model hears it (message_tab names it later); model/effort ride on the tab for queued starts.
    const id = Math.random().toString(36).slice(2, 8);
    const spec = { id, profile: mode, project: project || null, spawned: true, parent: from || null, brief,
                   ...(model ? { model } : {}), ...(effort ? { effort } : {}) };
    ctabs.open(verdict.queue ? { ...spec, queued: true, queuedAt: Date.now() } : spec);
    // A second open_tab in the same turn reads the ref before the state renders.
    capsRef.current = { ...capsRef.current, tabs: [...tabs, { ...spec, queued: !!verdict.queue,
                                                              queuedAt: verdict.queue ? Date.now() : null }] };
    return verdict.queue ? { ok: true, id, queued: verdict.queue, reason: `queued #${verdict.queue}` }
      : { ok: true, id, reason: "" };
  }, [ctabs.open]);   // eslint-disable-line react-hooks/exhaustive-deps -- ctabs is a new object each render; its methods are stable useCallbacks; the rest is read from capsRef
  // ⌘Enter (#93): the prompt opens in a new tab behind, same mode/project/model/effort. The user's tab, not a child.
  const backgroundTab = useCallback(({ text, pills, from }) => {
    const { tabs } = capsRef.current;
    if (tabs.filter(isLive).length >= MAX_TABS) return { ok: false, reason: `${MAX_TABS} tabs is the most` };
    const src = tabs.find((t) => t.id === from);
    if (!src?.profile) return { ok: false, reason: "this tab has no mode yet" };
    const spec = { id: Math.random().toString(36).slice(2, 8), profile: src.profile, project: src.project || null,
                   brief: text, briefPills: pills?.length ? pills : null, engaged: true,
                   ...(src.model ? { model: src.model } : {}), ...(src.effort ? { effort: src.effort } : {}) };
    ctabs.open(spec);
    capsRef.current = { ...capsRef.current, tabs: [...tabs, spec] };
    return { ok: true, id: spec.id };
  }, [ctabs.open]);   // eslint-disable-line react-hooks/exhaustive-deps -- ctabs is a new object each render; its methods are stable useCallbacks; the rest is read from capsRef
  // Scheduled prompts (Settings → Scheduled prompts) come in by handOff's door.
  useScheduledPrompts(handOff);
  // message_tab: the text becomes the child's next brief (lib/children.js followUp decides if it may).
  const messageChild = useCallback(({ id, text, from }) => {
    const { tabs, turns } = capsRef.current;
    const verdict = followUp(tabs, turns, { id, from, maxWorking: MAX_WORKING, maxTabs: MAX_TABS });
    if (verdict.refuse) return { ok: false, text: verdict.refuse };
    const t = tabs.find((x) => x.id === id);
    const label = t.profile === "dev" ? `dev · ${(t.project || "").split("/").pop()}` : t.profile;
    ctabs.followUp(id, `Follow-up from the tab that opened this one:\n\n${text}`, !!verdict.queue);
    const when = verdict.queue ? "queued: every working slot is taken, so it reopens when one frees"
      : turns[id]?.running || turns[id]?.waiting ? "it takes it when its current turn ends"
      : t.done ? "the done tab reopens and starts on it" : "it's starting on it now";
    return { ok: true, label, text: `sent to ${label} (tab ${id}); ${when}. Its report wakes this tab` };
  }, [ctabs.followUp]);   // eslint-disable-line react-hooks/exhaustive-deps -- ctabs is a new object each render; its methods are stable useCallbacks; the rest is read from capsRef
  // Start a queued child in render (own-state update) so a freed slot is taken before paint.
  {
    const next = nextQueued(ctabs.tabs, trace.tabs, { maxWorking: MAX_WORKING, maxTabs: MAX_TABS });
    if (next) ctabs.start(next);
  }
  // console_tools.tabs (#57): list tabs, close named ones. Refused for working, waiting, on-screen or the asker.
  const manageTabs = useCallback(({ close = [], from }) => {
    const { tabs, active, turns } = capsRef.current;
    const label = (t) => (t.profile === "dev" ? `dev · ${(t.project || "").split("/").pop()}`
                          : t.profile || "new, picking a mode");
    const blocker = (t) => (t.id === from ? "it's the tab asking"
      : turns[t.id]?.running ? "it's working"
      : turns[t.id]?.waiting ? "it's waiting on the user"
      : t.id === active ? "the user is looking at it" : null);
    const closed = [], refused = [];
    for (const id of close) {
      const t = tabs.find((x) => x.id === id);
      const why = t ? blocker(t) : "no such tab";
      if (why) refused.push(`${id}: ${why}`);
      else { ctabs.close(id); closed.push(t); }
    }
    const left = tabs.filter((t) => !closed.includes(t));
    const state = (t) => [
      t.id === from ? "this tab" : null,
      t.queued ? `queued (#${queuePlace(tabs, t.id)}), starts when a slot frees`
        : t.done ? "done: its work landed and it closed itself under its parent"
        : turns[t.id]?.running ? "working" : turns[t.id]?.waiting ? "waiting on the user" : "idle",
      t.needs ? `needs the user: ${t.needs}` : null,
      t.parent && tabs.some((p) => p.id === t.parent) ? `opened by ${t.parent}` : null,
      t.id === active ? "on screen" : null,
      t.spawned && t.reported ? "finished handoff (report delivered)" : null,
      t.spawned && !t.reported && t.held ? `handoff not finished, no report yet (${HELD[t.held] || t.held})` : null,
      t.engaged ? "user has typed in it" : null,
    ].filter(Boolean).join(", ");
    const lines = left.map((t) => `${t.id} · ${label(t)} · ${t.title ? `"${t.title.slice(0, 60)}"` : "untitled"} · ${state(t)}`);
    const text = [
      closed.length ? `closed: ${closed.map((t) => `${t.id} (${t.title || label(t)})`).join(", ")}` : null,
      refused.length ? `not closed: ${refused.join("; ")}` : null,
      `${left.filter(isLive).length} of ${MAX_TABS} tabs open`
        + (left.length > left.filter(isLive).length ? ", plus queued and done ones:" : ":"),
      ...lines,
    ].filter(Boolean).join("\n");
    return { ok: true, text, closed: closed.map((t) => t.title || label(t)) };
  }, [ctabs.close]);   // eslint-disable-line react-hooks/exhaustive-deps -- ctabs is a new object each render; its methods are stable useCallbacks; the rest is read from capsRef
  // A report goes into the parent's tab, unread there unless on screen.
  const viewRef = useRef({});
  viewRef.current = { active: ctabs.active, sheet };
  // A `held` report isn't one (the turn ended short): the parent hears nothing until a turn finishes.
  // A finished one closes the child if its work landed (lib/children.js landing), else it stays
  // marked "needs you". Never for a tab the user has typed in.
  const tellParent = useCallback((parent, report) => {
    if (!parent) return;
    reportConsole(parent, report);
    const { active, sheet: s } = viewRef.current;
    if (parent !== active || s === "peek") setUnreadTabs((u) => new Set(u).add(parent));
  }, []);
  const onReport = useCallback(async (parent, report) => {
    if (!report?.from) return;
    ctabs.set(report.from, "held", report.held || null);
    if (report.held) return;
    ctabs.flag(report.from, "reported");
    const t = capsRef.current.tabs.find((x) => x.id === report.from);
    if (!t || t.engaged) return tellParent(parent, report);
    const wt = t.profile === "dev" ? (capsRef.current.turns[t.id]?.worktree?.path || t.worktree) : null;
    const status = wt ? await fetchJSON(`/api/console/worktree?project=${encodeURIComponent(t.project || "")}`
                                        + `&worktree=${encodeURIComponent(wt)}`)
      .catch((e) => ({ error: e.message })) : null;
    const land = landing({ text: report.text, status });
    if (land.done) {
      ctabs.finish(t.id);
      return tellParent(parent, report);
    }
    ctabs.set(t.id, "needs", land.why);
    tellParent(parent, { ...report, status: "needs you",
      text: needsLine(report.text) ? report.text : `${report.text}\n\nNeeds you: ${land.why}. The tab is still open.` });
  }, [tellParent]);   // eslint-disable-line react-hooks/exhaustive-deps -- ctabs is a new object each render; its methods are stable useCallbacks; the rest is read from capsRef
  // Esc on the picker closes the tab, or makes the last one the general console.
  const pickMode = useCallback((id, profile, project) => ctabs.configure(id, profile, project),
    [ctabs.configure]);   // eslint-disable-line react-hooks/exhaustive-deps -- ctabs is a new object each render; its methods are stable useCallbacks
  const cancelPick = useCallback((id) => {
    if (ctabs.tabs.filter(isLive).length > 1) ctabs.close(id); else ctabs.configure(id, "general");
  }, [ctabs.tabs, ctabs.close, ctabs.configure]);   // eslint-disable-line react-hooks/exhaustive-deps -- ctabs is a new object each render; its methods are stable useCallbacks

  // ⌥T new, ⌥W close, ⌥1–9 go to (⌘ versions never reach the page in Chrome). e.code: ⌥ remaps e.key.
  useEffect(() => {
    const onKey = (e) => {
      if (!e.altKey || e.metaKey || e.ctrlKey || e.shiftKey) return;
      const digit = /^Digit([1-9])$/.exec(e.code);
      if (e.code === "KeyT") { e.preventDefault(); openTab(); }
      else if (e.code === "KeyW") { e.preventDefault(); closeTab(ctabs.active); }
      else if (digit) {
        // In the bar's order: children follow their parent (ConsoleTabs).
        const t = barOrder(ctabs.tabs, ctabs.active)[Number(digit[1]) - 1];
        if (t) { e.preventDefault(); showTab(t.id); }
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [openTab, closeTab, showTab, ctabs.active, ctabs.tabs]);

  const [timeline, setTimeline] = useState(null);   // null | "all" | "failed"

  // Portal targets for the console: its prompt, its document pane (console/DocPane.jsx), its bar.
  const [composerEl, setComposerEl] = useState(null);
  const [docEl, setDocEl] = useState(null);
  const [barEl, setBarEl] = useState(null);
  // What the status bar folded, plus its actions (lib/barHost.js); actions sit in a ref so the context changes only with the fold.
  const [barCut, setBarCut] = useState("");
  const barHost = useRef({});
  barHost.current = {
    onFull: () => setSheet((s) => (s === "full" ? "read" : "full")),
    onView: () => {
      if (viewing) return closeView();
      setViewing(true);
      setSheet((s) => (s === "full" ? "read" : s));
    },
    onTimeline: () => { setTimeline((t) => (t ? null : "all")); setSheet((s) => (s === "peek" ? "read" : s)); },
    onNewTab: openTab,
    viewing, timeline: !!timeline, hasTimeline: trace.calls.length > 0, hasThumb: !!trace.views?.length,
    canNew: ctabs.tabs.filter(isLive).length < MAX_TABS,
  };
  const barHostValue = useMemo(() => ({ cut: barCut, host: barHost }), [barCut]);

  // A pin dropped on the sheet or Kestrel lands in the prompt's pills.
  const pinDrop = useMemo(() => ({
    onDragOver: (e) => {
      if (!e.dataTransfer.types.includes(PIN_MIME)) return;
      e.preventDefault();
      e.dataTransfer.dropEffect = pinDropEffect(e);
    },
    onDrop: (e) => {
      const pin = readPin(e.dataTransfer);
      if (!pin) return;
      e.preventDefault();
      e.stopPropagation();
      pinContext(pin.label, pin.data);
    },
  }), [pinContext]);

  // Usage: log each card's band (needs/has/quiet) once it has reported; a guess would make days_active never zero.
  const bands = DESK_IDS.map((id) => `${id}:${sal[id]?.level ?? ""}`).join(" ");
  useEffect(() => {
    for (const id of DESK_IDS) {
      const lvl = sal[id]?.level;
      if (lvl != null) seen(id, lvl === NEEDS ? "needs" : lvl === HAS ? "has" : "quiet");
    }
  }, [bands]);   // eslint-disable-line react-hooks/exhaustive-deps -- bands is sal's levels as a string, so this fires on a band changing, not on every sal object (above)

  // A card waiting in any tab tops the list and answers into its own tab (a question's row raises the sheet).
  const waiting = trace.waiting;
  const waitingByTab = useMemo(() => ctabs.tabs
    .map((t, i) => ({ tab: t, n: i + 1, w: trace.tabs[t.id]?.waiting }))
    .filter((x) => x.w), [ctabs.tabs, trace.tabs]);
  // One modal at a time (two would fight over keys): the showing tab's card, else the first asking.
  const modalTab = trace.tabs[ctabs.active]?.waiting ? ctabs.active : waitingByTab[0]?.tab.id || null;
  // A card arriving while Kestrel isn't in front also notifies, once per request; at_desk() gates.
  const askedRef = useRef(new Set());
  useEffect(() => {
    for (const { tab, w } of waitingByTab) {
      const key = `${tab.id}:${w.requestId}`;
      if (askedRef.current.has(key)) continue;
      askedRef.current.add(key);
      if (!document.hidden && document.hasFocus()) continue;
      const what = w.question ? "has a question" : `wants to run ${toolLabel(w.name)}`;
      const name = tab.title && tab.title.length > 40 ? `${tab.title.slice(0, 40)}…` : tab.title;
      postJSON("/api/console/notify", { kind: "ask", id: key, tab: tab.id, at: String(Date.now()),
                                        title: "Kestrel needs you",
                                        body: `${name || "A tab"} ${what}${w.detail ? `: ${w.detail}` : ""}` })
        .catch(() => {});
    }
  }, [waitingByTab]);
  const usageWindow = useUsageWindow();
  // Tabs a quit or dead backend cut off (console_work.py): one Resume row plus a banner per tab; nothing resumes unclicked.
  const { data: intData } = useWidgetData("/api/console/interrupted");
  const [intGone, setIntGone] = useState(() => new Set());
  const interrupted = useMemo(() => {
    const open = new Map(ctabs.tabs.map((t, i) => [t.id, { t, n: i + 1 }]));
    return (intData?.tabs || [])
      .filter((r) => open.has(r.tab) && !open.get(r.tab).t.done && !intGone.has(r.tab))
      .map((r) => ({ ...r, ...open.get(r.tab) }));
  }, [intData, ctabs.tabs, intGone]);
  const settleInterrupted = useCallback((tab) => {
    setIntGone((s) => new Set(s).add(tab));
    postJSON("/api/console/interrupted/dismiss", { tab }).catch(() => {});
  }, []);
  const resumeInterrupted = (rows) => rows.forEach((r) => {
    resumeInterruptedConsole(r.tab, resumeText(r));
    settleInterrupted(r.tab);
  });
  // Usage-limited tabs (LimitBanner), less any a quit also cut off: that row already resumes them.
  const limitedTabs = useMemo(() => ctabs.tabs
    .map((t, i) => ({ tab: t, n: i + 1 }))
    .filter(({ tab }) => trace.tabs[tab.id]?.limited && !trace.tabs[tab.id]?.running
                         && !interrupted.some((r) => r.tab === tab.id)),
  [ctabs.tabs, trace.tabs, interrupted]);
  // Left-behind branches (console_worktree.orphans), less any an open tab still holds.
  const { data: orphanData, setData: setOrphanData, reload: reloadOrphans } = useWidgetData("/api/console/orphans");
  useVisiblePoll(reloadOrphans, 120000);
  // After a close: the backend answers once that socket's release has swept the tree (console.console_orphans).
  releasedRef.current = (tree) =>
    fetchJSON(`/api/console/orphans?released=${encodeURIComponent(tree)}`)
      .then(setOrphanData).catch(() => reloadOrphans());
  const heldTrees = ctabs.tabs.filter((t) => !t.done)
    .map((t) => trace.tabs[t.id]?.worktree?.path || t.worktree).filter(Boolean).join("\n");
  const orphans = useMemo(() => {
    const held = new Set(heldTrees.split("\n"));
    return (orphanData?.orphans || []).filter((o) => !o.path || !held.has(o.path));
  }, [orphanData, heldTrees]);
  const [orphanReview, setOrphanReview] = useState(null);
  // The status bar carries Resume for the tab on screen: a folded console hides the banner.
  const cutRow = interrupted.find((r) => r.tab === ctabs.active);
  const activeLimited = !cutRow && limitedTabs.some(({ tab }) => tab.id === ctabs.active);
  const cutoff = cutRow ? {
    word: "cut off", text: `Kestrel quit mid-turn, which cut off ${cutWhat(cutRow)}`,
    resume: () => resumeInterrupted([cutRow]), resumeTitle: "Send a 'continue' that says what was cut off",
    dismiss: () => settleInterrupted(cutRow.tab),
  } : activeLimited ? {
    word: "usage limit",
    text: usageWindow.open ? "stopped by the usage limit · the window is open again"
      : `stopped by the usage limit${usageWindow.resetsAt
        ? ` · resumable at ${new Date(usageWindow.resetsAt).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })}` : ""}`,
    resume: usageWindow.open ? () => resumeConsole(ctabs.active) : null,
    resumeTitle: "Pick up where it left off",
  } : null;
  const asks = useMemo(() => {
    const many = ctabs.tabs.length > 1;
    const left = orphans.length ? [{
      id: "console:orphans", card: null, tier: OWED, kind: "",
      title: orphans.length === 1 ? "A closed tab's branch isn't merged"
        : `${orphans.length} branches from closed tabs aren't merged`,
      why: orphans.slice(0, 2).map((o) => `${o.project_name} · ${o.branch} (${orphanLine(o)})`).join("; ")
        + (orphans.length > 2 ? ` and ${orphans.length - 2} more` : ""),
      acts: [{ label: "Review", style: "go", run: () => { setSide({ view: "orphans" }); setSideOpen(true); } }],
    }] : [];
    const cut = limitedTabs.length ? [{
      id: "console:limited", card: null, tier: CLOCK, kind: "",
      title: limitedTabs.length === 1
        ? `A tab was stopped by the usage limit${many ? ` · tab ${limitedTabs[0].n}` : ""}`
        : `${limitedTabs.length} tabs were stopped by the usage limit`,
      // With auto-resume (lib/autoResume.js) each tab's banner resumes a minute or two after reset.
      why: (usageWindow.open ? "the window is open again: pick up where they left off"
        : `resumable once the window resets${usageWindow.resetsAt
          ? ` at ${new Date(usageWindow.resetsAt).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })}` : ""}`)
        + (autoResumeOn() ? " (they resume automatically shortly after)" : ""),
      acts: usageWindow.open
        ? [{ label: limitedTabs.length === 1 ? "Resume" : "Resume all", style: "go",
             run: () => limitedTabs.forEach(({ tab }) => resumeConsole(tab.id)) }]
        : [],
    }] : [];
    const quit = interrupted.length ? [{
      id: "console:interrupted", card: null, tier: CLOCK, kind: "",
      title: interruptedTitle(interrupted.length)
        + (interrupted.length === 1 && many ? ` · tab ${interrupted[0].n}` : ""),
      why: interrupted.map((r) => `${r.t.title || `tab ${r.n}`}: ${cutWhat(r)}`).join("; "),
      acts: [{ label: interrupted.length === 1 ? "Resume" : "Resume all", style: "go",
               run: () => resumeInterrupted(interrupted) },
             { label: "Dismiss", run: () => interrupted.forEach((r) => settleInterrupted(r.tab)) }],
    }] : [];
    const blocking = waitingByTab.map(({ tab, n, w }) => ({
      id: `console:${w.requestId}`, card: null, tier: BLOCKING, kind: "blocking",
      title: (w.question ? "Kestrel has a question" : `Kestrel is asking: ${toolLabel(w.name)}`)
        + (many ? ` · tab ${n}` : ""),
      why: w.detail || "the turn is paused until you answer",
      acts: w.question
        ? [{ label: "Answer", style: "go", raise: true, tab: tab.id }]
        : [{ label: "Yes", style: "go", run: () => answerConsole(w.requestId, true, tab.id) },
           { label: "No", run: () => answerConsole(w.requestId, false, tab.id) }],
    }));
    // One cause, one row: a broken Messages folds into the People row.
    const msgs = sources.messages;
    const msgsBroken = msgs && (msgs.persistently_failing || msgs.state === "broken");
    const merge = (a) => (msgsBroken && a.id === "people" ? {
      ...a, tier: BROKEN, kind: "fix",
      why: `${a.why} · Messages can't be read${msgs.detail ? ` (${clip(msgs.detail, 60)})` : ""}`,
      acts: [...a.acts, { label: "Fix access", style: "fixb", sources: true }],
    } : a);
    const courses = cards.courses?.courses || [];
    return [...blocking, ...quit, ...cut, ...orderAsks(DESK_IDS.flatMap((id) => cards[id].asks).map(merge), DESK_IDS), ...left]
      .map((a) => (a.note ? a : { ...a, note: noteFor(a, courses) }));
  }, [cards, waitingByTab, limitedTabs, usageWindow.open, usageWindow.resetsAt, sources, ctabs.tabs.length, orphans, interrupted]);   // eslint-disable-line react-hooks/exhaustive-deps -- resumeInterrupted only reads settleInterrupted, which is stable

  // Snoozes (/api/snoozes): re-read every minute and at the next expiry.
  const { data: snz, reload: reloadSnoozes } = useWidgetData("/api/snoozes");
  useVisiblePoll(reloadSnoozes, 60000);
  const [clock, setClock] = useState(Date.now());
  const until = useMemo(() => Object.fromEntries(
    Object.entries(snz?.snoozes || {}).filter(([, u]) => new Date(u).getTime() > clock)), [snz, clock]);
  const nextExpiry = Math.min(...Object.values(until).map((u) => new Date(u).getTime()));
  useDebounce(() => setClock(Date.now()),
              Number.isFinite(nextExpiry) ? Math.max(1000, nextExpiry - Date.now() + 500) : null, [until]);
  const active = useMemo(() => asks.filter((a) => !until[a.id]), [asks, until]);
  const snoozed = useMemo(() => asks.filter((a) => until[a.id]).map((a) => ({ ask: a, until: until[a.id] })),
    [asks, until]);

  // "Later" options per ask (clock: minutes; deadline: its eve; else next event end, tonight, tomorrow), max four for keys 1–4.
  // todays is empty when the calendar card shows tomorrow.
  const todays = cards.calendar?.view === "today" ? cards.calendar.upcoming || [] : [];
  const nextEvent = todays.find((ev) => !ev.all_day && ev.end);
  const snoozeOptions = useCallback((a) => {
    const o = [];
    if (a.clock) return [{ label: "5 min", until: minutesFrom(5) }, { label: "10 min", until: minutesFrom(10) }];
    if (nextEvent && new Date(nextEvent.end) > new Date()) {
      o.push({ label: `After ${clip(nextEvent.summary, 22)}`, until: nextEvent.end,
               hint: `until ${fmtTime(nextEvent.end)}` });
    }
    const eve = a.deadline && dayBefore(a.deadline);
    if (eve && eve > (snz?.day || "")) {
      o.push({ label: `Before ${weekday(a.deadline)}`, day: eve, hint: `${eve} morning` });
    }
    if (new Date().getHours() < 20 && new Date().getHours() >= 4) o.push({ label: "Tonight", preset: "tonight", hint: "8 PM" });
    o.push({ label: "Tomorrow", preset: "tomorrow", hint: "tomorrow 8 AM" });
    return o.slice(0, 4);
  }, [nextEvent, snz]);

  // Snooze, then offer undo. Resolves to the `until` given, or null.
  const snoozeAsk = useCallback((a, o) => {
    const body = { ask_id: a.id, ...(o.preset ? { preset: o.preset } : o.day ? { day: o.day } : { until: o.until }) };
    return postJSON("/api/snoozes", body).then((res) => {
      reloadSnoozes();
      offerUndo({
        label: `Snoozed “${a.title}” until ${o.hint || o.label}`,
        undo: () => postJSON("/api/snoozes/wake", { ask_id: a.id }).then(reloadSnoozes),
      });
      return res.until;
    }).catch(() => null);
  }, [reloadSnoozes]);
  const wake = useCallback((a) => postJSON("/api/snoozes/wake", { ask_id: a.id }).then(reloadSnoozes),
    [reloadSnoozes]);

  // Map badges count unsnoozed asks; a card with every ask snoozed draws as "today", dashed.
  const askCount = useMemo(() => {
    const n = {};
    for (const a of active) if (a.card) n[a.card] = (n[a.card] || 0) + 1;
    return n;
  }, [active]);
  const snoozedCards = useMemo(() => new Set(
    snoozed.map((s) => s.ask.card).filter((c) => c && !askCount[c])), [snoozed, askCount]);

  // The next thing with a clock on it, for the all-clear.
  const upcoming = todays.find((ev) => !ev.all_day && new Date(ev.start) > new Date());
  const nextClock = upcoming ? `${upcoming.summary} at ${fmtTime(upcoming.start)}` : null;
  const cleared = useClearedToday();

  const openCard = useCallback((id) => {
    opened(id);   // the plan view's "open": selecting a row or node counts
    // On the rooms desk a card lives in its room.
    const lay = layoutRef.current;
    if (lay === "hybrid" && roomOf(id)) { setCview(roomOf(id)); setCfocus(id); return; }
    setSide({ view: "card", id });
    setSideOpen(true);
  }, []);

  const openFull = useCallback((key) => {
    // Every route into a full view lands here, so the open is counted once.
    sheetOpened(key);
    setStack((s) => (s.includes(key) ? s : [...s, key]));
    setShowing(key);
  }, []);

  const closeSide = useCallback(() => {
    setSide({ view: "list" });
    setSideOpen(false);
  }, []);

  const closeFull = useCallback((key) => {
    setStack((s) => {
      const next = s.filter((k) => k !== key);
      setShowing((cur) => (cur === key ? next[next.length - 1] ?? null : cur));
      return next;
    });
    if (SHEET_TOPIC[key]) broadcastRefresh(SHEET_TOPIC[key]);
  }, []);

  const closeAllFull = useCallback(() => {
    stack.forEach((k) => SHEET_TOPIC[k] && broadcastRefresh(SHEET_TOPIC[k]));
    setStack([]);
    setShowing(null);
  }, [stack]);

  // Esc unwinds the top: full console (unless a card waits: Esc is its "no"), newest full view, then card/Sources. The palette owns it while up.
  const escape = useCallback(() => {
    if (paletteOpen) return;
    if (sheet === "full") { if (!waiting) setSheet("read"); }
    else if (stack.length) closeFull(stack[stack.length - 1]);
    else if (cview && side.view === "list") { setCview(null); setCfocus(null); }
    else closeSide();
  }, [paletteOpen, sheet, waiting, stack, closeFull, closeSide, cview, side.view]);
  useStackEscape(escape);
  // Esc inside the console is the console's, except leaving full screen.
  useEffect(() => {
    if (sheet !== "full") return;
    const onKey = (e) => {
      if (e.key !== "Escape" || waiting || paletteOpen) return;
      if (e.target.closest?.(".console-pane, .prompt-bar")) setSheet("read");
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [sheet, waiting, paletteOpen]);

  const act = useCallback((a, x) => {
    if (x.run) x.run();
    else if (x.sheet) openFull(x.sheet);
    else if (x.raise && x.tab) showTab(x.tab);
    else if (x.raise) setSheet((s) => (s === "peek" ? "read" : s));
    else if (x.sources) setSide({ view: "sources" });
    else if (x.draft) {
      // A drafted prompt: the pill and the words go to the composer, nothing is sent.
      if (x.draft.pin) pinContext(...x.draft.pin);
      draftConsole(x.draft.text);
    } else if (a.card) openCard(a.card);
  }, [openFull, openCard, pinContext, showTab]);

  const commands = useMemo(() => buildCommands({
    desk: DESK,
    openPanel: openFull,
    jumpTo: openCard,
    refreshAll: broadcastRefresh,
    focusConsole: () => focusComposer.current?.(),
    openLookLab: () => setLabOpen(true),
  }), [openFull, openCard, paletteOpen]);   // eslint-disable-line react-hooks/exhaustive-deps -- rebuilt on each palette open so state-reading commands are current; the setters are stable (above)

  // ⌘K's Notes group, from the second letter.
  const searchNotes = useCallback((q) => fetchJSON(`/api/vault/search?q=${encodeURIComponent(q)}&limit=8`)
    .then((r) => r.notes.map((n) => ({
      id: `note:${n.path}`, title: n.title, group: "Notes", section: true, positions: [],
      hint: `${n.folder || "vault"} · ${new Date(n.edited).toLocaleDateString([], { month: "short", day: "numeric" })}`,
      run: () => openFull(`note:${n.path}`),
    }))), [openFull]);

  const full = stack.length > 0;
  const wide = side.view !== "list";
  const loading = Object.keys(sal).length === 0;
  // Classic: the top bar's second line. Rooms: that line is the focus line, so this goes under Today.
  const brief = <BriefLine asks={active} onPin={pinContext} onFull={() => openFull("brief")} />;

  return (
    <div className={`plan${full ? " full" : ""}${wide ? " side-wide" : ""}${sideOpen ? " side-open" : ""}${sideHidden ? " side-hidden" : ""}${sheet === "full" ? " console-full" : ""}`}
      data-cview={cview || undefined}>
      <header className="plan-top" ref={measureTop}>
        {layout === "hybrid" && <TodaySentence cards={cards} loading={!cards.calendar?.payload && !cards.calendar?.error} />}
        <GlanceStrip
          onExpandCalendar={() => openCard("calendar")}
          onOpenStatus={() => { setSide({ view: "sources" }); setSideOpen(true); }}
          onPin={pinContext}
          needCount={active.length}
          onNeeds={() => setSideOpen((v) => !v)}
          onPalette={() => setPaletteOpen(true)}
          onSettings={() => openFull("settings")}
          onWeather={() => openFull("weather")} />
        {layout === "hybrid"
          ? <FocusLine asks={active} act={act} onOpen={openCard} snooze={{ options: snoozeOptions, run: snoozeAsk }} />
          : brief}
      </header>

      <div className="plan-main">
        {full && (
          <FullView stack={stack} showing={showing} onShow={setShowing}
            onClose={closeFull} onBack={closeAllFull}
            onPin={pinContext} onOpen={openFull} />
        )}

        {/* The work column stays mounted: the console holds a live session. */}
        {/* Rooms precede it in the DOM so Tab reaches them first; under 860px a room folds the console. */}
        {layout === "hybrid" && (
          <Rooms view={cview} setView={(v) => {
              setCview(v); setCfocus(null);
              if (window.matchMedia?.("(max-width: 860px)").matches) setSheet((s) => (s === "read" ? "peek" : s));
            }}
            cards={cards} sal={sal} asks={active} act={act} onPin={pinContext}
            openFull={openFull} focus={cfocus} onPalette={() => setPaletteOpen(true)}
            snoozedCards={snoozedCards} loading={loading} brief={brief}
            cleared={cleared} snooze={{ options: snoozeOptions, run: snoozeAsk }} />
        )}

        <div className="plan-work">
          <section className={`plan-stage ${sheet}${viewing ? " viewing" : ""}`} ref={measureDock}>
            {viewing && (
              <ViewPane views={trace.views} job={job} onClose={closeView}
                plan={trace.plan} edited={trace.edited} background={trace.background}
                subagents={[...trace.calls.filter((c) => (c.name === "Agent" || c.name === "Task") && c.state === "live"
                  && !(trace.agents || []).some((a) => a.tool_use_id === c.id)),
                  ...(trace.agents || []).map((a) => ({ ...a, detail: a.description }))]}
                project={ctabs.tabs.find((t) => t.id === ctabs.active)?.project || null}
                worktree={trace.worktree?.path || null}
                preview={trace.preview} previewUrl={trace.previewUrl} runs={trace.runs} openOn={viewOn} />
            )}
            <PlanMap cards={cards} sal={sal} sources={sources} trace={trace}
              askCount={askCount} snoozedCards={snoozedCards} unread={unreadTabs.size > 0}
              agents={agents} activeTab={ctabs.active} onAgent={showTab}
              hot={hot} onHot={setHot} onOpen={openCard}
              onFull={(id) => (PANEL_FOR[id] ? openFull(PANEL_FOR[id]) : openCard(id))}
              onPin={pinContext} keyed={keyed} onKey={() => setKeyed((k) => !k)}
              onHub={() => { setSheet("read"); focusComposer.current?.(); }}
              hubDrop={pinDrop}
              onVault={() => openFull("notes")} />
            <BarHostContext.Provider value={barHostValue}>
            <div className={`stage-sheet${waiting && !waiting.question ? " waiting" : ""}`} {...pinDrop}>
              <StatusBar trace={trace} sheet={sheet} unread={unread} onToggle={toggleSheet}
                onFull={barHost.current.onFull}
                viewing={viewing} timeline={!!timeline}
                onView={barHost.current.onView}
                onTimeline={barHost.current.onTimeline}
                onFailed={() => { setTimeline("failed"); setSheet((s) => (s === "peek" ? "read" : s)); }}
                tabs={<ConsoleTabs tabs={ctabs.tabs} active={ctabs.active} turns={trace.tabs}
                        unread={unreadTabs} onShow={showTab} onClose={closeTab}
                        onPin={ctabs.pin} onMove={ctabs.move} onRest={restTab}
                        onRetitle={retitleTab} onRename={ctabs.rename} canRetitle={canRetitle} />}
                newTab={<NewTabButton count={ctabs.tabs.filter(isLive).length} onOpen={openTab} />}
                barSlot={setBarEl} onCut={setBarCut} cutoff={cutoff}
                ownMenu={!ctabs.tabs.find((t) => t.id === ctabs.active)?.profile} />
              {/* Every live tab stays mounted (a hidden one keeps its socket); queued/done ones only when on screen. */}
              {ctabs.tabs.filter((t) => t.id === ctabs.active || isLive(t)).map((t) => (
                <main key={t.id} className={`console-pane${t.id === ctabs.active ? "" : " tab-hidden"}`}>
                  {t.profile ? (
                    <ConsolePane tabId={t.id} active={t.id === ctabs.active} onTitle={ctabs.title}
                      profile={t.profile} project={t.project || null} spawned={!!t.spawned}
                      tabModel={t.model || null} tabEffort={t.effort ?? null} onTabSet={ctabs.set}
                      parent={t.parent || null} held={t.reported ? null : t.held || null}
                      reported={!!t.reported}
                      interrupted={interrupted.find((r) => r.tab === t.id) || null}
                      onInterrupted={settleInterrupted}
                      onReport={onReport} onShowTab={showTab}
                      onTyped={ctabs.flag} onTabs={manageTabs} onWorktree={ctabs.set} onMessageTab={messageChild}
                      brief={t.brief || null} briefPills={t.briefPills || null} onBriefSent={ctabs.briefSent}
                      onOpenTab={handOff} onBackgroundTab={backgroundTab}
                      pills={pills} onRemovePill={removePill} onClearPills={clearPills}
                      onAddPill={addPill} onUpdatePill={updatePill} onPinContext={pinContext}
                      focusComposer={focusComposer} composerTarget={composerEl} docTarget={docEl}
                      barTarget={barEl} onRaise={raiseSheet}
                      usageInBar={false} modal={t.id === modalTab} />
                  ) : (
                    <ModePicker onPick={(profile, project) => pickMode(t.id, profile, project)}
                      onCancel={() => cancelPick(t.id)} />
                  )}
                </main>
              ))}
              {timeline && trace.calls.length > 0 && (
                <Timeline trace={trace} failedOnly={timeline === "failed"}
                  onShowAll={() => setTimeline("all")} />
              )}
            </div>
            </BarHostContext.Provider>
            <div className="prompt-bar" ref={setComposerEl} />
            <div className="doc-stage" ref={setDocEl} />
            {/* A left-behind branch's review: no tab behind it, so no comments; merges via the panel's endpoint. */}
            {orphanReview && docEl && createPortal(
              <DiffPane key={`${orphanReview.project}:${orphanReview.branch}`} scope="branch"
                project={orphanReview.project} busy={false} connected
                worktree={{ path: orphanReview.path, branch: orphanReview.branch, base: orphanReview.base }}
                onMerge={({ push }) => postJSON("/api/console/orphans/merge",
                  { project: orphanReview.project, branch: orphanReview.branch, push })
                  .then((r) => { reloadOrphans(); return r; })
                  .catch((e) => ({ ok: false, text: e.message }))}
                onClose={() => setOrphanReview(null)} />, docEl)}
          </section>
        </div>

        {/* An open card stacks over the list rather than replacing it. */}
        {/* On the rooms desk the column opens only as a sheet; a click off it puts it away, like Esc. */}
        {layout === "hybrid" && wide && !full && <div className="hy-scrim" onClick={closeSide} aria-hidden="true" />}
        <aside className="plan-side">
          {side.view === "card" ? (
            <div className="side-panel side-card">
              <CardDetail id={side.id} card={cards[side.id]} level={sal[side.id]?.level}
                onClose={closeSide} onFull={openFull}
                onPin={pinContext} openSheet={openFull} />
            </div>
          ) : side.view === "orphans" ? (
            <OrphansPanel orphans={orphans} onClose={closeSide} onChanged={reloadOrphans}
              onReview={(o) => setOrphanReview(o)} />
          ) : side.view === "sources" ? (
            <div className="side-panel side-card">
              <div className="side-head">
                <h2 className="side-title">Sources</h2>
                <button className="pbtn" onClick={() => openFull("status")}>⤢ Full view</button>
                <button className="side-x" aria-label="Close Sources" title="Close · Esc"
                  onClick={closeSide}><Icon name="close" size={12} /></button>
              </div>
              <div className="side-body detail">
                {/* The status sheet's own body, not a second copy of it. */}
                <SheetBody id="status" onPin={pinContext} onOpen={openFull} />
              </div>
            </div>
          ) : null}
          <div className="side-panel side-list">
            <NeedsList asks={active} snoozed={snoozed} sal={sal} loading={loading}
              cleared={cleared} nextClock={nextClock} waiting={!!waiting}
              keysOn={layout !== "hybrid" && !paletteOpen && !full && sheet !== "full"}
              snoozeOptions={snoozeOptions} onSnooze={snoozeAsk} onWake={wake}
              onAct={act} onOpen={openCard} onOpenNote={(p) => openFull(`note:${p}`)}
              hot={hot} onHot={setHot} current={side.view === "card" ? side.id : null}
              onHide={hideSide} onDock={sideHidden ? dockSide : null} />
          </div>
        </aside>

      </div>

      <AgentLayer trace={trace} tabs={ctabs.tabs} />
      <UndoToast />
      <HabitNudge />
      <LookChip onOpen={() => setLabOpen(true)} />
      {labOpen && <LookLab onClose={() => setLabOpen(false)}
        view={stack.length ? String(stack[stack.length - 1]) : `desk · console ${sheet}`} />}
      {paletteOpen && (
        <CommandPalette commands={commands} searchNotes={searchNotes}
          onClose={() => setPaletteOpen(false)} />
      )}
    </div>
  );
}
