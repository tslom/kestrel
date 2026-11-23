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

// The location half as a map.
//
// Every other location surface is a number per day — kilometres, minutes at
// home, a count of places — and none of them can answer "where was I". This
// draws the fixes themselves: one path per day, and a disc at every place the
// same clustering the `places` metric uses says you stopped.
//
// Three decisions worth keeping.
//
// **The basemap comes from our own origin.** Tiles are fetched once by
// `app/tiles.py` and cached; the frontend never talks to a tile server. That
// was forced (blocked image hosts, localhost app) but it also means panning is
// free after the first look, and a dead network degrades to the vector-only
// map rather than to a broken sheet.
//
// **Integer zoom only.** A slippy map with fractional zoom needs scaled tile
// blits and a resample on every frame; stepping whole zoom levels keeps every
// tile pixel-aligned and crisp, and the thing being read here is a campus at
// four or five fixed scales, not a continuous flight.
//
// **Places are the provider's, not the map's.** Same stay detection, same
// 3-minute dwell floor as `location.places`. A map that clustered its own way
// would disagree with the card, and the disagreement would be invisible.
// A place carries a `name` when the vault declares one (see app/places.py),
// and `ambiguous_with` when another declared place is close enough that no
// coordinate could tell the two apart — say so rather than pick.

const TILE = 256;
// How many places get a numbered pin. Matches the `.map-list` slice below, so
// every pin on the map has a row beside it and every row has a pin — a number
// pointing at nothing is worse than no number.
const PIN_TOP_N = 8;
const PIN_R = 8;        // pin head radius
const PIN_H = 19;       // anchor point to head centre
const MIN_Z = 3, MAX_Z = 19;
// Wheel delta banked before one zoom level is spent. 90 is roughly one
// deliberate mouse notch, or a short trackpad flick — tuned by hand on this
// machine's trackpad, where a lazy two-finger swipe emits ~40 events of
// deltaY 1-4 and should move one or two levels, not six.
const WHEEL_STEP = 90;
const WHEEL_STEP_PINCH = 24;
const WHEEL_IDLE_MS = 180;      // gap after which a flick counts as a new one

// Web Mercator, in world pixels at a zoom. The only projection maths here;
// everything else is offsets from the view centre.
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

// A view that shows everything, without zooming past what the tiles can do.
// A single-point day has no extent at all, so it gets a default scale rather
// than an infinite one.
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
  // A month, not the week the rest of the location surfaces default to. A
  // routine is the thing a map shows that a number can't, and it needs more
  // than one instance of each weekday to be visible at all — on a Monday,
  // "this week" is a map of this morning.
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

  // Pulled out of the effect so naming a place can re-run it: a correction has
  // to redraw the map it was made on, and `keepView` is why it doesn't also
  // throw away the pan and zoom you were looking at when you made it.
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

  // One draw of everything, called on any change of data, view or size. Tiles
  // that aren't loaded yet kick off a load and redraw when they arrive, so the
  // map appears in one pass and fills in rather than blocking on the network.
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
          // Desaturated and lightened on the way down. The only keyless tile
          // server left is osm.org's standard style, which is a finished map
          // in its own right — greens, reds, bold labels — and a basemap's job
          // here is to be the paper the fixes are drawn on. Where ctx.filter
          // isn't supported the tiles simply land in full colour.
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
    // The home radius is a real distance, so it has to be drawn as one: metres
    // per pixel at this latitude, not a fixed circle that lies at every zoom.
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
        // A break is a break: joining across a gap would draw a straight line
        // through a journey nobody recorded.
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

    // --- pins: the disc says how long, the pin says which one.
    //
    // Two different questions, so two marks. Size already carries dwell
    // honestly and carrying identity in it too would mean reading rank off an
    // area, which nobody can do. The number in the head is the row number in
    // the list beside the map, which is what makes the two halves one view.
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

      // A name is the whole point of declaring a place, so show it — but only
      // where it fits. Ranked order means a clash is resolved in favour of the
      // longer stay, rather than by whichever happened to be drawn last.
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

  // Fullscreen on the frame rather than the sheet: the places list is a
  // separate scroll region and taking it fullscreen too would just make a
  // tall column of text with a map wedged above it.
  //
  // `full` tracks the *browser's* opinion via fullscreenchange, never the
  // click, because Escape and the system chrome can leave fullscreen without
  // telling us — a flag set on click alone goes stale on the first Escape and
  // the button then lies about what it does.
  const toggleFull = useCallback(() => {
    const box = boxRef.current;
    if (!box) return;
    if (document.fullscreenElement) document.exitFullscreen?.();
    else box.requestFullscreen?.().catch(() => {});
  }, []);

  useEffect(() => {
    const onChange = () => {
      setFull(document.fullscreenElement === boxRef.current);
      // The frame's box changes before the browser paints; draw() re-reads
      // clientWidth/Height and resizes the canvas, so one more pass is all
      // fullscreen needs. rAF rather than a timeout so it lands on the frame
      // the new size is first true for.
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

  // Pan. Tracked in pixels and converted back to a centre on release of each
  // move, so the view state stays a lat/lon/zoom and nothing else has to know
  // about pixels.
  const onPointerDown = (e) => {
    // Only the canvas starts a drag. Capturing the pointer on the frame
    // retargets the eventual `click` to the frame, which silently ate every
    // press on the zoom controls sitting inside it — the buttons looked dead
    // and the map deselected instead.
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

  // Zoom about the cursor: the point under the pointer stays under it, which
  // is the only zoom that feels like a map rather than like a slideshow.
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

  // Wheel is bound by hand rather than through React's onWheel: React registers
  // wheel passively, so preventDefault there is ignored with a console warning
  // and the dock scrolls out from under the map while you zoom.
  //
  // The delta is *accumulated* rather than acted on per event, which is the
  // whole difference between a map and a slot machine. One two-finger flick on
  // a trackpad emits dozens of small wheel events; stepping a full zoom level
  // on each one crossed six levels before the fingers left the glass, so the
  // map appeared to teleport and the only way back was the fit button.
  // Zoom is integer here because the basemap draws whole tiles at integer z,
  // so smoothing has to happen on the input side: bank the deltas, spend one
  // level each time the threshold is crossed.
  useEffect(() => {
    const box = boxRef.current;
    if (!box) return;
    const onWheel = (e) => {
      e.preventDefault();
      const now = performance.now();
      // A new gesture starts from zero. Without this the tail of one flick
      // adds to the head of the next and the second one over-shoots.
      if (now - wheelRef.current.at > WHEEL_IDLE_MS) wheelRef.current.acc = 0;
      wheelRef.current.at = now;
      // deltaMode 1 is lines (Firefox, some mice), 2 is pages.
      const scale = e.deltaMode === 1 ? 16 : e.deltaMode === 2 ? 100 : 1;
      wheelRef.current.acc += e.deltaY * scale;
      // A pinch arrives as ctrl+wheel on macOS. It is a deliberate zoom rather
      // than a scroll that happens to be over the map, so it earns a lower bar.
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

          {/* Attribution is not optional and not decorative: these are other
              people's tiles, and the licence asks for the credit on the map. */}
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
