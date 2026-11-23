import { Fragment, useMemo, useState } from "react";
import Icon from "../ui/icons.jsx";
import { fetchJSON } from "../lib/api.js";
import { useLoad } from "../lib/refresh.js";
import { highlight, langOf } from "../lib/syntax.js";
import { anchor, commitMessage, conflictMessage, isCode, lines, numbered, place,
         where } from "../lib/review.js";

// A dev tab's diffs (backend: console_diff.py). The card sits in the log at
// the end of each turn that changed the repo — files and counts, nothing
// else — and opens the pane: the file list on the left, the selected file's
// hunks on the right, and a switch between scopes: the latest turn (or any
// earlier one in the log, from the picker beside it), everything
// uncommitted, and — for a tab in its own worktree — its branch against its
// base, which is the review: what a merge would land
// (console_worktree.review, merge-base and all), the Merge button, and what
// the merge would stop on. The change card above it is the ledger of
// *calls*; this is the lines, read off git, so Bash's edits are in it too.
//
// Any line in any scope takes a comment (shift-click a second one for a
// range). Console owns them, per tab folder: they sit as chips in the
// composer and go with the next message you send (lib/review.js has the
// wording), because the one who can act on "this should use the helper" is
// the tab that wrote the line.

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

// Each code row's tokens: the whole file's line when the pane has the file
// and the line still reads the same in it (the tree may have moved since the
// diff), else the hunk's own lines tokenized as a run — right unless the
// hunk opens inside a string or comment that started above it.
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

// Both sides of a file in full, for rowTokens. The branch view's old side
// is where the branch left its base and its new side the tab's HEAD; the
// uncommitted view's are HEAD and the working tree. A turn's diff has no
// revision to ask for, so it colours hunk by hunk.
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

// The draft's text lives in the pane, not here: a shift-click that widens
// the range moves the editor to another row, which is a new mount.
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

// What a merge of the branch into its base would stop on, as git would
// leave it in the file: the base's lines, then the tab's.
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

// The branch view's strip: where the branch stands against its base, and
// the merge (Console's mergeWorktree — the bar's button and the tab's
// worktree_merge tool take the same path).
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

// Which turn the turn scope shows: newest first, "latest" following the log
// as turns land while the pane is open.
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

// `turns`: the log's turn diffs (lib/review.js turnDiffs); `turn` the entry
// the pane opened on, or null for the latest. `comments`/`onComments`:
// Console's, which go with the next message; without them (a left-behind
// branch's review, which has no tab to read them) lines take no comment.
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

  function load(v) {
    if (v === "turn" || !project) return Promise.resolve();
    return fetchJSON(v === "branch" ? `/api/console/review?${scopeQuery}` : `/api/console/diff?${scopeQuery}`)
      .then((d) => setLoaded((p) => ({ ...p, [v]: d })))
      .catch((err) => setLoaded((p) => ({ ...p, [v]: { error: err.message, files: [] } })))
      .finally(() => setGen((g) => g + 1));
  }
  // On open (and the desk's refresh broadcasts); Console calls reloadRef
  // when a turn ends, since that's when the tab may have committed.
  useLoad(() => load(view));
  if (reloadRef) reloadRef.current = () => load(view);

  const setComments = (next) => onComments?.(next);

  const review = loaded.branch;
  // An entry an attach rebuilt the log without reads as the latest.
  const picked = turnPick ? turns.findIndex((t) => t.entry === turnPick) : -1;
  const turnAt = picked >= 0 ? picked : turns.length - 1;
  const data = view === "turn" ? { files: turns[turnAt]?.files || [] } : loaded[view];
  const files = data?.files || [];
  const file = files.find((f) => f.path === current) || files[0];
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

  // A click opens a comment on that line; a shift-click widens the open one
  // to run from where it started to here. What's typed goes along.
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
    // The merge's own reason is written to the tab (its tool returns it);
    // here it's you reading, so the two refusals the pane can act on get
    // said to you. Anything else is shown as the merge put it.
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
        {review.dirty.length === 1 ? " isn't" : " aren't"} in this review, and the merge waits for
        {review.dirty.length === 1 ? " it" : " them"} to be committed: {review.dirty.slice(0, 6).join(", ")}
        {review.dirty.length > 6 ? ` and ${review.dirty.length - 6} more` : ""}
      </Band>);
  }

  const empty = data?.error ? `Couldn't read it: ${data.error}`
    : view === "turn" ? (turns.length ? "Nothing changed." : "No turn in this session's log has changed the repo yet.")
    : view === "all" ? "Nothing uncommitted."
    : review && !review.ahead ? `No commits beyond ${worktree.base} yet.`
      + (review.dirty?.length ? " What's there is uncommitted: see uncommitted." : "")
    : "Nothing to review.";

  return (
    <div className="diff-pane" tabIndex={-1} onKeyDown={(e) => {
      if (e.target.closest?.("textarea, input")) return;
      if (e.key === "Escape") { e.stopPropagation(); onClose(); }
      if (e.key !== "j" && e.key !== "k") return;
      const i = files.indexOf(file);
      const next = files[(i + (e.key === "j" ? 1 : -1) + files.length) % files.length];
      if (next) setCurrent(next.path);
    }}>
      <div className="dp-head">
        <span className="dp-seg">
          <button className={view === "turn" ? "on" : ""} onClick={() => pick("turn")}
            disabled={!turns.length} title="What one turn changed: the latest, or pick another">turn</button>
          {worktree && (
            <button className={view === "branch" ? "on" : ""} onClick={() => pick("branch")}
              title={`${worktree.branch} against ${worktree.base}: everything the tab committed, what a merge would land`}>
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
          {data !== undefined && files.length === 0 && <p className="status">{empty}</p>}
          {files.map((f) => {
            const [dir, name] = base(f.path);
            const cf = view === "branch" && conflictOf(f.path);
            return (
              <button key={f.path} className={`dp-frow${f === file ? " on" : ""}`}
                onClick={() => setCurrent(f.path)} title={f.path}>
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
          })}
        </div>
        <div className="dp-patch">
          {file && (
            <Patch key={`${view}:${file.path}:${view === "turn" ? turnAt : gen}`} file={file} view={view}
              review={review} scope={scopeQuery} branch={worktree?.branch} base={worktree?.base}
              conflict={view === "branch" ? conflictOf(file.path) : null}
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
