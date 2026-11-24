import { useEffect, useState } from "react";
import SheetHead from "../ui/SheetHead.jsx";
import Band from "../ui/Band.jsx";
import { fetchJSON } from "../lib/api";
import { useSheetData } from "../lib/sheet.js";
import { fmtAgo, fmtDay, fmtTime } from "../lib/time";
import { dragPin } from "../lib/drag.js";

// The schedule: what's next, how's the season. Whole year, no range picker (seasons don't fit a
// week or month); exhibitions stay on the schedule and out of a record.

const VIEWS = [null];
const NEXT_N = 8;
const STRIP_MAX = 14;           // a sport's most recent games
const OUTCOME = { W: "won", L: "lost", T: "tied" };

const sportName = (s) => s.replace(/^(Men's|Women's) /, (m, who) => `${who === "Men's" ? "M" : "W"} `);
const outcome = (g) => g.result?.trim()[0]?.toUpperCase();
const opponent = (g) => g.opponent?.replace(/\s*\(Exhibition\)$/, "") || g.note || "Meet";
const prefix = { home: "vs", away: "at", neutral: "vs" };

function Season({ games }) {
  const bySport = new Map();
  for (const g of games) {
    if (!OUTCOME[outcome(g)] || /exhibition/i.test(g.opponent || "")) continue;
    if (!bySport.has(g.sport)) bySport.set(g.sport, []);
    bySport.get(g.sport).push(g);
  }
  const sports = [...bySport.entries()].sort((a, b) => b[1].length - a[1].length);
  if (!sports.length) return <p className="widget-empty">No results yet this year.</p>;
  return (
    <ul className="sp-season">
      {sports.map(([sport, list]) => {
        const n = (o) => list.filter((g) => outcome(g) === o).length;
        const ties = n("T");
        return (
          <li key={sport} {...dragPin(`${sport} · season`, list)}>
            <span className="sp-name" title={sport}>{sportName(sport)}</span>
            <span className="sp-strip">
              {list.slice(-STRIP_MAX).map((g) => (
                <i key={g.uid} className={`sp-${outcome(g).toLowerCase()}`}
                  title={`${fmtDay(g.day)}, ${prefix[g.side] || "vs"} ${opponent(g)}: ${g.result}`}>{outcome(g)}</i>
              ))}
            </span>
            <span className="sp-record">{n("W")}-{n("L")}{ties ? `-${ties}` : ""}</span>
          </li>
        );
      })}
    </ul>
  );
}

export default function SportsSheet({ onPin }) {
  const { rows, refreshedAt, error, loading, payloads, reload } = useSheetData("athletics", VIEWS, "this-year");
  const [today, setToday] = useState("");
  useEffect(() => { fetchJSON("/api/healthz").then((h) => setToday(h?.day || "")).catch(() => {}); }, []);

  const games = [...rows("")].sort((a, b) => (a.start || a.day).localeCompare(b.start || b.day));
  const next = today ? games.filter((g) => g.day >= today && !g.result).slice(0, NEXT_N) : [];
  return (
    <>
      <SheetHead refreshedAt={fmtAgo(refreshedAt)} label="usc sports · this year"
        data={error ? null : payloads && { games: games.length, next }} onPin={onPin} onRefresh={reload} />
      {error && <p className="widget-error">{error}</p>}
      {loading && !payloads && <p className="widget-empty">Loading…</p>}
      {payloads && !error && (
        <>
          <Band title="Next up">
            {next.length === 0 ? <p className="widget-empty">Nothing else scheduled this year.</p> : (
              <ul className="event-list">
                {next.map((g) => (
                  <li key={g.uid} {...dragPin(`${g.sport} · ${opponent(g)}`, g)}>
                    <span className="event-time">{fmtDay(g.day)}{g.time_tba || !g.start?.includes("T") ? "" : `, ${fmtTime(g.start)}`}</span>
                    <span className="event-summary">{sportName(g.sport)} {prefix[g.side] || "vs"} {opponent(g)}</span>
                    <span className="event-cal">{g.tv || ""}</span>
                  </li>
                ))}
              </ul>
            )}
          </Band>
          <Band title="Season" note="oldest to newest"><Season games={games} /></Band>
        </>
      )}
    </>
  );
}
