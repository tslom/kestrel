import { useState } from "react";
import Icon from "../ui/icons.jsx";
import { postJSON } from "../lib/api";

// Branches left behind (console_worktree.orphans: a closed tab's or a night job's `claude --worktree`)
// still holding unmerged commits or uncommitted files; merged ones are swept at startup and tab close.
// Merge is the review pane's (clean rebase + fast-forward only, pushed if the repo's review says so); Discard asks once.

const s = (n) => (n === 1 ? "" : "s");

export function orphanLine(o) {
  const bits = [];
  if (o.ahead) bits.push(`${o.ahead} commit${s(o.ahead)}`);
  if (o.dirty_count) bits.push(`${o.dirty_count} uncommitted`);
  if (o.behind) bits.push(`${o.base} moved on`);
  return bits.join(" · ");
}

function Row({ o, onReview, onChanged }) {
  const [arming, setArming] = useState(false);
  const [busy, setBusy] = useState(false);
  const [said, setSaid] = useState(null);   // { ok, text }
  const push = (() => { try { return localStorage.getItem(`review-push:${o.project}`) === "1"; } catch { return false; } })();

  async function call(what) {
    setBusy(true);
    setArming(false);
    try {
      const r = await postJSON(`/api/console/orphans/${what}`, { project: o.project, branch: o.branch, push });
      setSaid({ ok: !!r.ok, text: r.text || (r.ok ? "done" : r.reason) });
      if (r.ok) onChanged?.();
    } catch (e) {
      setSaid({ ok: false, text: e.message });
    } finally {
      setBusy(false);
    }
  }

  return (
    <li className="orph">
      <div className="orph-head">
        <span className="orph-repo">{o.project_name}</span>
        <span className="orph-branch" title={o.path || "no worktree: the branch alone"}>{o.branch}</span>
        <span className={`orph-kind ${o.kind}`}>{o.kind === "tab" ? "tab" : "--worktree"}</span>
      </div>
      <div className="orph-meta">
        {orphanLine(o)}{o.when ? ` · last commit ${o.when}` : ""}
        {o.locked ? " · lock left by a session that's gone" : ""}
      </div>
      {o.commits?.length > 0 && (
        <ul className="orph-commits">
          {o.commits.slice(0, 3).map((c) => (
            <li key={c.short}><code>{c.short}</code> {c.subject}</li>
          ))}
          {o.ahead > 3 && <li className="orph-more">+{o.ahead - 3} more</li>}
        </ul>
      )}
      {said && <p className={`orph-said ${said.ok ? "good" : "bad"}`}>{said.ok ? "" : "Not merged. "}{said.text}</p>}
      <div className="orph-acts">
        {o.path && (
          <button className="pbtn" onClick={() => onReview(o)} disabled={busy}
            title="Its commits and diff in the review pane, with any conflicts a merge would stop on">Review</button>
        )}
        <button className="pbtn go" onClick={() => call("merge")}
          disabled={busy || !o.ahead || o.dirty_count > 0 || (!o.path && o.behind > 0)}
          title={o.dirty_count ? "Uncommitted files: commit or discard them first"
            : !o.ahead ? "No commits to merge"
            : !o.path && o.behind ? `${o.base} moved on and the branch has no worktree to rebase in: open a dev tab on ${o.project_name} to land it`
            : `Rebase onto ${o.base} and fast-forward it${push ? ", then push" : ""}. A conflict stops it and changes nothing`}>
          {busy ? "…" : `Merge → ${o.base}`}</button>
        <span className="grow" />
        {arming ? (
          <>
            <button className="pbtn fixb" onClick={() => call("discard")} disabled={busy}>
              Discard {orphanLine({ ...o, behind: 0 }) || "it"}?</button>
            <button className="pbtn" onClick={() => setArming(false)}>Keep</button>
          </>
        ) : (
          <button className="pbtn quiet" onClick={() => setArming(true)} disabled={busy}
            title="Delete the branch and its worktree, uncommitted files and all">Discard</button>
        )}
      </div>
    </li>
  );
}

export default function OrphansPanel({ orphans, onClose, onReview, onChanged }) {
  return (
    <div className="side-panel side-card">
      <div className="side-head">
        <h2 className="side-title">Left behind</h2>
        <button className="side-x" aria-label="Close" title="Close · Esc" onClick={onClose}>
          <Icon name="close" size={12} /></button>
      </div>
      <div className="side-body detail">
        <p className="orph-lead">
          Branches from tabs and <code>--worktree</code> sessions that have closed, holding work
          that isn&apos;t merged. Merged ones are cleaned up on their own.
        </p>
        {orphans.length ? (
          <ul className="orph-list">
            {orphans.map((o) => <Row key={`${o.project}:${o.branch}`} o={o} onReview={onReview} onChanged={onChanged} />)}
          </ul>
        ) : <p className="status">Nothing left behind.</p>}
      </div>
    </div>
  );
}
