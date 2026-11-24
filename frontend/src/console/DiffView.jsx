import { Fragment, useMemo, useState } from "react";
import Icon from "../ui/icons.jsx";
import { fetchJSON } from "../lib/api.js";
import { useLoad } from "../lib/refresh.js";
import { highlight, langOf } from "../lib/syntax.js";
import { anchor, commitMessage, conflictMessage, isCode, lines, numbered, place,
         where } from "../lib/review.js";

// A dev tab's diffs (backend console_diff.py): a card per changing turn opens this pane (file list, hunks)
// with scopes: a turn (picker), everything uncommitted, and for a worktree tab its branch against its
// base = the review (console_worktree.review: what a merge lands, Merge, what it would stop on, then the
// uncommitted files apart since a merge carries only commits). Lines are read off git, so Bash edits show.
// Any line takes a comment (shift-click a second for a range); Console owns them per tab folder as
// composer chips sent with the next message (lib/review.js), since the tab that wrote the line can act.

const STATUS = { added: "A", deleted: "D", modified: "M", renamed: "R" };
const SIGN = { add: "+", del: "−", ctx: " " };

function base(path) {
  const i = path.lastIndexOf("/");
  return i < 0 ? ["", path] : [path.slice(0, i + 1), path.slice(i + 1)];
}

function Counts({ f }) {
  if (f.binary) return <span className="df-n">bin</span>;
  return (
    <span className="df-n">
      {f.additions > 0 && <span className="df-add">+{f.additions}</span>}
      {f.deletions > 0 && <span className="df-del">−{f.deletions}</span>}
    </span>
  );
}

export function DiffCard({ files, onOpen }) {
  const add = files.reduce((n, f) => n + f.additions, 0);
  const del = files.reduce((n, f) => n + f.deletions, 0);
  return (
    <div className="diff-card">
      <button className="diff-card-head" onClick={() => onOpen(files, files[0]?.path)}
        title="Review the lines this turn changed">
        <Icon name="file" size={12} />
        <span>{files.length} file{files.length === 1 ? "" : "s"} changed</span>
        <span className="df-add">+{add}</span><span className="df-del">−{del}</span>
        <span className="diff-card-open">review</span>
      </button>
      {files.slice(0, 6).map((f) => {
        const [dir, name] = base(f.path);
        return (
          <button key={f.path} className="diff-card-row" onClick={() => onOpen(files, f.path)}>
            <span className={`df-st ${f.status}`}>{STATUS[f.status] || "M"}</span>
            <span className="df-path"><span className="df-dir">{dir}</span>{name}</span>
            <Counts f={f} />
          </button>
        );
      })}
      {files.length > 6 && (
        <button className="note-more" onClick={() => onOpen(files, files[6].path)}>
          +{files.length - 6} more
        </button>
      )}
    </div>
  );
}

function Tokens({ toks }) {
  return toks.map((t, i) => (t.t ? <span key={i} className={`sh-${t.t}`}>{t.s}</span> : t.s));
}

const joined = (toks) => toks.map((t) => t.s).join("");

// A row's tokens: the whole file's line when it still reads the same (the tree may have moved), else
// the hunk's lines as a run (wrong only if the hunk opens inside a string/comment from above).
function rowTokens(rows, lang, sides) {
  if (!lang) return [];
  const whole = { old: sides?.old != null ? highlight(sides.old, lang) : null,
                  new: sides?.new != null ? highlight(sides.new, lang) : null };
  const local = [];
  let from = 0;
  const run = (to) => {
    const olds = [], news = [];
    for (let k = from; k < to; k++) {
      if (rows[k].kind === "ctx" || rows[k].kind === "del") olds.push(k);
      if (rows[k].kind === "ctx" || rows[k].kind === "add") news.push(k);
    }
    const o = highlight(olds.map((k) => rows[k].text).join("\n"), lang);
    const n = highlight(news.map((k) => rows[k].text).join("\n"), lang);
    olds.forEach((k, j) => { if (rows[k].kind === "del") local[k] = o[j]; });
    news.forEach((k, j) => { local[k] = n[j]; });
  };
  rows.forEach((r, k) => { if (r.kind === "hunk") { run(k); from = k + 1; } });
  run(rows.length);
  return rows.map((r, k) => {
    if (!isCode(r)) return null;
    const line = r.kind === "del" ? whole.old?.[r.o - 1] : whole.new?.[r.n - 1];
    return line && joined(line) === r.text ? line : local[k] || null;
  });
}

