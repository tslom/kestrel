import { useRef, useState } from "react";
import { useDebounce } from "../lib/timers.js";
import { fetchJSON, postJSON } from "../lib/api";
import { broadcastRefresh, useWidgetData } from "../lib/refresh";
import { HAS, QUIET } from "../lib/salience";
import { offerUndo } from "../lib/undo.js";
import WidgetError from "../ui/WidgetError.jsx";
import Icon from "../ui/icons.jsx";
import { dragPin } from "../lib/drag.js";

// §25 food tracker (FOOD_PLAN.md). The card answers three things at a glance —
// where today stands against a lean bulk's targets, what to eat next on
// campus, and whether the day is closed — and takes a log in one field. The
// sheet owns everything else (menus, the week, setup).
//
// Writes go out on the "food" topic, and closing a day on "habits" too, since
// that check-in is the Log food habit.
export const foodChanged = () => broadcastRefresh("food");

export const fmtN = (v) => Math.round(v || 0).toLocaleString();
// B12's target is 2.4 µg and omega-3's 1.6 g: whole numbers would read "1/2".
const fmtFine = (v, target) => (target != null && target < 10
  ? (+(v || 0).toFixed(1)).toString() : fmtN(v));

const SETUP_LABEL = { birth_year: "birth year", height_cm: "height" };

// One logged entry's undo is its delete; one delete's undo is its restore.
export function logFood(body, label) {
  return postJSON("/api/food/log", body).then((e) => {
    foodChanged();
    offerUndo({
      label: label || `Logged ${e.name}`,
      undo: () => fetchJSON(`/api/food/log/${e.id}`, { method: "DELETE" }).then(foodChanged),
    });
    return e;
  });
}

export function deleteEntry(e) {
  return fetchJSON(`/api/food/log/${e.id}`, { method: "DELETE" }).then((gone) => {
    foodChanged();
    offerUndo({
      label: `Removed ${e.name}`,
      undo: () => postJSON("/api/food/log/restore", gone).then(foodChanged),
    });
  });
}

export function closeDay(day, reopen) {
  return postJSON("/api/food/close", { day, reopen }).then(() => {
    foodChanged();
    broadcastRefresh("habits");
  });
}

// A bar for one nutrient. `kind` decides the color: a cap that's over goes
// red, a minimum that's met goes green, everything else is plain data.
export function NutrientBar({ label, value, target, unit, kind = "min", missing }) {
  // A day with nothing logged has no totals at all, and `NaN%` is a width the
  // browser drops — the fill would stretch to the whole track.
  const frac = target && value ? value / target : 0;
  const tone = kind === "max" ? (frac > 1 ? "over" : "")
    : frac >= (kind === "range" ? 0.92 : 1) ? (kind === "range" && frac > 1.12 ? "over" : "met") : "";
  return (
    <li className="food-bar" title={missing ? `${missing} logged item(s) don't know this` : undefined}>
      <span className="food-bar-label">{label}</span>
      <span className="food-bar-track">
        <span className={`food-bar-fill ${tone}`} style={{ width: `${Math.min(frac, 1) * 100}%` }} />
      </span>
      <span className="food-bar-value">
        {fmtFine(value, target)}{target ? `/${fmtFine(target, target)}` : ""} {unit}{missing ? "*" : ""}
      </span>
    </li>
  );
}

// A guessed number reads as a range ("650–800"), an exact one as itself.
export const kcalText = (e) => (e.range?.kcal
  ? `${fmtN(e.range.kcal[0])}–${fmtN(e.range.kcal[1])}` : fmtN(e.nutrients.kcal));

