import { useWidgetData } from "../lib/refresh";
import { HAS, QUIET } from "../lib/salience";
import { lead, restLine } from "../lib/notable";
import Lede from "../ui/Lede.jsx";
import WidgetError from "../ui/WidgetError.jsx";
import RefreshButton from "../ui/RefreshButton.jsx";
import Icon from "../ui/icons.jsx";
import { dragPin } from "../lib/drag.js";
import { fmtDur } from "../lib/time.js";

// Music card, on the lede form.
//
// This card had the most on it and the least at stake: four stat cells, the
// top track, a genre mix with a coverage caveat, the last thing played, and a
// rolling-week sparkline with a top artist — all of it about whether you put
// music on. Listening is something you did, never something you have to do,
// so it reports HAS or QUIET and earns even that.
//
// The top track survives the cut where the genre mix and the last-played line
// don't, because it's the one line here a person actually reads. The rest —
// plays browser, genres, ranges, the week — is the sheet's.
const CARD_METRICS = [
  { key: "listen_min", label: "Listened", worse: null, floor: 90, fmt: fmtDur },
  { key: "plays", label: "Tracks", worse: null, floor: 25,
    fmt: (v) => `${Math.round(v)}` },
];

export function useMusicCard() {
  const { data: sum, error, reload, busy: refreshing } = useWidgetData("/api/spotify/summary");

  const today = sum?.today;
  // Both readings share one baseline series of daily minutes: the backend
  // keeps per-day totals, not per-day play counts, so `plays` is scored
  // against minutes scaled by today's own minutes-per-track. Approximate, and
  // labelled as a track count only in the rest line — it exists so a day of
  // forty half-finished skips can still lead, which minutes alone would miss.
  const perTrack = today?.plays ? today.listen_min / today.plays : null;
  const readings = sum?.connected
    ? CARD_METRICS.map((m) => ({
        ...m, day: today.day,
        value: today[m.key] ?? null,
        series: (sum.baseline || []).map((p) => ({
          day: p.day,
          value: m.key === "listen_min" ? p.value
            : perTrack ? p.value / perTrack : null,
        })),
      }))
    : [];
  const hit = sum?.connected ? lead(readings) : null;
  const rest = restLine(readings);

  const level = !sum ? null : hit ? HAS : QUIET;
  const summary = !sum?.connected ? "spotify not connected"
    : today.plays ? rest : "nothing played today";

  return { sum, error, reload, refreshing, readings, hit, level, summary };
}

export default function MusicWidget({ card, onPin }) {
  const { sum, error, reload, refreshing, readings, hit } = card;
  const today = sum?.today;

  const pin = ["music · today", { source: "spotify", summary: sum }];

  return (
    <section className="widget" {...(sum?.connected ? dragPin(...pin) : {})}>
      <header>
        <h2>Music <span className="widget-sub">today</span></h2>
        <div className="widget-actions">
          <RefreshButton onClick={reload} busy={refreshing} />
          <button className="pin" disabled={!sum?.connected}
            onClick={() => onPin(...pin)}
            title="Attach today's listening, the week and 28-day baselines to your next console message"><Icon name="pin" /> pin</button>
        </div>
      </header>
      {error && <WidgetError error={error} />}
      {!sum && !error && <><div className="skel" /><div className="skel w60" /></>}
      {sum && !sum.connected && (
        <p className="widget-empty">
          Spotify not connected — run{" "}
          <code>python -m app.providers.spotify authorize</code> in the backend.
        </p>
      )}
      {sum?.connected && !today.plays && (
        <p className="widget-empty">Nothing played today.</p>
      )}
      {sum?.connected && today.plays > 0 && (
        <>
          <Lede hit={hit} readings={readings} label="music" />
          {today.top_track && (
            <p className="music-line" title="Most-repeated track today"
              {...dragPin(`track · ${today.top_track.track}`, today.top_track)}>
              <span className="music-note"><Icon name="music" size={12} /></span>
              <span className="music-track">{today.top_track.track}</span>
              <span className="music-artist"> · {today.top_track.artist}</span>
              {today.top_track.count > 1 && (
                <span className="music-count"> ×{today.top_track.count}</span>
              )}
            </p>
          )}
        </>
      )}
    </section>
  );
}