// Both sides of a file for rowTokens: branch = merge-base vs HEAD; uncommitted = HEAD vs working tree;
// a turn's diff has no revision, so it colours hunk by hunk.
function loadSides(file, view, review, scope) {
  if (view === "turn" || file.binary || !langOf(file.path)) return null;
  const blob = (rev, path) => fetchJSON(`/api/console/blob?${scope}&rev=${encodeURIComponent(rev)}`
    + `&path=${encodeURIComponent(path)}`).then((d) => d.text);
  const oldRev = view === "branch" ? review?.merge_base : "HEAD";
  const old = file.status === "added" || !oldRev ? null : blob(oldRev, file.old_path || file.path);
  const now = file.status === "deleted" ? null
    : view === "branch" ? blob("HEAD", file.path)
    : fetchJSON(`/api/console/file?${scope}&path=${encodeURIComponent(file.path)}`).then((d) => d.content);
  return Promise.all([old?.catch(() => null), now?.catch(() => null)])
    .then(([o, n]) => ({ old: o ?? null, new: n ?? null }));
}

// Draft text lives in the pane: a range-widening shift-click remounts the editor on another row.
function NoteEditor({ draft, label, onChange, onSave, onCancel }) {
  const body = draft.body || "";
  return (
    <div className="dn-box dn-edit">
      <div className="dn-head"><span className="dn-where">{label}</span></div>
      <textarea autoFocus rows={3} value={body} placeholder="A comment for the tab…"
        onChange={(e) => onChange(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); onCancel(); }
          if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) { e.preventDefault(); if (body.trim()) onSave(); }
        }} />
      <div className="dn-acts">
        <span className="dn-hint">⌘↩ to {draft.id ? "save" : "add"} · Esc to cancel</span>
        <button className="dn-btn" onClick={onCancel}>Cancel</button>
        <button className="dn-btn go" disabled={!body.trim()} onClick={onSave}>
          {draft.id ? "Save" : "Comment"}</button>
      </div>
    </div>
  );
}

function NoteCard({ c, stale, onEdit, onDelete }) {
  return (
    <div className={`dn-box${stale ? " stale" : ""}`}>
      <div className="dn-head">
        <span className="dn-where">{lines(c)}{stale ? " · no longer in this diff" : ""}</span>
        <button className="dn-link" onClick={onEdit}>edit</button>
        <button className="dn-link" onClick={onDelete}>delete</button>
      </div>
      {stale && <pre className="dn-quote">{c.quote.map((r) => `${SIGN[r.kind]}${r.text}`).join("\n")}</pre>}
      <div className="dn-body">{c.body}</div>
    </div>
  );
}

// A merge conflict as git would leave it: base lines, then the tab's.
function ConflictView({ c, base, branch, lang }) {
  const side = (rows, cls, key) => {
    const toks = lang ? highlight(rows.join("\n"), lang) : [];
    return rows.map((text, i) => (
      <tr key={`${key}${i}`} className={`dl ${cls}`}>
        <td className="dl-t" colSpan={4}>{" "}{toks[i] ? <Tokens toks={toks[i]} /> : text}</td>
      </tr>
    ));
  };
  const mark = (text, label) => (
    <tr className="dl cf-mark"><td className="dl-t" colSpan={4}>{text}<span className="cf-label">{label}</span></td></tr>
  );
  return (
    <div className="dp-conflict" role="note">
      <div className="dpc-head">
        <span className="dpc-tag">conflict</span>
        {c.kind === "contents" || !c.kind
          ? `${base} changed these lines too. Merging stops here until the tab resolves them.`
          : `${c.kind}: ${c.message || "merging stops on this file until the tab resolves it."}`}
      </div>
      {c.regions.map((r, k) => (
        <table key={k} className="dp-lines dpc-lines"><tbody>
          {side(r.before, "ctx", "b")}
          {mark("<<<<<<< ", `${base}, now`)}
          {side(r.ours, "cf-ours", "o")}
          {r.ancestor && <>{mark("||||||| ", "where they split")}{side(r.ancestor, "cf-anc", "a")}</>}
          {mark("=======", "")}
          {side(r.theirs, "cf-theirs", "t")}
          {mark(">>>>>>> ", `${branch}, this tab`)}
          {side(r.after, "ctx", "f")}
          {r.cut && <tr className="dl meta"><td className="dl-t" colSpan={4}>cut: the region is longer than the pane keeps</td></tr>}
        </tbody></table>
      ))}
    </div>
  );
}

