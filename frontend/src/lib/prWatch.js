import { useRef, useState } from "react";
import { postJSON } from "./api.js";
import { useTicker } from "./poll.js";
import { useLoad } from "./refresh.js";

// PR watch (app/console_pr.py): a dev tab linked to its PR polls every two minutes and is woken with a note when
// it moves. The backend is stateless: the last snapshot lives here per tab (localStorage) and goes back with each
// poll. Polling lives in the tab's console, so closed/settled tabs and merged/closed PRs stop it.
// `watch`: { ref (PR URL once known; "" for a link made from the branch), last (snapshot), final (merged/closed) }.

export const PR_POLL_MS = 120_000;

// A `gh pr create` names its PR by its --head branch if it pushed one other than the tree's own, else the tree's branch.
export function createdRef(command) {
  return /\bgh\s+pr\s+create\b[^\n]*?(?:--head|-H)[=\s]+["']?([\w./-]+)/.exec(command || "")?.[1] || "";
}
const key = (tab) => `prwatch:${tab}`;

function read(tab) {
  try { return JSON.parse(localStorage.getItem(key(tab)) || "null"); } catch { return null; }
}
function save(tab, w) {
  try {
    if (w) localStorage.setItem(key(tab), JSON.stringify(w));
    else localStorage.removeItem(key(tab));
  } catch { /* per-viewer nicety: the watch just won't survive a reload */ }
}

// The bar chip's words for a snapshot.
export function prSummary(pr) {
  if (!pr) return "";
  if (pr.state === "MERGED") return "merged";
  if (pr.state === "CLOSED") return "closed";
  const bits = [];
  if (pr.draft) bits.push("draft");
  if (pr.mergeable === "CONFLICTING") bits.push("conflict");
  bits.push({ pass: "checks ✓", fail: "checks ✗", pending: "checks …", none: "" }[pr.verdict] || "");
  if (pr.review === "APPROVED") bits.push("approved");
  else if (pr.review === "CHANGES_REQUESTED") bits.push("changes asked");
  return bits.filter(Boolean).join(" · ");
}

// The chip's tone: what most needs looking at.
export function prTone(pr) {
  if (!pr) return "";
  if (pr.state === "MERGED") return "merged";
  if (pr.state === "CLOSED") return "";
  if (pr.mergeable === "CONFLICTING" || pr.verdict === "fail" || pr.review === "CHANGES_REQUESTED") return "bad";
  if (pr.verdict === "pending") return "wait";
  return "good";
}

export function usePrWatch({ tabId, project, worktree, onNotes }) {
  const [watch, setWatchState] = useState(() => (project ? read(tabId) : null));
  const [error, setError] = useState(null);
  const [checking, setChecking] = useState(false);
  const setWatch = (w) => { setWatchState(w); save(tabId, w); };

  // One poll at a time: two in flight carry the same `last` and report the same news twice.
  const inflight = useRef(null);
  function check(ref = watch?.ref ?? "", last = watch?.last ?? null) {
    if (!project) return Promise.resolve(null);
    if (!inflight.current) inflight.current = poll(ref, last).finally(() => { inflight.current = null; });
    return inflight.current;
  }
  // `ref`/`last` override the watch's own, for the first link.
  async function poll(ref, last) {
    setChecking(true);
    try {
      const r = await postJSON("/api/console/pr", { project, worktree, ref, last });
      setWatch({ ref: r.pr.url || ref, last: r.pr, final: r.final });
      setError(null);
      if (r.notes?.length) onNotes?.(r.notes, r.pr);
      return { pr: r.pr };
    } catch (e) {
      // A first link finding no PR links nothing; a failed poll (offline, gh logged out) keeps the watch.
      setError(e.message);
      return { error: e.message };
    } finally {
      setChecking(false);
    }
  }

  // A reload picks a live watch back up at once, not two minutes later.
  useLoad(() => (watch && !watch.final ? check() : null));
  // While hidden too: waking an idle tab nobody's looking at is the point.
  useTicker(() => check(), watch && !watch.final ? PR_POLL_MS : null,
            { whenHidden: true });

  return {
    pr: watch?.last || null,
    final: !!watch?.final,
    watching: !!watch && !watch.final,
    error, checking,
    // "" links the PR for the branch the tab's tree is on; else a number,
    // URL or branch name. Resolves to { pr } or { error }.
    link: (ref = "") => check(ref, null),
    unlink: () => { setWatch(null); setError(null); },
    refresh: () => check(),
  };
}
