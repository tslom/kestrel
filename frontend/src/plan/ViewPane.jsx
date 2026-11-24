import { Suspense, lazy, useEffect, useRef, useState } from "react";
import { useDebounce } from "../lib/timers.js";
import Icon from "../ui/icons.jsx";
import { Lightbox } from "../ui/DocPage.jsx";
import { fetchJSON, postJSON } from "../lib/api.js";
import { stopShell } from "../lib/turn.js";
import { renderMarkdown } from "../ui/markdown.jsx";

// xterm loads only for a computer-use job; the load must not reject (see ui/markdown.jsx KaTeX).
const TerminalPane = lazy(() => import("../console/TerminalPane.jsx").catch(() => ({
  default: () => <p className="vp-empty">The terminal didn’t load. Reload (⌘R) to open it.</p>,
})));

// Claude's view: takes the map's place (the map stays mounted behind). Tabs:
//   seen (images tools handed the model this turn; newest unless picked) / preview (dev tab's running app)
//   / terminal (computer-use job, forward when it needs an answer) / files (under the tab's folder)
//   / plan (last proposed) / tasks (`!` background commands and subagents).

const LOCAL = /^https?:\/\/(localhost|127\.0\.0\.1)(:\d+)?(\/|$)/;
const saved = (project) => `preview:${project}`;
const label = (url) => url.replace(/^https?:\/\//, "");

function Seen({ views }) {
  const [picked, setPicked] = useState(null);       // a src, or null for the newest
  const [zoom, setZoom] = useState(false);
  const view = views.find((v) => v.src === picked) || views.at(-1);
  if (!view) {
    return <p className="vp-empty">Nothing yet this turn. Images Claude reads or screenshots it takes show up here.</p>;
  }
  return (
    <>
      <button className="vp-shot" onClick={() => setZoom(true)} title="Enlarge">
        <img src={view.src} alt={view.detail || "What Claude is looking at"} />
      </button>
      {view.detail && <div className="vp-caption">{view.detail}</div>}
      {views.length > 1 && (
        <div className="vp-strip">
          {views.map((v, i) => (
            <button key={i} className={v === view ? "on" : ""} onClick={() => setPicked(v.src)}
                    title={v.detail || undefined} aria-label={`Image ${i + 1} of ${views.length}`}>
              <img src={v.src} alt="" />
            </button>
          ))}
        </div>
      )}
      {zoom && (
        <Lightbox onClose={() => setZoom(false)}>
          <img className="vp-full" src={view.src} alt={view.detail || "What Claude is looking at"} />
        </Lightbox>
      )}
    </>
  );
}

function Files({ project, worktree, edited }) {
  const [query, setQuery] = useState("");
  const [hits, setHits] = useState([]);
  const [file, setFile] = useState(null);       // { path, content, mtime, read_only }
  const [draft, setDraft] = useState("");
  const [note, setNote] = useState("");
  // A dev tab in its own worktree reads and saves there, not in the main checkout.
  const scope = (project ? `&project=${encodeURIComponent(project)}` : "")
    + (project && worktree ? `&worktree=${encodeURIComponent(worktree)}` : "");

  useDebounce((stale) => {
    if (!query.trim()) { setHits([]); return; }
    fetchJSON(`/api/console/files?q=${encodeURIComponent(query)}${scope}`)
      .then((r) => { if (!stale()) setHits(r.files); })
      .catch(() => { if (!stale()) setHits([]); });
  }, query.trim() ? 120 : 0, [query, scope]);

  async function open(path) {
    setNote("");
    try {
      const f = await fetchJSON(`/api/console/file?path=${encodeURIComponent(path)}${scope}`);
      setFile(f); setDraft(f.content); setQuery(""); setHits([]);
    } catch (err) { setNote(err.message); }
  }

  async function save() {
    try {
      const { mtime } = await postJSON("/api/console/file",
        { project, worktree, path: file.path, content: draft, mtime: file.mtime }, "PUT");
      setFile({ ...file, content: draft, mtime });
      setNote("Saved.");
    } catch (err) { setNote(err.message); }
  }

  const dirty = file && draft !== file.content;
  return (
    <>
      <div className="vp-bar">
        <input value={query} onChange={(e) => setQuery(e.target.value)} spellCheck={false}
               placeholder="Find a file" aria-label="Find a file" />
        {file && !file.read_only && (
          <button type="button" className="vp-btn" onClick={save} disabled={!dirty}>Save</button>
        )}
      </div>
      {(query ? hits : file ? [] : edited).map((p) => (
        <button key={p} className="vp-file" onClick={() => open(p)} title={p}>{p.split("/").slice(-2).join("/")}</button>
      ))}
      {note && <p className="vp-note">{note}</p>}
      {file && !query && (
        <>
          <div className="vp-caption">{file.path}{file.read_only ? " (generated, read only)" : ""}</div>
          <textarea className="vp-editor" value={draft} readOnly={file.read_only} spellCheck={false}
                    aria-label={`Contents of ${file.path}`} onChange={(e) => setDraft(e.target.value)} />
        </>
      )}
      {!file && !query && !edited.length && <p className="vp-empty">Files Claude changes show up here.</p>}
    </>
  );
}

// A project.json script's run (backend: console_project.py), newest last.
function Run({ run }) {
  const [err, setErr] = useState(null);
  const stop = () => postJSON("/api/console/project/stop", { id: run.id }).catch((e) => setErr(e.message));
  return (
    <div className="vp-task">
      <div className="vp-task-head">
        <b>{run.name}</b> <code>{run.command}</code>
        <span>{run.running ? `running · :${run.port}` : `exit ${run.code}`}</span>
        {run.running && <button className="vp-btn" onClick={stop}>Stop</button>}
        {err && <span className="vp-err">{err}</span>}
      </div>
      {run.output && <pre className="vp-task-out">{run.output.trimEnd().split("\n").slice(-40).join("\n")}</pre>}
    </div>
  );
}

function Tasks({ background, subagents, runs }) {
  return (
    <div className="vp-tasks">
      {[...runs].reverse().map((r) => <Run key={r.id} run={r} />)}
      {subagents.map((s) => (
        <div key={s.id} className="vp-task">
          <div className="vp-task-head">
            <b>Subagent</b> {s.detail}
            {s.status && <span>{s.status === "running" ? (s.last_tool || "running") : s.status}</span>}
          </div>
          {s.summary && <pre className="vp-task-out">{s.summary}</pre>}
        </div>
      ))}
      {background.map((s) => (
        <div key={s.id} className="vp-task">
          <div className="vp-task-head">
            <code>{s.command}</code>
            <span>{s.running ? "running" : `exit ${s.code}`}</span>
            {s.running && <button className="vp-btn" onClick={() => stopShell(s.id)}>Stop</button>}
          </div>
          {s.output && <pre className="vp-task-out">{s.output}</pre>}
        </div>
      ))}
      {!subagents.length && !background.length && !runs.length && <p className="vp-empty">Nothing running in the background.</p>}
    </div>
  );
}

// Addresses this project was previewed at, newest first (address suggestions and chips).
const recentKey = (project) => `preview-recent:${project}`;
const originOf = (u) => { try { return new URL(u).origin; } catch { return u; } };
const MAX_RECENT = 8;
function readRecent(project) {
  try { return JSON.parse(localStorage.getItem(recentKey(project)) || "[]"); } catch { return []; }
}

// The running app in a frame. Another origin, so its history can't be driven: back/forward walk the
// addresses gone to through this bar. Opens at: `ask`, else this tree's previewUrl from
// .kestrel/project.json (own port, so worktrees don't show each other's server), else the last
// address used here, else the project's first listener.
function Preview({ project, worktree, start, ask }) {
  const tree = worktree || project;
  const first = ask?.url || start || localStorage.getItem(saved(tree)) || "";
  const [hist, setHist] = useState(() => ({ stack: first ? [first] : [], at: first ? 0 : -1 }));
  const url = hist.stack[hist.at] || "";
  const [draft, setDraft] = useState(url);
  const [nonce, setNonce] = useState(0);
  const [servers, setServers] = useState([]);
  const [recent, setRecent] = useState(() => readRecent(project));
  const [verify, setVerify] = useState(() => localStorage.getItem(`autoverify:${project}`) === "1");
  function toggleVerify(on) {
    setVerify(on);
    try { localStorage.setItem(`autoverify:${project}`, on ? "1" : "0"); } catch { /* per-viewer nicety */ }
  }

  function remember(next) {
    const list = [next, ...readRecent(project).filter((u) => u !== next)].slice(0, MAX_RECENT);
    setRecent(list);
    try {
      localStorage.setItem(recentKey(project), JSON.stringify(list));
      localStorage.setItem(saved(tree), next);
      // What the console's auto-verify reads (Console.jsx messagePayload).
      localStorage.setItem(saved(project), next);
    } catch { /* per-viewer nicety */ }
  }
  function go(next) {
    if (next === url) { setNonce((n) => n + 1); return; }
    setHist((h) => ({ stack: [...h.stack.slice(0, h.at + 1), next], at: h.at + 1 }));
    setDraft(next);
    remember(next);
  }
  function step(by) {
    const at = hist.at + by;
    if (at < 0 || at >= hist.stack.length) return;
    setHist({ ...hist, at });
    setDraft(hist.stack[at]);
  }

  // A new ask while open (react.dev: adjusting state when a prop changes).
  const [askN, setAskN] = useState(ask?.n || 0);
  if ((ask?.n || 0) !== askN) {
    setAskN(ask?.n || 0);
    if (ask?.url) go(ask.url);
  }

  useEffect(() => {
    fetchJSON(`/api/console/preview?project=${encodeURIComponent(project)}`)
      .then((r) => {
        setServers(r.servers);
        if (!first && r.servers[0]) go(r.servers[0].url);
      })
      .catch(() => setServers([]));
    // eslint-disable-next-line react-hooks/exhaustive-deps -- once per project; go is a render-local helper and only applies the first server
  }, [project]);

  // Same origin is this very app: framed it would reconnect this window's saved sessions from a second place.
  const itself = url && new URL(url, location.href).origin === location.origin;
  const listening = new Set(servers.map((s) => s.url.replace(/\/$/, "")));
  // One chip per address (`:3000` = `:3000/`); the last-used form beats a listener's bare origin.
  const bare = (u) => u.replace(/\/$/, "");
  const chips = [...(start ? [start] : []), ...recent, ...servers.map((s) => s.url)]
    .filter((u, i, all) => all.findIndex((v) => bare(v) === bare(u)) === i).slice(0, 6);
  return (
    <>
      <form className="vp-bar" onSubmit={(e) => {
        e.preventDefault();
        const next = draft.trim();
        if (LOCAL.test(next)) go(next);
      }}>
        <button type="button" className="vp-btn vp-nav" title="Back" aria-label="Back"
                disabled={hist.at <= 0} onClick={() => step(-1)}>
          <Icon name="arrowLeft" size={12} />
        </button>
        <button type="button" className="vp-btn vp-nav" title="Forward" aria-label="Forward"
                disabled={hist.at >= hist.stack.length - 1} onClick={() => step(1)}>
          <Icon name="arrowRight" size={12} />
        </button>
        <button type="button" className="vp-btn vp-nav" title="Reload" aria-label="Reload"
                disabled={!url || itself} onClick={() => setNonce((n) => n + 1)}>
          <Icon name="refresh" size={12} />
        </button>
        <input value={draft} onChange={(e) => setDraft(e.target.value)} spellCheck={false}
               list={`vp-recent-${tree}`} placeholder="http://localhost:3000" aria-label="Preview address" />
        <datalist id={`vp-recent-${tree}`}>
          {recent.map((u) => <option key={u} value={u} />)}
        </datalist>
        {url && !itself && (
          <a className="vp-btn vp-nav" href={url} target="_blank" rel="noopener noreferrer"
             title="Open in the browser" aria-label="Open in the browser">
            <Icon name="external" size={12} />
          </a>
        )}
      </form>
      <label className="vp-verify" title="Claude opens this in Chrome after changing what it shows. Uses more of your limit.">
        <input type="checkbox" checked={verify} disabled={!url || itself}
               onChange={(e) => toggleVerify(e.target.checked)} />
        Have Claude check this after its edits
      </label>
      {chips.length > 0 && (
        <div className="vp-servers">
          {chips.map((u) => {
            const live = listening.has(originOf(u));
            return (
              <button key={u} className={`vp-btn${u === url ? " on" : ""}${live ? " live" : ""}`} onClick={() => go(u)}
                      title={live ? `${u} — listening from this project` : `${u} — used before`}>
                {label(u)}
              </button>
            );
          })}
        </div>
      )}
      {itself ? <p className="vp-empty">That’s Kestrel itself, which is already this window.</p>
        : url ? <iframe key={`${hist.at}:${nonce}`} className="vp-frame" src={url} title="Preview"
                        sandbox="allow-scripts allow-same-origin allow-forms allow-popups" />
          : <p className="vp-empty">
              Nothing from this project is listening yet. Start its dev server, or
              type a localhost address above.
            </p>}
    </>
  );
}

export default function ViewPane({ views, project, worktree, job, plan, edited, background, subagents,
  preview = null, previewUrl = null, runs = [], openOn = null, onClose }) {
  // Forward while the job wants an answer, back to screenshots after; only for a job. Adjusted in render on id/ask change.
  const [tab, setTab] = useState(job?.show ? "terminal" : openOn || "seen");
  const jobSig = `${job?.id ?? ""}|${job?.show ? 1 : 0}`;
  const [lastJob, setLastJob] = useState({ sig: jobSig, had: !!job });
  if (lastJob.sig !== jobSig) {
    setLastJob({ sig: jobSig, had: !!job });
    if (job?.show) setTab("terminal");
    else if (lastJob.had) setTab("seen");
  }
  // An ask for the preview (ProjectBar.jsx) turns the pane to it.
  const [lastAsk, setLastAsk] = useState(preview?.n || 0);
  if ((preview?.n || 0) !== lastAsk) {
    setLastAsk(preview?.n || 0);
    if (preview?.url) setTab("preview");
  }
  const showing = (tab === "preview" && !project) || (tab === "terminal" && !job)
    || (tab === "plan" && !plan) ? "seen" : tab;
  const working = background.filter((s) => s.running).length + subagents.length
    + runs.filter((r) => r.running).length;
  return (
    <section className="view-pane" aria-label="Claude's view">
      <header className="vp-head">
        <button className={`vp-tab${showing === "seen" ? " on" : ""}`} onClick={() => setTab("seen")}>
          Claude’s view
        </button>
        {project && (
          <button className={`vp-tab${showing === "preview" ? " on" : ""}`} onClick={() => setTab("preview")}>
            Preview
          </button>
        )}
        {job && (
          <button className={`vp-tab${showing === "terminal" ? " on" : ""}`} onClick={() => setTab("terminal")}>
            Terminal
          </button>
        )}
        <button className={`vp-tab${showing === "files" ? " on" : ""}`} onClick={() => setTab("files")}>Files</button>
        {plan && (
          <button className={`vp-tab${showing === "plan" ? " on" : ""}`} onClick={() => setTab("plan")}>Plan</button>
        )}
        <button className={`vp-tab${showing === "tasks" ? " on" : ""}`} onClick={() => setTab("tasks")}>
          Tasks{working ? ` (${working})` : ""}
        </button>
        <button className="vp-x" onClick={onClose} title="Back to the map" aria-label="Back to the map">
          <Icon name="close" size={12} />
        </button>
      </header>
      <div className="vp-body">
        {showing === "seen" && <Seen views={views} />}
        {showing === "preview" && <Preview key={`${project}:${worktree}`} project={project}
                                           worktree={worktree} start={previewUrl} ask={preview} />}
        {showing === "files" && <Files key={`${project}:${worktree}`} project={project} worktree={worktree} edited={edited} />}
        {showing === "plan" && <div className="vp-plan">{renderMarkdown(plan)}</div>}
        {showing === "tasks" && <Tasks background={background} subagents={subagents} runs={runs} />}
        {showing === "terminal" && (
          <>
            {job.show && <p className="vp-ask">If Claude is asking something, answer here with the arrow keys and Enter.</p>}
            <Suspense fallback={null}><TerminalPane key={job.id} jobId={job.id} /></Suspense>
          </>
        )}
      </div>
    </section>
  );
}