function Patch({ file, view, review, scope, conflict, base: baseName, branch, notes, draft,
                 onPick, onDraft, onSave, onCancel, onEdit, onDelete }) {
  const [sides, setSides] = useState(null);
  useLoad(() => loadSides(file, view, review, scope)?.then(setSides));
  const rows = useMemo(() => numbered(file.lines), [file]);
  const lang = langOf(file.path);
  const toks = useMemo(() => rowTokens(rows, lang, sides), [rows, lang, sides]);
  const placed = useMemo(() => place(rows, notes), [rows, notes]);
  const mine = draft && draft.path === file.path ? draft : null;
  const draftAt = mine && !mine.id ? place(rows, [mine]).at.keys().next().value ?? -1 : -1;
  const [lo, hi] = mine?.span ? [Math.min(...mine.span), Math.max(...mine.span)] : [-1, -1];
  const editorFor = (d) => (
    <NoteEditor key={d.id || "new"} draft={d} label={lines(d)} onChange={onDraft} onSave={onSave} onCancel={onCancel} />);
  const card = (c, stale = false) => (mine?.id === c.id ? editorFor(mine)
    : <NoteCard key={c.id} c={c} stale={stale} onEdit={() => onEdit(c)} onDelete={() => onDelete(c.id)} />);
  const editor = mine && !mine.id && editorFor(mine);
  return (
    <>
      {file.old_path && <div className="dp-note">renamed from {file.old_path}</div>}
      {file.binary && <div className="dp-note">binary, or too big to keep before the turn — changed, lines not shown</div>}
      {conflict && <ConflictView c={conflict} base={baseName} branch={branch} lang={lang} />}
      {(placed.outdated.length > 0 || (editor && draftAt < 0)) && (
        <div className="dn-outdated">
          {placed.outdated.map((c) => card(c, true))}
          {draftAt < 0 && editor}
        </div>
      )}
      <table className={`dp-lines${lang ? " syn" : ""}`}><tbody>
        {rows.map((r, i) => (
          <Fragment key={i}>
            <tr className={`dl ${r.kind}${i >= lo && i <= hi && isCode(r) ? " picked" : ""}`}>
              <td className="dl-o">{r.o ?? ""}</td>
              <td className="dl-n">{r.n ?? ""}</td>
              <td className="dl-g">
                {isCode(r) && (
                  <button className="dl-c" tabIndex={-1} onClick={(e) => onPick(rows, i, e.shiftKey)}
                    aria-label={`Comment on line ${r.n ?? r.o}`}
                    title="Comment on this line · shift-click another to cover the lines between">
                    <Icon name="plus" size={10} />
                  </button>
                )}
              </td>
              <td className="dl-t">{r.kind === "hunk" || r.kind === "meta" ? r.text
                : <><span className="dl-sign">{SIGN[r.kind]}</span>{toks[i] ? <Tokens toks={toks[i]} /> : r.text}</>}</td>
            </tr>
            {(placed.at.get(i) || []).map((c) => (
              <tr key={c.id} className="dl-note"><td colSpan={4}>{card(c)}</td></tr>
            ))}
            {draftAt === i && <tr className="dl-note"><td colSpan={4}>{editor}</td></tr>}
          </Fragment>
        ))}
      </tbody></table>
      {file.truncated && <div className="dp-note">cut here — the file's diff is longer than the pane keeps</div>}
    </>
  );
}

// The branch strip: standing against base, and the merge (Console's mergeWorktree, same path as the worktree_merge tool).
function BranchBar({ review, worktree, busy, merging, push, onPush, onMerge }) {
  const behind = review?.behind || 0;
  const why = merging ? "Merging…"
    : busy ? "The tab is mid-turn; merge once it's done"
    : !review ? "Reading the branch…"
    : review.error ? "Couldn't read the branch"
    : !review.ahead ? `Nothing to merge: no commits beyond ${worktree.base}`
    : review.dirty?.length ? "Uncommitted changes in the tab's worktree: they have to be committed (or dropped) first"
    : `Rebase onto ${worktree.base}, then fast-forward the main checkout, which is what the app runs`
      + (push ? `, then push ${worktree.base}` : "");
  const ready = !merging && !busy && review && !review.error && review.ahead > 0 && !review.dirty?.length;
  return (
    <div className="dp-branch">
      <span className="dpb-name" title={worktree.path}>{worktree.branch}</span>
      <span className="dpb-to">→ {worktree.base}</span>
      {review && !review.error && (
        <span className="dpb-n">
          {review.ahead} commit{review.ahead === 1 ? "" : "s"}
          {behind > 0 && <span title={`${worktree.base} has ${behind} commit${behind === 1 ? "" : "s"} this branch doesn't: the merge rebases onto ${behind === 1 ? "it" : "them"} first`}>
            {" "}· {behind} behind</span>}
        </span>
      )}
      <span className="bar-spacer" />
      <label className="dpb-push" title={`Also push ${worktree.base} to its upstream after merging`}>
        <input type="checkbox" checked={push} onChange={(e) => onPush(e.target.checked)} /> push
      </label>
      <button className="btn dpb-merge" disabled={!ready} onClick={onMerge} title={why}>
        {merging ? "merging…" : `Merge into ${worktree.base}`}
      </button>
    </div>
  );
}

function Band({ tone, children, action, onAction, onDismiss }) {
  return (
    <div className={`dp-band ${tone}`} role={tone === "bad" ? "alert" : "status"}>
      <div className="dp-band-text">{children}</div>
      {action && onAction && <button className="dp-ask" onClick={onAction}>{action}</button>}
      {onDismiss && (
        <button className="dp-x" onClick={onDismiss} aria-label="Dismiss"><Icon name="close" size={10} /></button>
      )}
    </div>
  );
}

const firstLine = (s) => (s || "").trim().split("\n")[0];

// Which turn the turn scope shows: newest first, "latest" follows the log.
function TurnPicker({ turns, at, onPick }) {
  const latest = turns.length - 1;
  return (
    <select className="dp-turn" value={at === latest ? "latest" : String(at)}
      onChange={(e) => onPick(e.target.value === "latest" ? null : turns[+e.target.value].entry)}
      aria-label="Which turn" title="Any turn in this session's log that changed the repo">
      <option value="latest">latest turn</option>
      {turns.map((t, i) => i).reverse().slice(1).map((i) => (
        <option key={i} value={i}>turn {i + 1} · {firstLine(turns[i].asked).slice(0, 48) || "no message"}</option>
      ))}
    </select>
  );
}

// `turns`: lib/review.js turnDiffs; `turn`: the entry opened on, null = latest. `comments`/`onComments`:
// Console's; without them (a left-behind branch has no tab) lines take no comment.
export function DiffPane({ turns = [], turn = null, path, scope, project, worktree, busy,
                           comments = [], onComments, onClose, onSend, onMerge, reloadRef }) {
  const [view, setView] = useState(scope || (turns.length ? "turn" : "all"));
  const [turnPick, setTurnPick] = useState(turn);   // an entry, or null: the latest
  const [current, setCurrent] = useState(path);
  const [loaded, setLoaded] = useState({});      // view → its payload (or { error })
  const [gen, setGen] = useState(0);             // bumps per load, so each file re-reads its sides
  const [draft, setDraft] = useState(null);      // the comment being written or edited
  const [showAll, setShowAll] = useState(false);
  const [showCommits, setShowCommits] = useState(false);
  const [result, setResult] = useState(null);    // the last merge's answer
  const [merging, setMerging] = useState(false);
  const [flash, setFlash] = useState("");
  const pushKey = `review-push:${project}`;
  const [push, setPush] = useState(() => localStorage.getItem(pushKey) === "1");
  const scopeQuery = `project=${encodeURIComponent(project || "")}`
    + (worktree ? `&worktree=${encodeURIComponent(worktree.path)}` : "");

  // The branch view reads the uncommitted payload too, shown apart (a merge carries only commits).
  function load(v) {
    if (v === "turn" || !project) return Promise.resolve();
    const one = (k) => fetchJSON(k === "branch" ? `/api/console/review?${scopeQuery}` : `/api/console/diff?${scopeQuery}`)
      .then((d) => setLoaded((p) => ({ ...p, [k]: d })))
      .catch((err) => setLoaded((p) => ({ ...p, [k]: { error: err.message, files: [] } })));
    return Promise.all(v === "branch" ? [one("branch"), one("all")] : [one(v)])
      .finally(() => setGen((g) => g + 1));
  }
  // On open and refresh broadcasts; Console calls reloadRef at turn end (when it may have committed).
  useLoad(() => load(view));
  if (reloadRef) reloadRef.current = () => load(view);

  const setComments = (next) => onComments?.(next);

  const review = loaded.branch;
  // An entry an attach rebuilt the log without reads as the latest.
  const picked = turnPick ? turns.findIndex((t) => t.entry === turnPick) : -1;
  const turnAt = picked >= 0 ? picked : turns.length - 1;
  const data = view === "turn" ? { files: turns[turnAt]?.files || [] } : loaded[view];
  const committed = data?.files || [];
  // Branch view only: uncommitted files after the commits; a path can be in both, so keys carry the part.
  const loose = view === "branch" && !data?.error
    ? (loaded.all?.files || []).map((f) => ({ ...f, uncommitted: true })) : [];
  const files = [...committed, ...loose];
  const rowKey = (f) => (f.uncommitted ? `u:${f.path}` : f.path);
  const file = files.find((f) => rowKey(f) === current) || files.find((f) => f.path === current) || files[0];
  const conflicts = (result?.conflicts?.length ? result.conflicts.map((p) => ({ path: p, regions: [] })) : null)
    || (view === "branch" && review?.conflicts) || [];
  const conflictOf = (p) => review?.conflicts?.find((c) => c.path === p)
    || conflicts.find((c) => c.path === p) || null;
  const byFile = comments.reduce((m, c) => m.set(c.path, (m.get(c.path) || 0) + 1), new Map());

  function pick(v) {
    setView(v);
    setResult(null);
    load(v);
  }

  // A click opens a comment; a shift-click widens the open one (typed text goes along).
  function startComment(rows, i, extend) {
    if (!onComments) return;
    const open = draft && !draft.id && draft.path === file.path ? draft : null;
    const from = extend && open?.span ? open.span[0] : i;
    const a = anchor(rows, from, i);
    if (a) setDraft({ path: file.path, ...a, span: [from, i], body: open?.body || "" });
  }

  function saveDraft() {
    const body = (draft.body || "").trim();
    if (!body) return;
    if (draft.id) setComments(comments.map((c) => (c.id === draft.id ? { ...c, body } : c)));
    else {
      const { path: p, side, start, line, quote } = draft;
      setComments([...comments, { id: crypto.randomUUID(), path: p, side, start, line, quote, body }]);
    }
    setDraft(null);
  }

  // To the tab, then close, so you watch it land.
  function ask(text) {
    if (!onSend(text)) {
      setFlash("Not sent: the tab isn't connected, or is still reading a file dropped on it. Nothing was lost; try again in a moment.");
      return;
    }
    onClose();
  }

  async function merge() {
    if (comments.length && !window.confirm(
      `${comments.length} comment${comments.length === 1 ? " hasn't" : "s haven't"} been sent to the tab. Merge anyway?`)) return;
    setMerging(true);
    let r;
    try {
      r = await onMerge({ push });
    } finally {
      setMerging(false);
    }
    setResult(r || null);
    load("branch");
  }

  const askResolve = (list) => ask(conflictMessage(list, { branch: worktree.branch, base: worktree.base }));

  const bands = [];
  if (flash) bands.push(<Band key="flash" tone="bad" onDismiss={() => setFlash("")}>{flash}</Band>);
  if (view === "branch" && result) {
    const tone = !result.ok ? "bad" : result.commits ? "good" : "plain";
    const text = result.text || "";
    // The tool returns the merge's reason to the tab; here the two refusals the pane can act on are said to you.
    const said = result.ok ? text.charAt(0).toUpperCase() + text.slice(1)
      : result.conflicts?.length ? `${worktree.base} moved on, and the branch's commits conflict with it in `
        + `${result.conflicts.join(", ")}. Nothing changed in the main checkout; the tab resolves it in its worktree.`
      : result.dirty?.length ? `The tab's worktree has uncommitted changes (${result.dirty.slice(0, 6).join(", ")}`
        + `${result.dirty.length > 6 ? ", …" : ""}). Nothing changed in the main checkout.`
      : text;
    bands.push(
      <Band key="result" tone={tone} onDismiss={() => setResult(null)}
        action={result.conflicts?.length ? "Ask the tab to resolve" : result.dirty?.length ? "Ask the tab to commit" : null}
        onAction={onSend && (() => (result.conflicts?.length ? askResolve(conflicts) : ask(commitMessage(result.dirty))))}>
        {result.ok ? said : <><strong>Not merged.</strong> {said}</>}
      </Band>);
  } else if (view === "branch" && review?.conflicts?.length) {
    const list = review.conflicts;
    bands.push(
      <Band key="conflicts" tone="bad" action="Ask the tab to resolve" onAction={onSend && (() => askResolve(list))}>
        <strong>Conflicts with {worktree.base}.</strong> It has moved on and changed
        {list.length === 1 ? " a file" : ` ${list.length} files`} this branch changes too, so the merge
        will stop on {list.length === 1 ? "it" : "them"}:{" "}
        {list.map((c, k) => (
          <Fragment key={c.path}>{k > 0 && ", "}
            <button className="dp-band-file" onClick={() => setCurrent(c.path)}>{c.path}</button>
            {c.kind && c.kind !== "contents" ? ` (${c.kind})` : ""}
          </Fragment>
        ))}
      </Band>);
  }
  if (view === "branch" && review?.dirty?.length && !result?.dirty) {
    bands.push(
      <Band key="dirty" tone="plain" action="Ask the tab to commit" onAction={onSend && (() => ask(commitMessage(review.dirty)))}>
        {review.dirty.length} uncommitted file{review.dirty.length === 1 ? "" : "s"} in the tab's worktree
        {" "}(listed under <em>uncommitted</em>) {review.dirty.length === 1 ? "isn't" : "aren't"} in any commit, so
        the merge won't carry {review.dirty.length === 1 ? "it" : "them"}, and it won't run until
        {review.dirty.length === 1 ? " it's" : " they're"} committed or dropped: {review.dirty.slice(0, 6).join(", ")}
        {review.dirty.length > 6 ? ` and ${review.dirty.length - 6} more` : ""}
      </Band>);
  }

  const empty = data?.error ? `Couldn't read it: ${data.error}`
    : view === "turn" ? (turns.length ? "Nothing changed." : "No turn in this session's log has changed the repo yet.")
    : view === "all" ? "Nothing uncommitted."
    : review && !review.ahead ? `No commits beyond ${worktree.base} yet.`
    : "Nothing to review.";
  const row = (f) => {
    const [dir, name] = base(f.path);
    const cf = view === "branch" && !f.uncommitted && conflictOf(f.path);
    return (
      <button key={rowKey(f)} className={`dp-frow${f === file ? " on" : ""}${f.uncommitted ? " loose" : ""}`}
        onClick={() => setCurrent(rowKey(f))} title={f.path}>
        <span className={`df-st ${f.status}`}>{STATUS[f.status] || "M"}</span>
        <span className="df-path"><span className="df-name">{name}</span>
          <span className="df-dir">{dir}</span></span>
        {cf && <span className="df-cf" title={`conflicts with ${worktree.base}`}>conflict</span>}
        {byFile.get(f.path) > 0 && (
          <span className="df-nc" title="your comments on it">
            <Icon name="message" size={10} />{byFile.get(f.path)}</span>
        )}
        <Counts f={f} />
      </button>
    );
  };

  return (
    <div className="diff-pane" tabIndex={-1} onKeyDown={(e) => {
      if (e.target.closest?.("textarea, input")) return;
      if (e.key === "Escape") { e.stopPropagation(); onClose(); }
      if (e.key !== "j" && e.key !== "k") return;
      const i = files.indexOf(file);
      const next = files[(i + (e.key === "j" ? 1 : -1) + files.length) % files.length];
      if (next) setCurrent(rowKey(next));
    }}>
      <div className="dp-head">
        <span className="dp-seg">
          <button className={view === "turn" ? "on" : ""} onClick={() => pick("turn")}
            disabled={!turns.length} title="What one turn changed: the latest, or pick another">turn</button>
          {worktree && (
            <button className={view === "branch" ? "on" : ""} onClick={() => pick("branch")}
              title={`${worktree.branch} against ${worktree.base}: the commits a merge would land, then what's still uncommitted`}>
              vs {worktree.base}</button>
          )}
          <button className={view === "all" ? "on" : ""} onClick={() => pick("all")}
            title="Everything not yet committed">uncommitted</button>
        </span>
        {view === "turn" && turns.length > 0 && (
          <TurnPicker turns={turns} at={turnAt} onPick={(e) => { setTurnPick(e); setDraft(null); }} />
        )}
        <span className="dp-file">{file ? file.path : ""}</span>
        {view !== "turn" && (
          <button className="dp-x" onClick={() => load(view)} aria-label="Read it again" title="Read it again">
            <Icon name="refresh" size={11} /></button>
        )}
        <button className="dp-x" onClick={onClose} aria-label="Close diff" title="Close · Esc">
          <Icon name="close" size={11} /></button>
      </div>
      {view === "branch" && worktree && (
        <BranchBar review={review} worktree={worktree} busy={busy} merging={merging} push={push}
          onPush={(v) => { setPush(v); localStorage.setItem(pushKey, v ? "1" : "0"); }} onMerge={merge} />
      )}
      {bands}
      <div className="dp-body">
        <div className="dp-files">
          {data === undefined && <p className="status pulse">reading…</p>}
          {view === "branch" && review?.commits?.length > 0 && (
            <div className="dp-commits">
              {review.commits.slice(0, showCommits ? undefined : 3).map((c) => (
                <div key={c.sha} className="dp-commit" title={`${c.short} · ${c.when}\n${c.subject}`}>
                  <span className="dpc-sha">{c.short}</span><span className="dpc-subj">{c.subject}</span>
                </div>
              ))}
              {review.commits.length > 3 && (
                <button className="note-more" onClick={() => setShowCommits((v) => !v)}>
                  {showCommits ? "fewer" : `+${review.commits.length - 3} more commits`}</button>
              )}
            </div>
          )}
          {data !== undefined && committed.length === 0 && <p className="status">{empty}</p>}
          {loose.length > 0 && committed.length > 0 && <div className="dp-part">committed · the merge lands these</div>}
          {committed.map(row)}
          {loose.length > 0 && (
            <div className="dp-part loose" title="In the worktree but in no commit: worktree_merge carries only commits">
              uncommitted · won't merge yet</div>
          )}
          {loose.map(row)}
        </div>
        <div className="dp-patch">
          {file?.uncommitted && (
            <div className="dp-note dp-loose">uncommitted: in the worktree, in no commit yet, so a merge
              wouldn't carry this. Diffed against the tab's last commit.</div>
          )}
          {file && (
            <Patch key={`${view}:${rowKey(file)}:${view === "turn" ? turnAt : gen}`} file={file}
              view={file.uncommitted ? "all" : view}
              review={review} scope={scopeQuery} branch={worktree?.branch} base={worktree?.base}
              conflict={view === "branch" && !file.uncommitted ? conflictOf(file.path) : null}
              notes={comments.filter((c) => c.path === file.path)}
              draft={draft} onPick={startComment} onDraft={(body) => setDraft((d) => ({ ...d, body }))}
              onSave={saveDraft} onCancel={() => setDraft(null)}
              onEdit={(c) => setDraft({ ...c })} onDelete={(id) => setComments(comments.filter((c) => c.id !== id))} />
          )}
        </div>
      </div>
      {comments.length > 0 && (
        <div className="dp-tray">
          {showAll && (
            <div className="dp-tray-list">
              {comments.map((c) => (
                <div key={c.id} className="dp-tray-row">
                  <button className="dp-tray-where" onClick={() => setCurrent(c.path)}>{where(c)}</button>
                  <span className="dp-tray-body">{c.body}</span>
                  <button className="dn-link" onClick={() => setComments(comments.filter((x) => x.id !== c.id))}>delete</button>
                </div>
              ))}
            </div>
          )}
          <div className="dp-tray-row main">
            <button className="dp-tray-n" onClick={() => setShowAll((v) => !v)} aria-expanded={showAll}>
              <Icon name="message" size={11} /> {comments.length} comment{comments.length === 1 ? "" : "s"}
              <Icon name={showAll ? "chevronDown" : "chevronRight"} size={10} />
            </button>
            <span className="dp-tray-hint">
              {comments.length === 1 ? "goes" : "go"} with your next message, in the composer below</span>
            <button className="dn-link" onClick={() => {
              if (window.confirm(`Discard ${comments.length === 1 ? "this comment" : `all ${comments.length} comments`}?`)) setComments([]);
            }}>discard</button>
          </div>
        </div>
      )}
    </div>
  );
}
