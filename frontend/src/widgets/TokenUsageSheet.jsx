import { useState } from "react";
import { useWidgetData } from "../lib/refresh";

// Model usage, off the session transcripts (app/token_usage.py): tokens and
// API-equivalent cost per tab, model, mode, project or day.
//
// Leads with: did model tiering save anything (lede reprices the same tokens on one model) and
// where did the 5h / weekly window go. "Cost" is list price, not a bill; the note under the table says so.
// Design: DESIGN.md → Model usage (`app/token_usage.py`, `widgets/TokenUsageSheet.jsx`).

const RANGES = [["5h", "5h window"], ["week", "week window"], ["today", "today"],
  ["30d", "30 days"], ["90d", "90 days"], ["all", "all"]];
const GROUPS = [["tab", "tab"], ["model", "model"], ["provider", "provider"],
  ["account", "account"], ["mode", "mode"], ["project", "project"], ["day", "day"]];

const usd = (v) => (v >= 100 ? `$${Math.round(v).toLocaleString()}`
  : v >= 1 ? `$${v.toFixed(2)}` : v > 0 ? `$${v.toFixed(3)}` : "$0");

const count = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;

function tok(n) {
  if (n >= 1e9) return `${(n / 1e9).toFixed(2)}B`;
  if (n >= 1e6) return `${(n / 1e6).toFixed(1)}M`;
  if (n >= 1e3) return `${Math.round(n / 1e3)}k`;
  return String(n);
}

// An epoch-seconds stamp as "5m ago" / "3h ago" / "2d ago".
function ago(at) {
  if (!at) return "never";
  const s = Math.max(0, Date.now() / 1000 - at);
  if (s < 90) return "just now";
  if (s < 5400) return `${Math.round(s / 60)}m ago`;
  if (s < 129600) return `${Math.round(s / 3600)}h ago`;
  return `${Math.round(s / 86400)}d ago`;
}

const perM = (v) => (v == null ? "?" : v >= 1 ? `$${+v.toFixed(2)}` : `$${+v.toPrecision(2)}`);

function Tiering({ mix, ifAll }) {
  // The one comparison that answers "does the mix save anything": what the
  // priced calls cost, errands included, against the same tokens on each
  // single model. Unpriced calls are on neither side.
  const parts = Object.entries(ifAll || {}).filter(([, v]) => v > 0);
  if (!parts.length || !mix) return null;
  return (
    <p className="usage-lede">
      The same tokens on one model:{" "}
      {parts.map(([m, v], i) => {
        const saved = 1 - mix / v;
        return (
          <span key={m}>
            {i > 0 && " · "}
            {m} {usd(v)}{" "}
            <span className={saved >= 0 ? "tu-good" : "tu-bad"}>
              ({saved >= 0 ? `mix saves ${Math.round(saved * 100)}%`
                : `mix costs ${Math.round((mix / v - 1) * 100)}% more`})
            </span>
          </span>
        );
      })}
    </p>
  );
}

function Cost({ row }) {
  // Unpriced is said, never shown as $0: a model no list has is a gap in the
  // number, not a free call.
  if (row.unpriced && !row.cost) return <span className="tu-unpriced">unpriced</span>;
  return (
    <>
      {usd(row.cost)}
      {row.unpriced > 0 && <span className="tu-unpriced" title={`${row.unpriced} unpriced`}> +?</span>}
    </>
  );
}

function Providers({ list }) {
  // Only once something besides Claude ran in the range: Antigravity and Codex
  // errands (the delegates log), priced from the same lists.
  if (!list || list.length < 2) return null;
  return (
    <p className="usage-lede">
      By provider:{" "}
      {list.map((p, i) => (
        <span key={p.key}>
          {i > 0 && " · "}
          {p.label} {tok(p.tokens)} (<Cost row={p} />), {count(p.calls, p.key === "claude" ? "call" : "run")}
        </span>
      ))}
    </p>
  );
}

function Prices({ prices, sources }) {
  // Where each model's price came from and how fresh it is, so a number can
  // be checked; an unpriced model says why.
  if (!prices?.length) return null;
  const by = Object.fromEntries((sources || []).map((s) => [s.id, s]));
  const failed = (sources || []).filter((s) => !s.ok && s.note);
  return (
    <>
      <h3 className="usage-head">Prices, $ per million tokens</h3>
      <table className="slice-table usage-table tu-table tu-prices">
        <thead>
          <tr>
            <th>model</th>
            <th title="fresh input">in</th>
            <th>out</th>
            <th title="cache read">cached</th>
            <th>from</th>
          </tr>
        </thead>
        <tbody>
          {prices.map((p) => {
            const pr = p.price;
            const src = pr && by[pr.source];
            return (
              <tr key={`${p.provider}:${p.model}`}>
                <td className="tu-label">
                  <span title={p.model}>{p.label}</span>
                  <small className="tu-sub">
                    {p.provider}{pr && pr.listed_as !== p.model ? ` · listed as ${pr.listed_as}` : ""}
                    {pr?.long_above ? ` · higher past ${tok(pr.long_above)} prompt` : ""}
                    {p.gap ? ` · ${p.gap}` : ""}
                  </small>
                </td>
                {pr ? (
                  <>
                    <td className="usage-touch">{perM(pr.input)}</td>
                    <td className="usage-touch">{perM(pr.output)}</td>
                    <td className="usage-touch">{perM(pr.cache_read)}</td>
                    <td className="usage-touch tu-from" title={src?.url || ""}>
                      {src?.label || pr.source}
                      <small className="tu-sub">{pr.source === "builtin" ? "by hand" : ago(pr.fetched_at)}</small>
                    </td>
                  </>
                ) : (
                  <td colSpan={4} className="usage-touch">
                    <span className="tu-unpriced">unpriced</span>
                    <small className="tu-sub">{p.unpriced_why}</small>
                  </td>
                )}
              </tr>
            );
          })}
        </tbody>
      </table>
      <p className="usage-note">
        Tried in order: Claude Code&apos;s own list for Claude, then LiteLLM&apos;s, then
        OpenRouter&apos;s, then the built-in table (Claude only). Fetched daily; a failed
        fetch keeps the last good list.
        {failed.map((s) => ` ${s.label}: last try failed ${ago(s.tried_at)} (${s.note}).`)}
      </p>
    </>
  );
}

