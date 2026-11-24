// A Brain fact id ("f997"): stays as written, hover shows the fact; a non-fact id never shows a card.
import Peek from "./Peek.jsx";
import { fetchJSON } from "../lib/api";
import { fmtDay } from "../lib/time";

const cache = new Map();   // id -> Promise<fact | null>; per page load, like cites

function resolveFact(id) {
  if (!cache.has(id))
    cache.set(id, fetchJSON(`/api/brain/fact/${id}`).catch(() => null));
  return cache.get(id);
}

const STATUS = { pending: "unconfirmed", superseded: "replaced by a newer fact",
                 rejected: "rejected" };

async function card(id) {
  const f = await resolveFact(id);
  if (!f) return null;
  const note = [f.source_label, STATUS[f.status]].filter(Boolean).join(" · ");
  return (
    <>
      <p className="peek-head"><span className="peek-kind">Brain</span>
        <span>{f.page_title}</span><span>{fmtDay(f.as_of)}</span></p>
      <p className="peek-title">{f.claim}</p>
      {note && <p className="peek-note">{note}</p>}
    </>
  );
}

export default function FactRef({ id, children }) {
  return (
    <Peek load={() => card(id)}>
      <span className="fact-ref" tabIndex={0}>{children}</span>
    </Peek>
  );
}
