import { useState } from "react";
import { useWidgetData } from "../lib/refresh.js";
import "./delegates.css";

// Other models at work (backend app/delegates): the log's rows for the three
// tools Claude sends them with. `t` is the log's tool entry: `raw` its input,
// `result` / `error` from its tool_result (console.DELEGATE_TOOL_NAMES).
//
//   delegate   one line, "→ gemini": the mark that another provider answered
//   fan_out    one line, the mix it ran ("claude ×2 · gemini ×2")
//   shortlist  the card: Claude's picks, a preview of each, and the choice
//
// A row opens to the brief that left and what came back, because the brief
// is the one thing that crossed to another company.
export const DELEGATE_TOOL = /__(delegate|fan_out|shortlist)$/;

const NAMES = { claude: "Claude", gemini: "Gemini", codex: "ChatGPT" };
const nameOf = (id) => NAMES[id] || id;

function parse(raw) {
  try { return JSON.parse(raw) || {}; } catch { return {}; }
}

// "variant 2 (gemini · gemini-3-pro, 41s): /path" and its FAILED form, as
// console_tools writes them.
function variantsIn(result) {
  const out = [];
  for (const m of (result || "").matchAll(/^(?:=== )?variant (\d+) \(([a-z]+)[^)]*\): (FAILED)?/gm))
    out.push({ n: Number(m[1]), provider: m[2], failed: !!m[3] });
  return out;
}

function mixOf(list) {
  const tally = [];
  for (const id of list) {
    const row = tally.find((x) => x.id === id);
    if (row) row.n += 1; else tally.push({ id, n: 1 });
  }
  return tally;
}

export function ProviderMark({ id, n }) {
  return (
    <span className={`dg-who dg-${id}`}>
      {nameOf(id)}{n > 1 && <span className="tally-n">×{n}</span>}
    </span>
  );
}

function Row({ t, live, head, brief }) {
  const [open, setOpen] = useState(false);
  const redacted = /Taken out of the brief before it left: ([^.]+)\./.exec(t.result || "")?.[1];
  return (
    <div className={`delegate-row${t.error ? " bad" : ""}${live ? " live" : ""}`}>
      <button className="dg-head" aria-expanded={open} onClick={() => setOpen((v) => !v)}
        title={open ? "Hide the brief" : "Show the brief that was sent and what came back"}>
        <span className="dg-arrow" aria-hidden="true">→</span>
        {head}
        {redacted && <span className="dg-redacted" title="Replaced with placeholders before the brief left this machine">{redacted} removed</span>}
        {t.error ? <span className="dg-state bad">failed</span>
          : live ? <span className="dg-state pulse">working</span> : null}
      </button>
      {open && (
        <div className="dg-body">
          <div className="dg-label">the brief (all it was given)</div>
          <pre className="tool-cmd">{brief}</pre>
          {t.result != null && (
            <>
              <div className="dg-label">came back</div>
              <pre className={`tool-cmd script-result${t.error ? " error" : ""}`}>{t.result}</pre>
            </>
          )}
        </div>
      )}
    </div>
  );
}

function Shortlist({ t, busy, onPick }) {
  const input = parse(t.raw);
  const picks = (Array.isArray(input.picks) ? input.picks : [input.picks]).map(Number).filter(Boolean);
  const reasons = Array.isArray(input.reasons) ? input.reasons : [];
  const { data, error } = useWidgetData(`/api/delegates/batches/${encodeURIComponent(input.batch || "")}`);
  const [chosen, setChosen] = useState(null);
  const [wide, setWide] = useState(null);
  if (t.error) return <div className="delegate-row bad"><span className="dg-state bad">shortlist failed</span> {t.result}</div>;
  const rows = picks.map((n, i) => ({
    n, why: reasons[i] || "", v: data?.variants?.find((x) => x.variant === n) }));
  const total = data?.variants?.length;
  return (
    <div className="shortlist" data-testid="shortlist">
      <div className="sl-head">
        <span className="sl-title">Shortlist</span>
        {total != null && <span className="sl-sub">{picks.length} of {total} made the cut</span>}
        {input.note && <p className="sl-note">{input.note}</p>}
      </div>
      {error && <p className="sl-note bad">The batch didn&apos;t load: {error}</p>}
      <div className={`sl-grid${wide != null ? " one" : ""}`}>
        {rows.filter((r) => wide == null || wide === r.n).map((r, i) => {
          const src = `/api/delegates/batches/${encodeURIComponent(input.batch)}/${r.n}/file`;
          const image = /\.(png|jpe?g|gif|webp|svg)$/i.test(r.v?.file || "");
          return (
            <figure key={r.n} className={`sl-pick${chosen === r.n ? " chosen" : ""}`}>
              <div className="sl-frame">
                {image
                  ? <img src={src} alt={`Variant ${r.n}`} />
                  : <iframe src={src} title={`Variant ${r.n}`} sandbox="allow-scripts" loading="lazy" />}
              </div>
              <figcaption>
                <div className="sl-line">
                  <span className="sl-rank">{wide == null ? i + 1 : `#${r.n}`}</span>
                  {r.v && <ProviderMark id={r.v.provider} />}
                  {r.v?.model && <span className="sl-model">{r.v.model}</span>}
                  <span className="sl-n">variant {r.n}</span>
                </div>
                {r.why && <p className="sl-why">{r.why}</p>}
                <div className="sl-acts">
                  <button className="bar-btn sl-go" disabled={busy || chosen != null}
                    title={busy ? "After this turn" : "Tell Claude to go with this one"}
                    onClick={() => { if (onPick(r.n, r.v?.provider, input.batch)) setChosen(r.n); }}>
                    {chosen === r.n ? "chosen" : "use this one"}
                  </button>
                  <button className="bar-btn" onClick={() => setWide(wide == null ? r.n : null)}>
                    {wide == null ? "bigger" : "back to all"}
                  </button>
                  <a className="bar-btn" href={src} target="_blank" rel="noreferrer">open</a>
                </div>
              </figcaption>
            </figure>
          );
        })}
      </div>
    </div>
  );
}

export default function DelegateCall({ t, live, busy, onPick }) {
  const kind = DELEGATE_TOOL.exec(t.name)?.[1];
  if (kind === "shortlist") return <Shortlist t={t} busy={busy} onPick={onPick} />;
  const input = parse(t.raw);
  if (kind === "delegate") {
    const id = String(input.provider || "").toLowerCase();
    return (
      <Row t={t} live={live} brief={input.brief || t.detail || ""} head={
        <>
          <ProviderMark id={id} />
          {input.model && <span className="sl-model">{input.model}</span>}
          <span className="dg-task">{String(input.brief || "").split("\n")[0]}</span>
        </>
      } />
    );
  }
  const ran = variantsIn(t.result);
  const planned = Array.isArray(input.mix) && input.mix.length ? input.mix.map((m) => String(m).toLowerCase()) : null;
  const mix = mixOf(ran.length ? ran.map((v) => v.provider) : planned || []);
  const count = ran.length || planned?.length || input.n || 3;
  const failed = ran.filter((v) => v.failed).length;
  return (
    <Row t={t} live={live} brief={input.task || t.detail || ""} head={
      <>
        <span className="dg-fan">{count} versions</span>
        {mix.map((m) => <ProviderMark key={m.id} id={m.id} n={m.n} />)}
        {failed > 0 && <span className="dg-state bad">{failed} failed</span>}
        <span className="dg-task">{String(input.task || "").split("\n")[0]}</span>
      </>
    } />
  );
}