function Detail({ row, group }) {
  const outside = ((group === "model" || group === "account") && row.provider && row.provider !== "claude")
    || (group === "provider" && row.key !== "claude");
  if (group === "model") {
    return (
      <small className="tu-sub">
        {row.provider} · {count(row.calls, outside ? "run" : "call")}
        {row.price ? ` · ${row.price.source_label}` : " · no price"}
      </small>
    );
  }
  if (outside) {
    return <small className="tu-sub">{count(row.calls, "run")}</small>;
  }
  if (group === "tab") {
    return (
      <small className="tu-sub">
        {row.mode} · {row.project}{row.worktree ? ` · ${row.worktree}` : ""}
        {row.models?.length ? ` · ${row.models.join(", ")}` : ""}
      </small>
    );
  }
  return <small className="tu-sub">{count(row.sessions, "session")} · {count(row.calls, "call")}</small>;
}

function Table({ range, group }) {
  const { data, error } = useWidgetData(
    `/api/token-usage/summary?range=${range}&group=${group}`);
  if (error) return <p className="widget-error">{error}</p>;
  if (!data) return <p className="widget-empty">Reading the transcripts…</p>;
  const { total, rows } = data;
  if (!rows.length) return <p className="widget-empty">No model calls in {data.what}.</p>;
  const top = Math.max(...rows.map((r) => r.cost), 1e-9);
  return (
    <>
      <p className="usage-lede">
        <strong className="tu-big">{usd(total.cost)}</strong> API-equivalent in {data.what}:{" "}
        {tok(total.tokens)} tokens over {total.sessions} session{total.sessions === 1 ? "" : "s"}.
        Prompt caching saved {usd(total.cache_saved)} ({tok(total.cache_read)} read from cache).
        {total.unpriced > 0 && <> <span className="tu-unpriced">{count(total.unpriced, "call")} unpriced</span>, not in the total.</>}
      </p>
      <Providers list={data.providers} />
      <Tiering mix={data.mix_cost} ifAll={data.if_all_on} />
      <table className="slice-table usage-table tu-table">
        <thead>
          <tr>
            <th>{group}</th>
            <th title="output tokens">out</th>
            <th title="input + cache read + cache write">in</th>
            <th title="API-equivalent cost, list prices">cost</th>
            <th aria-label="share" />
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.key}>
              <td className="tu-label">
                <span title={r.label}>{r.label}</span>
                <Detail row={r} group={group} />
              </td>
              <td className="usage-touch">{tok(r.output)}</td>
              <td className="usage-touch" title={`${tok(r.input)} fresh · ${tok(r.cache_read)} cached · ${tok(r.cache_write)} written`}>
                {tok(r.input + r.cache_read + r.cache_write)}
              </td>
              <td className="usage-touch" title={`without caching ${usd(r.uncached)}`}>
                <Cost row={r} />
              </td>
              <td className="tu-share"><span style={{ width: `${(r.cost / top) * 100}%` }} /></td>
            </tr>
          ))}
        </tbody>
      </table>
      {data.more > 0 && <p className="usage-note">{data.more} more below these.</p>}
      <p className="usage-note">{data.note}</p>
      <Prices prices={data.prices} sources={data.price_sources} />
    </>
  );
}

export default function TokenUsageSheet() {
  const [range, setRange] = useState("week");
  const [group, setGroup] = useState("tab");
  return (
    <div className="usage-sheet tu-sheet">
      <div className="chip-row" role="radiogroup" aria-label="Range">
        {RANGES.map(([v, label]) => (
          <button key={v} role="radio" aria-checked={range === v}
            className={`chip${range === v ? " active" : ""}`}
            onClick={() => setRange(v)}>{label}</button>
        ))}
      </div>
      <div className="chip-row" role="radiogroup" aria-label="Group by">
        {GROUPS.map(([v, label]) => (
          <button key={v} role="radio" aria-checked={group === v}
            className={`chip${group === v ? " active" : ""}`}
            onClick={() => setGroup(v)}>by {label}</button>
        ))}
      </div>
      {/* Keyed, so a new range or grouping is a fresh load, not an effect. */}
      <Table key={`${range}:${group}`} range={range} group={group} />
    </div>
  );
}
