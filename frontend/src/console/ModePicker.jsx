import { useEffect, useMemo, useRef, useState } from "react";
import { fetchJSON, postJSON } from "../lib/api.js";

// What a new tab is for, chosen before it starts — a session's cwd, prompt
// and tools are fixed at start, so this is the one moment to choose them
// (backend: console_profiles.py). G / S / D pick from the keyboard; in the
// project list, ↑↓ and ↵, N names a new project (an empty git repo the tab
// then opens on), and M moves the selected one into a folder. A folder is
// grouping in app.db: the directory stays where it is. Esc closes the tab.

const MODES = [
  { id: "general", key: "g", name: "General", what: "The vault — notes, tasks, calendar, mail, everything else." },
  { id: "study", key: "s", name: "Study", what: "Tutoring on coursework: works it through with you instead of handing over answers." },
  { id: "dev", key: "d", name: "Dev", what: "Claude Code on a project: its CLAUDE.md, subagents, a diff of every turn." },
];

function ago(mtime) {
  if (!mtime) return "";
  const d = (Date.now() / 1000 - mtime) / 86400;
  return d < 1 ? "today" : d < 2 ? "yesterday" : d < 30 ? `${Math.round(d)}d ago` : "";
}

// Folders in the order the list already has them: the one holding the most
// recently touched project first, so the top row (and a bare ↵) is still the
// last thing you worked on. "" is the projects filed nowhere.
function grouped(projects) {
  const groups = new Map();
  for (const p of projects || []) {
    const folder = p.folder || "";
    if (!groups.has(folder)) groups.set(folder, []);
    groups.get(folder).push(p);
  }
  return [...groups].map(([folder, rows]) => ({ folder, rows }));
}

