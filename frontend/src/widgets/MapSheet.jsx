import { useCallback, useEffect, useRef, useState } from "react";
import RangePicker from "../ui/RangePicker.jsx";
import SheetHead from "../ui/SheetHead.jsx";
import Band from "../ui/Band.jsx";
import Icon from "../ui/icons.jsx";
import { Stat } from "../ui/charts.jsx";
import { fetchJSON } from "../lib/api";
import { fmtDur, shortDay } from "../lib/time";
import { palette } from "../lib/tokens.js";
import { PlaceNamer, VisitList } from "./MapPlaces.jsx";

// The location half as a map: one path per day, a disc at every stay.
// Basemap tiles come from our own origin (`app/tiles.py`), integer zoom only,
// places are the provider's clustering, never the map's own.
// Design: DESIGN.md → `frontend/src/widgets/MapSheet.jsx`.

const TILE = 256;
// Must match the `.map-list` slice below: every pin has a row and every row a pin.
const PIN_TOP_N = 8;
const PIN_R = 8;        // pin head radius
const PIN_H = 19;       // anchor point to head centre
const MIN_Z = 3, MAX_Z = 19;
// Wheel delta banked per zoom level: ~one mouse notch; a lazy trackpad swipe is ~40 events of deltaY 1-4.
const WHEEL_STEP = 90;
const WHEEL_STEP_PINCH = 24;
const WHEEL_IDLE_MS = 180;      // gap after which a flick counts as a new one

// Web Mercator, in world pixels at a zoom.
const worldSize = (z) => TILE * 2 ** z;
const lonToX = (lon, z) => ((lon + 180) / 360) * worldSize(z);
const latToY = (lat, z) => {
  const s = Math.min(0.9999, Math.max(-0.9999, Math.sin((lat * Math.PI) / 180)));
  return (0.5 - Math.log((1 + s) / (1 - s)) / (4 * Math.PI)) * worldSize(z);
};
const xToLon = (x, z) => (x / worldSize(z)) * 360 - 180;
const yToLat = (y, z) => {
  const n = Math.PI * (1 - (2 * y) / worldSize(z));
  return (180 / Math.PI) * Math.atan(Math.sinh(n));
};

// Fits all bounds; a single-point day has no extent, so it falls to MAX_Z.
function fitView(bounds, w, h) {
  if (!bounds) return { lat: 0, lon: 0, z: 3 };
  const lat = (bounds.min_lat + bounds.max_lat) / 2;
  const lon = (bounds.min_lon + bounds.max_lon) / 2;
  for (let z = MAX_Z; z > MIN_Z; z--) {
    const dx = Math.abs(lonToX(bounds.max_lon, z) - lonToX(bounds.min_lon, z));
    const dy = Math.abs(latToY(bounds.min_lat, z) - latToY(bounds.max_lat, z));
    if (dx <= w * 0.86 && dy <= h * 0.86) return { lat, lon, z };
  }
  return { lat, lon, z: MIN_Z };
}

