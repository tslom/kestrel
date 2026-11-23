import { useEffect, useState } from "react";
import RangePicker from "../ui/RangePicker.jsx";
import SheetHead from "../ui/SheetHead.jsx";
import YearHeat from "../ui/CalendarHeat.jsx";
import { fetchJSON } from "../lib/api";
import Band from "../ui/Band.jsx";
import { Bars, RankBars, Stat, rankNote } from "../ui/charts.jsx";
import { useSheetData } from "../lib/sheet.js";
import { capRows } from "../lib/pin.js";
import { fmtAgo, fmtDay, fmtDur, fmtTime, shortDay } from "../lib/time";
import { dragPin } from "../lib/drag.js";

// Music sheet — a listening habit, not a metric picker (IDEAS §1).
//
// Spotify's six metrics are all answers to two questions: *how much* and
// *what*. The generic panel made you pick one at a time and then read
// `top_repeat` as a word, which is how a day of listening ended up looking
// like a schema. Here the day's totals sit on top, the hour histogram says
// when you actually listen, and artists and tracks are ranked against each
// other rather than listed — the comparison is the whole point of a top ten.
//
// `bracket` (first and last play of the day) is deliberately not read: the
// hour histogram already says when the listening happened, in more detail,
// and a bracket that runs past midnight can't be drawn on a midnight-anchored
// axis without lying about which day it belongs to.
const VIEWS = ["plays", "listen_min", "novelty", "top_repeat", "shape"];

const TOP_N = 8;

const sum = (rows, f) => rows.reduce((s, r) => s + (f(r) || 0), 0);

// Plays are stored as instants (`played_at` is UTC with a Z), unlike calendar
// events, which the backend has already re-expressed in the zone they'll be
// lived in. There's no zone attached to a play, so the browser's is the only
// one available: a run of songs played in another timezone lands in the hour
// it would have been *here*. For "when do I listen", that's the honest read.
const hourOf = (iso) => new Date(iso).getHours();
const hourLabel = (h) => `${h % 12 || 12}${h < 12 ? "a" : "p"}`;

// The most-played N of something, by whatever `weigh` counts. Keyed on the
// Spotify id rather than the name so two artists with the same track title
// don't merge into one row.
function top(plays, keyOf, labelOf, weigh) {
  const acc = new Map();
  for (const p of plays) {
    const k = keyOf(p);
    if (!k) continue;
    const cur = acc.get(k) || { key: k, label: labelOf(p), value: 0, plays: [] };
    cur.value += weigh(p);
    cur.plays.push(p);
    acc.set(k, cur);
  }
  const all = [...acc.values()].sort((a, b) => b.value - a.value);
  return { all, top: all.slice(0, TOP_N) };
}

// What the listening adds up to, from Last.fm's tags on the same plays. A share
// of tagged plays, and a tag is a crowd's word for an artist, so the note says
// how much of the range the tags cover.
function Genres({ range }) {
  const [state, setState] = useState(null);
  useEffect(() => {
    let live = true;
    const read = (metric) => fetchJSON(`/api/lastfm?${new URLSearchParams({ range, metric })}`);
    Promise.all([read("genres"), read("coverage")])
      .then(([g, c]) => { if (live) setState({ genres: g.data, coverage: c.data }); })
      .catch((e) => { if (live) setState({ error: e.message }); });
    return () => { live = false; };
  }, [range]);
  if (!state) return null;
  if (state.error) return <Band title="Genres"><p className="widget-error">{state.error}</p></Band>;
  const seen = state.coverage.filter((r) => r.value != null);
  const covered = seen.length ? sum(seen, (r) => r.value) / seen.length : null;
  return (
    <Band title="Genres" note={covered != null ? `${Math.round(covered * 100)}% of plays tagged` : ""}>
      <RankBars rows={state.genres.slice(0, TOP_N).map((g) => ({
        key: g.genre, label: g.genre, value: g.plays, data: g }))}
        fmt={(v) => `${v} plays`}
        pinLabel={(r) => `genre · ${r.label}`}
        empty="No tagged plays in this range." />
    </Band>
  );
}