// "How much X?" — asked once, only for a guessed amount of a dish whose size
// varies. A tap scales the entry; × dismisses and keeps the guess.
export function PortionQuestions({ questions }) {
  const [busy, setBusy] = useState(false);
  if (!questions?.length) return null;
  const answer = (q, x) => {
    setBusy(true);
    postJSON(`/api/food/log/${q.id}/portion`, { x }).then(foodChanged).finally(() => setBusy(false));
  };
  return (
    <ul className="food-asks">
      {questions.map((q) => (
        <li key={q.id}>
          <span className="food-ask-q">{q.q}</span>
          <span className="food-ask-opts">
            {q.options.map((o) => (
              <button key={o.x} className="chip" disabled={busy} onClick={() => answer(q, o.x)}>
                {o.label}</button>
            ))}
            <button className="food-x" disabled={busy} onClick={() => answer(q, null)}
              title="Skip — keep the guess"><Icon name="close" size={10} /></button>
          </span>
        </li>
      ))}
    </ul>
  );
}

// One tap re-logs a usual food at its usual amount, or yesterday's meal.
export function Usuals({ day, limit = 4 }) {
  const { data } = useWidgetData(`/api/food/recents?day=${day}`, "food");
  if (!data || (!data.foods.length && !data.meals.length)) return null;
  const again = (m) => postJSON("/api/food/repeat", { from_day: m.day, to_day: day, meal: m.meal })
    .then((r) => {
      foodChanged();
      offerUndo({
        label: `Logged yesterday's ${m.meal}`,
        undo: () => Promise.all(r.entries.map((e) => fetchJSON(`/api/food/log/${e.id}`,
          { method: "DELETE" }))).then(foodChanged),
      });
    });
  return (
    <div className="food-usuals">
      {data.foods.slice(0, limit).map((f) => (
        <button key={f.ref} className="chip" title={`${f.servings} × ${f.serving_desc} · ${f.protein} g protein`}
          onClick={() => logFood({ food: f.ref, day }, `Logged ${f.name}`)}>
          {f.name}{f.servings !== 1 ? ` ${+f.servings.toFixed(2)}×` : ""} <span className="food-usual-n">{f.kcal}</span>
        </button>
      ))}
      {data.meals.map((m) => (
        <button key={m.meal} className="chip" title={m.names.join(", ")} onClick={() => again(m)}>
          same {m.meal} as yesterday</button>
      ))}
    </div>
  );
}

// Morning weigh-ins, judged on the 7-day average; this checks off Weigh in.
export function WeighIn({ placeholder = "Weigh-in, lb (morning, after the bathroom, before eating)" }) {
  const [w, setW] = useState("");
  const [msg, setMsg] = useState(null);
  function save() {
    const v = parseFloat(w);
    if (!v) return;
    postJSON("/api/food/weigh", { lb: v }).then((r) => {
      setW("");
      setMsg(r.avg7_lb ? `7-day average ${r.avg7_lb} lb` : null);
      foodChanged();
      broadcastRefresh("habits");
    }).catch((e) => setMsg(e.message));
  }
  return (
    <div className="widget-add food-weigh">
      <input value={w} placeholder={placeholder} inputMode="decimal"
        onChange={(e) => setW(e.target.value)} onKeyDown={(e) => e.key === "Enter" && save()} />
      {msg && <span className="food-note">{msg}</span>}
    </div>
  );
}

