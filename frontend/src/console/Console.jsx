import { Fragment, memo, useCallback, useContext, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import BarMenu from "./BarMenu.jsx";
import { createPortal } from "react-dom";
import Icon from "../ui/icons.jsx";
import { fetchJSON, postJSON } from "../lib/api.js";
import { sessionPin } from "../lib/pills.js";
import { createVoice } from "../lib/voice.js";
import { PIN_MIME, pinDropEffect, readPin } from "../lib/drag.js";
import { emitTurn, runningAgents, setConsoleActions } from "../lib/turn.js";
import { briefVerdict, buildReport, lastTurn } from "../lib/handoff.js";
import { refreshSoon } from "../lib/refresh";
import { useDebounce, useTimeout } from "../lib/timers.js";
import { AskCard, PermCard } from "./Prompt.jsx";
import UsageChips from "./UsageChips.jsx";
import CliStatus, { ContextChip } from "./CliStatus.jsx";
import { McpPanel, RewindButton, TaskPanel } from "./Parity.jsx";
import { RecentSessions, SessionsPanel } from "./SessionsPanel.jsx";
import { DiffCard, DiffPane } from "./DiffView.jsx";
import { chip, loadComments, saveComments, turnDiffs, withComments } from "../lib/review.js";
import ShellBlock from "./ShellBlock.jsx";
import { ShellCode } from "../lib/shell.jsx";
import ScriptCall from "./ScriptCall.jsx";
import AgentStrip from "./AgentStrip.jsx";
import AgentCall from "./AgentCall.jsx";
import DelegateCall, { DELEGATE_TOOL } from "./DelegateCall.jsx";
import RetryMenu, { ModelMark } from "./RetryMenu.jsx";
import TaskNote from "./TaskNote.jsx";
import ProjectBar from "./ProjectBar.jsx";
import { createdRef, usePrWatch } from "../lib/prWatch.js";
import LimitBanner from "./LimitBanner.jsx";
import { cutWhat, resumeText } from "../lib/interrupted.js";
import { ModelChip } from "./ModelPicker.jsx";
import { writeAuto } from "../lib/autoResume.js";
import { cachedDefault, cachedModels, effortKey, effortsFor, findModel, modeKey, modelKey,
         nerdBits, nerdStats, readEnterMode, rememberDefault, rememberModels,
         writeEnterMode } from "../lib/consolePrefs.js";
import { HostItems, toolLabel } from "../plan/StatusBar.jsx";
import { BarHostContext, folded } from "../lib/barHost.js";
import { renderMarkdownCached, renderUserMath, userMathSpans } from "../ui/markdown.jsx";
import MathInput from "../ui/MathInput.jsx";
import { TURN_END_EVENT } from "./UsageChips.jsx";
import ErrorBoundary from "../ui/ErrorBoundary.jsx";
import DocPane from "./DocPane.jsx";
import FilePane from "./FilePane.jsx";
import { DocPaneContext, docRefsIn } from "../lib/docPane.js";
import { FileContext } from "../lib/files.js";

// What a woken parent's model is sent (wake): the reports ride above it as
// <handoff_report>s, and HANDOFF_APPEND says what to do with them.
const WAKE_TEXT = "(Nobody typed this: a tab you handed off to reported back, above. "
  + "Carry on from it: message_tab it if something's missing, else tell the user what came of it.)";
// …and when the PR the tab watches moved (lib/prWatch.js), riding as <pr_watch>.
const PR_WAKE_TEXT = "(Nobody typed this: the PR this tab watches changed, above. "
  + "If it's yours to act on (a failing check, review comments, a conflict), do it and push; "
  + "else tell the user in a line.)";
const MAX_WAKES = 6;

const clipText = (s, n) => { const t = String(s || "").replace(/\s+/g, " ").trim(); return t.length > n ? `${t.slice(0, n - 1)}…` : t; };
const agoShort = (sec) => {
  const m = Math.max(0, Math.round((Date.now() / 1000 - sec) / 60));
  return m < 60 ? `${m}m` : m < 48 * 60 ? `${Math.round(m / 60)}h` : `${Math.round(m / 1440)}d`;
};
// The window's other tabs that have a conversation to point at, for `@`
// (#94). Read off the stored tab list rather than passed down, so the
// memoised panes don't re-render on every tab change for a popup.
function otherTabs(self) {
  try {
    const { tabs = [] } = JSON.parse(localStorage.getItem("console_tabs") || "{}");
    return tabs.filter((t) => t.id !== self && t.profile).flatMap((t) => {
      const sid = localStorage.getItem(`console_session:${t.id}`);
      if (!sid) return [];
      const tag = t.profile === "dev" ? (t.project || "").split("/").pop() : t.profile;
      return [{ kind: "tab", key: `t:${t.id}`, sid, project: t.project || null, tag,
                label: t.name || t.title || t.brief || "new" }];
    });
  } catch { return []; }
}
const WS_URL = `${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws/console`;
// Ordered by how much they ask, loosest of the ordinary three first. "never
// ask" sits at the bottom rather than next to "auto", which it resembles in
// name and not in kind: it is the only one where a send or a delete can
// happen without you.
//
// `acceptEdits` used to head this list as "auto-edit" and was removed on
// 2026-09-17. It read as the cautious middle and wasn't: the CLI settles
// in-workspace Bash writes under it without consulting the backend, so `rm`
// inside the vault ran cardless while adding a task stopped for a click.
// `auto` is that trade made deliberately and the right way round. Sessions
// that stored it are migrated by readMode().
const HISTORY_MAX = 200;      // prompts kept per folder for Up and Ctrl+R
const MODES = [
  { value: "auto", label: "auto" },
  { value: "default", label: "ask" },
  { value: "plan", label: "plan" },
  { value: "bypass", label: "never ask" },
];
// What the picker calls it vs. what the wire calls it. The select's own value
// stays short so the toolbar reads cleanly.
const BYPASS = "bypassPermissions";
const wireMode = (m) => (m === "bypass" ? BYPASS : m);
// After an unexpected close: a backend restart is up again in a second or
// two, and six tries cover a slow one (~37s) without retrying forever.
const RETRY_DELAYS_MS = [1000, 2000, 4000, 8000, 10000, 12000];

// The stored mode, current-vocabulary. An unknown value (a retired mode, a
// half-written key) lands on the default rather than on a blank select.
//
// Per tab mode (general / study / dev): each keeps its own, and starts where
// the backend's console_profiles.DEFAULT_MODE does: general and study auto,
// dev never ask, including a tab another tab opened (this page sends no mode
// for it until you pick one). General inherits the key from before modes
// existed.
const MODE_DEFAULT = { general: "auto", study: "auto", dev: "bypass" };
// Effort is kept per mode like the model (lib/consolePrefs.js); "" is the
// CLI's own default.
const styleKey = (profile) => `console_output_style:${profile}`;

// Dev's default became never ask on 2026-11-03. A pick is remembered per
// profile, so an "auto" stored under the old default would keep every new dev
// tab there regardless — cleared once, so the new default reaches them.
const DEV_BYPASS_MIGRATED = "console_mode:dev:bypass-default";

function readMode(profile = "general") {
  if (profile === "dev" && !localStorage.getItem(DEV_BYPASS_MIGRATED)) {
    if (localStorage.getItem(modeKey("dev")) === "auto") localStorage.removeItem(modeKey("dev"));
    localStorage.setItem(DEV_BYPASS_MIGRATED, "1");
  }
  const stored = localStorage.getItem(modeKey(profile));
  if (stored === "acceptEdits") return "auto";   // retired; auto is the nearer of the two
  return MODES.some((m) => m.value === stored) ? stored : MODE_DEFAULT[profile] || "auto";
}

const BYPASS_WARNING =
  "\"never ask\" turns off approval cards completely.\n\n"
  + "Claude can send mail, delete calendar events, run rm and drop database "
  + "rows without stopping to ask — including on instructions it reads in an "
  + "email, a message, or an attachment it was summarizing.\n\n"
  + "You still get the list of what changed, but only once the turn is over.\n\n"
  + "Turn it on?";
// What each change row's verb is: destructive ones are the reason the summary
// exists, so they are the ones that get a colour.
const CHANGE_TONE = {
  deleted: "gone", sent: "gone", rewrote: "over", ran: "ran",
};
// 34_100 -> "34.1k". The turn footer gets one line to say what the turn cost,
// so the counts are readable at a glance or not read at all.
const compact = (n) => (n < 1000 ? `${n}`
  : n < 100_000 ? `${(n / 1000).toFixed(1)}k` : `${Math.round(n / 1000)}k`);

// A Desktop file's age, which is how you find the screenshot you just took —
// every one of them is named "Screenshot <date> at <time>".
function deskAge(mtime) {
  const s = Date.now() / 1000 - mtime;
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return new Date(mtime * 1000).toLocaleDateString([], { month: "short", day: "numeric" });
}

// The turn's "still going" line: what the model is doing (the socket's
// `phase`) and for how long, which is the terminal spinner's job. A bare
// "working…" for ten seconds reads as stuck; "thinking · 9s" doesn't.
function WorkingLine({ since, phase }) {
  const [, tick] = useState(0);
  useEffect(() => {
    const id = setInterval(() => tick((n) => n + 1), 1000);
    return () => clearInterval(id);
  }, []);
  const secs = since ? Math.floor((Date.now() - since) / 1000) : 0;
  const what = phase?.phase === "thinking" ? "thinking"
    // mcp__vault__tasks_list → tasks_list; the chip that replaces this line
    // has the full name and its input
    : phase?.phase === "tool" ? `calling ${phase.name ? phase.name.split("__").pop() : "a tool"}`
    : "working";
  return <p className="status pulse">{what}…{secs >= 1 ? ` · ${secs}s` : ""}</p>;
}

// Consecutive tool calls are one activity, not N rows: a turn that reads six
// files should cost six words. Collapsed, the run says how many calls and
// which tools; while it is still running it says the call in flight instead,
// because that line is the only sign anything is happening. Expanded, it is
// the old per-call list, with the full input on hover.
const Entry = ({ draw }) => draw();

// How many user turns the log draws at once, and how many more each "show
// earlier" adds.
const TURN_WINDOW = 30;

// A Bash call's whole command, from its raw input when that parses (it's
// clipped at 2000 chars, and a clipped one doesn't) — else the one-line detail.
function bashCommand(t) {
  try {
    const c = JSON.parse(t.raw)?.command;
    if (typeof c === "string" && c) return c;
  } catch { /* clipped or absent */ }
  return t.detail || "";
}

// The tool that starts a subagent ("Task" before the CLI renamed it), and the
// strip's statuses for one that has stopped (console_agents.TERMINAL).
const AGENT_TOOLS = new Set(["Agent", "Task"]);
const AGENT_DONE = new Set(["completed", "failed", "stopped", "killed"]);

function ToolRun({ tools, open, live, onToggle }) {
  const tally = [];
  for (const t of tools) {
    const name = toolLabel(t.name);
    const seen = tally.find((x) => x.name === name);
    if (seen) seen.n += 1;
    else tally.push({ name, n: 1 });
  }
  const current = tools[tools.length - 1];
  // One call has nothing to summarise — the tally would drop the only fact on
  // the line (which file), so a lone call keeps reading the way it always did.
  const solo = tools.length === 1;
  return (
    <div className={`activity${open ? " open" : ""}${live ? " live" : ""}`}>
      <button
        className="activity-head" onClick={onToggle}
        title={open ? "Hide the calls"
                    : solo ? (current.raw || current.detail)
                    : tally.map((t) => `${t.name}${t.n > 1 ? ` ×${t.n}` : ""}`).join(", ")}
      >
        <Icon name={open ? "chevronDown" : "chevronRight"} size={10} />
        {/* the count is also what pulses while a call is in flight, so a lone
            call keeps it (as a "1") rather than losing the only sign of life */}
        {(!solo || live) && (
          <span className={`activity-count${live ? " pulse" : ""}`}>{tools.length}</span>
        )}
        {solo || live ? (
          <>
            <span className="tool-name">{toolLabel(current.name)}</span>
            {/* what it's for, then (dimmer) the command that does it */}
            {current.desc && <span className="tool-desc">{current.desc}</span>}
            <span className="tool-detail">{current.detail}</span>
          </>
        ) : (
          <span className="activity-tally">
            {tally.map((t) => (
              <span key={t.name} className="tally-item">
                {t.name}{t.n > 1 && <span className="tally-n">×{t.n}</span>}
              </span>
            ))}
          </span>
        )}
      </button>
      {open && (
        <div className="activity-calls">
          {tools.map((t, i) => {
            if (/__run_script$/.test(t.name)) return <ScriptCall key={i} t={t} />;
            const cmd = /Bash$/.test(t.name) ? bashCommand(t) : null;
            return (
              <div key={i} className={`entry tool${cmd ? " has-cmd" : ""}`} title={t.raw || t.detail}>
                <span className="tool-name">{toolLabel(t.name)}</span>{" "}
                {t.desc && <span className="tool-desc">{t.desc} </span>}
                {cmd
                  ? <pre className="tool-cmd"><ShellCode code={cmd} /></pre>
                  : <span className="tool-detail">{t.detail}</span>}
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}

// `composerTarget`: an element to render the prompt into instead of the pane's
// foot; `barTarget` likewise for the bar (the plan view's status bar), with
// `onRaise` lifting a folded sheet when ⋯ opens a panel. `usageInBar`: false when something else shows usage (the plan view's
// status bar carries one rotating chip).
//
// One Console is one tab (`tabId`). Every tab stays mounted — its socket, its
// turn and its log live on while another is showing — so `active` decides
// only what a single window can hold one of: the composer, the speakers, the
// push-to-talk key, ⌘K's "focus the console". `onTitle` reports the tab's
// first message, which is what its tab is called.
//
// `profile` / `project`: the tab's mode and a dev tab's repo (lib/tabs.js).
// `spawned`: another tab opened this one; `brief` is what it was handed,
// sent as the first message once (then `onBriefSent`). `onOpenTab` is how
// this tab's model opens another (console_tools.open_tab): it resolves to
// { ok, reason } and the answer goes back down the socket. `parent` is the
// tab a spawned one was opened from: when the brief's turn ends, `onReport`
// carries its outcome there. `onShowTab` goes to a tab (a report's source).
// `onTyped(tabId, "engaged")`: the user sent something here themselves.
// `onTabs({ close, from })` answers console_tools.tabs: { ok, text, closed }.
// `tabModel` / `tabEffort`: this tab's own model and effort (open_tab gave
// it them), used instead of the profile's remembered pick; a change made in
// such a tab stays its own, through `onTabSet(tabId, key, value)`.
export default function Console({ tabId = "main", active = true, onTitle,
                                  profile = "general", project = null, spawned = false,
                                  tabModel = null, tabEffort = null, onTabSet,
                                  parent = null, held = null, reported = false, brief = null, briefPills = null,
                                  onBriefSent, onOpenTab, onBackgroundTab,
                                  interrupted = null, onInterrupted,
                                  onReport, onShowTab, onTyped, onTabs, onWorktree, onMessageTab,
                                  pills, onRemovePill, onClearPills, onAddPill, onUpdatePill,
                                  onPinContext, focusComposer, composerTarget, docTarget,
                                  barTarget = null, onRaise,
                                  usageInBar = true, modal = active }) {
  const sessionKey = `console_session:${tabId}`;
  // A conversation rewind waiting on its first turn: the record to resume the
  // stored session at (as a fork). Kept across a reload — until a turn lands
  // the fork has no transcript, and dropping this would resume the whole
  // conversation you just cut.
  const rewindAtKey = `${sessionKey}:at`;
  // A spawned tab keeps its own mode, and has none until you pick one — the
  // backend starts it in the profile's default until then.
  const tabModeKey = `console_mode_tab:${tabId}`;
  const storedMode = () => {
    if (!spawned) return readMode(profile);
    const m = localStorage.getItem(tabModeKey);
    return MODES.some((x) => x.value === m) ? m : null;
  };
  const projectQ = project ? `?project=${encodeURIComponent(project)}` : "";
  const projectName = project ? project.split("/").pop() : null;
  const emit = (ev) => emitTurn({ ...ev, tab: tabId });
  const activeRef = useRef(active);
  activeRef.current = active;
  // The session id as announced — remembered only once a turn lands in it.
  const announcedRef = useRef(null);
  // The socket's handlers close over the first render; this reaches the current one.
  const onOpenTabRef = useRef(onOpenTab);
  onOpenTabRef.current = onOpenTab;
  const onTabsRef = useRef(onTabs);
  onTabsRef.current = onTabs;
  const onMessageTabRef = useRef(onMessageTab);
  onMessageTabRef.current = onMessageTab;
  const onTabSetRef = useRef(onTabSet);
  onTabSetRef.current = onTabSet;
  const mergeCallRef = useRef(null);   // the worktree_merge call in flight, by id
  const [entries, setEntries] = useState([]);
  // The document beside the chat (DocPane.jsx): what it shows, or null.
  // `auto` is whether a new reply may open it on its own — off once the
  // user closes it, back on the next time they open it themselves.
  const [docView, setDocView] = useState(null);
  const [docCover, setDocCover] = useState(false);
  const docAuto = useRef({ on: true, key: null, seq: 0 });
  // Every document this chat has pointed at, for the pane's picker: asked
  // for whenever the pane takes a document, so only while it's open.
  const [recentDocs, setRecentDocs] = useState([]);
  const loadRecentDocs = useCallback(() => fetchJSON("/api/cite/docs/recent")
    .then((r) => setRecentDocs(r.docs || [])).catch(() => {}), []);
  const docPane = useMemo(() => ({
    open({ ref, page, quote, onVerdict }) {
      docAuto.current.on = true;
      setDocView({ ref: String(ref).toLowerCase(), page: page || 1, quote,
                   onVerdict, seq: ++docAuto.current.seq });
      loadRecentDocs();
    },
    // A file a reply named (FileLink.jsx), in the same slot: a PDF in
    // DocPane, anything else viewable in FilePane. One at a time, no split.
    file(hit) {
      setDocView2(null);
      setDocView({ ref: `file:${hit.path}`, file: hit, page: 1, seq: ++docAuto.current.seq });
      loadRecentDocs();
    },
  }), [loadRecentDocs]);
  // A second document under the first (homework under the handout): the
  // pane splits; each half picks and closes on its own.
  const [docView2, setDocView2] = useState(null);
  function closeDoc() {
    // A file isn't a document the chat cited: closing it leaves auto-open be.
    if (docView?.file) { setDocView(null); return; }
    // The lower half moves up rather than the pane closing under it.
    if (docView2) { setDocView(docView2); setDocView2(null); loadRecentDocs(); return; }
    docAuto.current.on = false;
    setDocView(null);
  }
  // The bar's "docs": back to what the chat last pointed at, else the newest kept.
  function toggleDocs() {
    if (docView) { setDocView2(null); docAuto.current.on = false; setDocView(null); return; }
    const inChat = entries.findLast((e) => e.kind === "text" && docRefsIn(e.text).length);
    const ref = inChat ? docRefsIn(inChat.text)[0].ref : null;
    if (ref) { docPane.open({ ref, page: 1 }); return; }
    fetchJSON("/api/cite/docs/recent").then((r) => {
      setRecentDocs(r.docs || []);
      if (r.docs?.length) docPane.open({ ref: r.docs[0].ref, page: 1 });
    }).catch(() => {});
  }
  // A long study chat starts over without losing the thread: the model
  // writes the carry-over and hands it to a fresh study tab (open_tab).
  const FRESH_TEXT = "This chat is getting long. Start a fresh study tab for me with open_tab "
    + "(mode study). Its brief must stand alone: the course and what I'm studying for, "
    + "what we've covered so far, my weak spots and quiz scores, the documents we've used "
    + "(their paths, so it can open them with doc_pages), exactly where we stopped, and "
    + "what to do next. Then tell me in one line that it's open.";
  const [input, setInput] = useState("");
  // Typed mid-turn: { id, text, pills }, already with the CLI, which reads
  // each at the next tool boundary (queued_in) like Claude Code's queue.
  const [queued, setQueued] = useState([]);
  const queuedRef = useRef(queued);                   // for the socket handlers
  queuedRef.current = queued;
  // Held for their own turn when this one ends: { id, text, pills }, the
  // server's list (app/console_queue.py), so it outlives a reload. Edited,
  // reordered, steered in or dropped by asking the server (#92).
  const [holding, setHolding] = useState([]);
  const holdingRef = useRef(holding);
  holdingRef.current = holding;
  const [holdEdit, setHoldEdit] = useState(null);      // { id, text } while one is being edited
  const [enterMode, setEnterModeState] = useState(readEnterMode);
  const unqueuedRef = useRef(() => {});
  const [histAt, setHistAt] = useState(null);         // where Up has got to, or null
  const [histSearch, setHistSearch] = useState(false);  // Ctrl+R
  const [suggestion, setSuggestion] = useState("");   // the CLI's guess at what's next
  const historyKey = `console_history:${project || profile}`;
  const historyRef = useRef(null);
  if (historyRef.current === null) {
    try { historyRef.current = JSON.parse(localStorage.getItem(historyKey) || "[]"); }
    catch { historyRef.current = []; }
  }
  const stashRef = useRef("");                        // Ctrl+S
  const [mathOpen, setMathOpen] = useState(false);    // Ctrl+M
  const mathRef = useRef("");                         // the formula being typed
  // The $…$ span in the prompt the field is editing ({start, end, tex}), or
  // null when it's writing a new one at the caret.
  const [mathEdit, setMathEdit] = useState(null);
  // What the field has written, so a plain one like $y$ still draws and
  // stays clickable (the transcript's own test for "looks like math" skips it).
  const mathMadeRef = useRef(new Set());
  const lastEscRef = useRef(0);
  // The next "Resumed session" line is a reconnect nobody asked for (the
  // backend restarted after a dev turn's edit), so it isn't news.
  const quietResumeRef = useRef(false);
  const [busy, setBusy] = useState(false);
  const busyRef = useRef(false);        // for the socket handlers, pinned to their epoch
  busyRef.current = busy;
  // What the model is doing before there's text or a chip to show for it
  // (the socket's `phase`), and when the turn began — the working line's
  // label and clock.
  const [phase, setPhase] = useState(null);
  const turnStartRef = useRef(null);
  if (busy && turnStartRef.current == null) turnStartRef.current = Date.now();
  if (!busy) turnStartRef.current = null;
  // Reply text arrives a few characters per message, and each one used to
  // re-render the whole log. It collects here and lands once a frame —
  // or every 100ms when the window is hidden and frames stop.
  const textBufRef = useRef("");
  const textRafRef = useRef(null);
  const textFallback = useTimeout();
  const retryRef = useRef(0);           // reconnects tried since a session last came up
  // Whether the next socket may pick up this tab's session where it is on the
  // backend (console_link) — true for a reload or a dropped socket, false for
  // a reconnect you asked for (New chat, another session, a model change).
  const attachRef = useRef(true);
  // Messages held while an attach rebuilds the log, so nothing live lands
  // ahead of the replay; and which transcript load is the current one.
  const holdRef = useRef(null);
  const transcriptGenRef = useRef(0);
  const retryTimer = useTimeout();     // the next reconnect, RETRY_DELAYS_MS apart
  const stableTimer = useTimeout();    // a socket up long enough to count as recovered
  const [connected, setConnected] = useState(false);
  const [sessionId, setSessionId] = useState(() => localStorage.getItem(sessionKey) || null);
  // A dev tab's own git worktree ({ path, branch, base }), from the socket.
  const [worktree, setWorktree] = useState(null);
  // What a path in a reply is relative to (the worktree the agent works in,
  // then its repo; the vault is the backend's own fallback), and where a
  // clicked file opens: this tab's document slot.
  const fileCtx = useMemo(() => ({
    bases: [worktree?.path, project].filter(Boolean),
    view: (hit) => docPane.file(hit),
  }), [worktree?.path, project, docPane]);
  const [merging, setMerging] = useState(false);
  // The model and effort this tab starts with: its own when open_tab gave it
  // them (a queued child keeps them on its tab until it starts), else the
  // profile's remembered pick, read again at each connect so Settings
  // reaches a tab when it reconnects. An effort the tab's own model doesn't
  // take is dropped rather than sent.
  const ownPick = tabModel != null || tabEffort != null;
  const startPick = () => {
    const m = tabModel || localStorage.getItem(modelKey(profile)) || "default";
    let e = tabEffort ?? (localStorage.getItem(effortKey(profile)) || "");
    if (tabModel && tabEffort == null && e) {
      const ok = effortsFor(cachedModels(profile), m, cachedDefault(profile));
      if (ok.length && !ok.includes(e)) e = "";
    }
    return { model: m, effort: e };
  };
  const [model, setModel] = useState(() => startPick().model);
  // What "default" resolves to for this mode — the backend says, at start.
  const [defaultModel, setDefaultModel] = useState(() => cachedDefault(profile));
  const [mode, setMode] = useState(() => storedMode() || MODE_DEFAULT[profile]);
  const [barMore, setBarMore] = useState(false);   // ⋯: the bar's menu (console/BarMenu.jsx)
  const [moreEl, setMoreEl] = useState(null);
  const closeMore = useCallback(() => setBarMore(false), []);
  // The diff pane: { files, path, scope, seq } — one turn's files and the
  // file showing, or the bar's view — or null. `reload` is the open pane's,
  // for the end of a turn.
  const [diffOpen, setDiffOpen] = useState(null);
  const diffSeqRef = useRef(0);
  const diffReloadRef = useRef(null);
  // The diff pane's line comments: chips in the composer that go with the
  // next message sent (lib/review.js withComments). Per tab folder in
  // localStorage, so a reload keeps them; the worktree arrives after mount,
  // which moves the folder, so they're read per folder rather than once.
  const [commentStore, setCommentStore] = useState({});
  const commentFolder = worktree?.path || project;
  const comments = useMemo(() => (!commentFolder ? []
    : commentStore[commentFolder] ?? loadComments(commentFolder)), [commentFolder, commentStore]);
  // Stored in the handler, not a state updater: a send that unmounts the
  // pane would otherwise leave a sent comment to come back next time.
  const setComments = (next) => {
    if (!commentFolder) return;
    saveComments(commentFolder, next);
    setCommentStore((s) => ({ ...s, [commentFolder]: next }));
  };
  const [rateLimits, setRateLimits] = useState({}); // rate_limit_type -> latest RateLimitInfo
  const [cliVersion, setCliVersion] = useState(null); // the CLI this session runs
  const [ctx, setCtx] = useState(null);               // the context window, as /context sees it
  const [tasks, setTasks] = useState([]);             // the CLI's task list (TaskCreate/Update)
  // Subagents at work (AgentStrip); finished ones hide once you send again.
  const [agents, setAgents] = useState([]);
  // A turn the usage limit cut off: { at } until it's resumed (LimitBanner).
  const limitedKey = `console_limited:${tabId}`;
  const [limited, setLimited] = useState(() => {
    try { return JSON.parse(localStorage.getItem(limitedKey)); } catch { return null; }
  });
  const markLimited = (v) => {
    setLimited(v);
    if (v) localStorage.setItem(limitedKey, JSON.stringify(v)); else localStorage.removeItem(limitedKey);
    emit({ type: "limited", on: !!v });
  };
  // On mount, before the plan view has subscribed (a child's effects run
  // first): the bus holds it and replays it to useTurnTrace (lib/turn.js).
  useEffect(() => {
    // eslint-disable-next-line react-you-might-not-need-an-effect/no-event-handler -- no event: this tells the bus (outside React) once, on mount
    if (limited) emit({ type: "limited", on: true });
  }, []);  // eslint-disable-line react-hooks/exhaustive-deps -- once on mount, replaying the stored flag to the bus (above)
  const [agentsSeen, setAgentsSeen] = useState(() => new Set());
  const agentsRef = useRef(agents);
  agentsRef.current = agents;
  const settleAgents = () => setAgentsSeen(new Set(
    agentsRef.current.filter((a) => ["completed", "failed", "stopped", "killed"].includes(a.status)).map((a) => a.id)));
  // aliases → resolved model, name and effort levels; the last session's
  // list until this one's arrives (lib/consolePrefs.js)
  const [models, setModels] = useState(() => cachedModels(profile));
  // The model id the session is actually running, as the stream names it
  // (the backend's `model` frame); null until it does, after a reconnect too.
  const [liveModel, setLiveModel] = useState(null);
  // The same id for the socket's handlers: each reply in the log is tagged
  // with the model that wrote it, which is what the "model changed" mark
  // and a reply's retry menu read (a resumed log carries the same tag,
  // sessions.session_transcript).
  const liveModelRef = useRef(null);
  // "Retry with…": the message to send again once the fork's session is up.
  const retryTextRef = useRef(null);
  const sendTextRef = useRef(null);
  const [effort, setEffort] = useState(() => startPick().effort);
  const [mcp, setMcp] = useState(null);               // the /mcp panel: null = closed
  const [styles, setStyles] = useState([]);           // output styles the CLI offers
  const [outputStyle, setOutputStyle] = useState(() => localStorage.getItem(styleKey(profile)) || "");
  // "Code and conversation": the conversation half waits for the files'
  // `rewound` — a failed file rewind must not cut the conversation anyway.
  const thenConversationRef = useRef(null);
  const rewindConversationRef = useRef(null);
  const [showSessions, setShowSessions] = useState(false);
  const [epoch, setEpoch] = useState(0); // bump to reconnect
  const [commands, setCommands] = useState([
    // client-side intercept, not a real command — seeded so it's discoverable
    { name: "help", description: "list available commands (rendered locally, zero tokens)",
      argument_hint: "", scope: "builtin", kind: "command" },
    { name: "mcp", description: "MCP servers: status, reconnect, enable/disable (a local panel)",
      argument_hint: "", scope: "builtin", kind: "command" },
  ]);
  // Which collapsed runs are open, keyed by the flat index of their first
  // entry — entries are only ever appended, so that key stays put and a run
  // you opened does not close itself when the next tool call lands.
  const [openRuns, setOpenRuns] = useState(() => new Set());
  const [acIndex, setAcIndex] = useState(0);
  const [acDismissed, setAcDismissed] = useState(false);
  // What's hovering the pane: "file" | "context" | false. Two kinds land very
  // differently — one is read and summarised, the other is already-shaped rows
  // — so the veil names which before the drop commits to it.
  const [dragging, setDragging] = useState(false);
  // Voice. `voiceOut` is the only one that has to reach the session: it adds
  // the prompt rule about writing something speakable, which is fixed when
  // the session starts, so toggling it reconnects the way model/mode do.
  const [voiceCap, setVoiceCap] = useState(null);     // what this machine can do
  const [voiceOut, setVoiceOut] = useState(() => localStorage.getItem("console_voice") === "1");
  const [wakeOn, setWakeOn] = useState(false);
  const [micState, setMicState] = useState("off");    // off | listening | thinking
  const [voiceNote, setVoiceNote] = useState("");     // transient, next to the composer
  const wsRef = useRef(null);
  const scrollRef = useRef(null);
  const inputRef = useRef(null);
  const acListRef = useRef(null);

  // ⌘K's "focus the console" needs a way in, and the composer's ref is private
  // to this file (four call sites lean on it). Hand the desk a function rather
  // than the element, so the ref stays ours.
  useEffect(() => {
    if (!focusComposer || !active) return;
    focusComposer.current = () => inputRef.current?.focus();
    return () => { focusComposer.current = null; };
  }, [focusComposer, active]);

  // A tab you switch to is a tab you're about to type in. Not on mount: the
  // window opening shouldn't take focus from wherever it was.
  const mountedRef = useRef(false);
  useEffect(() => {
    // eslint-disable-next-line react-you-might-not-need-an-effect/no-event-handler -- focusing is DOM work on the input only this owns; the switch happens in the parent
    if (mountedRef.current && active) inputRef.current?.focus();
    mountedRef.current = true;
  }, [active]);

  // What something outside the pane may do to the turn: answer a waiting
  // permission card (the plan view's status bar and its list row). Re-set
  // every render so the answer runs with this render's state; the approval
  // card itself still shows the answer, the same way a click on it would.
  useEffect(() => {
    setConsoleActions(tabId, {
      answer: (requestId, allow) => answerPermission(requestId, { allow }),
      stopShell: (id) => wsRef.current?.send(JSON.stringify({ type: "shell_stop", id })),
      // A drafted prompt (a debt nudge): into the composer, focused, unsent.
      draft: (text) => { setInput(text); inputRef.current?.focus(); },
      pin: (label, data) => onPinContext?.(label, data),
      resume: () => resume(),
      resumeWith: (text) => resumeWith(text),
      // A tab this one handed off to, done with its brief: shown now, and
      // held for the next send so the model here hears it too.
      report: (r) => {
        reportsRef.current.push(r);
        setEntries((prev) => [...prev, { kind: "report", ...r }]);
        // Taken by the held-command effect below, once this has rendered and
        // nothing is running; mid-turn, that's after the turn's end.
        setWakeDue(true);
      },
    });
    return () => setConsoleActions(tabId, null);
  });
  const reportsRef = useRef([]);
  // A report wants a turn of its own (wake, below).
  const [wakeDue, setWakeDue] = useState(false);
  // The PR this dev tab watches (lib/prWatch.js). Its notes are held like the
  // reports, and wake the tab the same way.
  const prNotesRef = useRef([]);
  const prw = usePrWatch({
    tabId, project: profile === "dev" ? project : null, worktree: worktree?.path || null,
    onNotes: (notes, pr) => {
      prNotesRef.current.push(...notes);
      setEntries((prev) => [...prev, { kind: "prwatch", number: pr.number, url: pr.url, notes }]);
      setWakeDue(true);
    },
  });
  // The socket handler is bound once; it reaches this render's watch through here.
  const prwRef = useRef(prw);
  prwRef.current = prw;
  // A `gh pr create` this turn: the ref it's found by once the turn ends
  // ("" for the tree's branch). null: nothing to link.
  const prLinkDueRef = useRef(null);

  // A handed-off tab starts on its brief, once: `onBriefSent` clears it from
  // the tab, so a reload resumes the session instead of asking again. A
  // parent's follow-up (message_tab) arrives the same way, as a new brief:
  // `briefRef` holds the last one sent, so a different one goes too, and
  // its turn reports back like the first. A busy tab takes it when its
  // turn ends.
  const briefRef = useRef(false);
  // A resume after a quit (lib/interrupted.js), waiting for the socket and
  // the turn like a brief does; the state is only there to wake the effect.
  const resumeRef = useRef(null);
  const [resumeAsk, setResumeAsk] = useState(0);
  // "sent" → "running" → reported, for a brief's turn only: turns the user
  // starts are their own, and nobody upstream is waiting on them.
  const reportRef = useRef(null);
  const briefAtRef = useRef(0);   // where the brief's work starts in the log
  /* eslint-disable react-you-might-not-need-an-effect/no-event-handler, react-you-might-not-need-an-effect/no-derived-state, react-you-might-not-need-an-effect/no-pass-live-state-to-parent -- the trigger is the socket: connected/busy flip in its handlers, and this needs the render after, whose state they can't see */
  useEffect(() => {
    if (!connected || busy) return;
    if (resumeRef.current) {
      if (sendText(resumeRef.current, false, false)) resumeRef.current = null;
      return;
    }
    if (!brief || briefRef.current === brief) return;
    briefAtRef.current = entries.length;
    // A ⌘Enter brief is the user's own message (typed, with its pills); a
    // handed-off one isn't.
    if (sendText(brief, briefPills?.length ? briefPills : false, !spawned)) {
      briefRef.current = brief;
      if (parent) reportRef.current = "sent";
      onBriefSent?.(tabId);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps -- fires on connect, a new brief, a turn ending or a resume; sendText and the refs are read, not watched
  }, [connected, brief, busy, resumeAsk]);
  /* eslint-enable react-you-might-not-need-an-effect/no-event-handler, react-you-might-not-need-an-effect/no-derived-state, react-you-might-not-need-an-effect/no-pass-live-state-to-parent */
  // Resume what a quit cut off. A handed-off tab that hadn't reported yet
  // reports when this work ends: the brief's own turn never got to.
  function resumeWith(text) {
    if (parent && !reported && reportRef.current == null) {
      briefRef.current = true;
      reportRef.current = "sent";
      briefAtRef.current = Math.max(0, entries.findIndex((e) => e.kind === "user"));
    }
    resumeRef.current = text;
    setResumeAsk((n) => n + 1);
  }
  // A reload mid-wait (the brief's turn errored, nothing reported yet) picks
  // the wait back up, or the report would never come.
  if (parent && held && !briefRef.current && reportRef.current == null) {
    briefRef.current = true;
    reportRef.current = "sent";
  }
  /* eslint-disable react-you-might-not-need-an-effect/no-event-handler, react-you-might-not-need-an-effect/no-pass-data-to-parent -- the trigger is the socket: connected/busy flip in its handlers, and this needs the render after, whose state they can't see; onReport hands a finished turn up, it isn't fetched data */
  useEffect(() => {
    if (reportRef.current === "sent" && busy) reportRef.current = "running";
    if (reportRef.current !== "running" || busy) return;
    // Not finished yet (lib/handoff.js): stopped, errored, cut off, or with
    // a queued message or a background subagent still to come. The report
    // waits for the turn that ends the work, whichever later turn that is,
    // and the tab says it's held meanwhile, not finished.
    const verdict = briefVerdict(lastTurn(entries), {
      queued: queuedRef.current.some((q) => !q.local) || holdingRef.current.length > 0,
      agents: runningAgents({ agents }).length > 0,
    });
    if (verdict !== "report") {
      reportRef.current = "sent";
      onReport?.(parent, { from: tabId, held: verdict });
      return;
    }
    reportRef.current = null;
    onReport?.(parent, buildReport(entries, briefAtRef.current, {
      from: tabId, sessionId,
      label: profile === "dev" ? `dev · ${projectName}` : profile,
    }));
    // eslint-disable-next-line react-hooks/exhaustive-deps -- on busy's edge only: a turn ending is what sends the report
  }, [busy]);
  /* eslint-enable react-you-might-not-need-an-effect/no-event-handler, react-you-might-not-need-an-effect/no-pass-data-to-parent */

  // The tab is named for what was first asked in it — the same thing the
  // resume list goes by.
  const firstAsk = entries.find((e) => e.kind === "user")?.text || null;
  // The first ask lands by several paths (typed, a resumed transcript, the
  // socket's replay), so the title follows whichever comes first.
  // eslint-disable-next-line react-you-might-not-need-an-effect/no-pass-live-state-to-parent -- the first ask lands by several paths (above); the parent keeps the title with the tab
  useEffect(() => { onTitle?.(tabId, firstAsk); }, [firstAsk]);   // eslint-disable-line react-hooks/exhaustive-deps -- onTitle is a callback prop; the title follows firstAsk alone
  const voiceRef = useRef(null);        // the mic/speaker controller, built on first use
  const onVoiceRef = useRef(() => {});  // re-pointed every render, so its closure is current
  const voiceOutRef = useRef(voiceOut);
  const pendingSpeechRef = useRef(null); // a spoken turn waiting for the last one to stop
  voiceOutRef.current = voiceOut;

  // On load we auto-resume the stored session — show its back-scroll too.
  useEffect(() => {
    const stored = localStorage.getItem(sessionKey);
    // The window reopening on its tabs isn't a resume anyone asked for
    // either: the back-scroll says where things stand.
    if (stored) { quietResumeRef.current = true; loadTranscript(stored); }
    // eslint-disable-next-line react-hooks/exhaustive-deps -- once on mount: the stored session is resumed a single time
  }, []);

  useEffect(() => {
    const ws = new WebSocket(WS_URL);
    wsRef.current = ws;
    ws.onopen = () => {
      setConnected(true);
      // Fifteen seconds up counts as recovered: the next drop gets the full retry
      // budget. Not on open alone, or a socket that dies on arrival every
      // time would reset it each round and never give up.
      stableTimer.start(() => { retryRef.current = 0; }, 15000);
      const { model: m, effort: e } = startPick();
      setModel(m);
      setEffort(e);
      const pm = storedMode();
      // A new socket is a new CLI: whatever it had running went with the old one.
      setAgents([]);
      setLiveModel(null);
      liveModelRef.current = null;
      emit({ type: "agents", items: [] });
      ws.send(JSON.stringify({
        type: "start",
        session_id: localStorage.getItem(sessionKey) || null,
        model: m === "default" ? null : m,
        permission_mode: pm ? wireMode(pm) : null,
        voice: localStorage.getItem("console_voice") === "1",
        effort: e || null,
        output_style: localStorage.getItem(styleKey(profile)) || null,
        resume_at: localStorage.getItem(rewindAtKey) || null,
        profile, project, spawned,
        tab: tabId, attach: attachRef.current,
      }));
      attachRef.current = true;
    };
    // A close nobody asked for is the backend restarting (an edit under
    // backend/app; launch_app.py holds that until no turn is running, but a
    // terminal-run --reload doesn't) — come back on our own and resume,
    // rather than sitting on "Connecting…" until ⌘R. A session that dies on
    // arrival every time gives up after a few tries instead of looping.
    ws.onclose = (ev) => {
      stableTimer.clear();
      setConnected(false);
      // This tab was opened in another window, which has the session now.
      if (ev?.code === 4001) {
        setEntries((p) => [...p, { kind: "status", text: "This tab is open in another window now — reload here to take it back." }]);
        return;
      }
      cutTurn("Connection lost mid-turn — the reply stopped here, and anything waiting for approval was denied.");
      // The CLI went with the socket, and what it had queued unread with it.
      unqueuedRef.current(queuedRef.current.filter((q) => !q.local).map((q) => q.id));
      // Coming back resumes the same session in the same mode: nothing to say.
      quietResumeRef.current = true;
      const tries = retryRef.current++;
      if (tries >= RETRY_DELAYS_MS.length) {
        setEntries((p) => [...p, { kind: "status", text: "Can't reach the backend — reload the window to try again." }]);
        return;
      }
      retryTimer.start(() => setEpoch((n) => n + 1), RETRY_DELAYS_MS[tries]);
    };
    const handle = (msg) => {
      // Output means the turn is on disk, so a tab's first turn can be resumed
      // even if it never finishes. Waiting for its result lost a whole long
      // first turn to a restart: the tab came back as a blank session (11-06).
      if (msg.type === "text_delta" || msg.type === "phase") {
        if (announcedRef.current && !localStorage.getItem(sessionKey))
          localStorage.setItem(sessionKey, announcedRef.current);
      }
      if (msg.type === "text_delta") {
        bufferText(msg.text);
        return;
      }
      flushText();
      if (msg.type === "phase") {
        setPhase({ phase: msg.phase, name: msg.name || null });
        return;
      }
      // Stop arrived with no turn running on the backend — the turn this page
      // thinks is live died with an earlier socket. Settle it here, or the Stop
      // button would be the only control showing and it would do nothing.
      if (msg.type === "idle") {
        cutTurn(null);
        return;
      }
      // The CLI names the session at init, before anything is said, but
      // writes no transcript until a turn starts — and resuming a session with
      // no transcript fails ("No conversation found"; the CLI exits 1 and the
      // socket dies). So the id is only *remembered* once a turn has produced
      // something in it (above, and the result branch), or when it is already
      // the stored one.
      if (msg.type === "session") {
        setSessionId(msg.id);
        announcedRef.current = msg.id;
        if (localStorage.getItem(sessionKey)) localStorage.setItem(sessionKey, msg.id);
        return;
      }
      if (msg.type === "worktree") {
        const wt = { path: msg.path, branch: msg.branch, base: msg.base };
        setWorktree(wt);
        emit({ type: "worktree", worktree: wt });
        // On the tab itself too (`onWorktree(tabId, "worktree", path)`), so
        // the window knows this tree is held before the socket's back after
        // a reload (PlanApp's left-behind list).
        onWorktree?.(tabId, "worktree", wt.path);
        return;
      }
      // The mode the session is actually in — a spawned tab's default, which
      // nothing stored says.
      // What the session is running (console.note_model): the bar's chip.
      if (msg.type === "model") {
        setLiveModel(msg.model || null);
        liveModelRef.current = msg.model || null;
        // A reply that began before the stream named its model is this one's.
        if (msg.model) setEntries((prev) => {
          const last = prev[prev.length - 1];
          return last?.kind === "text" && !last.model
            ? [...prev.slice(0, -1), { ...last, model: msg.model }] : prev;
        });
        return;
      }
      if (msg.type === "mode") {
        setMode(msg.mode === BYPASS ? "bypass" : msg.mode);
        if (msg.default_model) { setDefaultModel(msg.default_model); rememberDefault(profile, msg.default_model); }
        // A retry's fork has started (this frame closes `start`): ask it the
        // message the retried reply answered.
        const again = retryTextRef.current;
        retryTextRef.current = null;
        if (again) sendTextRef.current?.(again, false, true);
        return;
      }
      // This tab's model handing work to a new tab (console_tools.open_tab).
      // The window decides — it knows how many tabs are working — and the
      // answer goes back as the tool's result.
      if (msg.type === "open_tab") {
        const answer = onOpenTabRef.current?.({ mode: msg.mode, project: msg.project,
                                                brief: msg.brief, from: tabId,
                                                model: msg.model || null, effort: msg.effort || null })
          || { ok: false, reason: "this view has no tabs" };
        ws.send(JSON.stringify({ type: "tab_opened", request_id: msg.request_id, ...answer }));
        const on = [msg.model, msg.effort && `${msg.effort} effort`].filter(Boolean).join(" · ");
        setEntries((prev) => [...prev, { kind: answer.ok ? "handoff" : "status",
          text: answer.ok
            ? (answer.queued ? "Queued" : "Handed off to")
              + ` a new ${msg.project ? `dev · ${msg.project.split("/").pop()}` : msg.mode} tab`
              + (on ? ` on ${on}` : "")
              + (answer.queued ? ` (#${answer.queued}): it starts when a working tab finishes`
                : answer.reason ? ` (${answer.reason})` : "")
            : `Couldn't open a tab: ${answer.reason}` }]);
        return;
      }
      // This tab's model listing the window's tabs, maybe closing some
      // (console_tools.tabs). A close is said in the log: tabs vanishing
      // from the strip with nothing on screen to say why would read as a bug.
      if (msg.type === "tabs") {
        const answer = onTabsRef.current?.({ close: msg.close || [], from: tabId })
          || { ok: false, text: "this view has no tabs" };
        ws.send(JSON.stringify({ type: "tabs_answer", request_id: msg.request_id,
                                 ok: answer.ok, text: answer.text }));
        if (answer.closed?.length)
          setEntries((prev) => [...prev, { kind: "status",
            text: `Closed ${answer.closed.length === 1 ? "a tab" : `${answer.closed.length} tabs`}: ${answer.closed.join(", ")} (resumable from sessions)` }]);
        return;
      }
      // This tab's model following up with a tab it opened (message_tab).
      if (msg.type === "message_tab") {
        const answer = onMessageTabRef.current?.({ id: msg.id, text: msg.text, from: tabId })
          || { ok: false, text: "this view has no tabs" };
        ws.send(JSON.stringify({ type: "message_tab_answer", request_id: msg.request_id,
                                 ok: answer.ok, text: answer.text }));
        if (answer.ok)
          setEntries((prev) => [...prev, { kind: "handoff", text: `Followed up with ${answer.label || "a tab"}: ${
            msg.text.length > 140 ? `${msg.text.slice(0, 140)}…` : msg.text}` }]);
        return;
      }
      if (msg.type === "rate_limit") {
        // Stamped on arrival: the chips weigh it against the polled reading
        // by age, and the CLI only sends one when a status changes.
        const ev = { ...msg, at: Date.now() };
        setRateLimits((prev) => ({ ...prev, [msg.rate_limit_type]: ev }));
        emit({ type: "rate", msg: ev });
        return;
      }
      // One spoken sentence. It never joins the log — the same words are
      // already arriving as text_delta; this is the audio for them.
      if (msg.type === "speech") {
        // One mouth: a tab you aren't looking at reads its reply, it doesn't say it.
        if (!activeRef.current) return;
        voice().enqueueSpeech(msg.wav);   // built here, not only by the mic: speakers alone is a setup
        return;
      }
      // A turn nobody sent: a background command finished and Claude picked
      // the conversation back up. Lock the input as a send would, and open the
      // block with what woke it, or the reply reads as an answer to nothing.
      // Your message that opened the turn an attach is catching up on (or one
      // you queued into it): the stream never echoes it, so the backend kept it.
      if (msg.type === "tasknote") {
        setEntries((prev) => [...prev, { ...msg, type: undefined, kind: "tasknote" }]);
        return;
      }
      // The held list (app/console_queue.py), whole each time; held ones
      // handed back by a Stop, to the prompt ahead of what's typed; one
      // steered in from the list, now the CLI's (in at the next step).
      if (msg.type === "held") { setHolding(msg.items || []); return; }
      if (msg.type === "held_back") {
        const back = (msg.texts || []).filter(Boolean);
        if (back.length) setInput((cur) => [...back, cur].filter(Boolean).join("\n"));
        return;
      }
      if (msg.type === "steered") {
        setQueued((q) => [...q, { id: msg.id, text: msg.text, pills: msg.pills || [] }]);
        return;
      }
      if (msg.type === "replay_user") {
        setEntries((prev) => [...prev, { kind: "user", text: msg.text, pills: msg.pills }]);
        if (!msg.queued) {
          busyRef.current = true;
          setBusy(true);
          setPhase(null);
          emit({ type: "start" });
        }
        return;
      }
      if (msg.type === "turn_start") {
        setBusy(true);
        setPhase(null);
        emit({ type: "start" });
        // A queued message's own turn opens with that message (queued_in), and
        // one a background task woke opens with that task's row (tasknote).
        if (!msg.queued && !msg.quiet)
          setEntries((prev) => [...prev, { kind: "status",
            text: msg.why || "A background task finished" }]);
        return;
      }
      // A queued message, read by the CLI just now: out of the queue and
      // into the log, between the steps it came between.
      if (msg.type === "queued_in") {
        const q = queuedRef.current.find((x) => x.id === msg.id);
        if (!q) return;
        setQueued((list) => list.filter((x) => x.id !== msg.id));
        setEntries((prev) => [...prev, { kind: "user", text: q.text, pills: q.pills }]);
        return;
      }
      if (msg.type === "unqueued") {
        unqueuedRef.current(msg.ids || []);
        return;
      }
      // A four-second provider call is a chip on screen and dead air in the
      // room, so give the wait a sound of its own.
      if (msg.type === "tool_use" && voiceOutRef.current) voice().earcon();
      // The turn as the rest of the window sees it (lib/turn.js — the map's
      // lines). Emitted here, outside the entries updater, which must stay pure.
      if (msg.type === "tool_use") {
        setPhase(null);   // its chip says it now
        emit({ type: "call", id: msg.id, name: msg.name, detail: msg.detail, raw: msg.raw,
               parent: msg.parent });
        if (msg.name === "Bash" && /\bgh\s+pr\s+create\b/.test(msg.raw || "")) prLinkDueRef.current = createdRef(msg.raw);
        if (/worktree_merge$/.test(msg.name || "")) mergeCallRef.current = msg.id;
      } else if (msg.type === "tool_result") {
        emit({ type: "done", id: msg.id, error: msg.is_error, image: msg.image });
        // The tab's work landed: once you look away it can settle (#91).
        if (msg.id === mergeCallRef.current && !msg.is_error) onTabSetRef.current?.(tabId, "merged", Date.now());
      }
      else if (msg.type === "computer_start") emit({ type: "computer", id: msg.id, show: false });
      else if (msg.type === "computer_prompt") emit({ type: "computer", id: msg.id, show: msg.show });
      else if (msg.type === "computer_end") emit({ type: "computer_end" });
      else if (msg.type === "background") emit({ type: "background", items: msg.items });
      else if (msg.type === "suggestion") setSuggestion(msg.text);
      // The `!` command's pty, which the block opens a terminal on (ShellBlock).
      else if (msg.type === "shell_tty") {
        setEntries((prev) => {
          const at = prev.findLastIndex((e) => e.kind === "shell");
          return at < 0 ? prev : [...prev.slice(0, at), { ...prev[at], ttyId: msg.id }, ...prev.slice(at + 1)];
        });
      }
      else if (msg.type === "shell_output" || msg.type === "shell_done") {
        setEntries((prev) => {
          const at = prev.findLastIndex((e) => e.kind === "shell");
          if (at < 0) return prev;
          const e = prev[at];
          const next = msg.type === "shell_output" ? { ...e, output: e.output + msg.text }
            : { ...e, code: msg.code, background: !!msg.background };
          return [...prev.slice(0, at), next, ...prev.slice(at + 1)];
        });
        // No code and not moved: it was refused before it ran, so no turn
        // follows to unlock this.
        if (msg.type === "shell_done" && msg.code == null && !msg.background) {
          setBusy(false);
          emit({ type: "end", error: true });
        }
      } else if (msg.type === "permission_request") {
        if (msg.plan) emit({ type: "plan", text: msg.plan });
        emit({ type: "waiting", name: msg.tool, requestId: msg.request_id,
                   detail: msg.preview?.[0]?.lead || msg.preview?.[0]?.title || "" });
      } else if (msg.type === "permission_answered") {
        emit({ type: "answered" });
      } else if (msg.type === "question_request") {
        emit({ type: "waiting", name: "AskUserQuestion", question: true,
                   requestId: msg.request_id, detail: msg.questions?.[0]?.question || "" });
      } else if (msg.type === "result") {
        // The usage chips re-read on this (UsageChips.jsx): a turn ending is
        // when the subscription numbers moved.
        window.dispatchEvent(new Event(TURN_END_EVENT));
        // An open diff pane too: the turn may have committed, or answered
        // the comments it was sent.
        diffReloadRef.current?.();
        if (announcedRef.current) localStorage.setItem(sessionKey, announcedRef.current);
        // A rewound conversation's fork exists once a turn lands in it; from
        // here the stored id *is* the fork, so stop asking for the cut.
        localStorage.removeItem(rewindAtKey);
        // A Stop comes back is_error too; stopping a turn isn't it failing.
        emit({ type: "end", error: msg.is_error && !msg.interrupted, duration_ms: msg.duration_ms,
                   tokens_in: msg.tokens_in, tokens_out: msg.tokens_out });
        // Cut off by the usage window mid-work: remembered past a reload, so
        // the tab (and the "needs you" list) can offer to pick it back up.
        if (msg.limited) markLimited({ at: Date.now() });
        else if (!msg.is_error) { markLimited(null); writeAuto(tabId, null); }
        // A child's report that came in mid-turn is picked up once the
        // result's setBusy(false) has rendered (wakeDue, the held-command
        // effect). A Stop means the plan changed: the reports wait for you.
        setWakeDue((reportsRef.current.length > 0 || prNotesRef.current.length > 0) && !msg.interrupted);
        // The turn opened a PR: watch it (found from the tree's branch).
        if (prLinkDueRef.current != null) {
          const ref = prLinkDueRef.current;
          prLinkDueRef.current = null;
          if (!prwRef.current.watching) prwRef.current.link(ref).then((r) => r.pr && setEntries((p) => [...p,
            { kind: "status", text: `Watching PR #${r.pr.number}: this tab wakes when its checks, reviews, comments or conflicts change.` }]));
        }
      }
      // What this session loaded, described, at connect — a dev tab's repo
      // skills and plugins included, which the vault-side scan can't see.
      // It is the authority: it replaces anything the scan or the init
      // message's bare names said, short of the local /help.
      if (msg.type === "session_info") {
        if (msg.version) setCliVersion(msg.version);
        if (msg.models?.length) { setModels(msg.models); rememberModels(profile, msg.models); }
        if (msg.output_styles) setStyles(msg.output_styles);
        // help and mcp are answered here, not by the CLI.
        const loaded = (msg.commands || []).filter((c) => c.name && c.name !== "help" && c.name !== "mcp");
        if (loaded.length) {
          setCommands((prev) => {
            const byName = new Map(prev.map((c) => [c.name, c]));
            for (const c of loaded)
              byName.set(c.name, { name: c.name, description: c.description,
                                   argument_hint: c.argument_hint,
                                   scope: c.builtin ? "builtin" : "loaded", kind: "command" });
            return [...byName.values()];
          });
        }
        return;
      }
      // A call changed what these cards show. Any tab's: they're all mounted.
      if (msg.type === "refresh") {
        refreshSoon(msg.topics || []);
        return;
      }
      if (msg.type === "context") {
        setCtx(msg);
        return;
      }
      if (msg.type === "tasks") {
        setTasks(msg.items || []);
        return;
      }
      if (msg.type === "agents") {
        setAgents(msg.items || []);
        emit({ type: "agents", items: msg.items || [] });
        return;
      }
      if (msg.type === "mcp") {
        setMcp((m) => (m ? { servers: msg.servers || [], error: msg.error, loading: false } : m));
        return;
      }
      // Your message's rewind point lands on the newest message without one.
      if (msg.type === "checkpoint") {
        setEntries((prev) => {
          const at = prev.findLastIndex((e) => e.kind === "user");
          if (at < 0 || prev[at].checkpoint) return prev;
          const next = [...prev];
          next[at] = { ...prev[at], checkpoint: msg.id };
          return next;
        });
        return;
      }
      if (msg.type === "rewound") {
        setEntries((prev) => prev.map((e) => (e.kind === "user" && e.checkpoint === msg.id
          ? { ...e, rewind: msg.ok ? "done" : { error: msg.error } } : e)));
        if (msg.ok) {   // the pane would show the tree before it
          setDiffOpen(null);
          diffReloadRef.current = null;
        }
        const then = thenConversationRef.current;
        thenConversationRef.current = null;
        if (then?.checkpoint === msg.id && msg.ok) rewindConversationRef.current?.(then);
        return;
      }
      if (msg.type === "commands") {
        setCommands((prev) => {
          const known = new Set(prev.map((c) => c.name));
          const added = (msg.commands || [])
            .filter((n) => !known.has(n))
            .map((n) => ({ name: n, description: "", argument_hint: "", scope: "session", kind: "command" }));
          return added.length ? [...prev, ...added] : prev;
        });
        return;
      }
      // Read outside the updater, which must stay pure.
      const sessionStart = msg.type === "status" && /^(New|Resumed|Rewound) session\b/.test(msg.text);
      const quiet = sessionStart && quietResumeRef.current && msg.text.startsWith("Resumed");
      if (sessionStart) quietResumeRef.current = false;
      setEntries((prev) => {
        if (msg.type === "tool_use") {
          // The question card is the display for this one — a chip reading
          // "AskUserQuestion {"questions":[…]}" directly above it is the same
          // thing twice, with the raw JSON version on top.
          if (msg.name === "AskUserQuestion") return prev;
          // A subagent's call (it rides under the Agent call that started
          // it): marked, so the run's tally reads "Agent ↳Read×12" — the
          // subagent's work, not the parent reading twelve files.
          return [...prev, { kind: "tool", name: msg.parent ? `↳${msg.name}` : msg.name,
                             detail: msg.detail, desc: msg.desc, raw: msg.raw, id: msg.id }];
        }
        // run_script's result is shown under its script (console_code); no
        // other result carries text.
        if (msg.type === "tool_result" && typeof msg.text === "string") {
          const at = prev.findLastIndex((e) => e.kind === "tool" && e.id === msg.id);
          if (at < 0) return prev;
          return [...prev.slice(0, at), { ...prev[at], result: msg.text, error: msg.is_error },
                  ...prev.slice(at + 1)];
        }
        if (msg.type === "permission_request")
          return [...prev, {
            kind: "perm", requestId: msg.request_id, tool: msg.tool,
            input: msg.input, inputs: msg.inputs || (msg.input ? [msg.input] : []),
            plan: msg.plan, alwaysLabel: msg.always_label, guard: msg.guard,
            alwaysScope: msg.always_scope || "session",
            // What the ids in `inputs` actually name, what saying yes costs,
            // and the sentence Claude wrote just before asking. The card is
            // built from these; `inputs` stays for the raw view under it.
            preview: msg.preview || null, danger: msg.danger || null,
            why: msg.why || null,
            resolved: null, always: false, reason: "",
          }];
        // Answered somewhere other than this page: the phone door
        // (backend app/phone.py). The card settles here as if clicked.
        if (msg.type === "permission_answered")
          return prev.map((e) => (e.kind === "perm" && e.requestId === msg.request_id && e.resolved === null
            ? { ...e, resolved: !!msg.allow, by: msg.by || null } : e));
        // What the turn changed. It arrives just before the result, so it
        // lands inside that turn's block and scrolls with it.
        if (msg.type === "changes")
          return [...prev, { kind: "changes", items: msg.items || [] }];
        // A dev turn's lines (console_diff), after its ledger.
        if (msg.type === "diff")
          return [...prev, { kind: "diff", files: msg.files || [] }];
        if (msg.type === "question_request")
          return [...prev, {
            kind: "ask", requestId: msg.request_id,
            questions: msg.questions || [], resolved: null,
          }];
        if (msg.type === "status") {
          // The resume after a restart nobody asked for (11-17: the line
          // landed after every dev turn that edited the backend, reading as
          // if the tab had restarted itself). A warning in it still shows.
          if (quiet && !msg.text.includes("⚠")) return prev;
          return [...prev, { kind: "status", text: msg.text }];
        }
        if (msg.type === "result") {
          setBusy(false);
          setPhase(null);
          const bits = [msg.interrupted ? "stopped" : msg.is_error ? "error" : "done"];
          if (msg.duration_ms != null) bits.push(`${(msg.duration_ms / 1000).toFixed(1)}s`);
          if (msg.tokens_in) bits.push(`${compact(msg.tokens_in)} in`);
          if (msg.tokens_out) bits.push(`${compact(msg.tokens_out)} out`);
          // Stats for nerds (Settings): the turn's speed, app/console_stats.py.
          if (msg.stats && nerdStats()) bits.push(...nerdBits(msg.stats));
          return [...prev, { kind: "result", text: bits.join(" · ") }];
        }
        return prev;
      });
    };
    // Back on a session that kept working while this page was away (a reload,
    // a dropped socket): the log is the transcript up to where the current
    // turn began, then that turn's events, then whatever is live. Held in
    // order meanwhile, so a live event never lands ahead of the replay.
    const attach = async (msg) => {
      holdRef.current = [];
      if (msg.cut != null && msg.cut_session) await loadTranscript(msg.cut_session, msg.cut);
      else if (msg.turn) {
        // A turn whose start the backend can't place in the transcript: the
        // replay is the whole log, and the load from mount mustn't land on it.
        transcriptGenRef.current += 1;
        setEntries([]);
      }
      for (const ev of msg.replay || []) handle(ev);
      const held = holdRef.current;
      holdRef.current = null;
      for (const ev of held) handle(ev);
    };
    ws.onmessage = (e) => {
      const msg = JSON.parse(e.data);
      if (holdRef.current) { holdRef.current.push(msg); return; }
      if (msg.type === "attached") { attach(msg); return; }
      handle(msg);
    };
    // Detach before closing. Switching model/mode/session bumps `epoch`, which
    // closes this socket and opens the next one — and the close handshake can
    // land *after* the new socket's onopen, whose setConnected(true) it would
    // then undo. The bar reads "disconnected" and send() refuses to fire, with
    // a perfectly healthy socket underneath and only a reload to get out of it.
    return () => {
      retryTimer.clear();
      stableTimer.clear();
      flushText();
      ws.onopen = ws.onclose = ws.onmessage = null;
      ws.close();
    };
  }, [epoch]);   // eslint-disable-line react-hooks/exhaustive-deps -- one socket per epoch: a model/mode/session switch bumps it (above), and its handlers are that epoch's

  useEffect(() => {
    // The scan reads the vault's .claude — a dev tab's are the repo's, which
    // `session_info` brings at connect.
    // eslint-disable-next-line react-you-might-not-need-an-effect/no-event-handler -- a fetch: the general console's commands; a dev tab's come with session_info
    if (project) return;
    fetchJSON("/api/commands")
      .then((list) => {
        if (!Array.isArray(list)) return;
        setCommands((prev) => {
          const byName = new Map(prev.map((c) => [c.name, c]));
          for (const c of list) {
            const known = byName.get(c.name);
            if (!known || known.scope === "session") byName.set(c.name, c);
          }
          return [...byName.values()];
        });
      })
      .catch(() => {});
  }, [project]);

  // Follow the output only while the user is already at the bottom. Scrolling
  // up to read mid-turn used to be yanked back down by every streamed token;
  // now it unsticks, and the jump pill below brings them back. Sending a
  // message re-sticks — you want to see the reply to what you just asked.
  const stuckRef = useRef(true);
  const [stuck, setStuck] = useState(true);
  const stick = (v) => { stuckRef.current = v; setStuck(v); };
  // Only the user moves the view *up*. Our own pins, the smooth jump and
  // content growing under a pinned view all leave scrollTop the same or
  // larger, so "not at the bottom" alone isn't a reason to let go: a pin
  // whose scroll event lands after the next chunk grew the log read as the
  // user leaving, and that was the pill flicker and the lost follow.
  const lastTopRef = useRef(0);
  function onLogScroll() {
    const el = scrollRef.current;
    // Zero height is the tab being hidden (display: none), not the user: the
    // browser drops the position, and reading that as "at the bottom" would
    // pin a tab you'd scrolled up in. Coming back it reads 0, which mustn't
    // count as scrolling up either.
    if (!el?.clientHeight) { lastTopRef.current = 0; return; }
    const top = el.scrollTop;
    if (el.scrollHeight - top - el.clientHeight < 40) { if (!stuckRef.current) stick(true); }
    else if (top < lastTopRef.current - 1 && stuckRef.current) stick(false);
    lastTopRef.current = top;
    saveAnchor(el);
  }
  // A wheel or key up lets go at once, before its scroll event: a chunk
  // landing in between would pin the view back down and swallow the gesture.
  function onLogWheel(e) {
    const el = scrollRef.current;
    if (!(e.deltaY < 0 && stuckRef.current && el?.scrollTop > 0)) return;
    // A tool output box scrolling up inside the log isn't the log moving.
    for (let n = e.target; n && n !== el; n = n.parentElement) {
      if (n.scrollTop > 0 && n.scrollHeight > n.clientHeight) return;
    }
    stick(false);
  }
  function onLogKey(e) {
    if (["ArrowUp", "PageUp", "Home"].includes(e.key) && stuckRef.current) stick(false);
  }
  // Where you were reading, kept as the entry at the top of the view and how
  // far into it — not a scrollTop, which a reply landing while the tab was
  // away would shift. A hidden tab loses its scroll position entirely
  // (WebKit), so this is what brings it back to the same place.
  const anchorRef = useRef(null);   // { node, offset, top }
  function saveAnchor(el) {
    const top = el.getBoundingClientRect().top;
    for (const node of el.children) {
      const r = node.getBoundingClientRect();
      if (r.bottom > top) { anchorRef.current = { node, offset: r.top - top, top: el.scrollTop }; return; }
    }
  }
  function restoreAnchor(el) {
    const a = anchorRef.current;
    if (!a) return;
    if (a.node.isConnected) {
      el.scrollTop += a.node.getBoundingClientRect().top - el.getBoundingClientRect().top - a.offset;
    } else {
      el.scrollTop = a.top;   // the entry was windowed out; near enough
    }
  }
  function jumpToBottom() {
    stick(true);
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: "smooth" });
  }
  // The prompt is as tall as what's in it, so everything you've typed is on
  // screen; styles.css caps it, and past that it scrolls. Measured rather than
  // `field-sizing: content`, which the .app's WebKit may not have.
  const fitInput = () => {
    const el = inputRef.current;
    if (!el?.offsetWidth) return;
    const top = el.scrollTop;
    el.style.height = "auto";
    el.style.height = `${el.scrollHeight}px`;
    el.scrollTop = top;
  };
  useLayoutEffect(fitInput, [input, active, composerTarget]);
  // A wider pane rewraps it. Width only: fitting changes the height, and
  // answering that would loop.
  useEffect(() => {
    const el = inputRef.current;
    if (!el || typeof ResizeObserver === "undefined") return;
    let width = el.offsetWidth;
    const ro = new ResizeObserver(() => {
      if (el.offsetWidth !== width) { width = el.offsetWidth; fitInput(); }
    });
    ro.observe(el);
    return () => ro.disconnect();
  }, [active, composerTarget]);

  // Only the latest turns are drawn; older ones wait behind a button at the
  // top of the log. A long chat drew every turn it had ever had — markdown,
  // math, slides — and each one is DOM the browser lays out on every streamed
  // token. The button keeps your place: the log grows upward under it.
  const [turnLimit, setTurnLimit] = useState(TURN_WINDOW);
  const keepFromBottomRef = useRef(null);
  function showEarlier() {
    const el = scrollRef.current;
    if (el) keepFromBottomRef.current = el.scrollHeight - el.scrollTop;
    setTurnLimit((n) => n + TURN_WINDOW);
  }
  useLayoutEffect(() => {
    const el = scrollRef.current;
    if (el && keepFromBottomRef.current != null) el.scrollTop = el.scrollHeight - keepFromBottomRef.current;
    keepFromBottomRef.current = null;
  }, [turnLimit]);

  // A new first entry means a different log (new session, resume): re-stick.
  // Layout, not passive: it has to run before paint (no frame drawn off the
  // bottom) and before the observer below reads stuckRef for this commit.
  const firstEntryRef = useRef(null);
  useLayoutEffect(() => {
    const fresh = entries[0] !== firstEntryRef.current;
    firstEntryRef.current = entries[0];
    if (fresh) setTurnLimit(TURN_WINDOW);
    // A `!` arrives as the user entry and its empty shell block together, so
    // what you just sent can be the second-to-last entry.
    const tail = entries[entries.length - 1], before = entries[entries.length - 2];
    const justRan = tail?.kind === "shell" && before?.kind === "user" && !tail.output && tail.code === undefined;
    if (fresh || tail?.kind === "user" || justRan) stick(true);
    const el = scrollRef.current;
    if (stuckRef.current && el?.clientHeight) el.scrollTop = el.scrollHeight;
  }, [entries]);
  // Not everything that grows the log is an `entries` change: the working
  // line's phase, the agent strip, an image loading, a block expanding. Pin
  // on any DOM change while stuck. The observer's microtask runs before
  // paint, so the view never shows a frame short of the bottom.
  useEffect(() => {
    const el = scrollRef.current;
    if (!el || typeof MutationObserver === "undefined") return;
    const pin = () => {
      if (stuckRef.current && el.clientHeight) el.scrollTop = el.scrollHeight;
    };
    const mo = new MutationObserver(pin);
    mo.observe(el, { childList: true, subtree: true, characterData: true });
    el.addEventListener("load", pin, true);   // images, capture: load doesn't bubble
    return () => { mo.disconnect(); el.removeEventListener("load", pin, true); };
  }, []);
  // The log is laid out at zero height while the sheet is shut or the tab is
  // hidden, so the scroll above lands nowhere; re-pin whenever the pane
  // itself resizes — or, coming back from hidden scrolled up, return to the
  // place you were reading. Runs before paint, so there's no flash of the top.
  useEffect(() => {
    const el = scrollRef.current;
    if (!el || typeof ResizeObserver === "undefined") return;
    let shown = el.clientHeight > 0;
    const ro = new ResizeObserver(() => {
      const was = shown;
      shown = el.clientHeight > 0;
      if (!shown) return;
      if (stuckRef.current) el.scrollTo({ top: el.scrollHeight });
      else if (!was) restoreAnchor(el);
    });
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  // Dropping a file used to paste its local path into the textarea — the
  // browser's own default, and useless, because the console runs with the
  // vault as its cwd and can't read arbitrary disk anyway. Read it here
  // instead and attach what it *says* as a pill.
  async function ingest(label, request) {
    const id = onAddPill({ kind: "file", label, data: "",
                           note: "reading…", pending: true });
    try {
      const out = await request();
      onUpdatePill(id, { data: out.text || "", note: out.note || "",
                         doc: out.doc || null, image: out.image || null, pending: false });
    } catch (err) {
      onUpdatePill(id, { note: `could not be read: ${err.message}`, pending: false });
    }
  }

  function ingestFiles(files) {
    for (const f of files) {
      ingest(f.name, () => {
        const body = new FormData();
        body.append("file", f);
        return fetchJSON("/api/console/upload", { method: "POST", body });
      });
    }
  }

  // The tray's half: the backend opens the file itself, by name, out of the
  // Desktop — so a screenshot never has to be found in Finder first.
  function ingestDesk(name) {
    setSeenDesk((prev) => new Set(prev).add(name));
    setFresh(null);
    ingest(name, () => fetchJSON("/api/console/desktop/attach", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name }),
    }));
  }

  // ⌃⇧⌘4 puts the screenshot on the clipboard rather than the Desktop, and
  // every clipboard image arrives named "image.png" — so a paste is named
  // for when it happened, which is what tells two of them apart on the chip.
  function onPaste(e) {
    const files = [...(e.clipboardData?.files || [])];
    if (!files.length) return;      // plain text pastes as text
    e.preventDefault();
    const at = new Date().toLocaleTimeString([], { hour: "numeric", minute: "2-digit", second: "2-digit" });
    ingestFiles(files.map((f, i) => (f.name === "image.png" || !f.name)
      ? new File([f], `pasted ${at}${files.length > 1 ? ` (${i + 1})` : ""}.${(f.type.split("/")[1] || "png")}`, { type: f.type })
      : f));
  }

  // The tray, and the one-click chip for a screenshot taken moments ago. The
  // chip is checked on focus: the gesture is "screenshot, then come back to
  // the app", and coming back is exactly a focus event — no polling.
  const [tray, setTray] = useState(null);           // null = closed, else files
  const [fresh, setFresh] = useState(null);         // a just-taken screenshot
  const [seenDesk, setSeenDesk] = useState(() => new Set());
  const seenRef = useRef(seenDesk);
  seenRef.current = seenDesk;
  useEffect(() => {
    const FRESH_S = 5 * 60;
    function check() {
      fetchJSON("/api/console/desktop?limit=6").then(({ files }) => {
        const now = Date.now() / 1000;
        const f = files.find((x) => x.image && now - x.mtime < FRESH_S
          && !seenRef.current.has(x.name));
        setFresh(f || null);
      }).catch(() => {});
    }
    check();
    const onVis = () => { if (!document.hidden) check(); };
    window.addEventListener("focus", check);
    document.addEventListener("visibilitychange", onVis);
    return () => {
      window.removeEventListener("focus", check);
      document.removeEventListener("visibilitychange", onVis);
    };
  }, []);

  function toggleTray() {
    if (tray) { setTray(null); return; }
    setTray([]);
    fetchJSON("/api/console/desktop?limit=24")
      .then(({ files }) => setTray(files.length ? files : "empty"))
      .catch(() => setTray("empty"));
  }

  function dismissFresh() {
    if (fresh) setSeenDesk((prev) => new Set(prev).add(fresh.name));
    setFresh(null);
  }

  function onDrop(e) {
    // A dragged widget row/group/card lands as the pill its pin button would
    // have made — same label, same shaped payload, same replace-by-label.
    const pin = readPin(e.dataTransfer);
    if (pin) {
      e.preventDefault();
      setDragging(false);
      onPinContext(pin.label, pin.data);
      return;
    }
    const files = [...(e.dataTransfer?.files || [])];
    if (!files.length) return;      // a text/url drag keeps the native paste
    e.preventDefault();
    setDragging(false);
    ingestFiles(files);
  }

  // Sending mid-read would attach a file whose contents are still "reading…",
  // which the model would report as an unreadable attachment.
  const reading = pills.some((p) => p.pending);

  // Exactly one card holds the keyboard at a time. The SDK only ever has one
  // question in flight, but a card left unresolved by an interrupt stays in
  // the log, and two live cards fighting over the same arrow key is the one
  // way this gets confusing.
  const awaiting = entries.find(
    (e) => (e.kind === "perm" || e.kind === "ask") && e.resolved === null
  )?.requestId;
  const awaitingPerm = awaiting && entries.some((e) => e.kind === "perm" && e.requestId === awaiting)
    ? awaiting : null;
  // That card opens as a modal when this tab is the one the window picked
  // (`modal`: the showing tab if it's asking, else the first that is).
  const [cardInLog, setCardInLog] = useState(null);
  const modalCard = modal && awaiting && awaiting !== cardInLog
    ? entries.find((e) => e.requestId === awaiting && (e.kind === "perm" || e.kind === "ask"))
    : null;

  // `other`: ⌥Enter, the mid-turn mode the setting didn't pick.
  function send(other = false) {
    const text = input.trim();
    if ((!text && !comments.length) || reading) return;
    if (text) remember(text);
    setHistAt(null);
    setHistSearch(false);
    setSuggestion("");
    if (text.startsWith("/btw ")) { askAside(text.slice(5).trim()); setInput(""); return; }
    // Typed while an approval card waits: the terminal's "no, and tell Claude
    // what to do differently". The card is declined with this as its reason,
    // which reaches the model as the call's answer, so the turn turns on it
    // rather than the message sitting behind a call nobody approved (#99).
    // Line comments stay put for the next message.
    if (awaitingPerm && text && !text.startsWith("/") && !text.startsWith("!")) {
      answerPermission(awaitingPerm, { allow: false, reason: text });
      setInput("");
      return;
    }
    // The diff's line comments ride along, shown in the log as chips.
    const out = withComments(text, comments, worktree?.branch);
    const tags = out.tags;
    const done = () => { setInput(""); if (out.used) setComments([]); };
    // Mid-turn, the setting (⌥Enter the other way): "queue" holds it for a
    // turn of its own when this one ends; "steer" sends it in after the step
    // running now, not after the whole turn (as it does behind messages
    // still steering). Commands and `!` lines wait locally either way.
    const queueIt = (readEnterMode() === "queue") !== other;
    if (busy && queueIt && !/^[!/]/.test(text)) { if (sendHeld(out.text)) done(); return; }
    if (busy || queued.length) { if (sendQueued(out.text, true, { shown: text, tags })) done(); return; }
    if (sendText(out.text, true, true, { shown: text, tags })) done();
  }

  // Held server-side for its own turn when this one ends (#92). The list
  // comes back as a `held` frame; nothing to add here meanwhile.
  function sendHeld(text) {
    if (!connected || wsRef.current?.readyState !== WebSocket.OPEN) return false;
    wsRef.current.send(JSON.stringify({ ...messagePayload(text, true, true), hold: true }));
    onTyped?.(tabId, "engaged");
    onClearPills();
    return true;
  }
  // The held list's verbs, all answered by the server's new list.
  const holdSend = (frame) => {
    if (wsRef.current?.readyState === WebSocket.OPEN) wsRef.current.send(JSON.stringify(frame));
  };
  function holdTakeBack(item) {
    holdSend({ type: "held_drop", id: item.id });
    setInput((cur) => [item.text, cur].filter(Boolean).join("\n"));
    inputRef.current?.focus();
  }
  function setEnterMode(mode) {
    writeEnterMode(mode);
    setEnterModeState(mode);
  }

  // Off to the CLI now, under a uuid its replay hands back when it's read.
  // It joins the log then, between the steps it really came between.
  // `withPills` false: the diff pane's asks, which mustn't carry off what's
  // pinned to the prompt. `shown`: the log's text and chips when the message
  // carries more than was typed (send's line comments).
  function sendQueued(text, withPills = true, shown = null) {
    // A command or a `!` line isn't a message the model can take mid-step:
    // it waits here for the turn to end (the effect below), as all of them
    // used to.
    if (text.startsWith("!") || text.startsWith("/")) {
      setQueued((q) => [...q, { id: crypto.randomUUID(), text, local: true }]);
      return true;
    }
    if (!connected || wsRef.current?.readyState !== WebSocket.OPEN) return false;
    const id = crypto.randomUUID();
    const sent = withPills ? pills : [];
    wsRef.current.send(JSON.stringify({ ...messagePayload(text, withPills, true), queue_id: id }));
    onTyped?.(tabId, "engaged");
    setQueued((q) => [...q, { id, text: shown ? shown.shown : text,
                              pills: [...sent.map((x) => x.label), ...(shown?.tags || [])] }]);
    if (withPills) onClearPills();
    return true;
  }

  // The diff pane's comments and its "ask the tab" buttons: the tab's next
  // message, now if it's idle, else in at the next step as a typed one is.
  function sendFromPane(text) {
    if (busy || queued.length) return sendQueued(text, false);
    return sendText(text, false, true);
  }

  // Queued messages the backend took back from the CLI unread: to the
  // prompt, oldest first, ahead of whatever is being typed. Through a ref
  // for the socket handlers, which hold the render that opened them.
  unqueuedRef.current = unqueued;
  function unqueued(ids) {
    const back = queuedRef.current.filter((q) => ids.includes(q.id));
    if (!back.length) return;
    setQueued((q) => q.filter((x) => !ids.includes(x.id)));
    setInput((cur) => [...back.map((q) => q.text), cur].filter(Boolean).join("\n"));
  }

  // Your prompts, oldest first, kept per folder like the CLI's.
  function remember(text) {
    const next = [...historyRef.current.filter((t) => t !== text), text].slice(-HISTORY_MAX);
    historyRef.current = next;
    try { localStorage.setItem(historyKey, JSON.stringify(next)); } catch { /* per-viewer nicety */ }
  }

  function recall(step) {
    const list = historyRef.current;
    if (!list.length) return false;
    const at = histAt === null ? list.length : histAt;
    const next = at + step;
    if (next < 0) return true;
    if (next >= list.length) { setHistAt(null); setInput(""); return true; }
    setHistAt(next);
    setInput(list[next]);
    return true;
  }

  // `/btw`: a side question. It goes to its own one-turn model with what's on
  // screen, so it works mid-turn and the session never hears about it.
  async function askAside(question) {
    if (!question) return;
    const id = `btw-${Date.now()}`;
    setEntries((p) => [...p, { kind: "btw", id, question, answer: null }]);
    const context = entries.filter((e) => e.kind === "user" || e.kind === "text")
      .map((e) => `${e.kind === "user" ? "User" : "Assistant"}: ${e.text}`).join("\n\n");
    let answer;
    try { ({ answer } = await postJSON("/api/console/btw", { question, context })); }
    catch (err) { answer = `That couldn't be answered: ${err.message}`; }
    setEntries((p) => p.map((e) => (e.id === id ? { ...e, answer } : e)));
  }

  // The formula goes in as $…$ where the caret was, the notation the console
  // reads and the transcript draws; the field is one line above the prompt.
  // Once in, a formula is drawn in the preview over the prompt, and clicking
  // it opens it here again: Enter puts the edit back in its place, an emptied
  // field takes the formula out, Esc leaves it as it was.
  function closeMath(caret) {
    mathRef.current = "";
    setMathOpen(false);
    setMathEdit(null);
    requestAnimationFrame(() => {
      const el = inputRef.current;
      if (!el) return;
      el.focus();
      if (caret != null) el.setSelectionRange(caret, caret);
    });
  }
  function openMath(span = null) {
    mathRef.current = span?.tex || "";
    setMathEdit(span);
    setMathOpen(true);
  }
  function insertMath() {
    const tex = mathRef.current.trim();
    const piece = tex ? `$${tex}$` : "";
    if (tex) mathMadeRef.current.add(tex);
    // The span is re-found by its text: the prompt may have been typed in
    // since the field opened, which moves it.
    let at = mathEdit ? input.indexOf(`$${mathEdit.tex}$`, Math.max(0, mathEdit.start - 64)) : -1;
    if (mathEdit && at < 0) at = input.indexOf(`$${mathEdit.tex}$`);
    if (mathEdit && at >= 0) {
      const end = at + mathEdit.tex.length + 2;
      setInput(`${input.slice(0, at)}${piece}${input.slice(end)}`);
      closeMath(at + piece.length);
      return;
    }
    if (!piece) { closeMath(); return; }
    const caret = inputRef.current?.selectionStart ?? input.length;
    setInput(`${input.slice(0, caret)}${piece}${input.slice(caret)}`);
    closeMath(caret + piece.length);
  }

  // Ctrl+Enter: stop the turn and send what's in the prompt now. Queued with
  // the rest, and the Stop keeps the queue, so the CLI's next turn is it.
  function sendNow() {
    const text = input.trim();
    if (!text || !busy) return;
    remember(text);
    if (sendQueued(text)) setInput("");
    interrupt(true);
  }

  // ⌘Enter: this prompt, and its pills, as the first message of a new tab
  // in this one's mode and project, opened behind it; the composer clears so
  // the next thought can start here (#93). The window says no past MAX_TABS.
  function sendElsewhere() {
    const text = input.trim();
    if (!text || reading || !onBackgroundTab) return;
    const sent = [...pills];
    const answer = onBackgroundTab({ text, pills: sent, from: tabId });
    if (!answer?.ok) {
      setEntries((p) => [...p, { kind: "status", text: `Couldn't open a tab for it: ${answer?.reason || "no room"}` }]);
      return;
    }
    remember(text);
    setHistAt(null);
    setInput("");
    if (sent.length) onClearPills();
  }

  // Whatever was waiting comes back to the prompt — a Stop means the plan
  // changed. The backend takes it back from the CLI and says what it got
  // (`unqueued`); with no socket there's no CLI holding it.
  function stopTurn() {
    const open = wsRef.current?.readyState === WebSocket.OPEN;
    unqueued(queued.filter((q) => q.local || !open).map((q) => q.id));
    interrupt();
  }

  // A held command, once nothing is running and nothing sent is still unread;
  // failing that, a wake a child's report asked for. Here, not in the socket
  // handler that ended the turn: that handler's `wake` still sees busy, and
  // this runs after the render that cleared it.
  /* eslint-disable react-you-might-not-need-an-effect/no-event-handler, react-you-might-not-need-an-effect/no-derived-state, react-you-might-not-need-an-effect/no-adjust-state-on-prop-change, react-you-might-not-need-an-effect/no-pass-live-state-to-parent, react-you-might-not-need-an-effect/no-chain-state-updates -- the trigger is the socket: connected/busy flip in its handlers, and this needs the render after, whose state they can't see (above) */
  useEffect(() => {
    if (busy || !connected || reading) return;
    if (queued.length && queued[0].local) {
      const [next, ...rest] = queued;
      setQueued(rest);
      sendText(next.text);
    } else if (wakeDue) {
      setWakeDue(false);
      wake();
    }
  }, [busy, connected, reading, queued, wakeDue]); // eslint-disable-line react-hooks/exhaustive-deps -- sendText and wake are render-local; these are the conditions that let a queued send go
  /* eslint-enable react-you-might-not-need-an-effect/no-event-handler, react-you-might-not-need-an-effect/no-derived-state, react-you-might-not-need-an-effect/no-adjust-state-on-prop-change, react-you-might-not-need-an-effect/no-pass-live-state-to-parent, react-you-might-not-need-an-effect/no-chain-state-updates */

  // Esc twice: clear a prompt (Up brings it back), or from an empty one rewind.
  function doubleEscape() {
    if (input) { remember(input.trim()); setInput(""); return; }
    if (busy) return;
    const last = entries.findLast((e) => e.kind === "user" && e.checkpoint);
    if (last && window.confirm("Rewind the conversation to before your last message?"))
      rewind(last, "conversation");
  }

  // Split out of send() because a summoned turn has no composer to clear and
  // no draft to consume: what the wake word heard goes as-is.
  // `withPills` false: a handed-off tab's brief, which must not carry off
  // the pills sitting in the prompt you share with the tab you're in. An
  // array: exactly those pills (a ⌘Enter brief took its own along), and the
  // shared prompt's are left alone.
  const RESUME_TEXT = "Continue where you left off: the usage limit cut the last turn off mid-work.";
  const resume = () => sendText(RESUME_TEXT, false, true);
  // The same message, sent by the banner once the window has reset
  // (lib/autoResume.js). Not as typed: nobody wrote it, so it doesn't make
  // the tab the user's or unlock a child's handoff (console.unlock_handoff).
  const autoResume = () => sendText(RESUME_TEXT, false, false);
  sendTextRef.current = sendText;

  function sendText(raw, withPills = true, typed = withPills, shown = null) {
    const text = raw.trim();
    if (!text || reading) return false;
    // The user wrote here: the window won't close this tab to make room.
    if (typed) onTyped?.(tabId, "engaged");
    if (text === "/help") {
      // client-side: never reaches Claude, costs zero tokens
      setEntries((p) => [...p, { kind: "help" }]);
      return true;
    }
    if (text === "/mcp") {
      // The CLI's /mcp is a TUI screen an SDK session can't show; the panel is it.
      if (!connected) return false;
      openMcp();
      return true;
    }
    if (!connected || busy) return false;
    // `!`: the CLI's shell mode. You typed it, so it runs with no card, and
    // Claude answers the output (backend: run_shell).
    if (text.length > 1 && text.startsWith("!")) {
      wsRef.current.send(JSON.stringify({ type: "shell", command: text.slice(1).trim() }));
      setEntries((p) => [...p, { kind: "user", text }, { kind: "shell", output: "" }]);
      setBusy(true);
      setPhase(null);
      emit({ type: "start" });
      return true;
    }
    const sent = pillsFor(withPills);
    if (typed) wakesRef.current = 0;
    wsRef.current.send(JSON.stringify(messagePayload(text, withPills, typed)));
    setEntries((p) => [...p, { kind: "user", text: shown ? shown.shown : text,
                               pills: [...sent.map((x) => x.label), ...(shown?.tags || [])] }]);
    setBusy(true);
    setPhase(null);
    settleAgents();
    if (limited) markLimited(null);
    if (withPills === true) onClearPills();
    emit({ type: "start" });
    return true;
  }

  // A child tab's report wakes this one, as a finished background task
  // wakes Claude Code: a turn nobody typed, carrying the held reports, so the
  // model acts on them (a follow-up through message_tab, the next step, or a
  // word to the user) instead of sitting on them until the user's next
  // message. Mid-turn, it waits for the turn to end (the result handler).
  // MAX_WAKES in a row without the user is the most: a parent and child
  // passing work back and forth stop there and wait for a person.
  const wakesRef = useRef(0);
  function wake() {
    const n = reportsRef.current.length;
    const prOnly = !n && prNotesRef.current.length > 0;
    if (busy || !connected || reading || !wsRef.current || !(n || prOnly)) return false;
    if (wakesRef.current >= MAX_WAKES) return false;
    wakesRef.current += 1;
    wsRef.current.send(JSON.stringify(messagePayload(prOnly ? PR_WAKE_TEXT : WAKE_TEXT, false, false)));
    setEntries((p) => [...p, { kind: "wake",
      text: `Picking up ${prOnly ? "the PR's news" : n === 1 ? "the report" : `${n} reports`}${wakesRef.current === MAX_WAKES
        ? ` (the last automatic turn: after this, reports wait for your next message)` : ""}` }]);
    setBusy(true);
    setPhase(null);
    settleAgents();
    emit({ type: "start" });
    return true;
  }

  // One message as the backend takes it. Consumes the held handoff reports.
  function pillsFor(withPills) {
    return Array.isArray(withPills) ? withPills : withPills ? pills : [];
  }

  function messagePayload(text, withPills, typed) {
    const sent = pillsFor(withPills);
    const reports = reportsRef.current;
    reportsRef.current = [];
    const prNotes = prNotesRef.current;
    prNotesRef.current = [];
    return {
      type: "user", text,
      // Everything but a brief is the user: what lets a spawned tab hand off
      // (backend: unlock_handoff).
      typed,
      // `doc` is a kept PDF's ref — what lets the answer cite its pages.
      context: sent.map(({ label, data, kind, note, doc, image }) => ({ label, data, kind, note, doc, image })),
      verify_url: project && localStorage.getItem(`autoverify:${project}`) === "1"
        ? localStorage.getItem(`preview:${project}`) : null,
      // The tab's id in `from`: message_tab's handle on it.
      reports: reports.map((r) => ({ from: `${r.label} · tab ${r.from}`, text: `${r.status}. ${r.text}` })),
      pr_notes: prNotes,
    };
  }

  function answerPermission(requestId, { allow, always = false, reason = "" }) {
    wsRef.current?.send(JSON.stringify({
      type: "permission", request_id: requestId, allow, always, reason,
    }));
    emit({ type: "answered" });
    setEntries((prev) => prev.map((e) =>
      e.kind === "perm" && e.requestId === requestId
        ? { ...e, resolved: allow, always, reason } : e));
    inputRef.current?.focus();
  }

  // `answers` is null when the card was dismissed with esc — the backend turns
  // that into the same "did not answer" the CLI would have produced on its own.
  function answerQuestion(requestId, answers) {
    wsRef.current?.send(JSON.stringify({
      type: "question", request_id: requestId, answers,
    }));
    emit({ type: "answered" });
    setEntries((prev) => prev.map((e) =>
      // null is "still asking", so a dismissal settles as false rather than
      // leaving the card live forever with the turn already moved on.
      e.kind === "ask" && e.requestId === requestId
        ? { ...e, resolved: answers ?? false } : e));
    inputRef.current?.focus();
  }

  function interrupt(keepQueue = false) {
    // Stop the mouth first. The backend drops the sentences it hasn't sent,
    // but the ones already in the browser would otherwise keep playing after
    // the turn they belong to was cut off.
    voiceRef.current?.stopSpeech();
    // No open socket means no backend left to stop it — and no `result` or
    // `idle` coming back to unlock the composer. Stop has to work regardless.
    if (wsRef.current?.readyState !== WebSocket.OPEN) {
      cutTurn("Stopped.");
      return;
    }
    wsRef.current.send(JSON.stringify({ type: "interrupt", keep_queue: keepQueue }));
    // The backend denies everything still waiting on an answer, so a card left
    // on screen is already answered-for. Settling it here keeps someone from
    // picking an option that now goes nowhere.
    setEntries((prev) => prev.map((e) =>
      (e.kind === "perm" || e.kind === "ask") && e.resolved === null
        ? { ...e, resolved: false } : e));
  }

  // ---------- voice ----------

  function voice() {
    if (!voiceRef.current)
      voiceRef.current = createVoice({ onEvent: (ev) => onVoiceRef.current(ev) });
    return voiceRef.current;
  }

  onVoiceRef.current = (ev) => {
    switch (ev.type) {
      case "wake":
        setVoiceNote("listening…");
        voiceRef.current?.earcon();       // it heard its name; say so before the pause
        break;
      case "listening":
        setMicState(ev.on ? "listening" : "off");
        if (!ev.on) setVoiceNote("");
        break;
      case "barge_in":
        // The wake word said again while it was talking. The interrupt already
        // exists — the console has taken one from the Stop button since day
        // one, and the SDK owns the bookkeeping for a turn cut off mid-sentence.
        // This is a new trigger on that path, not new machinery. A `wake`
        // event follows it, so the note below is immediately overwritten by
        // "listening…"; what matters here is that the mouth stops.
        // Not conditional on `busy`: the model's turn ends when the result
        // lands, but the mouth outlives it by however much backlog is still
        // being synthesized — and that gap is exactly when a long answer gets
        // talked over. The backend closes its pump whether or not a turn is
        // still running.
        setVoiceNote("go ahead");
        interrupt();
        break;
      case "transcript":
        setMicState("off");
        setVoiceNote("");
        if (!ev.send) {
          // Push-to-talk lands in the composer to be read before it sends.
          // That's the line the upload refusal draws, held: a live mic is
          // fine because you're in the room to catch a bad transcript.
          setInput((cur) => (cur ? `${cur.trim()} ${ev.text}` : ev.text));
          inputRef.current?.focus();
        } else if (!sendText(ev.text)) {
          // Mid-turn (an interrupt is still landing): hold it and send when
          // the turn actually stops, or the spoken sentence is just lost.
          pendingSpeechRef.current = ev.text;
        }
        break;
      case "status":
        setMicState((s) => (s === "listening" ? "thinking" : s));
        setVoiceNote(ev.text);
        break;
      case "closed":
        setMicState("off"); setWakeOn(false); setVoiceNote("");
        break;
      default:
        break;
    }
  };

  /* eslint-disable react-you-might-not-need-an-effect/no-event-handler, react-you-might-not-need-an-effect/no-derived-state, react-you-might-not-need-an-effect/no-pass-live-state-to-parent -- the trigger is the socket: connected/busy flip in its handlers, and this needs the render after, whose state they can't see */
  useEffect(() => {
    if (busy || !pendingSpeechRef.current) return;
    const text = pendingSpeechRef.current;
    pendingSpeechRef.current = null;
    sendText(text);
    // eslint-disable-next-line react-hooks/exhaustive-deps -- on busy's edge only: a spoken turn waits for the last one to end
  }, [busy]);
  /* eslint-enable react-you-might-not-need-an-effect/no-event-handler, react-you-might-not-need-an-effect/no-derived-state, react-you-might-not-need-an-effect/no-pass-live-state-to-parent */

  useEffect(() => {
    fetchJSON("/api/voice/status").then(setVoiceCap).catch(() => {});
    return () => voiceRef.current?.stop();
  }, []);

  async function toggleWake() {
    const on = !wakeOn;
    setWakeOn(on);
    try {
      await voice().setWake(on);
      setVoiceNote(on ? `say "${voiceCap?.wake_word || ""}"` : "");
    } catch (err) {
      setWakeOn(false);
      setVoiceNote(`no microphone: ${err.message}`);
    }
  }

  function toggleVoiceOut() {
    const on = !voiceOut;
    setVoiceOut(on);
    localStorage.setItem("console_voice", on ? "1" : "0");
    if (!on) voiceRef.current?.stopSpeech();
    reconnectKeeping();   // the speakable-reply rule is baked at session start
  }

  async function talkStart() {
    if (micState === "listening") return;
    try {
      setMicState("listening");
      await voice().pttDown();
    } catch (err) {
      setMicState("off");
      setVoiceNote(`no microphone: ${err.message}`);
    }
  }

  function talkEnd() {
    voiceRef.current?.pttUp();
  }

  // Hold to talk without reaching for the mouse. Ctrl+Shift+Space because it
  // is free on macOS — Cmd+Space is Spotlight and plain Ctrl+Space switches
  // input sources.
  useEffect(() => {
    if (!voiceCap?.stt || !active) return;
    const isHotkey = (e) => e.code === "Space" && e.ctrlKey && e.shiftKey && !e.metaKey;
    const down = (e) => { if (isHotkey(e) && !e.repeat) { e.preventDefault(); talkStart(); } };
    const up = (e) => { if (e.code === "Space" || e.key === "Control" || e.key === "Shift") talkEnd(); };
    window.addEventListener("keydown", down);
    window.addEventListener("keyup", up);
    return () => {
      window.removeEventListener("keydown", down);
      window.removeEventListener("keyup", up);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps -- talkStart/talkEnd are render-local; the binding follows the mic and the tab being active
  }, [voiceCap, micState, active]);

  // A model, style or voice switch reconnects and resumes (a mode switch is
  // live: changeMode). The CLI announces a
  // session id at init, before anything is said, but writes no transcript
  // until the first turn — so resuming a session nothing was said in fails
  // ("No conversation found", the CLI exits 1) and the socket dies on the
  // next send. A new tab whose mode you set first did exactly that. With
  // nothing to keep, the switch starts fresh instead.
  function reconnectKeeping() {
    if (!entries.some((e) => e.kind === "user")) {
      localStorage.removeItem(sessionKey);
      setSessionId(null);
      announcedRef.current = null;
    }
    reconnect(false);
  }

  // Buffered reply text into the log. Called before anything else touches
  // the log, so a chip or a card never lands ahead of text that came first.
  // Refs and setters only, like cutTurn: the socket handlers hold an old copy.
  function flushText() {
    if (textRafRef.current != null) {
      cancelAnimationFrame(textRafRef.current);
      textRafRef.current = null;
    }
    textFallback.clear();
    const text = textBufRef.current;
    if (!text) return;
    textBufRef.current = "";
    setPhase(null);
    emit({ type: "text" });
    setEntries((prev) => {
      const last = prev[prev.length - 1];
      if (last?.kind === "text")
        return [...prev.slice(0, -1), { ...last, text: last.text + text }];
      return [...prev, { kind: "text", text,
                         ...(liveModelRef.current ? { model: liveModelRef.current } : {}) }];
    });
  }

  function bufferText(text) {
    textBufRef.current += text;
    if (textRafRef.current == null) {
      textRafRef.current = requestAnimationFrame(flushText);
      textFallback.start(flushText, 100);
    }
  }

  // A turn this page thinks is live, ended from this side: its socket is gone
  // (or the backend says nothing is running), so no `result` is coming for
  // it. The backend's teardown denies every card still waiting, so a card
  // left on screen is settled as denied — answering it would go nowhere.
  // Refs and setters only: the socket handlers call the copy from the render
  // that opened them.
  function cutTurn(why) {
    flushText();
    const wasBusy = busyRef.current;   // read now: the updater below runs later
    setEntries((prev) => {
      const next = prev.map((e) =>
        (e.kind === "perm" || e.kind === "ask") && e.resolved === null
          ? { ...e, resolved: false } : e);
      return why && wasBusy ? [...next, { kind: "status", text: why }] : next;
    });
    if (wasBusy) emit({ type: "end", error: true });
    busyRef.current = false;
    setBusy(false);
  }

  function reconnect(clearLog) {
    // Before the clear, so a new chat doesn't open on a note about the old one.
    cutTurn("Reconnected to apply the change — the reply stopped here, and anything waiting for approval was denied.");
    unqueued(queued.filter((q) => !q.local).map((q) => q.id));
    quietResumeRef.current = false;   // asked for: the new mode/model line is the answer
    if (clearLog) setEntries([]); // rate limits are account-wide, not session-scoped — keep them
    setShowSessions(false);
    // The old socket's onclose is detached, so say it here instead: the next
    // socket is still CONNECTING, and send() on that throws.
    setConnected(false);
    retryRef.current = 0;
    attachRef.current = false;   // a new session on purpose, not the old one picked up
    setEpoch((n) => n + 1);
  }

  function newSession() {
    localStorage.removeItem(sessionKey);
    localStorage.removeItem(rewindAtKey);
    setSessionId(null);
    reconnect(true);
  }

  // `until`: only the part written before that byte — an attach replays the
  // rest. The newest load wins: the one at mount can land after an attach's.
  function loadTranscript(id, until = null) {
    // SDK `resume` restores the model's context but not the visible log — pull
    // the past messages so you can see the conversation you're continuing.
    const gen = ++transcriptGenRef.current;
    const q = until == null ? projectQ : `${projectQ ? `${projectQ}&` : "?"}until=${until}`;
    return fetchJSON(`/api/sessions/${id}/transcript${q}`)
      .then((d) => { if (gen === transcriptGenRef.current) setEntries(d?.entries || []); })
      // A transcript that can't be read still means the log on screen belongs
      // to some other session, so clear it rather than leave it standing.
      .catch(() => { if (gen === transcriptGenRef.current) setEntries([]); });
  }

  function resumeSession(id) {
    localStorage.setItem(sessionKey, id);
    localStorage.removeItem(rewindAtKey);   // the cut was in the other session
    setSessionId(id);
    loadTranscript(id);
    reconnect(false); // keep the transcript we just loaded; the backend re-attaches context
  }

  // The model and --effort are both fixed at session start, so a pick of
  // either (the bar's ModelChip) is one reconnect; resume keeps the
  // conversation and the new pick applies.
  // A tab open_tab gave its own pick keeps the change its own.
  function changeModelEffort(m, level) {
    setModel(m);
    setEffort(level);
    if (ownPick) {
      onTabSet?.(tabId, "model", m);
      onTabSet?.(tabId, "effort", level);
    } else {
      localStorage.setItem(modelKey(profile), m);
      if (level) localStorage.setItem(effortKey(profile), level);
      else localStorage.removeItem(effortKey(profile));
    }
    setLiveModel(null);
    reconnectKeeping();
  }

  function openMcp() {
    setMcp({ servers: mcp?.servers || [], error: null, loading: true });
    wsRef.current?.send(JSON.stringify({ type: "mcp", action: "status" }));
  }

  function mcpAction(action, name, enabled) {
    setMcp((m) => ({ ...(m || {}), loading: true, error: null }));
    wsRef.current?.send(JSON.stringify({ type: "mcp", action, name, enabled }));
  }

  // Claude Code's three: "code" puts the repo's files back (dev only, on
  // this socket); "conversation" drops the message and everything after it
  // and puts its text back in the composer; "both" is files first.
  function rewind(entry, scope) {
    if (busy) return;
    if (scope === "conversation") { rewindConversation(entry); return; }
    thenConversationRef.current = scope === "both" ? entry : null;
    setEntries((prev) => prev.map((e) => (e.kind === "user" && e.checkpoint === entry.checkpoint
      ? { ...e, rewind: "pending" } : e)));
    wsRef.current?.send(JSON.stringify({ type: "rewind", id: entry.checkpoint }));
  }

  // The cut is resolved off the stored session's transcript
  // (sessions.rewind_point), then the tab reconnects resuming at it as a
  // fork — the original stays whole in "resume".
  // `retry`: a model's name. The cut is the same, but the message is sent
  // again by itself (retryTextRef, on the fork's `session` frame) and not
  // put back in the composer.
  async function rewindConversation(entry, retry = null) {
    const stored = localStorage.getItem(sessionKey);
    const mark = (rewind) => setEntries((prev) => prev.map((e) =>
      (e.kind === "user" && e.checkpoint === entry.checkpoint ? { ...e, rewind } : e)));
    if (!stored) { mark({ error: "this session has no saved turn to rewind yet" }); return false; }
    let at;
    try {
      const sep = projectQ ? `${projectQ}&` : "?";
      ({ at } = await fetchJSON(`/api/sessions/${stored}/rewind${sep}message=${encodeURIComponent(entry.checkpoint)}`));
    } catch (e) {
      mark({ error: e.message });
      return false;
    }
    if (retry) retryTextRef.current = entry.text;
    else setInput(entry.text);
    if (!at) {      // it was the first message: before it is a new session
      localStorage.removeItem(rewindAtKey);
      newSession();
      return true;
    }
    localStorage.setItem(rewindAtKey, at);
    setEntries((prev) => {
      const i = prev.findIndex((e) => e.kind === "user" && e.checkpoint === entry.checkpoint);
      return [...(i < 0 ? prev : prev.slice(0, i)),
              { kind: "status", text: retry
                ? `Retrying with ${retry}, as a fork. The first answer is still under Resume`
                : "Conversation rewound — the message is back in the box below" }];
    });
    setTasks([]);
    reconnect(false);
    return true;
  }
  rewindConversationRef.current = rewindConversation;

  // "Retry with…" on a reply (RetryMenu): fork the conversation from just
  // before the message it answered, on the picked model, and ask it again.
  // The pick becomes this tab's own, like one open_tab gave it: a retry
  // shouldn't change what every new tab starts on.
  async function retryWith(asked, m) {
    if (busy) return;
    const was = { model, tabModel };
    setModel(m);
    setLiveModel(null);
    if (onTabSet) onTabSet(tabId, "model", m);
    else localStorage.setItem(modelKey(profile), m);
    const name = m === "default" ? defaultModel || "the default model" : m;
    if (await rewindConversation(asked, name)) return;
    // The cut failed (it says why on the message): the tab stays as it was.
    setModel(was.model);
    if (onTabSet) onTabSet(tabId, "model", was.tabModel);
    else localStorage.setItem(modelKey(profile), was.model);
  }

  // The shortlist card's "use this one" (DelegateCall): the choice goes to
  // Claude as your next message.
  function pickVariant(n, provider, batch) {
    if (busy) return false;
    return sendText(`Go with variant ${n}${provider ? ` (${provider})` : ""} from batch ${batch}.`, false, true);
  }

  function changeStyle(style) {
    setOutputStyle(style);
    if (style) localStorage.setItem(styleKey(profile), style);
    else localStorage.removeItem(styleKey(profile));
    reconnectKeeping();
  }

  // Switching *into* "never ask" is confirmed every time, not once and
  // remembered. Every other mode in this list can be picked by accident and
  // costs a click to undo; this one can't be undone at all, and the moment to
  // notice you picked it is before the next turn, not in the summary
  // afterwards. Declining leaves the select where it was.
  // Returns false when the switch was declined, which the caller uses to put
  // the <select> back: state never changed, so React has no re-render to
  // restore it with, and the element would sit showing a mode the session
  // isn't in.
  function changeMode(m) {
    if (m === "bypass" && mode !== "bypass"
        && !window.confirm(BYPASS_WARNING)) return false;
    setMode(m); localStorage.setItem(spawned ? tabModeKey : modeKey(profile), m);
    // Live, not a reconnect: the backend switches the running CLI, so the
    // turn in flight and its cards survive and the next tool call is asked
    // under the new mode. A reconnect killed all of that, once per press
    // when cycling. With no socket up, the next start reads the stored mode.
    const ws = wsRef.current;
    if (ws?.readyState === WebSocket.OPEN)
      ws.send(JSON.stringify({ type: "mode", permission_mode: wireMode(m) }));
    return true;
  }

  // Closing unmounts the panel, which is what reopens it on the full list.
  function toggleSessions() {
    setShowSessions((s) => !s);
  }

  // The log reads as empty when nothing has actually been *said* in it. The
  // session-init line ("New session · mode=… · cwd=…") is a `status` entry,
  // so `entries.length === 0` stopped being true the moment the socket
  // connected — which is why the invitation used to flash and disappear a
  // half-second after the app opened, leaving the pane blank.
  const idle = entries.every((e) => e.kind === "status");
  const composerMath = useMemo(() => userMathSpans(input, mathMadeRef.current).length > 0, [input]);
  // That same line in an empty pane is mostly plumbing the bar already shows,
  // so the pane keeps only the sentence that matters: what this mode will do
  // without asking (console.py, mode_note), which has to be said before the
  // first message. One carrying a warning (reload_note) stays whole, and once
  // there's a conversation the line stays as the record.
  const sessionLine = (e) => idle && e.kind === "status"
    && /^(New session|Resumed|Rewound)\b/.test(e.text) && !e.text.includes("⚠");
  const modeNote = /mode=\S+ \((.+?)\)(?: · |$)/.exec(entries.findLast(sessionLine)?.text || "")?.[1];

  // Autocomplete: active while the input is a single "/token"
  const acQuery = /^\/[A-Za-z0-9:_-]*$/.test(input) ? input.slice(1).toLowerCase() : null;
  const acMatches = (() => {
    if (acQuery === null || acDismissed) return [];
    const prefix = [], substr = [];
    for (const c of commands) {
      const n = c.name.toLowerCase();
      if (n.startsWith(acQuery)) prefix.push(c);
      else if (n.includes(acQuery)) substr.push(c);
    }
    return [...prefix, ...substr];
  })();
  // `@`: the word at the end of the input, as a path under this tab's folder,
  // or another tab or a past session to attach (#94). One list, tabs first
  // (there are few, and they're what you're juggling), then files, then
  // sessions from the search behind chat_search, once there are 2 letters.
  // A tab or session lands as a digest pill (lib/pills.js), not text.
  const fileQuery = input.startsWith("!") ? null : /(?:^|\s)@([^\s]*)$/.exec(input)?.[1] ?? null;
  const [fileHits, setFileHits] = useState([]);   // { kind: "file" | "tab" | "session", … }
  const fileOff = fileQuery === null || acDismissed;
  useDebounce((stale) => {
    if (fileOff) { setFileHits([]); return; }
    const q = fileQuery.toLowerCase();
    const tabs = otherTabs(tabId).filter((t) => !q || t.label.toLowerCase().includes(q));
    const pq = project ? `&project=${encodeURIComponent(project)}` : "";
    const files = fetchJSON(`/api/console/files?q=${encodeURIComponent(fileQuery)}${pq}`
              + (project && worktree ? `&worktree=${encodeURIComponent(worktree.path)}` : ""))
      .then((r) => r.files.map((path) => ({ kind: "file", path, key: path }))).catch(() => []);
    const mine = announcedRef.current;
    const past = q.length < 2 ? Promise.resolve([]) : fetchJSON(`/api/sessions/search?q=${encodeURIComponent(fileQuery)}&limit=6${pq}`)
      .then((rows) => rows.filter((s) => s.id !== mine && !tabs.some((t) => t.sid === s.id))
        .slice(0, 5).map((s) => ({ kind: "session", key: `s:${s.id}`, sid: s.id, project, label: s.preview || s.id.slice(0, 8),
                                   when: s.mtime })))
      .catch(() => []);
    Promise.all([files, past]).then(([f, s]) => { if (!stale()) setFileHits([...tabs, ...f, ...s]); });
  }, fileOff ? 0 : 120, [fileQuery, acDismissed, project, worktree]);
  // The bar's merge, and the diff pane's: the same as the tab's
  // worktree_merge tool. A conflict never touches the main checkout; it
  // comes back as a line here and (from the bar) a drafted message asking
  // the tab to resolve it, which you can send or not. The pane shows the
  // conflict itself, so it passes `draft: false` and gets the answer back.
  async function mergeWorktree({ push = false, draft = true } = {}) {
    if (!worktree) return null;
    setMerging(true);
    let text, r;
    try {
      r = await postJSON("/api/console/worktree/merge", { project, worktree: worktree.path, push });
      text = (r.ok ? "Merge: " : "Not merged: ") + r.text;
      if (r.ok) onTabSet?.(tabId, "merged", Date.now());
      if (draft && r.conflicts?.length && !input.trim())
        setInput(`Merging into ${worktree.base} conflicted on ${r.conflicts.join(", ")}. `
          + `Rebase onto ${worktree.base}, resolve them, re-run the checks, then merge again.`);
    } catch (err) {
      text = `Not merged: ${err.message}`;
      r = { ok: false, text: err.message };
    } finally {
      setMerging(false);
    }
    setEntries((prev) => [...prev, { kind: "status", text }]);
    return r;
  }
  // A turn's card opens the pane on that turn; the bar opens a worktree
  // tab's branch, else everything uncommitted. A fresh key each time, so it
  // opens where it was asked to rather than where it last was.
  function openDiff(spec) {
    diffReloadRef.current = null;
    diffSeqRef.current += 1;
    setDiffOpen(spec && { turn: spec.turn || null, path: spec.path || null, seq: diffSeqRef.current,
                          scope: spec.turn ? "turn" : worktree ? "branch" : "all" });
  }
  const histHits = histSearch
    ? historyRef.current.filter((t) => t.toLowerCase().includes(input.toLowerCase())).reverse().slice(0, 12)
    : [];
  const acCount = histHits.length || fileHits.length || acMatches.length;
  const acSel = Math.min(acIndex, Math.max(acCount - 1, 0));
  const shellLive = busy && entries.findLast((e) => e.kind === "shell")?.code === undefined
    && entries.some((e) => e.kind === "shell");

  useEffect(() => {
    acListRef.current?.children[acSel]?.scrollIntoView({ block: "nearest" });
  }, [acSel]);

  function completeFile(hit) {
    if (hit.kind === "file") setInput(input.replace(/@[^\s]*$/, `@${hit.path} `));
    else {
      // The mention goes; what it named rides along as a pill.
      setInput(input.replace(/(^|\s)@[^\s]*$/, "$1"));
      onPinContext(`${hit.kind} · ${clipText(hit.label, 36)}`, sessionPin(hit.sid, hit.project));
    }
    setAcIndex(0);
    inputRef.current?.focus();
  }

  function completeCommand(name) {
    setInput(`/${name} `); // trailing space also closes the popup
    setAcIndex(0);
    inputRef.current?.focus();
  }

  // The pane follows the conversation: the newest reply's first doc
  // reference opens it there, once per reply (streaming re-renders this on
  // every token; the key keeps that to one open). An empty log is a new
  // chat, whose documents are someone else's business.
  const lastText = entries.findLastIndex((e) => e.kind === "text");
  const lastTextBody = lastText >= 0 ? entries[lastText].text : "";
  /* eslint-disable react-you-might-not-need-an-effect/no-event-handler, react-you-might-not-need-an-effect/no-chain-state-updates -- follows the reply as it streams in over the socket; an emptied log is a new chat, by whichever path emptied it */
  useEffect(() => {
    if (!entries.length) { setDocView(null); docAuto.current.on = true; return; }
    const first = docRefsIn(lastTextBody)[0];
    if (!first) return;
    const key = `${lastText}:${first.ref}:${first.page}`;
    if (docAuto.current.key === key) return;
    docAuto.current.key = key;
    if (docAuto.current.on) docPane.open(first);
    // eslint-disable-next-line react-hooks/exhaustive-deps -- follows the newest text entry; docPane and docAuto are read, not watched
  }, [lastText, lastTextBody, entries.length === 0]);
  /* eslint-enable react-you-might-not-need-an-effect/no-event-handler, react-you-might-not-need-an-effect/no-chain-state-updates */
  // Two things read off which model wrote each reply (`model` on a text
  // entry). `changes`: the reply where the model differs from the one
  // before it, for the "model changed" mark. `retries`: each turn's last
  // reply, with the message it answered, which is where "retry with…" sits;
  // a `!` command's turn has no message to ask again.
  const logMarks = useMemo(() => {
    const changes = new Map(), retries = new Map();
    let wrote = null, asked = null, lastReply = -1;
    const close = () => { if (asked && lastReply >= 0) retries.set(lastReply, asked); };
    entries.forEach((e, i) => {
      if (e.kind === "user") {
        close();
        lastReply = -1;
        asked = e.checkpoint && entries[i + 1]?.kind !== "shell" ? e : null;
      } else if (e.kind === "text") {
        lastReply = i;
        if (e.model) {
          if (wrote && wrote !== e.model) changes.set(i, { from: wrote, to: e.model });
          wrote = e.model;
        }
      }
    });
    close();
    return { changes, retries };
  }, [entries]);
  const docRefs = useMemo(() => {
    if (!docView) return [];
    const seen = new Set([docView.ref]);
    for (const e of entries) if (e.kind === "text") for (const r of docRefsIn(e.text)) seen.add(r.ref);
    return [...seen];
  }, [docView, entries]);

  // The bar: in the status bar's slot when there is one (only the showing
  // tab's), else at the head of the pane.
  const placeBar = (el) => (barTarget ? (active ? createPortal(el, barTarget) : null) : el);
  // What the status bar folded for room (plan/StatusBar.jsx); "" outside it.
  const barHost = useContext(BarHostContext);
  const barCut = barTarget ? barHost?.cut || "" : "";
  const hostFolded = ["view", "thumb", "timeline", "full", "new"].some((k) => folded(barCut, k));
  const modeSelect = (cutKey) => (
    <select
      value={mode}
      data-cut={cutKey || undefined}
      className={mode === "bypass" ? "mode-bypass" : undefined}
      aria-label="Permission mode"
      onChange={(e) => { if (!changeMode(e.target.value)) e.target.value = mode; }}
      title={mode === "bypass"
        ? "Nothing will ask — sends and deletes included"
        : "Permission mode"}
    >
      {MODES.map((m) => <option key={m.value} value={m.value}>{m.label}</option>)}
    </select>
  );
  const reviewClick = () => openDiff(diffOpen ? null : {});
  // The project's scripts and the PR chip (ProjectBar.jsx): on the bar, or in
  // ⋯ once the bar folds them. Waits for the socket's worktree, so the first
  // load reads the tab's own tree, not the main checkout's.
  const devBar = profile === "dev" && !!project && connected;
  const projectBar = (menu, parts) => (
    <ProjectBar key={`${project}:${worktree?.path || ""}`} project={project}
      worktree={worktree?.path || null} emit={emit} prw={prw} menu={menu} parts={parts}
      onDone={menu ? closeMore : undefined} />
  );
  async function watchPr() {
    const r = await prw.link("");
    setEntries((p) => [...p, { kind: "status", text: r.pr
      ? `Watching PR #${r.pr.number} (${r.pr.title}): this tab wakes when its checks, reviews, comments or conflicts change.`
      : `No PR to watch: ${r.error}` }]);
  }
  const reviewTitle = worktree ? `Review ${worktree.branch}: its commits against ${worktree.base}, `
    + "comments for the tab on any line, and the merge" : "Everything uncommitted in the repo";
  const mergeTitle = worktree ? `This tab works on ${worktree.branch}. Merge its commits into ${worktree.base}`
    + " (rebase, then fast-forward the main checkout, which is what the app runs)" : undefined;
  // A voice that's on sits in the row (it's live state); off, it's in ⋯.
  const voiceBtn = voiceCap?.tts ? (
    <button className={`bar-btn voice-btn${voiceOut ? " on" : ""}`} onClick={toggleVoiceOut}
      aria-label="Read replies aloud"
      title={voiceOut ? "Reading replies aloud — click to mute" : "Read replies aloud, a sentence at a time"}>
      <Icon name={voiceOut ? "volume" : "volumeOff"} size={12} />{!voiceOut && <span>read aloud</span>}
    </button>
  ) : null;
  const wakeBtn = voiceCap?.wake && voiceCap?.stt ? (
    <button className={`bar-btn voice-btn${wakeOn ? " on" : ""}`} onClick={toggleWake}
      aria-label="Listen for the wake word"
      title={wakeOn
        ? `Listening for "${voiceCap.wake_word}" — nothing is transcribed until it fires`
        : `Wait for "${voiceCap.wake_word}" and answer without being clicked`}>
      <Icon name="mic" size={12} />{!wakeOn && <span>wake word</span>}
    </button>
  ) : null;

  return (
    <DocPaneContext.Provider value={docPane}>
    <FileContext.Provider value={fileCtx}>
    <div
      className={`console${dragging ? " dropping" : ""}${busy ? " busy" : ""}${docView && !docTarget ? ` with-doc${docCover ? " doc-cover" : ""}` : ""}`}
      onDragOver={(e) => {
        // getData is blocked until the drop, so the *kind* has to come off
        // `types` — which is what lets the veil say which of the two things
        // is about to happen while there's still time to not do it.
        const types = [...(e.dataTransfer?.types || [])];
        const kind = types.includes(PIN_MIME) ? "context"
          : types.includes("Files") ? "file" : null;
        if (!kind) return;
        e.preventDefault();                 // without this the drop never fires
        e.dataTransfer.dropEffect = kind === "context" ? pinDropEffect(e) : "copy";
        setDragging(kind);
      }}
      onDragLeave={(e) => {
        // relatedTarget is null when the pointer leaves the window entirely,
        // and inside the pane when it merely crosses a child's border.
        if (!e.currentTarget.contains(e.relatedTarget)) setDragging(false);
      }}
      onDrop={onDrop}
    >
      {dragging && (
        <div className="drop-veil">
          {dragging === "file" ? "Drop to read into the console"
            : "Drop to attach as context"}
        </div>
      )}
      {/* One row: what you change mid-session (model, mode, diff, a voice
          that's on) stays out; the set-and-leave controls and the session
          actions (docs, resume, new) live in ⋯. Under the plan view the row
          is portalled into the status bar (`barTarget`), so the console has
          one row of chrome, not two. */}
      {placeBar(
      <div className={`console-bar${barTarget ? " in-status" : ""}`}>
        <span className={`dot ${connected ? "on" : "off"}`} title={connected ? "connected" : "disconnected"} />
        {profile !== "general" && (
          <span className={`bar-mode mode-${profile}`} title={project || undefined}>
            {profile === "dev" ? `dev · ${projectName}` : "study"}{spawned ? " · handed off" : ""}
          </span>
        )}
        <ModelChip models={models} model={model} defaultModel={defaultModel} effort={effort}
          live={liveModel} busy={busy} onPick={changeModelEffort} />
        {modeSelect("mode")}
        {usageInBar && <UsageChips rateLimits={rateLimits} />}
        {!barTarget && <span className="bar-spacer" />}
        {voiceOut && <span className="bar-fold" data-cut="voice">{voiceBtn}</span>}
        {wakeOn && <span className="bar-fold" data-cut="voice">{wakeBtn}</span>}
        {project && (
          <button className={`bar-btn${diffOpen ? " on" : ""}`} onClick={reviewClick} data-cut="review"
            title={reviewTitle}>
            {worktree ? "review" : "diff"}</button>
        )}
        {worktree && (
          <button className="bar-btn" disabled={busy || merging} onClick={() => mergeWorktree()}
            data-cut="merge" title={mergeTitle}>
            {merging ? "merging…" : <>merge<span data-cut="merge-short"> → {worktree.base}</span></>}
          </button>
        )}
        {devBar && projectBar(false, "both")}
        <button ref={setMoreEl} className={`bar-btn bar-more${barMore ? " on" : ""}`}
          aria-expanded={barMore} aria-haspopup="menu" aria-label="More console actions"
          onClick={() => setBarMore((v) => !v)}
          title={`Docs, resume, new · style, voice, MCP · usage${barCut ? " · and what the bar has no room for" : ""}`}>⋯</button>
        <BarMenu open={barMore} anchor={moreEl} onClose={closeMore}>
          {/* What the status bar folded for room (lib/barHost.js), first:
              it was one click away on the bar, so it's the first thing here. */}
          {devBar && (folded(barCut, "scripts") || folded(barCut, "pr"))
            && projectBar(true, folded(barCut, "scripts") && folded(barCut, "pr") ? "both"
              : folded(barCut, "scripts") ? "scripts" : "pr")}
          {(folded(barCut, "review") || folded(barCut, "merge") || folded(barCut, "mode")
            || hostFolded) && (
            <div className="bm-acts bm-folded">
              {folded(barCut, "mode") && <div className="bm-row bm-inline">{modeSelect(null)}</div>}
              {project && folded(barCut, "review") && (
                <button className={`bm-item${diffOpen ? " on" : ""}`} role="menuitem" title={reviewTitle}
                  onClick={() => { closeMore(); reviewClick(); }}>{worktree ? "review" : "diff"}</button>
              )}
              {worktree && folded(barCut, "merge") && (
                <button className="bm-item" role="menuitem" disabled={busy || merging} title={mergeTitle}
                  onClick={() => { closeMore(); mergeWorktree(); }}>
                  {merging ? "merging…" : `merge → ${worktree.base}`}</button>
              )}
              <HostItems cut={barCut} host={barHost?.host} done={closeMore} />
            </div>
          )}
          <div className="bm-acts">
            {profile !== "dev" && (
              <button className={`bm-item${docView ? " on" : ""}`} role="menuitem"
                onClick={() => { closeMore(); onRaise?.(); toggleDocs(); }}
                title="The document pane: PDFs this chat used, and recent ones">docs</button>
            )}
            {profile === "study" && !idle && (
              <button className="bm-item" role="menuitem" disabled={busy || !connected}
                onClick={() => { closeMore(); sendText(FRESH_TEXT, false, true); }}
                title="Carry this chat's progress into a fresh study tab">fresh</button>
            )}
            <button className={`bm-item${showSessions ? " on" : ""}`} role="menuitem"
              onClick={() => { closeMore(); onRaise?.(); toggleSessions(); }}
              title="Pick up a past session">resume</button>
            <button className="bm-item" role="menuitem"
              onClick={() => { closeMore(); onRaise?.(); newSession(); }}
              title="Start a new session in this tab">new</button>
            {devBar && (prw.watching ? (
              <button className="bm-item" role="menuitem" onClick={() => { closeMore(); prw.unlink(); }}
                title="Stop polling this PR; the tab won't be woken by it">unwatch PR #{prw.pr?.number}</button>
            ) : (
              <button className="bm-item" role="menuitem" disabled={prw.checking} onClick={() => { closeMore(); watchPr(); }}
                title="Find this branch's pull request and wake the tab when its checks, reviews, comments or conflicts change">
                watch PR</button>
            ))}
            <button className={`bm-item${mcp ? " on" : ""}`} role="menuitem" disabled={!connected}
              onClick={() => { closeMore(); onRaise?.(); if (mcp) setMcp(null); else openMcp(); }}
              title="MCP servers (/mcp)">mcp</button>
          </div>
          <div className="bm-row">
            <select value={enterMode} aria-label="Enter mid-turn"
              title="What Enter does while a turn runs: steer it in at the next step, or queue it for its own turn. ⌥Enter does the other."
              onChange={(e) => setEnterMode(e.target.value)}>
              <option value="steer">Enter mid-turn: steer</option>
              <option value="queue">Enter mid-turn: queue</option>
            </select>
          </div>
          {styles.length > 1 && (
            <div className="bm-row">
              <select value={styles.includes(outputStyle) ? outputStyle : ""}
                title="Output style (/output-style): how replies are written — ~/.claude/output-styles adds your own"
                onChange={(e) => changeStyle(e.target.value)}>
                <option value="">style</option>
                {styles.filter((s) => s !== "default").map((s) => <option key={s} value={s}>{s.toLowerCase()}</option>)}
              </select>
            </div>
          )}
          {((voiceBtn && (!voiceOut || folded(barCut, "voice")))
            || (wakeBtn && (!wakeOn || folded(barCut, "voice")))) && (
            <div className="bm-row">
              {(!voiceOut || folded(barCut, "voice")) && voiceBtn}
              {(!wakeOn || folded(barCut, "voice")) && wakeBtn}
            </div>
          )}
          {/* Every usage window at once (the bar's chip shows one at a time). */}
          {!usageInBar && (
            <div className="bm-row bm-usage"><UsageChips rateLimits={rateLimits} /></div>
          )}
          <div className="bm-row bm-meta">
            <ContextChip ctx={ctx} />
            <CliStatus sessionVersion={cliVersion} busy={busy} onReconnect={reconnectKeeping} />
            <span className="bar-item"
              title={[sessionId || "no session yet", cliVersion && `Claude Code ${cliVersion}`]
                .filter(Boolean).join("\n")}>
              {sessionId ? `session ${sessionId.slice(0, 8)}` : "no session yet"}
            </span>
          </div>
        </BarMenu>
      </div>)}
      {showSessions && (
        <SessionsPanel onResume={resumeSession} onClose={toggleSessions} project={project} />
      )}
      {mcp && <McpPanel state={mcp} onAction={mcpAction} onClose={() => setMcp(null)} />}

      <div className="console-log" ref={scrollRef} onScroll={onLogScroll}
           onWheel={onLogWheel} onKeyDown={onLogKey}>
        {idle && (
          <div className="console-hello">
            {profile === "dev" ? (
              <>
                <div className="hello-title">Claude Code, working in {projectName}</div>
                Its CLAUDE.md, skills and memory are loaded. It starts in "never
                ask": nothing stops for a click, and the diff that ends each
                turn is the review.
              </>
            ) : profile === "study" ? (
              <>
                <div className="hello-title">Study — working it through with you</div>
                Drop a photo of the problem or a PDF of the slides. It tutors
                rather than answers: hints first, the full solution when you
                ask for it, and what you miss goes in the course's review log.
              </>
            ) : (
              <>
                <div className="hello-title">Claude Code, working in your vault</div>
                Ask about your notes, tasks or the week's data. Drag a card in
                to attach what it's showing; <kbd>/</kbd> lists commands and
                skills.
              </>
            )}
            {modeNote && <div className="hello-mode">{modeNote}.</div>}
            <RecentSessions onResume={resumeSession} project={project} />
          </div>
        )}
        {/* presentation-only grouping: one block per exchange (user → tools →
            reply → result). Perm/help cards stand alone between blocks. Items
            carry their flat index, which is what the collapsed runs key on. */}
        {(() => {
          const groups = [];
          entries.forEach((e, i) => {
            if (sessionLine(e)) return;
            const last = groups[groups.length - 1];
            if (e.kind === "perm" || e.kind === "ask" || e.kind === "help") {
              groups.push({ lone: { e, i } });
            } else if (e.kind === "user" || !last || last.lone || last.closed) {
              groups.push({ items: [{ e, i }], closed: e.kind === "result" });
            } else {
              last.items.push({ e, i });
              if (e.kind === "result") last.closed = true;
            }
          });
          // Windowed by user turn, so a turn is never cut in half: the first
          // group drawn is the turnLimit-th user message from the end.
          let from = 0;
          for (let gi = groups.length - 1, n = 0; gi >= 0; gi--) {
            if (groups[gi].items?.[0]?.e.kind === "user" && ++n === turnLimit) { from = gi; break; }
          }
          const hidden = groups.slice(0, from)
            .filter((g) => g.items?.[0]?.e.kind === "user").length;
          const earlier = hidden > 0 && (
            <div key="earlier" className="log-earlier">
              <button type="button" onClick={showEarlier}>
                {hidden} earlier {hidden === 1 ? "turn" : "turns"} · show {Math.min(hidden, TURN_WINDOW)} more
              </button>
            </div>
          );
          return [earlier, ...groups.slice(from).map((g, k) => {
            const gi = from + k;
            if (g.lone) return renderEntry(g.lone.e, g.lone.i);
            const bare = g.items.every((it) => it.e.kind === "status");
            if (bare) return renderNotes(g.items, gi);
            return (
              // `g`: a lone card is keyed by its entry index, and a bare gi
              // collided with it (the card at entry 3, the turn after it at
              // group 3) — React then left stale copies of the card behind,
              // one more with every message (11-17).
              <div key={`g${gi}`} className={`turn${g.closed ? " settled" : ""}`}>
                {coalesce(g.items).map((r) => (
                  r.agent
                    ? <ErrorBoundary key={r.at} label="console agent">
                        <AgentCall t={r.agent} sessionId={sessionId} projectQ={projectQ}
                          running={agents.some((a) => a.tool_use_id === r.agent.id
                                                      && !AGENT_DONE.has(a.status))} />
                      </ErrorBoundary>
                    : r.delegate
                    ? <ErrorBoundary key={r.at} label="console delegate">
                        <DelegateCall t={r.delegate} busy={busy} onPick={pickVariant}
                          live={busy && r.delegate.result == null && r.at === entries.length - 1} />
                      </ErrorBoundary>
                    : r.tools
                    ? <ErrorBoundary key={r.at} label="console tools" resetKey={r.tools.length}>
                        <ToolRun
                          tools={r.tools} open={openRuns.has(r.at)}
                          live={busy && r.at + r.tools.length === entries.length}
                          onToggle={() => toggleRun(r.at)} />
                      </ErrorBoundary>
                    : renderEntry(r.e, r.at)
                ))}
              </div>
            );
          })];
        })()}
        {/* while a call is in flight its own line already pulses; two things
            saying "still going" is one more than the log needs — unless the
            model has moved on to thinking or writing the next call */}
        {busy && (phase || entries[entries.length - 1]?.kind !== "tool") && (
          <WorkingLine since={turnStartRef.current} phase={phase} />
        )}
        <AgentStrip agents={agents} hidden={agentsSeen} />
        {/* A tab a quit cut off resumes only on a click (the banner below),
            so while that banner is up the usage-limit one, and its resume
            at the reset, stand aside: one tab, one resume. Resume there sends
            a turn, which clears `limited`; Dismiss leaves it for a click. */}
        {limited && !busy && !interrupted && (
          <LimitBanner tab={tabId} limited={limited} rateLimits={rateLimits} onResume={resume}
            model={liveModel || findModel(models, model === "default" ? defaultModel : model)?.resolved
              || (model === "default" ? defaultModel : model)}
            onAuto={autoResume} onDecline={() => markLimited({ ...limited, manual: true })} />
        )}
        {interrupted && !busy && (
          <div className="limit-banner interrupted-banner" role="status">
            <span className="limit-text">Kestrel quit while this tab was working, which cut off {cutWhat(interrupted)}.</span>
            <button className="limit-resume" onClick={() => { resumeWith(resumeText(interrupted)); onInterrupted?.(tabId); }}
                    title="Send a 'continue' that says what was cut off">Resume</button>
            <button className="limit-dismiss" title="Leave it as it is"
                    onClick={() => { if (limited) markLimited({ ...limited, manual: true }); onInterrupted?.(tabId); }}>
              Dismiss</button>
          </div>
        )}
        {!stuck && (
          <div className="log-jump">
            <button className={busy ? "busy" : ""} onClick={jumpToBottom}>
              {busy ? <><span className="log-jump-dot" /> generating below</> : "jump to latest"}
              <Icon name="chevronDown" size={11} />
            </button>
          </div>
        )}
      </div>

      {/* The plan view draws the prompt under the stage, outside the sheet,
          so it stays on screen at every sheet height and over a full view.
          A portal keeps its state here, and React events still bubble from
          it to this pane — dropping onto the prompt is still a drop here. */}
      {/* The plan view gives the document the map's place (`docTarget`), so
          the conversation keeps its whole width; anywhere else it shares the
          console. Only the showing tab draws into the shared slot. */}
      {docView && (docTarget
        ? active && createPortal(renderDocPane(false), docTarget)
        : renderDocPane(true))}
      {diffOpen && (
        <DiffPane key={diffOpen.seq} turns={turnDiffs(entries)} turn={diffOpen.turn} path={diffOpen.path}
          scope={diffOpen.scope} project={project} worktree={worktree} busy={busy}
          comments={comments} onComments={setComments}
          reloadRef={diffReloadRef} onSend={sendFromPane}
          onMerge={(o) => mergeWorktree({ ...o, draft: false })} onClose={() => openDiff(null)} />
      )}
      {composerTarget ? active && createPortal(renderComposer(), composerTarget)
        : renderComposer()}
      {modalCard && createPortal(renderCardModal(), document.body)}
    </div>
    </FileContext.Provider>
    </DocPaneContext.Provider>
  );

  // The card waiting on you, in the middle of the screen over everything:
  // in the log it could sit under a peeked sheet or in a tab you weren't in.
  // A background tab's says which tab it is. "In the conversation" puts it
  // back in the log, for reading what led up to it first.
  function renderCardModal() {
    const label = profile === "dev" ? `dev · ${projectName}` : profile;
    return (
      <div className="card-modal-scrim">
        <div className="card-modal" role="dialog" aria-modal="true"
             aria-label={`${label} tab is asking`}>
          <div className="card-modal-head">
            <span>{active ? "Kestrel is asking" : `${label} tab is asking`}</span>
            <button type="button" className="card-modal-later" tabIndex={-1}
                    onClick={() => { setCardInLog(modalCard.requestId); onShowTab?.(tabId); onRaise?.(); }}>
              {active ? "show in the conversation" : "open the tab"}
            </button>
          </div>
          {modalCard.kind === "perm"
            ? <PermCard entry={modalCard} active onAnswer={answerPermission} />
            : <AskCard entry={modalCard} active onAnswer={answerQuestion} />}
        </div>
      </div>
    );
  }

  function renderDocPane(inside) {
    if (docView.file && docView.file.kind !== "pdf")
      return (
        <div className="doc-stack">
          <ErrorBoundary label="file pane" resetKey={docView.seq}>
            <FilePane view={docView} onClose={closeDoc} />
          </ErrorBoundary>
        </div>
      );
    const split = () => {
      const other = docRefs.find((r) => r !== docView.ref)
        || recentDocs.find((d) => d.ref !== docView.ref)?.ref;
      if (other) setDocView2({ ref: other, page: 1, seq: ++docAuto.current.seq });
    };
    return (
      <div className={`doc-stack${docView2 ? " split" : ""}`}>
        <ErrorBoundary label="document pane" resetKey={docView.seq}>
          <DocPane view={docView} docs={docRefs} recent={recentDocs} onClose={closeDoc}
            onCover={inside ? setDocCover : undefined}
            onSplit={docView2 ? undefined : split}
            onPick={(ref) => docPane.open({ ref, page: 1 })} />
        </ErrorBoundary>
        {docView2 && (
          <ErrorBoundary label="second document" resetKey={docView2.seq}>
            <DocPane view={docView2} docs={docRefs} recent={recentDocs}
              onClose={() => setDocView2(null)}
              onPick={(ref) => setDocView2({ ref, page: 1, seq: ++docAuto.current.seq })} />
          </ErrorBoundary>
        )}
      </div>
    );
  }

  function toggleRun(at) {
    setOpenRuns((prev) => {
      const next = new Set(prev);
      if (!next.delete(at)) next.add(at);
      return next;
    });
  }

  // Runs of tool calls become one item; everything else passes through.
  // An Agent call is its own row (AgentCall): what its subagent does never
  // rides this stream, so folded into a run it read as one more chip.
  function coalesce(items) {
    const out = [];
    for (const { e, i } of items) {
      const last = out[out.length - 1];
      if (e.kind === "tool" && AGENT_TOOLS.has(e.name)) out.push({ agent: e, at: i });
      // Another provider at work is its own row too (DelegateCall): it is
      // the log's mark that someone other than Claude answered.
      else if (e.kind === "tool" && DELEGATE_TOOL.test(e.name)) out.push({ delegate: e, at: i });
      else if (e.kind === "tool" && last?.tools) last.tools.push(e);
      else if (e.kind === "tool") out.push({ tools: [e], at: i });
      else out.push({ e, at: i });
    }
    return out;
  }

  // Connection notes stack up: every model or mode switch reconnects and says
  // the same shape of thing again. Only the last one is still true, so that is
  // the one on screen, with the count of what it covers standing in for the rest.
  function renderNotes(items, gi) {
    if (items.length === 1) return renderEntry(items[0].e, items[0].i);
    const at = items[0].i;
    const open = openRuns.has(at);
    const shown = open ? items : [items[items.length - 1]];
    return (
      <div key={`n${gi}`} className="notes">
        <button className="note-more" onClick={() => toggleRun(at)}
                title={open ? "Collapse" : "Show the earlier ones"}>
          {open ? "−" : `+${items.length - 1}`}
        </button>
        <div className="notes-list">
          {shown.map((it) => renderEntry(it.e, it.i))}
        </div>
      </div>
    );
  }

  // One entry that can't render costs that entry, as its plain text where it
  // has any — never the transcript, which has no other copy on screen. Entry
  // has to be a component: drawn inline, the markdown would run during
  // Console's own render, above the boundary that's meant to catch it.
  function renderEntry(e, i) {
    return (
      <ErrorBoundary key={i} label="console entry" resetKey={e.text}
                     fallback={e.text ? <div className="entry assistant"><pre className="md-p">{e.text}</pre></div> : null}>
        <Entry draw={() => drawEntry(e, i)} />
      </ErrorBoundary>
    );
  }

  // hoisted render helpers — same cases as before, just grouped into blocks
  function drawEntry(e, i) {
    // A `!` you ran is headed by its shell block; the line would only repeat it.
    if (e.kind === "user" && entries[i + 1]?.kind === "shell") return null;
    if (e.kind === "user")
      return (
        <div key={i} className="entry user">
          {e.pills?.length > 0 && (
            <span className="attached">{e.pills.map((p, j) => (
                <span key={j} className="attached-pill"><Icon name="pin" size={10} /> {p}</span>
              ))}</span>
          )}
          <pre>{renderUserMath(e.text, null, mathMadeRef.current)}</pre>
          <RewindButton entry={e} disabled={busy} files={!!project} onRewind={rewind} />
        </div>
      );
    if (e.kind === "text") {
      const change = logMarks.changes.get(i);
      const asked = logMarks.retries.get(i);
      return (
        <Fragment key={i}>
          {change && <ModelMark models={models} from={change.from} to={change.to} />}
          <div className="entry assistant">{renderMarkdownCached(e.text)}</div>
          {asked && (
            <RetryMenu models={models} model={model} defaultModel={defaultModel}
              answeredBy={e.model} disabled={busy} onRetry={(m) => retryWith(asked, m)}
              blocked={asked.pills?.length
                ? "This message had things pinned to it, and a retry can only resend its words. Rewind to it and send it again."
                : null} />
          )}
        </Fragment>
      );
    }
    if (e.kind === "tasknote") return <TaskNote key={i} e={e} />;
    // tool entries never reach here — coalesce() hands every run to ToolRun
    if (e.kind === "shell")
      return (
        <ShellBlock key={i} command={entries[i - 1]?.text?.replace(/^!\s*/, "") || ""}
          output={e.output} code={e.code} background={e.background} ttyId={e.ttyId}
          live={shellLive && e === entries.findLast((x) => x.kind === "shell")}
          onBackground={() => wsRef.current?.send(JSON.stringify({ type: "shell_background" }))} />
      );
    if (e.kind === "btw")
      return (
        <div key={i} className="btw-card">
          <div className="btw-q">/btw {e.question}</div>
          <div className="btw-a">{e.answer === null ? "thinking…" : renderMarkdownCached(e.answer)}</div>
        </div>
      );
    if (e.kind === "help") {
      const list = [...commands].sort((a, b) => a.name.localeCompare(b.name));
      return (
        <div key={i} className="help-card">
          <div className="help-head">shortcuts</div>
          {[["!", "run a shell command; Claude sees the output and answers it"],
            ["/", "commands and skills"],
            ["@", "name a file, with autocomplete"],
            ["?", "these shortcuts, on an empty prompt"],
            ["\\ Enter", "new line (or Shift+Enter)"],
            ["/btw", "a side question; it won't touch the conversation"],
            ["Enter, mid-turn", enterMode === "queue"
              ? "hold it for its own turn when this one ends (edit, reorder or steer it in from the list)"
              : "send it in after the current step; Up takes it back until then"],
            ["⌥Enter, mid-turn", enterMode === "queue" ? "steer it in after the current step instead"
              : "hold it for its own turn instead"],
            ["Ctrl+Enter", "stop the turn and send this now"],
            ["⌘Enter", "send this to a new tab behind this one (same mode and project)"],
            ["typing, at a card", "decline the call and tell Claude what to do instead"],
            ["↑ ↓", "earlier prompts"],
            ["Ctrl+R", "search earlier prompts"],
            ["Ctrl+B", "move a running ! command to the background"],
            ["Ctrl+S", "stash this prompt, or bring it back"],
            ["Ctrl+M", "type a formula: / for a fraction, ^ up, sqrt, pi"],
            ["Shift+Tab", "next permission mode (empty composer)"],
            ["Esc Esc", "clear the prompt, or rewind from an empty one"],
            ["Tab", "use the suggested prompt"]].map(([key, what]) => (
            <div key={key} className="help-row">
              <span className="help-name">{key}</span>
              <span className="help-desc">{what}</span>
            </div>
          ))}
          <div className="help-head">commands — rendered locally, nothing sent to Claude</div>
          {list.length === 0 && <p className="status">No commands discovered yet.</p>}
          {list.map((c) => (
            <div key={c.name} className="help-row">
              <span className="help-name">/{c.name}</span>
              {c.argument_hint && <span className="help-hint">{c.argument_hint}</span>}
              <span className="help-desc">{c.description}</span>
              <span className="scope-tag">{c.scope}</span>
            </div>
          ))}
        </div>
      );
    }
    // What the turn changed — the receipt auto mode trades approval cards
    // for. Deliberately not a summary anyone wrote: every row is one tool
    // call that ran and returned, so the list is countable rather than
    // agreeable. Rows past the eighth fold, because a long unattended run is
    // exactly when the count matters more than the detail.
    if (e.kind === "changes") {
      const open = openRuns.has(i);
      const shown = open ? e.items : e.items.slice(0, 8);
      const tally = [];
      for (const it of e.items) {
        const at = tally.find((t) => t.verb === it.verb);
        if (at) at.n += 1; else tally.push({ verb: it.verb, n: 1 });
      }
      return (
        <div key={i} className="changes-card">
          <div className="changes-head">
            <Icon name="checkCircle" size={12} />
            <span className="changes-count">
              {e.items.length} change{e.items.length === 1 ? "" : "s"}
            </span>
            <span className="changes-tally">
              {tally.map((t) => `${t.n} ${t.verb}`).join(" · ")}
            </span>
          </div>
          {shown.map((it, j) => (
            <div key={j} className={`change-row ${CHANGE_TONE[it.verb] || "made"}`}>
              <span className="change-verb">{it.verb}</span>
              <span className="change-scope">{it.scope}</span>
              <span className="change-detail" title={it.tool}>{it.detail}</span>
            </div>
          ))}
          {e.items.length > 8 && (
            <button className="note-more" onClick={() => toggleRun(i)}>
              {open ? "show fewer" : `+${e.items.length - 8} more`}
            </button>
          )}
        </div>
      );
    }
    if (e.kind === "diff")
      return <DiffCard key={i} files={e.files}
                       onOpen={(files, path) => openDiff({ turn: e, path })} />;
    if (e.kind === "handoff")
      return <p key={i} className="entry status handoff">↗ {e.text}</p>;
    if (e.kind === "wake")
      return <p key={i} className="entry status handoff">↻ {e.text}</p>;
    if (e.kind === "prwatch")
      return (
        <div key={i} className="entry report pr-note">
          <p className="entry status handoff">
            <Icon name="pullRequest" size={11} /> PR #{e.number}
            <a className="report-open" href={e.url} target="_blank" rel="noopener noreferrer">open</a>
          </p>
          <ul className="pr-notes">{e.notes.map((n, j) => <li key={j}>{n}</li>)}</ul>
        </div>
      );
    if (e.kind === "report")
      return (
        <div key={i} className="entry report">
          <p className="entry status handoff">
            ↩ {e.label} tab · {e.status}
            {onShowTab && <button className="report-open" onClick={() => onShowTab(e.from)}>open tab</button>}
          </p>
          {e.text && <div className="report-text">{renderMarkdownCached(e.text.length > 600 ? `${e.text.slice(0, 600)}…` : e.text)}</div>}
        </div>
      );
    if (modalCard && e.requestId === modalCard.requestId)
      return <p key={i} className="entry status card-away">Waiting on your answer — the card is open in the middle of the screen.</p>;
    // Live only where you can answer it: the keys are the window's, so a card
    // in a hidden tab mustn't take them.
    if (e.kind === "perm")
      return <PermCard key={i} entry={e} active={awaiting === e.requestId && active}
                       onAnswer={answerPermission} />;
    if (e.kind === "ask")
      return <AskCard key={i} entry={e} active={awaiting === e.requestId && active}
                      onAnswer={answerQuestion} />;
    return <p key={i} className={`entry ${e.kind}`}>{e.text}</p>;
  }

  function renderComposer() {
    return (
      <div className={`composer${input.startsWith("!") ? " shell-mode" : ""}`}>
        <TaskPanel tasks={tasks} />
        {queued.length > 0 && (
          <div className="queued-list" aria-label="Queued messages">
            {queued.map((q) => (
              <div key={q.id} className="queued-row"
                   title={q.local ? "Runs when this turn ends" : "Goes in when the current step finishes"}>
                {q.local ? "after this turn" : "steering"}: {q.text}
              </div>
            ))}
          </div>
        )}
        {/* Held for their own turns, in order (#92). Click one to edit it;
            ↑↓ reorder; "steer now" hands it in mid-turn (idle, "send now"
            makes it the next turn); × drops it. The server holds the list. */}
        {holding.length > 0 && (
          <ol className="held-list" aria-label="Held for the next turns">
            {holding.map((h, i) => (
              <li key={h.id} className="held-row">
                <span className="held-n">{i + 1}</span>
                {holdEdit?.id === h.id ? (
                  <input className="held-edit" value={holdEdit.text} autoFocus
                    onChange={(e) => setHoldEdit({ id: h.id, text: e.target.value })}
                    onBlur={() => setHoldEdit(null)}
                    onKeyDown={(e) => {
                      if (e.key === "Enter" && holdEdit.text.trim()) {
                        e.preventDefault();
                        holdSend({ type: "held_edit", id: h.id, text: holdEdit.text.trim() });
                        setHoldEdit(null);
                      }
                      if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); setHoldEdit(null); }
                    }} />
                ) : (
                  <button type="button" className="held-text" title="Edit it (Enter saves, Esc leaves it)"
                    onClick={() => setHoldEdit({ id: h.id, text: h.text })}>
                    {h.text}
                    {h.pills?.length > 0 && <span className="held-pills">+{h.pills.length}</span>}
                  </button>
                )}
                <span className="held-acts">
                  <button type="button" className="held-btn" aria-label="Move up" disabled={i === 0}
                    onClick={() => holdSend({ type: "held_move", id: h.id, to: i - 1 })}><Icon name="arrowUp" size={10} /></button>
                  <button type="button" className="held-btn down" aria-label="Move down" disabled={i === holding.length - 1}
                    onClick={() => holdSend({ type: "held_move", id: h.id, to: i + 1 })}><Icon name="arrowUp" size={10} /></button>
                  <button type="button" className="held-btn word" disabled={!connected}
                    title={busy ? "Hand it in now: read when the current step finishes" : "Send it now as the next turn"}
                    onClick={() => holdSend({ type: "held_steer", id: h.id })}>{busy ? "steer now" : "send now"}</button>
                  <button type="button" className="held-btn" aria-label="Back to the prompt" title="Back to the prompt"
                    onClick={() => holdTakeBack(h)}><Icon name="undo" size={10} /></button>
                  <button type="button" className="held-btn" aria-label="Drop it" title="Drop it"
                    onClick={() => holdSend({ type: "held_drop", id: h.id })}><Icon name="close" size={10} /></button>
                </span>
              </li>
            ))}
          </ol>
        )}
        {histHits.length > 0 && (
          <div className="cmd-popup" ref={acListRef} role="listbox" aria-label="Prompt history">
            {histHits.map((text, i) => (
              <button key={text} type="button" className={`cmd-row ${i === acSel ? "selected" : ""}`}
                onMouseDown={(e) => e.preventDefault()}
                onClick={() => { setInput(text); setHistSearch(false); }}>
                <span className="cmd-desc">{text}</span>
              </button>
            ))}
          </div>
        )}
        {histHits.length === 0 && fileHits.length > 0 && (
          <div className="cmd-popup" ref={acListRef} role="listbox">
            {fileHits.map((hit, i) => (
              <button key={hit.key} type="button" className={`cmd-row at-${hit.kind} ${i === acSel ? "selected" : ""}`}
                onMouseDown={(e) => e.preventDefault()} onClick={() => completeFile(hit)}
                title={hit.kind === "file" ? undefined : `Attach a digest of this ${hit.kind} (what was asked, files changed, its last reply)`}>
                {hit.kind === "file" ? <span className="cmd-name">@{hit.path}</span> : (
                  <>
                    <span className="cmd-name">
                      <Icon name={hit.kind === "tab" ? "message" : "clock"} size={11} />
                      {clipText(hit.label, 60)}
                    </span>
                    <span className="cmd-desc">
                      {hit.kind === "tab" ? `tab · ${hit.tag}` : `session · ${agoShort(hit.when)} ago`}
                    </span>
                  </>
                )}
              </button>
            ))}
          </div>
        )}
        {histHits.length === 0 && fileHits.length === 0 && acMatches.length > 0 && (
          <div className="cmd-popup" ref={acListRef} role="listbox">
            {acMatches.map((c, i) => (
              <button
                key={c.name}
                type="button"
                className={`cmd-row ${i === acSel ? "selected" : ""}`}
                onMouseDown={(e) => e.preventDefault()}
                onClick={() => completeCommand(c.name)}
              >
                <span className="cmd-name">/{c.name}</span>
                {c.argument_hint && <span className="cmd-hint">{c.argument_hint}</span>}
                {c.description && <span className="cmd-desc">{c.description}</span>}
              </button>
            ))}
          </div>
        )}
        {(pills.length > 0 || comments.length > 0) && (
          <div className="pills">
            {comments.map((c) => (
              <span key={c.id} className="pill pill-comment" title={`${c.body}\n\nGoes with your next message`}>
                <button type="button" onClick={() => openDiff({ path: c.path })}>
                  <Icon name="message" size={10} />{chip(c)}
                  <span className="pill-note">{c.body}</span>
                </button>
                <button type="button" aria-label="Drop this comment"
                  onClick={() => setComments(comments.filter((x) => x.id !== c.id))}>
                  <Icon name="close" size={10} /></button>
              </span>
            ))}
            {pills.map((p) => (
              <button
                key={p.id}
                className={`pill${p.kind === "file" || p.kind === "session" ? " pill-file" : ""}${p.pending ? " pending" : ""}`}
                title={p.note || undefined}
                onClick={() => onRemovePill(p.id)}
              >
                {p.kind === "file" && <Icon name="paperclip" size={10} />}
                {p.kind === "session" && <Icon name="message" size={10} />}
                {p.label}
                {/* the note is what the model will actually see of an
                    unreadable file, so it belongs on the chip, not in a
                    tooltip the user has to go looking for */}
                {p.kind === "file" && !p.data && p.note && (
                  <span className="pill-note">{p.note}</span>
                )}
                <Icon name="close" size={10} />
              </button>
            ))}
          </div>
        )}
        {tray && (
          <div className="desk-tray" role="listbox" aria-label="Desktop files">
            <div className="desk-tray-head">
              Desktop — newest first
              <button type="button" className="desk-tray-x" onClick={() => setTray(null)}
                      aria-label="Close"><Icon name="close" size={10} /></button>
            </div>
            {tray === "empty" && <p className="status">Nothing on the Desktop.</p>}
            {Array.isArray(tray) && tray.length === 0 && <p className="status pulse">looking…</p>}
            {Array.isArray(tray) && tray.length > 0 && (
              <div className="desk-grid">
                {tray.map((f) => (
                  <button key={f.name} type="button"
                    className={`desk-item${seenDesk.has(f.name) ? " seen" : ""}`}
                    title={`${f.name}\n${deskAge(f.mtime)}`}
                    onClick={() => { ingestDesk(f.name); setTray(null); }}>
                    {f.image
                      ? <img src={`/api/console/desktop/preview?name=${encodeURIComponent(f.name)}`}
                             alt="" loading="lazy" />
                      : <span className="desk-ext">{(f.name.split(".").pop() || "file").slice(0, 4)}</span>}
                    <span className="desk-name">{f.name}</span>
                    <span className="desk-age">{deskAge(f.mtime)}</span>
                  </button>
                ))}
              </div>
            )}
          </div>
        )}
        {fresh && !tray && (
          <div className="desk-fresh">
            <button type="button" className="desk-fresh-add" onClick={() => ingestDesk(fresh.name)}
                    title={fresh.name}>
              <img src={`/api/console/desktop/preview?name=${encodeURIComponent(fresh.name)}`} alt="" />
              <span>Attach the screenshot from {deskAge(fresh.mtime)}</span>
            </button>
            <button type="button" className="desk-tray-x" onClick={dismissFresh}
                    aria-label="Dismiss"><Icon name="close" size={10} /></button>
          </div>
        )}
        {voiceNote && <div className="voice-note">{voiceNote}</div>}
        {/* The prompt as it will read, drawn only while it holds a formula:
            the textarea keeps the $…$ source, this shows what it says, and a
            formula clicked here opens in the math field again. */}
        {composerMath && (
          <div className="composer-preview" aria-label="Your message with its formulas drawn">
            {renderUserMath(input, (span) => openMath(span), mathMadeRef.current)}
          </div>
        )}
        {mathOpen && (
          <div className="composer-math">
            <MathInput key={mathEdit ? `e${mathEdit.start}:${mathEdit.tex}` : "new"} autoFocus
              label="Formula" value={mathEdit?.tex || ""}
              onChange={(latex) => { mathRef.current = latex; }}
              onEnter={insertMath} onEscape={() => closeMath()} />
            <span className="composer-math-hint">
              {mathEdit ? "Enter saves · empty removes · Esc cancels" : "Enter adds it · Esc cancels"}
            </span>
          </div>
        )}
        <div className="composer-row">
          {input.startsWith("!") && <span className="composer-mode">shell</span>}
          <textarea
            ref={inputRef}
            value={input}
            placeholder={!connected ? "Connecting…"
              : awaitingPerm ? "Type to decline that call and tell Claude what to do instead…"
              : suggestion ? `${suggestion} (Tab to use)`
              : `Message ${projectName || (profile === "study" ? "your study tab" : "the vault")}… (? for shortcuts)`}
            onChange={(e) => { setInput(e.target.value); setAcIndex(0); setAcDismissed(false); setHistAt(null); }}
            onKeyDown={(e) => {
              const ctrl = e.ctrlKey && !e.metaKey;
              if (ctrl && e.key === "r") { e.preventDefault(); setHistSearch((s) => !s); setAcIndex(0); return; }
              if (ctrl && e.key === "m") { e.preventDefault(); openMath(); return; }
              if (ctrl && e.key === "s") {
                // Stash a half-written prompt, and bring it back on an empty one.
                e.preventDefault();
                if (input) { stashRef.current = input; setInput(""); }
                else if (stashRef.current) { setInput(stashRef.current); stashRef.current = ""; }
                return;
              }
              if (ctrl && e.key === "b" && shellLive) {
                e.preventDefault();
                wsRef.current?.send(JSON.stringify({ type: "shell_background" }));
                return;
              }
              if (ctrl && e.key === "Enter") { e.preventDefault(); sendNow(); return; }
              if (e.metaKey && !e.ctrlKey && e.key === "Enter") { e.preventDefault(); sendElsewhere(); return; }
              // Shift+Tab switches the permission mode only on an empty composer
              // (the user, 11-22); with something typed it's plain Shift+Tab, focus
              // moving back, which every other control in the app now gets.
              // ⌥⇧Tab is always WebKit's move-focus-back.
              if (e.key === "Tab" && e.shiftKey && (input || e.altKey)) return;
              if (e.key === "Tab" && e.shiftKey) {
                e.preventDefault();
                // Mid-turn too: the switch is live and leaves the turn running.
                // Never onto "never ask": its confirm is no place for a stray key.
                const cycle = MODES.filter((m) => m.value !== "bypass" || m.value === mode);
                const at = cycle.findIndex((m) => m.value === mode);
                changeMode(cycle[(at + 1) % cycle.length].value);
                return;
              }
              if (suggestion && !input && (e.key === "Tab" || e.key === "ArrowRight")) {
                e.preventDefault();
                setInput(suggestion);
                setSuggestion("");
                return;
              }
              if (histHits.length > 0) {
                if (e.key === "ArrowDown") { e.preventDefault(); setAcIndex((acSel + 1) % acCount); return; }
                if (e.key === "ArrowUp") { e.preventDefault(); setAcIndex((acSel - 1 + acCount) % acCount); return; }
                if (e.key === "Enter" || e.key === "Tab") {
                  e.preventDefault(); setInput(histHits[acSel]); setHistSearch(false); return;
                }
              }
              if (e.key === "Escape" && histSearch) { e.preventDefault(); setHistSearch(false); return; }
              if (acCount > 0) {
                if (e.key === "ArrowDown") { e.preventDefault(); setAcIndex((acSel + 1) % acCount); return; }
                if (e.key === "ArrowUp") { e.preventDefault(); setAcIndex((acSel - 1 + acCount) % acCount); return; }
                if (fileHits.length > 0 && (e.key === "Tab" || e.key === "Enter")) {
                  e.preventDefault(); completeFile(fileHits[acSel]); return;
                }
                if (e.key === "Tab") { e.preventDefault(); completeCommand(acMatches[acSel].name); return; }
                if (e.key === "Escape") { e.preventDefault(); setAcDismissed(true); return; }
              }
              if (e.key === "Escape") {
                const now = performance.now();
                if (now - lastEscRef.current < 500) { lastEscRef.current = 0; doubleEscape(); }
                else lastEscRef.current = now;
                return;
              }
              // Up from an empty prompt takes back what's queued, then history.
              // Only from the top line, so it still moves through a long prompt.
              if (e.key === "ArrowUp" && e.target.selectionStart === 0 || (e.key === "ArrowUp" && histAt !== null)) {
                // The newest held one first: it's last in line.
                if (!input && holding.length) {
                  e.preventDefault();
                  holdTakeBack(holding[holding.length - 1]);
                  return;
                }
                if (!input && queued.length) {
                  e.preventDefault();
                  const last = queued[queued.length - 1];
                  // A sent one comes back only if the CLI hasn't read it
                  // (the backend's `unqueued` answer); a held command is ours.
                  if (last.local || wsRef.current?.readyState !== WebSocket.OPEN) unqueued([last.id]);
                  else wsRef.current.send(JSON.stringify({ type: "unqueue", id: last.id }));
                  return;
                }
                if (recall(-1)) { e.preventDefault(); return; }
              }
              if (e.key === "ArrowDown" && histAt !== null) { e.preventDefault(); recall(1); return; }
              // `?` on an empty prompt is the shortcuts, as in the CLI.
              if (e.key === "?" && !input) {
                e.preventDefault();
                setEntries((p) => [...p, { kind: "help" }]);
                return;
              }
              if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault();
                // `\` then Enter: a new line, for a keyboard that hasn't got Shift to hand.
                if (input.endsWith("\\")) setInput(`${input.slice(0, -1)}\n`);
                else send(e.altKey);
              }
            }}
            onPaste={onPaste}
            rows={1}
          />
          {profile === "study" && (
            <button className={`btn mic-btn math-btn${mathOpen ? " on" : ""}`}
              onClick={() => (mathOpen ? closeMath() : openMath())}
              title="Type a formula (Ctrl+M)" aria-label="Type a formula">ƒx</button>
          )}
          <button className={`btn mic-btn${tray ? " on" : ""}`} onClick={toggleTray}
            title="Attach from the Desktop — or paste (⌘V) or drop a file here"
            aria-label="Attach from the Desktop">
            <Icon name="paperclip" size={14} />
          </button>
          {/* The bar's "new" isn't on screen in the plan view, which draws
              only this composer — so the composer carries its own. Not
              mid-turn: reconnecting would drop the reply being written. Hidden
              while nothing has been said — it's already a new chat. */}
          {!idle && (
            <button className="btn mic-btn" onClick={newSession} disabled={busy}
              title={busy ? "Stop the reply first" : "New chat"}
              aria-label="New chat">
              <Icon name="plus" size={14} />
            </button>
          )}
          {voiceCap?.stt && (
            <button
              className={`btn mic-btn${micState !== "off" ? ` ${micState}` : ""}`}
              onPointerDown={(e) => { e.preventDefault(); talkStart(); }}
              onPointerUp={talkEnd}
              onPointerLeave={talkEnd}
              onPointerCancel={talkEnd}
              title="Hold to talk (or hold ⌃⇧Space) — the transcript lands here to read before it sends"
            >
              <Icon name="mic" size={14} />
            </button>
          )}
          {busy ? (
            <>
              {awaitingPerm && input.trim()
                ? <button className="btn" onClick={() => send()} title="Decline the waiting call, with this as what to do instead (Enter)">Decline &amp; redirect</button>
                : (input.trim() || comments.length > 0) && (enterMode === "queue"
                  ? <button className="btn" onClick={() => send()} title="Hold this for its own turn when this one ends (⌥Enter steers it in instead)">Queue</button>
                  : <button className="btn" onClick={() => send()} title="Send this in when the current step finishes (⌥Enter queues it for after the turn; Ctrl+Enter stops and sends now)">Steer</button>)}
              <button className="btn stop" onClick={stopTurn}>Stop</button>
            </>
          ) : (
            <button className="btn send-btn" onClick={() => send()} aria-label="Send"
                    disabled={!connected || (!input.trim() && !comments.length) || reading}
                    title={reading ? "still reading a dropped file" : "Send (Enter)"}>
              <Icon name="arrowUp" size={14} />
            </button>
          )}
        </div>
      </div>
    );
  }
}

// What the app mounts. The app re-renders whenever any card's fetch starts or
// lands (widgets/cards.js runs there), and none of that concerns the console.
// Its props are all stable callbacks plus `pills`, so memo keeps a long
// transcript from re-rendering on every one.
export const ConsolePane = memo(Console);