export default function MapSheet({ onPin }) {
  // A month, not a week: a routine needs several of each weekday to show.
  const [range, setRange] = useState("this-month");
  const [ranges, setRanges] = useState(null);
  const [trace, setTrace] = useState(null);
  const [error, setError] = useState(null);
  const [loading, setLoading] = useState(true);
  const [view, setView] = useState(null);          // {lat, lon, z}
  const [picked, setPicked] = useState(null);      // a place, or null

  const canvasRef = useRef(null);
  const boxRef = useRef(null);
  const wheelRef = useRef({ acc: 0, at: 0 });
  const [full, setFull] = useState(false);
  const tilesRef = useRef(new Map());              // "z/x/y" -> HTMLImageElement
  const dragRef = useRef(null);
  const seq = useRef(0);

  useEffect(() => {
    fetchJSON("/api/providers")
      .then((all) => setRanges(all.location?.ranges || []))
      .catch(() => setRanges([]));
  }, []);

  // `keepView`: naming a place reloads without losing the pan and zoom.
  const load = useCallback((keepView = false) => {
    const mine = ++seq.current;
    setLoading(true);
    setError(null);
    fetchJSON(`/api/location/trace?range=${range}`)
      .then((t) => {
        if (mine !== seq.current) return;
        setTrace(t);
        setPicked(null);
        if (!keepView) {
          const box = boxRef.current;
          setView(fitView(t.bounds, box?.clientWidth || 520, box?.clientHeight || 380));
        }
        setLoading(false);
      })
      .catch((e) => {
        if (mine !== seq.current) return;
        setError(e.message);
        setLoading(false);
      });
  }, [range]);

  useEffect(() => { load(); }, [load]);

  const reload = useCallback(() => load(true), [load]);

  // One draw of everything; unloaded tiles start a load and redraw on arrival.
  const draw = useCallback(() => {
    const cv = canvasRef.current;
    if (!cv || !view || !trace) return;
    const box = boxRef.current;
    const w = box.clientWidth, h = box.clientHeight;
    const dpr = window.devicePixelRatio || 1;
    if (cv.width !== w * dpr || cv.height !== h * dpr) {
      cv.width = w * dpr;
      cv.height = h * dpr;
    }
    const ctx = cv.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    const c = palette();
    ctx.fillStyle = c.paper;
    ctx.fillRect(0, 0, w, h);

    const cx = lonToX(view.lon, view.z), cy = latToY(view.lat, view.z);
    const px = (lon) => lonToX(lon, view.z) - cx + w / 2;
    const py = (lat) => latToY(lat, view.z) - cy + h / 2;

    // --- basemap
    const x0 = Math.floor((cx - w / 2) / TILE), x1 = Math.floor((cx + w / 2) / TILE);
    const y0 = Math.floor((cy - h / 2) / TILE), y1 = Math.floor((cy + h / 2) / TILE);
    const span = 2 ** view.z;
    for (let tx = x0; tx <= x1; tx++) {
      for (let ty = y0; ty <= y1; ty++) {
        if (ty < 0 || ty >= span) continue;
        const wrapped = ((tx % span) + span) % span;
        const key = `${view.z}/${wrapped}/${ty}`;
        let img = tilesRef.current.get(key);
        if (!img) {
          img = new Image();
          img.onload = () => draw();
          img.onerror = () => { img.failed = true; };
          img.src = `/api/tiles/${key}.png`;
          tilesRef.current.set(key, img);
        }
        if (img.complete && img.naturalWidth) {
          // Mute osm's loud standard style into paper; without ctx.filter tiles stay full colour.
          ctx.save();
          ctx.filter = "grayscale(0.9) contrast(0.82) brightness(1.06)";
          ctx.globalAlpha = 0.72;
          ctx.drawImage(img, Math.round(tx * TILE - cx + w / 2),
                        Math.round(ty * TILE - cy + h / 2), TILE, TILE);
          ctx.restore();
        }
      }
    }

    // --- home
    const hx = px(trace.home.lon), hy = py(trace.home.lat);
    // Real metres per pixel at this latitude, not a fixed-size circle.
    const mPerPx = (156543.03392 * Math.cos((view.lat * Math.PI) / 180)) / 2 ** view.z;
    const hr = trace.home.radius_m / mPerPx;
    ctx.beginPath();
    ctx.arc(hx, hy, hr, 0, Math.PI * 2);
    ctx.fillStyle = `${c.accent}14`;
    ctx.fill();
    ctx.strokeStyle = `${c.accent}55`;
    ctx.setLineDash([4, 3]);
    ctx.lineWidth = 1;
    ctx.stroke();
    ctx.setLineDash([]);

    // --- one path per day, broken wherever the phone went quiet
    ctx.lineWidth = 1.4;
    ctx.lineJoin = "round";
    ctx.lineCap = "round";
    const gapS = (trace.gap_min || 20) * 60;
    for (const day of trace.days) {
      ctx.strokeStyle = `${c.data}66`;
      ctx.beginPath();
      let pen = false;
      for (let i = 0; i < day.points.length; i++) {
        const [lat, lon, t] = day.points[i];
        const prev = day.points[i - 1];
        // Never join across a gap: it draws a line through an unrecorded journey.
        if (!prev || t - prev[2] > gapS) pen = false;
        if (!pen) { ctx.moveTo(px(lon), py(lat)); pen = true; }
        else ctx.lineTo(px(lon), py(lat));
      }
      ctx.stroke();
    }

    // --- places, area ∝ dwell so a long stay reads as a big disc honestly
    const maxMin = Math.max(1, ...trace.places.map((p) => p.minutes));
    for (const p of trace.places) {
      const r = 3 + 13 * Math.sqrt(p.minutes / maxMin);
      const x = px(p.lon), y = py(p.lat);
      ctx.beginPath();
      ctx.arc(x, y, r, 0, Math.PI * 2);
      ctx.fillStyle = p === picked ? `${c.accent}CC` : `${c.data}55`;
      ctx.fill();
      ctx.lineWidth = p === picked ? 2 : 1;
      ctx.strokeStyle = p === picked ? c.accent : `${c.data}AA`;
      ctx.stroke();
    }

    // --- pins: the disc says how long, the pin's number is its list row.
    //
    // Not every place gets one. Twenty-one pins is a thicket, and the ones
    // worth labelling are the ones you can act on: whatever the list is
    // showing, plus anything the vault has named, plus the selection.
    const pinned = trace.places
      .map((p, i) => ({ p, rank: i + 1 }))
      .filter(({ p, rank }) => rank <= PIN_TOP_N || p.name || p === picked);
    const placed = [];
    const clear = (box) => !placed.some((b) =>
      box.x < b.x + b.w && box.x + box.w > b.x
      && box.y < b.y + b.h && box.y + box.h > b.y);

    ctx.textBaseline = "middle";
    for (const { p, rank } of pinned) {
      const x = px(p.lon), y = py(p.lat);
      if (x < -40 || y < -40 || x > w + 40 || y > h + 40) continue;
      const on = p === picked;
      const head = y - PIN_H;
      const ink = on ? c.accent : c.data;

      // stem, then head — drawn as one path so the join never shows a seam
      ctx.beginPath();
      ctx.moveTo(x, y);
      ctx.lineTo(x - 3.4, head + 4);
      ctx.lineTo(x + 3.4, head + 4);
      ctx.closePath();
      ctx.fillStyle = ink;
      ctx.fill();
      ctx.beginPath();
      ctx.arc(x, head, PIN_R, 0, Math.PI * 2);
      ctx.fillStyle = ink;
      ctx.fill();
      ctx.strokeStyle = c.paper;
      ctx.lineWidth = 1.5;
      ctx.stroke();

      ctx.fillStyle = c.paper;
      ctx.font = "600 9px ui-sans-serif, system-ui, sans-serif";
      ctx.textAlign = "center";
      ctx.fillText(String(rank), x, head);

      // Labels only where they fit; ranked order lets the longer stay win a clash.
      if (!p.name) continue;
      ctx.font = "500 10px ui-sans-serif, system-ui, sans-serif";
      ctx.textAlign = "left";
      const tw = ctx.measureText(p.name).width;
      const box = { x: x + PIN_R + 4, y: head - 8, w: tw + 10, h: 16 };
      if (!on && !clear(box)) continue;
      placed.push(box);
      ctx.fillStyle = `${c.paper}E6`;
      ctx.fillRect(box.x, box.y, box.w, box.h);
      ctx.strokeStyle = `${ink}44`;
      ctx.lineWidth = 1;
      ctx.strokeRect(box.x, box.y, box.w, box.h);
      ctx.fillStyle = ink;
      ctx.fillText(p.name, box.x + 5, head);
    }
    ctx.textAlign = "start";
  }, [view, trace, picked]);

  useEffect(() => { draw(); }, [draw]);

  // Fullscreen the frame, not the sheet. `full` follows fullscreenchange, never
  // the click: Escape leaves fullscreen without telling us.
  const toggleFull = useCallback(() => {
    const box = boxRef.current;
    if (!box) return;
    if (document.fullscreenElement) document.exitFullscreen?.();
    else box.requestFullscreen?.().catch(() => {});
  }, []);

  useEffect(() => {
    const onChange = () => {
      setFull(document.fullscreenElement === boxRef.current);
      // draw() re-reads the frame size; rAF lands on the first frame the new size is true.
      requestAnimationFrame(() => draw());
    };
    document.addEventListener("fullscreenchange", onChange);
    return () => document.removeEventListener("fullscreenchange", onChange);
  }, [draw]);

  useEffect(() => {
    const onResize = () => draw();
    window.addEventListener("resize", onResize);
    return () => window.removeEventListener("resize", onResize);
  }, [draw]);

  const onPointerDown = (e) => {
    // Only the canvas starts a drag: capturing on the frame retargets `click` and kills the zoom buttons.
    if (e.target !== canvasRef.current) return;
    dragRef.current = { x: e.clientX, y: e.clientY, moved: 0 };
    e.currentTarget.setPointerCapture(e.pointerId);
  };
  const onPointerMove = (e) => {
    const d = dragRef.current;
    if (!d || !view) return;
    const dx = e.clientX - d.x, dy = e.clientY - d.y;
    d.x = e.clientX;
    d.y = e.clientY;
    d.moved += Math.abs(dx) + Math.abs(dy);
    setView((v) => ({
      z: v.z,
      lon: xToLon(lonToX(v.lon, v.z) - dx, v.z),
      lat: yToLat(latToY(v.lat, v.z) - dy, v.z),
    }));
  };
  const onPointerUp = (e) => {
    const d = dragRef.current;
    dragRef.current = null;
    if (!d || d.moved > 4 || !trace || !view) return;   // a drag is not a click
    const box = boxRef.current.getBoundingClientRect();
    const mx = e.clientX - box.left, my = e.clientY - box.top;
    const cx = lonToX(view.lon, view.z), cy = latToY(view.lat, view.z);
    let best = null, bestD = 18;                        // within 18px of a disc
    for (const p of trace.places) {
      const dx = lonToX(p.lon, view.z) - cx + box.width / 2 - mx;
      const dy = latToY(p.lat, view.z) - cy + box.height / 2 - my;
      const dist = Math.hypot(dx, dy);
      if (dist < bestD) { best = p; bestD = dist; }
    }
    setPicked(best);
  };

  // Zoom about the cursor: the point under the pointer stays under it.
  const zoomAt = (dz, mx, my) => {
    setView((v) => {
      const z = Math.max(MIN_Z, Math.min(MAX_Z, v.z + dz));
      if (z === v.z) return v;
      const box = boxRef.current;
      const w = box.clientWidth, h = box.clientHeight;
      const ax = mx == null ? w / 2 : mx, ay = my == null ? h / 2 : my;
      const lon = xToLon(lonToX(v.lon, v.z) - cxOff(w, ax), v.z);
      const lat = yToLat(latToY(v.lat, v.z) - cyOff(h, ay), v.z);
      // re-centre so the anchor lands back where it was after the scale change
      return {
        z,
        lon: xToLon(lonToX(lon, z) + cxOff(w, ax), z),
        lat: yToLat(latToY(lat, z) + cyOff(h, ay), z),
      };
    });
  };
  const cxOff = (w, ax) => w / 2 - ax;
  const cyOff = (h, ay) => h / 2 - ay;
  // The wheel listener below is bound once; it zooms through this.
  const zoomRef = useRef(zoomAt);
  zoomRef.current = zoomAt;

  // Bound by hand: React's onWheel is passive, so preventDefault is ignored and the dock scrolls.
  // Deltas are banked and spent one level per WHEEL_STEP; a trackpad flick otherwise crosses six levels.
  useEffect(() => {
    const box = boxRef.current;
    if (!box) return;
    const onWheel = (e) => {
      e.preventDefault();
      const now = performance.now();
      // A new gesture starts from zero, or the second flick over-shoots.
      if (now - wheelRef.current.at > WHEEL_IDLE_MS) wheelRef.current.acc = 0;
      wheelRef.current.at = now;
      // deltaMode 1 is lines (Firefox, some mice), 2 is pages.
      const scale = e.deltaMode === 1 ? 16 : e.deltaMode === 2 ? 100 : 1;
      wheelRef.current.acc += e.deltaY * scale;
      // A pinch arrives as ctrl+wheel on macOS; it is deliberate, so a lower bar.
      const step = e.ctrlKey ? WHEEL_STEP_PINCH : WHEEL_STEP;
      if (Math.abs(wheelRef.current.acc) < step) return;
      const dz = wheelRef.current.acc < 0 ? 1 : -1;
      wheelRef.current.acc = 0;
      const r = box.getBoundingClientRect();
      zoomRef.current(dz, e.clientX - r.left, e.clientY - r.top);
    };
    box.addEventListener("wheel", onWheel, { passive: false });
    return () => box.removeEventListener("wheel", onWheel);
  }, []);

  const refit = () => {
    const box = boxRef.current;
    if (trace) setView(fitView(trace.bounds, box.clientWidth, box.clientHeight));
  };

  const dayCount = trace?.days.length || 0;
  const note = trace
    ? `${dayCount} day${dayCount === 1 ? "" : "s"} · ${trace.fixes.toLocaleString()} fixes`
    : "";

  return (
    <>
      {/* No refreshedAt: /location/trace carries no refreshed_at to state.
          `reload` keeps the pan and zoom, same as after naming a place. */}
      <SheetHead label={`map · ${range}`} data={trace} onPin={onPin} onRefresh={reload} />
      <RangePicker ranges={ranges} range={range} onPick={setRange} />

      {error && <p className="widget-error">{error}</p>}

      <Band title="Where the days went" note={note}
        pin={[`places · ${range}`, trace?.places]}>
        <div className="map-frame" ref={boxRef}
          onPointerDown={onPointerDown}
          onPointerMove={onPointerMove} onPointerUp={onPointerUp}>
          <canvas ref={canvasRef} className="map-canvas" />

          {loading && <p className="map-overlay">Loading…</p>}
          {!loading && trace && !trace.fixes && (
            <p className="map-overlay">
              No fixes in this range — OwnTracks reports movement, so a quiet
              phone and a quiet week look the same here.
            </p>
          )}

          <div className="map-controls">
            <button title="Zoom in" onClick={() => zoomAt(1)}>+</button>
            <button title="Zoom out" onClick={() => zoomAt(-1)}>−</button>
            <button title="Fit to the range" onClick={refit}>
              <Icon name="expand" size={12} />
            </button>
            <button
              title={full ? "Leave full screen (Esc)" : "Full screen"}
              aria-pressed={full}
              onClick={toggleFull}
            >{full ? "⤡" : "⤢"}</button>
          </div>

          {/* The OSM licence requires this credit on the map. */}
          <span className="map-credit">© OpenStreetMap contributors</span>
        </div>

        {trace && trace.shown < trace.fixes && (
          <p className="map-note">
            Showing {trace.shown.toLocaleString()} of {trace.fixes.toLocaleString()}
            {" "}fixes — the path is thinned at this range; the places below are
            computed over all of them.
          </p>
        )}
      </Band>

      <Band title={picked ? "This place" : "Places"}
        note={picked ? "" : `${trace?.places.length || 0} over the range`}>
        {picked ? (
          <div className="map-place">
            <div className="hero-row">
              <Stat value={fmtDur(picked.minutes)} label="dwelt" />
              <Stat value={picked.visits} label="visits" />
              <Stat value={picked.days} label="days" />
            </div>
            <p className="map-note">
              {picked.at_home ? "Home. " : ""}
              {picked.lat.toFixed(5)}, {picked.lon.toFixed(5)} — first seen{" "}
              {shortDay(picked.first.slice(0, 10))}, last{" "}
              {shortDay(picked.last.slice(0, 10))}.
            </p>
            {!picked.name && (
              <PlaceNamer place={picked} range={range} onNamed={reload} />
            )}
            <VisitList place={picked} range={range} />
            <button className="chip" onClick={() => setPicked(null)}>back to all</button>
          </div>
        ) : (
          <ul className="map-list">
            {(trace?.places || []).slice(0, PIN_TOP_N).map((p, i) => (
              <li key={p.id} className={p === picked ? "is-picked" : undefined}>
                <button onClick={() => { setPicked(p); setView((v) => ({ ...v, lat: p.lat, lon: p.lon })); }}>
                  <span className="map-list-rank" aria-hidden="true">{i + 1}</span>
                  <span className="map-list-dwell">{fmtDur(p.minutes)}</span>
                  <span className="map-list-where">
                    {p.name || (p.at_home ? "home"
                      : `${p.lat.toFixed(4)}, ${p.lon.toFixed(4)}`)}
                    {p.ambiguous_with?.length > 0 && (
                      <em className="map-list-ambiguous"
                        title={`Too close to ${p.ambiguous_with.join(", ")} to tell apart from location alone`}
                        > or {p.ambiguous_with.join(" / ")}</em>
                    )}
                  </span>
                  <span className="map-list-visits">{p.visits} visits · {p.days}d</span>
                </button>
              </li>
            ))}
            {!trace?.places.length && !loading && (
              <li className="widget-empty">No place held you longer than three minutes.</li>
            )}
          </ul>
        )}
      </Band>
    </>
  );
}