// The one-field logger: type, pick, logged. Foods you've logged before and
// today's dining-hall items come first (the backend ranks), so the common
// case is three letters and Enter.
export function QuickAdd({ day, meal, placeholder = "Log food… (e.g. eggs, orange chicken)", autoFocus }) {
  const [q, setQ] = useState("");
  const [hits, setHits] = useState(null);
  const [sel, setSel] = useState(0);
  const [err, setErr] = useState(null);
  const box = useRef(null);

  // All digits is a barcode (UPC/EAN): one Open Food Facts lookup.
  const term = q.trim();
  const code = /^\d{8,14}$/.test(term) ? term : null;
  useDebounce((stale) => {
    if (term.length < 2) { setHits(null); return; }
    (code ? fetchJSON(`/api/food/barcode/${code}`).then((f) => ({ results: [f] }))
      : fetchJSON(`/api/food/search?q=${encodeURIComponent(term)}`))
      .then((r) => { if (!stale()) { setHits(r.results.slice(0, 8)); setSel(0); setErr(null); } })
      .catch((e) => { if (!stale()) setErr(e.message); });
  }, term.length < 2 ? 0 : code ? 400 : 180, [q]);

  function pick(h) {
    const ref = h.ref || h.id;
    // An SR28 food is per 100 g; its first household portion is what a
    // person means by "one" (1 large egg, 1 medium banana).
    const portion = !h.ref && h.portions?.length ? h.portions[0][0] : undefined;
    setErr(null);
    logFood({ food: String(ref), portion, day, meal }, `Logged ${h.name}`)
      .then(() => { setQ(""); setHits(null); })
      .catch((e) => setErr(e.message));
  }

  function onKey(e) {
    if (!hits?.length) return;
    if (e.key === "ArrowDown") { e.preventDefault(); setSel((s) => Math.min(s + 1, hits.length - 1)); }
    else if (e.key === "ArrowUp") { e.preventDefault(); setSel((s) => Math.max(s - 1, 0)); }
    else if (e.key === "Enter") { e.preventDefault(); pick(hits[sel]); }
    else if (e.key === "Escape") { setHits(null); }
  }

  return (
    <div className="food-add widget-add" ref={box}>
      <input value={q} placeholder={placeholder} autoFocus={autoFocus}
        onChange={(e) => setQ(e.target.value)} onKeyDown={onKey} />
      {err && <p className="widget-error">{err}</p>}
      {hits && (
        <ul className="food-hits">
          {hits.length === 0 && <li className="widget-empty">No match — ask Kestrel to create it.</li>}
          {hits.map((h, i) => {
            const n = h.nutrients || {};
            const per = h.ref ? h.serving_desc
              : h.portions?.length ? h.portions[0][0] : "100 g";
            const scale = !h.ref && h.portions?.length ? h.portions[0][1] / 100 : 1;
            return (
              <li key={h.ref || h.id} className={i === sel ? "sel" : ""}
                onMouseEnter={() => setSel(i)} onMouseDown={(e) => { e.preventDefault(); pick(h); }}>
                <span className="food-hit-name">
                  {h.name}
                  {h.venue && <span className="food-hit-venue"> · {h.venue}</span>}
                  {h.confidence < 0.9 && <span className="food-est" title="estimated">est</span>}
                </span>
                <span className="food-hit-n">
                  {fmtN((n.kcal || 0) * scale)} kcal · {fmtN((n.protein || 0) * scale)}g P · {per}
                </span>
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}

export function useFoodCard() {
  const { data, error, reload } = useWidgetData("/api/food/day", ["food", "habits"]);
  const { data: sug } = useWidgetData("/api/food/suggest", "food");

  const closed = Boolean(data?.closed);
  const tg = data?.targets?.nutrients;
  const kcal = data?.totals?.kcal || 0;
  const protein = data?.totals?.protein || 0;
  const level = !data ? null : closed ? QUIET : HAS;
  const summary = !data ? null
    : closed ? `closed · ${fmtN(kcal)} kcal, ${fmtN(protein)} g protein`
    : !data.entries.length ? (data.setup_needed.length ? "set up targets" : "nothing logged yet")
    : tg ? `${fmtN(kcal)}/${fmtN(tg.kcal.target)} kcal · ${fmtN(protein)}/${tg.protein.target} g protein`
    : `${fmtN(kcal)} kcal · ${fmtN(protein)} g protein`;
  return { data, error, reload, sug, level, summary };
}

export default function FoodWidget({ card, onPin, onExpand }) {
  const { data, error, sug } = card;
  const [busy, setBusy] = useState(false);
  const tg = data?.targets;
  const tot = data?.totals || {};
  const plate = sug?.plates?.[0];

  function logPlate(p) {
    setBusy(true);
    Promise.all(p.items.map((it) => postJSON("/api/food/log", {
      food: it.ref, meal: sug.meal, venue: p.venue,
    }))).then((es) => {
      foodChanged();
      offerUndo({
        label: `Logged ${p.venue_name} plate`,
        undo: () => Promise.all(es.map((e) => fetchJSON(`/api/food/log/${e.id}`,
          { method: "DELETE" }))).then(foodChanged),
      });
    }).finally(() => setBusy(false));
  }

  return (
    <section className="widget food" {...dragPin("Food", data)}>
      <header>
        <h2>Food {tg && <span className="widget-sub">{tg.training ? "training day" : "rest day"}</span>}</h2>
        <div className="widget-actions">
          <button className="pin" disabled={!data}
            onClick={() => onPin("Food", data)}
            title="Attach this data to your next console message"><Icon name="pin" /> pin</button>
        </div>
      </header>
      {error && <WidgetError error={error} />}
      {!data && !error && <><div className="skel" /><div className="skel w60" /></>}
      {data && (
        <>
          {data.setup_needed.length > 0 && (
            <p className="widget-empty food-setup">
              Targets need your {data.setup_needed.map((k) => SETUP_LABEL[k] || k).join(", ")} —{" "}
              <button className="link" onClick={onExpand}>set up</button>
            </p>
          )}
          <ul className="food-bars">
            <NutrientBar label="Energy" value={tot.kcal} target={tg?.nutrients.kcal.target}
              unit="kcal" kind="range" />
            <NutrientBar label="Protein" value={tot.protein} target={tg?.nutrients.protein.target}
              unit="g" />
            {tg && data.gaps.slice(0, 3).map((k) => (
              <NutrientBar key={k} label={data.nutrients[k][0]} value={tot[k]}
                target={tg.nutrients[k].target} unit={data.nutrients[k][1]}
                missing={data.missing[k]} />
            ))}
          </ul>
          {data.range?.kcal && (
            <p className="food-range" title="Estimated items carry a range; published and corrected ones don't">
              likely {fmtN(data.range.kcal[0])}–{fmtN(data.range.kcal[1])} kcal
              {data.range.protein && <> · {data.range.protein[0]}–{data.range.protein[1]} g protein</>}
              {" "}· {Math.round(data.range.exact_share * 100)}% of calories exact
            </p>
          )}
          <PortionQuestions questions={data.questions} />
          {data.calibration?.ready && data.calibration.verdict !== "consistent" && (
            <p className="food-cal" title={data.calibration_line}>
              Scale check: logging looks ~{fmtN(Math.abs(data.calibration.gap_kcal))} kcal/day
              {" "}{data.calibration.verdict} ({Math.abs(data.calibration.gap_pct)}%) against your weight trend
            </p>
          )}
          {plate && !data.closed && (
            <div className="food-next">
              <span className="food-next-label">next ({sug.meal})</span>
              <span className="food-next-plate" title={plate.items.map((i) => i.serving_desc).join(" · ")}>
                <b>{plate.venue_name}</b>: {plate.items.map((i) => i.name).join(" + ")}
                {" "}· +{fmtN(plate.adds.protein)} g P, {fmtN(plate.adds.kcal)} kcal
              </span>
              <button className="chip" disabled={busy} onClick={() => logPlate(plate)}
                title="Log these items">ate it</button>
            </div>
          )}
          {data.dd?.allowance_per_day != null && (
            <p className={`food-dd ${data.dd.pace}`}>
              DD ${fmtN(data.dd.left)} left · ${data.dd.allowance_per_day.toFixed(0)}/day to use it
              all ({data.dd.pace}, last 7d ${data.dd.per_day_7d.toFixed(0)}/day)
            </p>
          )}
          {!data.closed && <QuickAdd day={data.day} />}
          {!data.closed && <Usuals day={data.day} />}
          {!data.weight.weighed_today && data.day === data.today && <WeighIn />}
          <div className="food-foot">
            <span className="food-count">{data.entries.length} logged</span>
            {data.closed
              ? <button className="chip active" onClick={() => closeDay(data.day, true)}
                  title="Reopen the day (un-checks Log food)"><Icon name="check" size={10} /> done for today</button>
              : <button className="chip" disabled={!data.entries.length}
                  onClick={() => closeDay(data.day, false)}
                  title="Close the day — checks off the Log food habit">done for today</button>}
          </div>
        </>
      )}
    </section>
  );
}
