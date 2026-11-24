import { useEffect, useState } from "react";
import RangePicker from "../ui/RangePicker.jsx";
import SheetHead from "../ui/SheetHead.jsx";
import Band from "../ui/Band.jsx";
import Icon from "../ui/icons.jsx";
import { Stat } from "../ui/charts.jsx";
import { fetchJSON } from "../lib/api";
import { useSheetData } from "../lib/sheet.js";
import { fmtAgo, fmtDay, shortDay } from "../lib/time";
import { weatherIcon } from "./GlanceStrip.jsx";

// Each day a low-to-high span on one shared scale (a cold snap sits visibly left); two lines would make you subtract.

const VIEWS = ["tmax", "tmin", "precip_prob", "weather_code", "wind_speed", "uv_index"];
const RAIN_WORTH_SAYING = 10;   // percent; under this the column stays blank

const deg = (v) => `${Math.round(v)}°`;

function Days({ days, today }) {
  const lo = Math.min(...days.map((d) => d.lo)), hi = Math.max(...days.map((d) => d.hi));
  const at = (v) => `${((v - lo) / Math.max(1, hi - lo)) * 100}%`;
  return (
    <ul className="wx-days">
      {days.map((d) => (
        <li key={d.day} className={d.day === today ? "today" : d.day < today ? "past" : undefined}
          title={[`${deg(d.lo)} to ${deg(d.hi)}`, d.rain != null && `${Math.round(d.rain)}% chance of rain`,
                  d.wind != null && `wind ${Math.round(d.wind)} mph`, d.uv != null && `UV ${Math.round(d.uv)}`]
            .filter(Boolean).join(", ")}>
          <span className="wx-day">{d.day === today ? "Today" : fmtDay(d.day)}</span>
          <span className="wx-ico">{d.code != null && <Icon name={weatherIcon({ weather_code: d.code, is_day: true })} size={14} />}</span>
          <span className="wx-track">
            <span className="wx-span" style={{ left: at(d.lo), right: `calc(100% - ${at(d.hi)})` }}>
              <b className="wx-lo">{deg(d.lo)}</b><b className="wx-hi">{deg(d.hi)}</b>
            </span>
          </span>
          <span className="wx-rain">{d.rain >= RAIN_WORTH_SAYING ? `${Math.round(d.rain)}%` : ""}</span>
        </li>
      ))}
    </ul>
  );
}

export default function WeatherSheet({ onPin }) {
  const { ranges, range, pickRange, rows, refreshedAt, error, loading, payloads, reload }
    = useSheetData("weather", VIEWS, "this-week");
  const [today, setToday] = useState("");
  useEffect(() => { fetchJSON("/api/healthz").then((h) => setToday(h?.day || "")).catch(() => {}); }, []);

  const of = (metric) => new Map(rows(metric).map((r) => [r.day, r.value]));
  const lows = of("tmin"), rain = of("precip_prob"), codes = of("weather_code"),
    wind = of("wind_speed"), uv = of("uv_index");
  const days = rows("tmax").filter((r) => r.value != null && lows.get(r.day) != null)
    .map((r) => ({ day: r.day, hi: r.value, lo: lows.get(r.day), rain: rain.get(r.day),
                   code: codes.get(r.day), wind: wind.get(r.day), uv: uv.get(r.day) }));
  const most = (key) => days.filter((d) => d[key] != null)
    .reduce((a, d) => (!a || d[key] > a[key] ? d : a), null);
  const warm = most("hi"), wet = most("rain"), sun = most("uv");
  const cold = days.reduce((a, d) => (!a || d.lo < a.lo ? d : a), null);

  const pinLabel = `weather · ${range}`;
  return (
    <>
      <SheetHead refreshedAt={fmtAgo(refreshedAt)} label={pinLabel}
        data={error ? null : payloads && { range, days }} onPin={onPin} onRefresh={reload} />
      <RangePicker ranges={ranges} range={range} onPick={pickRange} />
      {error && <p className="widget-error">{error}</p>}
      {loading && !payloads && <p className="widget-empty">Loading…</p>}
      {payloads && !error && (days.length === 0
        ? <p className="widget-empty">No forecast or history for this range.</p>
        : <>
          <div className="hero-row">
            <Stat value={deg(warm.hi)} label="Warmest" sub={shortDay(warm.day)} />
            <Stat value={deg(cold.lo)} label="Coolest" sub={shortDay(cold.day)} />
            {wet?.rain >= RAIN_WORTH_SAYING && <Stat value={`${Math.round(wet.rain)}%`} label="Best chance of rain" sub={shortDay(wet.day)} />}
            {sun && <Stat value={Math.round(sun.uv)} label="Peak UV" sub={shortDay(sun.day)} />}
          </div>
          <Band title="Days" note="low to high" pin={[pinLabel, days]}>
            <Days days={days} today={today} />
          </Band>
        </>)}
    </>
  );
}
