import { useState } from "react";
import { useWidgetData } from "../lib/refresh";

// Model usage, off the session transcripts (app/token_usage.py): tokens and
// API-equivalent cost per tab, model, mode, project or day.
//
// Two questions lead the sheet because they're why it exists: did the model
// tiering save anything (the lede reprices the same tokens as if one model
// had done it all), and where did the 5h / weekly window go (the range chips
// start on the live window when the usage chips know its reset).
//
// "Cost" is list price for the tokens, not a bill: on a subscription nothing
// is charged per token. The note under the table says so every time.

const RANGES = [["5h", "5h window"], ["week", "week window"], ["today", "today"],
  ["30d", "30 days"], ["90d", "90 days"], ["all", "all"]];
const GROUPS = [["tab", "tab"], ["model", "model"], ["provider", "provider"],
  ["mode", "mode"], ["project", "project"], ["day", "day"]];

const usd = (v) => (v >= 100 ? `$${Math.round(v).toLocaleString()}`
  : v >= 1 ? `$${v.toFixed(2)}` : v > 0 ? `$${v.toFixed(3)}` : "$0");

const count = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;

function tok(n) {
  if (n >= 1e9) return `${(n / 1e9).toFixed(2)}B`;
  if (n >= 1e6) return `${(n / 1e6).toFixed(1)}M`;
  if (n >= 1e3) return `${Math.round(n / 1e3)}k`;
  return String(n);
}

function Tiering({ claude, ifAll }) {
  // The one comparison that answers "does tiering save anything": what
  // Claude's calls cost, against the same tokens on each single Claude
  // model. Gemini and Codex errands aren't in it on either side.
  const parts = Object.entries(ifAll || {}).filter(([, v]) => v > 0);
  if (!parts.length || !claude) return null;
  return (
    <p className="usage-lede">
      Claude&apos;s tokens on one model:{" "}
      {parts.map(([m, v], i) => {
        const saved = 1 - claude / v;
        return (
          <span key={m}>
            {i > 0 && " · "}
            {m} {usd(v)}{" "}
            <span className={saved >= 0 ? "tu-good" : "tu-bad"}>
              ({saved >= 0 ? `mix saves ${Math.round(saved * 100)}%`
                : `mix costs ${Math.round((claude / v - 1) * 100)}% more`})
            </span>
          </span>
        );
      })}
    </p>
  );
}

function Providers({ list }) {
  // Only once something besides Claude ran in the range: Gemini and Codex
  // errands (the delegates log), counted in tokens; they have no price here.
  if (!list || list.length < 2) return null;
  return (
    <p className="usage-lede">
      By provider:{" "}
      {list.map((p, i) => (
        <span key={p.key}>
          {i > 0 && " · "}
          {p.label} {tok(p.tokens)}{p.cost ? ` (${usd(p.cost)})` : ""}, {count(p.calls, p.key === "claude" ? "call" : "run")}
        </span>
      ))}
    </p>
  );
}

function Detail({ row, group }) {
  const outside = (group === "model" && row.provider && row.provider !== "claude")
    || (group === "provider" && row.key !== "claude");
  if (outside) {
    return (
      <small className="tu-sub">
        {group === "model" ? `${row.provider} · ` : ""}{count(row.calls, "run")} · tokens only
      </small>
    );
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
      </p>
      <Providers list={data.providers} />
      <Tiering claude={data.claude_cost} ifAll={data.if_all_on} />
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
                {r.unpriced && !r.cost ? "—" : usd(r.cost)}
              </td>
              <td className="tu-share"><span style={{ width: `${(r.cost / top) * 100}%` }} /></td>
            </tr>
          ))}
        </tbody>
      </table>
      {data.more > 0 && <p className="usage-note">{data.more} more below these.</p>}
      <p className="usage-note">{data.note}</p>
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
