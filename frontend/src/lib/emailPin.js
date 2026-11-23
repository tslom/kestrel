// One email → one console pill. Shared by every place a single message
// renders (widget peek, review card, range expansion row), which pass
// slightly different shapes: queue cards have from_name/from_addr/snippet,
// range rows a combined `from`. The id lets the console act on the message
// via POST /api/email/{id}/action.
import { GMAIL_URL } from "./googleLinks";
import { pinFiles } from "./pin.js";
import { dragPin } from "./drag.js";

export { GMAIL_URL };

// "Filed/Courses/CSCI 103" -> "Courses / CSCI 103": review files everything
// under Filed/, so the parent says nothing on a chip or a row.
export const shortLabel = (name) =>
  (name || "").replace(/^Filed\//, "").split("/").join(" / ");

/**
 * The `{ label, data }` one message becomes. Split out of `pinEmail` because
 * the two gestures need the same pair for different things: the button calls
 * it, the drag source carries it.
 */
export function emailPin(m) {
  const subject = m.subject || "(no subject)";
  const short = subject.length > 36 ? `${subject.slice(0, 36)}…` : subject;
  return {
    label: `email · ${short}`,
    data: {
      source: "email",
      kind: "message",
      id: m.id,
      thread_id: m.thread_id,
      from: m.from_name || m.from || m.from_addr,
      from_addr: m.from_addr,
      received: m.received,
      subject,
      summary: m.summary,
      snippet: m.snippet,
      recommended: m.action,
      files: pinFiles(m.attachments),
      gmail_url: GMAIL_URL(m.id),
    },
  };
}

export function pinEmail(onPin, m) {
  const { label, data } = emailPin(m);
  onPin(label, data);
}

/** Drag-source props for one message row. */
export function dragEmail(m) {
  const { label, data } = emailPin(m);
  return dragPin(label, data);
}
