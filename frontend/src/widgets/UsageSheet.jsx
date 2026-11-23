import { useEffect, useState } from "react";
import { fetchJSON } from "../lib/api";

// What the desk's ranking got right, read back from /api/usage.
//
// The hard part is not the numbers, it's not lying with them. A card read at a
// glance leaves no click, so a plain "least clicked" list would put the cards
// that work best at the top of a list headed "candidates to cut". Every row
// therefore shows the band history next to the touch count, and the only
// automatic verdict — `idle` — requires *both* that the desk judged the card
// had nothing to say on every day it saw it, and that you never opened it.
//
// Nothing here deletes or hides a card. It answers a question; the cut is
// still a decision you make.

const RANGES = [
  ["7", "week"],
  ["30", "month"],
  ["365", "all"],
];

function ago(iso) {
  if (!iso) return "never";
  const days = Math.floor((Date.now() - new Date(iso).getTime()) / 86400000);
  if (days <= 0) return "today";
  if (days === 1) return "yesterday";
  if (days < 30) return `${days}d ago`;
  return `${Math.floor(days / 30)}mo ago`;
}

function Row({ row }) {
  // Two denominators, deliberately: days_seen is how long the card has been
  // on the desk (a card added last week can't have a month of history), and
  // days_active is how much of that it had something to report.
  const quiet = row.days_seen - row.days_active;
  return (
    <tr className={row.idle ? "usage-idle" : ""}>
      <td>{row.label}</td>
      <td className="usage-bands" title={`${row.days_active} of ${row.days_seen} days it had something to say; quiet on ${quiet}`}>
        <span className="usage-active">{row.days_active}</span>
        <span className="usage-sep">/</span>
        <span className="usage-seen">{row.days_seen}</span>
      </td>
      <td className="usage-touch" title={`${row.opens} unfolds, ${row.sheet_opens} sheet opens, on ${row.days_touched} days`}>
        {row.touches || "—"}
      </td>
      <td className="usage-last">{ago(row.last_touch)}</td>
    </tr>
  );
}

export default function UsageSheet() {
  const [days, setDays] = useState("30");
  const [data, setData] = useState(null);
  const [err, setErr] = useState(null);

  useEffect(() => {
    let live = true;
    setErr(null);
    fetchJSON(`/api/usage/summary?days=${days}`)
      .then((d) => { if (live) setData(d); })
      .catch((e) => { if (live) setErr(e.message); });
    return () => { live = false; };
  }, [days]);

  if (err) return <p className="widget-error">{err}</p>;
  if (!data) return <p className="widget-empty">Reading…</p>;

  const labels = data.labels || [];
  const idle = labels.filter((r) => r.idle);

  return (
    <div className="usage-sheet">
      <div className="chip-row">
        {RANGES.map(([v, label]) => (
          <button key={v} className={`chip${days === v ? " active" : ""}`}
            onClick={() => setDays(v)}>{label}</button>
        ))}
      </div>

      {!labels.length ? (
        <p className="widget-empty">
          Nothing recorded yet — the desk starts counting on its next load.
        </p>
      ) : (
        <>
          <p className="usage-lede">
            {data.days_covered} day{data.days_covered === 1 ? "" : "s"} recorded
            since {data.since}.
            {idle.length > 0 && ` ${idle.length} card${idle.length === 1 ? " has" : "s have"} been quiet every day and never opened.`}
          </p>

          <table className="slice-table usage-table">
            <thead>
              <tr>
                <th>card</th>
                {/* "active" is days the card had something to say, out of the
                    days it was on the desk. A low ratio isn't a bad card —
                    it's a card that is usually correctly folded. */}
                <th title="days with something to say / days on the desk">active</th>
                <th title="unfolds plus sheet opens">opened</th>
                <th>last</th>
              </tr>
            </thead>
            <tbody>
              {labels.map((r) => (
                <Row key={r.label} row={r} />
              ))}
            </tbody>
          </table>

          <p className="usage-note">
            An unopened card in the top rows is not automatically dead — a card
            that says its piece in one line has done its job without a click.
            The greyed rows are the narrow case worth a second look: quiet on
            every recorded day <em>and</em> never opened.
          </p>
        </>
      )}

      {data.commands?.length > 0 && (
        <>
          <h4 className="usage-head">⌘K</h4>
          <table className="slice-table usage-table">
            <tbody>
              {data.commands.map((c) => (
                <tr key={c.label}>
                  <td>{c.label}</td>
                  <td className="usage-touch">{c.runs}</td>
                  <td className="usage-last">{ago(c.last_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      )}
    </div>
  );
}
