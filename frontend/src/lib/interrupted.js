// Tabs a quit (or a dead backend) cut off mid-work (backend: console_work.py),
// and the "continue" each is sent when you click Resume. Nothing resumes on
// its own: the work may have half-landed, and the model has to look before it
// redoes anything — which is most of what the message says.

const kindLabel = (k) => (/bash|shell/.test(k || "") ? "background command" : "background agent");

// "a turn and 2 background agents" — what the row and the tab's banner say.
export function cutWhat(row) {
  const parts = [];
  if (row.turn) parts.push("a turn");
  const agents = (row.agents || []).filter((a) => kindLabel(a.kind) === "background agent").length;
  const shells = (row.agents || []).length - agents;
  if (agents) parts.push(`${agents} background agent${agents === 1 ? "" : "s"}`);
  if (shells) parts.push(`${shells} background command${shells === 1 ? "" : "s"}`);
  return parts.join(" and ") || "its work";
}

export function resumeText(row) {
  const lines = [
    row.turn
      ? "Kestrel quit while you were working, which cut your last turn off mid-work."
      : "Kestrel quit while background work you started was still running.",
  ];
  const agents = row.agents || [];
  if (agents.length) {
    lines.push("", "These were stopped with it:");
    for (const a of agents)
      lines.push(`- ${kindLabel(a.kind)} ${a.description ? `"${a.description}"` : ""}${a.id ? ` (${a.id})` : ""}`.replace(/ {2,}/g, " "));
  }
  lines.push("", "Pick up where you left off. Before redoing anything, check what already landed: "
    + "partial output files, what the stopped agents and commands wrote, and the worktree's "
    + "git status and diff. Restart only what didn't finish.");
  return lines.join("\n");
}

// The Needs-you row's title for the tabs still open here.
export function interruptedTitle(n) {
  return n === 1 ? "A tab was interrupted when Kestrel quit"
    : `${n} tabs were interrupted when Kestrel quit`;
}