// `minutes` comes from the provider's own `listen_min` rather than being
// re-summed from these rows: it is the same arithmetic over the same plays,
// and two derivations of one number are two chances to print a different one.
function PlayDays({ plays, minutes }) {
  if (plays.length === 0) return <p className="widget-empty">No plays in this range.</p>;
  const byDay = new Map();
  for (const p of plays) {
    if (!byDay.has(p.day)) byDay.set(p.day, []);
    byDay.get(p.day).push(p);
  }
  return [...byDay.entries()].map(([day, rows]) => (
    <section key={day} className="slice-day"
      {...dragPin(`plays · ${fmtDay(day)}`, capRows(rows))}>
      <h3>{fmtDay(day)}
        <span className="day-note">{rows.length} plays{minutes.has(day) ? ` · ${fmtDur(minutes.get(day))}` : ""}</span>
      </h3>
      <ul className="event-list">
        {rows.map((p) => (
          <li key={p.played_at} {...dragPin(`play · ${p.track}`, p)}>
            <span className="event-time">{fmtTime(p.played_at)}</span>
            <span className="event-summary" title={p.album || undefined}>{p.track}</span>
            <span className="event-cal">{p.artist}</span>
          </li>
        ))}
      </ul>
    </section>
  ));
}

export default function MusicSheet({ onPin }) {
  const { ranges, range, pickRange, rows, refreshedAt, error, loading, payloads, reload }
    = useSheetData("spotify", VIEWS, "this-week");

  const plays = rows("plays");
  const minutes = rows("listen_min");
  const shape = rows("shape");
  const totalMin = sum(minutes, (r) => r.value);
  const minPerDay = new Map(minutes.map((r) => [r.day, r.value]));

  // Novelty is a share of *that day's* plays, so the range's figure is the
  // plays-weighted mean, not the mean of the daily shares — a two-song day
  // with one new artist would otherwise count as much as a two-hour one.
  const playsPerDay = new Map();
  for (const p of plays) playsPerDay.set(p.day, (playsPerDay.get(p.day) || 0) + 1);
  const novRows = rows("novelty").filter((r) => r.value != null && playsPerDay.has(r.day));
  const novWeight = sum(novRows, (r) => playsPerDay.get(r.day));
  const novelty = novWeight
    ? sum(novRows, (r) => r.value * playsPerDay.get(r.day)) / novWeight : null;

  const artists = top(plays, (p) => p.artist_id || p.artist_key, (p) => p.artist,
    (p) => p.ms / 60000);
  const tracks = top(plays, (p) => p.track_id, (p) => p.track, () => 1);

  const longest = shape.length ? Math.max(...shape.map((r) => r.longest_listen_min)) : null;

  // One bucket per hour, always all 24 — the empty hours are the shape of the
  // day as much as the full ones, and dropping them would slide 2am next to
  // 9am and call it a habit.
  const byHour = Array.from({ length: 24 }, (_, h) => ({ key: h, label: hourLabel(h), v: 0 }));
  for (const p of plays) byHour[hourOf(p.played_at)].v += 1;
  const peak = byHour.reduce((a, b) => (b.v > a.v ? b : a), byHour[0]);

  const days = minutes.length;

  const pinLabel = `music · ${range}`;
  const pinData = payloads && {
    range,
    minutes: Math.round(totalMin),
    plays: plays.length,
    artists: artists.all.length,
    top_artists: artists.top.map((a) => ({ artist: a.label, minutes: Math.round(a.value) })),
    top_tracks: tracks.top.map((t) => ({ track: t.label, plays: t.value })),
  };

  return (
    <>
      <SheetHead refreshedAt={fmtAgo(refreshedAt)} label={pinLabel}
        data={error ? null : pinData} onPin={onPin} onRefresh={reload} />

      <RangePicker ranges={ranges}
        range={range} onPick={pickRange} />

      {error && <p className="widget-error">{error}</p>}
      {loading && !payloads && <p className="widget-empty">Loading…</p>}

      {payloads && !error && (
        <>
          <Band title="Listening" note={totalMin ? fmtDur(totalMin) : ""}
            pin={[`listening · ${range}`, { minutes: totalMin, days: minutes }]}>
            <div className="hero-row">
              <Stat value={plays.length.toLocaleString()} label="Plays"
                sub={days > 1 ? `${Math.round(plays.length / days)}/day` : ""} />
              <Stat value={artists.all.length.toLocaleString()} label="Artists"
                sub={tracks.all.length ? `${tracks.all.length} tracks` : ""} />
              {novelty != null && (
                <Stat value={`${Math.round(novelty * 100)}%`} label="New artists"
                  sub="first heard this week" />
              )}
              {longest != null && (
                <Stat value={fmtDur(longest)} label="Longest sitting"
                  sub={`${Math.round(sum(shape, (r) => r.listen_sessions))} sittings`} />
              )}
            </div>
            {days > 1 && (
              <Bars items={minutes.map((r) => ({ key: r.day, label: shortDay(r.day), v: r.value }))}
                series={[{ key: "min", label: "minutes", cls: "seg-music" }]}
                fmt={(v) => `${Math.round(v)}m`}
                pin={(it) => [`listening · ${it.label}`, { day: it.key, minutes: it.v }]}
                empty="No listening in this range." />
            )}
          </Band>

          <Band title="When" note={plays.length ? `peak ${hourLabel(peak.key)}` : ""}>
            <Bars items={byHour} height={130} avgLine={false}
              series={[{ key: "plays", label: "plays", cls: "seg-music" }]}
              pin={(it) => [`plays · ${it.label}`,
                capRows(plays.filter((p) => hourOf(p.played_at) === it.key))]}
              empty="No plays in this range." />
          </Band>

          <Band title="Artists" note={rankNote(TOP_N, artists.all.length)}>
            <RankBars rows={artists.top.map((a) => ({
              key: a.key, label: a.label, value: a.value,
              data: { artist: a.label, minutes: Math.round(a.value), plays: a.plays.length },
            }))}
              fmt={(v) => fmtDur(v)}
              pinLabel={(r) => `artist · ${r.label}`}
              empty="No plays in this range." />
          </Band>

          <Genres range={range} />

          <YearHeat source="spotify" metric="listen_min" label="listening" fmt={(v) => fmtDur(v)} />

          <Band title="Tracks" note={rankNote(TOP_N, tracks.all.length)}>
            <RankBars rows={tracks.top.map((t) => ({
              key: t.key, label: t.label, value: t.value,
              title: `${t.label} — ${t.plays[0].artist}`,
              data: { track: t.label, artist: t.plays[0].artist, plays: t.value },
            }))}
              fmt={(v) => `×${v}`}
              pinLabel={(r) => `track · ${r.label}`}
              empty="No plays in this range." />
          </Band>

          {days > 1 && rows("top_repeat").length > 0 && (
            <Band title="On repeat" note="one track per day"
              pin={[`on repeat · ${range}`, rows("top_repeat")]}>
              <ul className="event-list">
                {[...rows("top_repeat")].reverse().map((r) => (
                  <li key={r.day} {...dragPin(`on repeat · ${fmtDay(r.day)}`, r)}>
                    <span className="event-time">{shortDay(r.day)}</span>
                    <span className="event-summary">{r.track}</span>
                    <span className="event-cal">{r.artist} ×{r.count}</span>
                  </li>
                ))}
              </ul>
            </Band>
          )}

          <Band title="Plays" note={`${plays.length}`}
            pin={[`plays · ${range}`, capRows(plays)]}>
            <PlayDays plays={plays} minutes={minPerDay} />
          </Band>
        </>
      )}
    </>
  );
}