export default function ModePicker({ onPick, onCancel }) {
  const [dev, setDev] = useState(false);
  const [projects, setProjects] = useState(null);
  const [roots, setRoots] = useState([]);
  const [sel, setSel] = useState(0);
  // The new-project row: null = closed, else the name being typed.
  const [draft, setDraft] = useState(null);
  const [root, setRoot] = useState("");
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);
  // The move row: null = closed, else { path, name } and the folder typed.
  const [moving, setMoving] = useState(null);
  const [folder, setFolder] = useState("");
  const listRef = useRef(null);
  const rootRef = useRef(null);
  const pointerRef = useRef("");
  const groups = useMemo(() => grouped(projects), [projects]);
  // What ↑↓ walks: the rows in the order they're drawn.
  const rows = useMemo(() => groups.flatMap((g) => g.rows), [groups]);
  const folders = useMemo(() => groups.map((g) => g.folder).filter(Boolean).sort(), [groups]);

  useEffect(() => { rootRef.current?.focus(); }, []);
  // Choosing Dev is what asks for the projects, once per picker.
  function enterDev() {
    setDev(true);
    if (projects) return;
    fetchJSON("/api/console/projects")
      .then((d) => { setProjects(d.projects || []); setRoots(d.roots || []); setRoot((d.roots || [])[0] || ""); })
      .catch(() => setProjects([]));
  }

  function closeDraft() { setDraft(null); setErr(""); rootRef.current?.focus(); }

  async function create() {
    if (busy || !draft?.trim()) return;
    setBusy(true); setErr("");
    try {
      const p = await postJSON("/api/console/projects", { name: draft.trim(), root });
      onPick("dev", p.path);
    } catch (e) {
      setErr(e.message);
      setBusy(false);
    }
  }

  function onDraftKey(e) {
    e.stopPropagation();  // typing a name isn't G / S / D
    if (e.key === "Enter") { e.preventDefault(); create(); }
    else if (e.key === "Escape") { e.preventDefault(); closeDraft(); }
  }
  useEffect(() => {
    listRef.current?.querySelector(".mp-project.sel")?.scrollIntoView({ block: "nearest" });
  }, [sel]);

  // The selection follows the pointer only when the pointer moved: ↑↓
  // scrolling the list under a still pointer fires mouse events on whatever
  // row slid beneath it, which would drag the selection back.
  function followPointer(e, p) {
    const at = `${e.clientX},${e.clientY}`;
    const moved = pointerRef.current && pointerRef.current !== at;
    pointerRef.current = at;
    if (moved) setSel(rows.indexOf(p));
  }

  function openMove(p) {
    setMoving(p); setFolder(p.folder || ""); setErr("");
  }
  function closeMove() { setMoving(null); setErr(""); rootRef.current?.focus(); }

  async function move(to) {
    if (busy || !moving) return;
    setBusy(true); setErr("");
    try {
      const r = await postJSON("/api/console/projects/folder", { project: moving.path, folder: to });
      const next = projects.map((p) => (p.path === moving.path ? { ...p, folder: r.folder } : p));
      setProjects(next);
      // The row changed places; the selection goes with it.
      setSel(grouped(next).flatMap((g) => g.rows).findIndex((p) => p.path === moving.path));
      closeMove();
    } catch (e) {
      setErr(e.message);
    } finally {
      setBusy(false);
    }
  }

  function onMoveKey(e) {
    e.stopPropagation();  // typing a folder isn't G / S / D
    if (e.key === "Enter") { e.preventDefault(); move(folder); }
    else if (e.key === "Escape") { e.preventDefault(); closeMove(); }
  }

  function onKey(e) {
    if (e.metaKey || e.ctrlKey || e.altKey) return;
    if (e.key === "Escape") {
      e.preventDefault();
      if (dev) setDev(false); else onCancel();
      return;
    }
    if (dev && projects && e.key.toLowerCase() === "n") {
      e.preventDefault(); setDraft(""); return;
    }
    if (dev && rows.length) {
      if (e.key === "ArrowDown") { e.preventDefault(); setSel((i) => (i + 1) % rows.length); return; }
      if (e.key === "ArrowUp") { e.preventDefault(); setSel((i) => (i - 1 + rows.length) % rows.length); return; }
      if (e.key === "Enter") { e.preventDefault(); onPick("dev", rows[sel].path); return; }
      if (e.key.toLowerCase() === "m") { e.preventDefault(); openMove(rows[sel]); return; }
    }
    const m = MODES.find((x) => x.key === e.key.toLowerCase());
    if (!m) return;
    e.preventDefault();
    if (m.id === "dev") enterDev(); else onPick(m.id);
  }

  return (
    <div className="mode-picker" tabIndex={-1} ref={rootRef} onKeyDown={onKey}>
      <div className="mp-head">New tab — what is it for?</div>
      <div className="mp-modes">
        {MODES.map((m) => (
          <button key={m.id} className={`mp-mode${dev && m.id === "dev" ? " on" : ""}`}
            onClick={() => (m.id === "dev" ? enterDev() : onPick(m.id))}>
            <span className="mp-name"><kbd>{m.key.toUpperCase()}</kbd> {m.name}</span>
            <span className="mp-what">{m.what}</span>
          </button>
        ))}
      </div>
      {dev && (
        <div className="mp-projects">
          <div className="mp-sub">Which project? <span className="mp-hint">↑↓ ↵ · n new · m move to folder · esc back</span></div>
          {projects === null && <p className="status pulse">looking…</p>}
          {projects && draft === null && (
            <button className="mp-project mp-new" onClick={() => setDraft("")}>
              <span className="mp-pname"><kbd>N</kbd> New project</span>
              <span className="mp-pmeta">empty git repo</span>
            </button>
          )}
          {draft !== null && (
            <div className="mp-create">
              <input autoFocus value={draft} placeholder="project-name" spellCheck={false}
                readOnly={busy} onChange={(e) => { setDraft(e.target.value); setErr(""); }}
                onKeyDown={onDraftKey} aria-label="New project name" />
              {roots.length > 1 && (
                <select value={root} onChange={(e) => setRoot(e.target.value)} onKeyDown={onDraftKey}
                  disabled={busy} aria-label="Project root">
                  {roots.map((r) => <option key={r} value={r}>{r.replace(/^\/Users\/[^/]+/, "~")}/</option>)}
                </select>
              )}
              <button onClick={create} disabled={busy || !draft.trim()}>{busy ? "creating…" : "create ↵"}</button>
              <button onClick={closeDraft} disabled={busy}>esc</button>
              {err && <p className="mp-err">{err}</p>}
            </div>
          )}
          {moving && (
            <div className="mp-create mp-moving">
              <span className="mp-movewhat">Move <b>{moving.name}</b> to</span>
              <input autoFocus value={folder} placeholder="folder name" spellCheck={false}
                readOnly={busy} onChange={(e) => { setFolder(e.target.value); setErr(""); }}
                onKeyDown={onMoveKey} aria-label={`Folder for ${moving.name}`} />
              <button onClick={() => move(folder)} disabled={busy}>
                {busy ? "moving…" : folder.trim() ? "move ↵" : "no folder ↵"}
              </button>
              <button onClick={closeMove} disabled={busy}>esc</button>
              {folders.length > 0 && (
                <div className="mp-chips">
                  {folders.map((f) => (
                    <button key={f} onClick={() => move(f)} disabled={busy}
                      className={f === moving.folder ? "on" : ""}>{f}</button>
                  ))}
                  {moving.folder && <button onClick={() => move("")} disabled={busy}>no folder</button>}
                </div>
              )}
              {err && <p className="mp-err">{err}</p>}
            </div>
          )}
          {projects?.length === 0 && <p className="status">No projects under the dev roots.</p>}
          <div className="mp-list" ref={listRef} role="listbox">
            {groups.map(({ folder: name, rows: inside }) => (
              <div key={name} className="mp-group" role="group" aria-label={name || "No folder"}>
                {folders.length > 0 && (
                  <div className="mp-folder">{name || "No folder"} <span>{inside.length}</span></div>
                )}
                {inside.map((p) => {
                  const on = rows[sel]?.path === p.path;
                  return (
                    <div key={p.path} className={`mp-row${on ? " sel" : ""}`}
                      onMouseMove={(e) => followPointer(e, p)}>
                      <button role="option" aria-selected={on}
                        className={`mp-project${on ? " sel" : ""}`}
                        onClick={() => onPick("dev", p.path)} title={p.path}>
                        <span className="mp-pname">{p.name}</span>
                        <span className="mp-pmeta">{p.git ? ago(p.mtime) : "no git — no diffs"}</span>
                        <span className="mp-proot">{p.root.replace(/^\/Users\/[^/]+/, "~")}</span>
                      </button>
                      <button className="mp-move" tabIndex={-1} onClick={() => openMove(p)}
                        aria-label={`Move ${p.name} to a folder`}>move</button>
                    </div>
                  );
                })}
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}
