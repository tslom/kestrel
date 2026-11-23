import { useEffect, useState } from "react";
import { fetchJSON, postJSON } from "../lib/api";
import { useWidgetData } from "../lib/refresh";
import { shortDay } from "../lib/time";
import SheetHead from "../ui/SheetHead.jsx";
import WidgetError from "../ui/WidgetError.jsx";
import Icon from "../ui/icons.jsx";
import {
  NutrientBar, PortionQuestions, QuickAdd, Usuals, WeighIn, closeDay, deleteEntry, fmtN,
  foodChanged, kcalText, logFood,
} from "./FoodWidget.jsx";

// §25 food sheet. Four tabs: Today (the log and every nutrient), Menus (the
// three dining halls, tap to log), Week (the trend a lean bulk is judged on),
// Setup (the inputs targets are computed from).
const TABS = ["today", "menus", "week", "setup"];
const MEAL_OF = { Breakfast: "breakfast", Brunch: "lunch", Lunch: "lunch", Dinner: "dinner" };

export default function FoodSheet({ onPin }) {
  const [tab, setTab] = useState("today");
  const { data, error, reload, busy } = useWidgetData("/api/food/day", ["food", "habits"]);
  useEffect(() => {
    if (data?.setup_needed?.length && !data.entries.length) setTab("setup");
  }, [data?.setup_needed?.length]);   // eslint-disable-line react-hooks/exhaustive-deps -- jumps to Setup when what's missing changes, not on every reload of the day's entries

  return (
    <>
      <SheetHead refreshedAt={data ? shortDay(data.day) : ""} label="Food" data={data}
        onPin={onPin} onRefresh={reload} busy={busy} />
      <div className="chip-row">
        {TABS.map((t) => (
          <button key={t} className={`chip ${tab === t ? "active" : ""}`}
            onClick={() => setTab(t)}>{t}</button>
        ))}
      </div>
      {error && <WidgetError error={error} />}
      {tab === "today" && data && <Today data={data} />}
      {tab === "menus" && <Menus today={data?.day} />}
      {tab === "week" && <Week />}
      {tab === "setup" && data && <Setup data={data} />}
    </>
  );
}

function Today({ data }) {
  const tg = data.targets;
  const byMeal = data.meals.map((m) => [m, data.entries.filter((e) => e.meal === m)])
    .filter(([, es]) => es.length);

  function step(e, delta) {
    const servings = Math.max(0.25, Math.round((e.servings + delta) * 4) / 4);
    postJSON(`/api/food/log/${e.id}`, { servings }, "PATCH").then(foodChanged);
  }

  return (
    <div className="food-today">
      {tg && (
        <p className="food-ctx">
          {tg.training ? "Training day" : "Rest day"} ({tg.training_why}) · maintenance ~{fmtN(tg.maintenance)} kcal
          {tg.maintenance_from === "scale" && <> (from your weight trend; formula ~{fmtN(tg.formula_maintenance)})</>}
          {data.weight.trend && <> · weight {data.weight.trend.pct_per_month > 0 ? "+" : ""}
            {data.weight.trend.pct_per_month}%/month</>}
        </p>
      )}
      <QuickAdd day={data.day} autoFocus />
      <Usuals day={data.day} limit={8} />
      <PortionQuestions questions={data.questions} />
      {data.range?.kcal && (
        <p className="food-range">
          Today is likely {fmtN(data.range.kcal[0])}–{fmtN(data.range.kcal[1])} kcal:
          {" "}{Math.round(data.range.exact_share * 100)}% of it is published or corrected, the rest estimated.
        </p>
      )}
      {byMeal.length === 0 && <p className="widget-empty">Nothing logged yet.</p>}
      {byMeal.map(([meal, es]) => (
        <div key={meal} className="food-meal">
          <h4 className="people-section">{meal}</h4>
          <ul className="food-entries">
            {es.map((e) => (
              <li key={e.id}>
                <span className="food-entry-name">
                  {e.name}
                  {e.trust !== "exact" && (
                    <span className={`food-est ${e.trust}`}
                      title={e.portion_conf != null && e.portion_conf < 0.75
                        ? "estimated, and the amount was guessed" : "estimated nutrition"}>
                      {e.trust === "rough" ? "rough" : "est"}</span>
                  )}
                  <span className="food-entry-serv"> {e.serving_desc}</span>
                </span>
                <span className="food-step">
                  <button onClick={() => step(e, -0.5)} title="Less">−</button>
                  <span>{+e.servings.toFixed(2)}×</span>
                  <button onClick={() => step(e, 0.5)} title="More">+</button>
                </span>
                <span className="food-entry-n">{kcalText(e)} kcal · {fmtN(e.nutrients.protein)}g P</span>
                <button className="food-x" onClick={() => deleteEntry(e)} title="Remove">
                  <Icon name="trash" size={11} />
                </button>
              </li>
            ))}
          </ul>
        </div>
      ))}
      <div className="food-foot">
        {data.closed
          ? <button className="chip active" onClick={() => closeDay(data.day, true)}>
              <Icon name="check" size={10} /> done for today</button>
          : <button className="chip" disabled={!data.entries.length}
              onClick={() => closeDay(data.day, false)}>done for today</button>}
      </div>
      <Suggestions day={data.day} />
      <h4 className="people-section">nutrients</h4>
      {!tg && <p className="widget-empty">Targets appear once setup is done.</p>}
      <ul className="food-bars wide">
        {Object.entries(data.nutrients).map(([k, [label, unit]]) => (
          <NutrientBar key={k} label={label} value={data.totals[k]} unit={unit}
            target={tg?.nutrients[k]?.target} kind={tg?.nutrients[k]?.kind}
            missing={data.missing[k]} />
        ))}
      </ul>
      {Object.keys(data.missing).length > 0 && (
        <p className="food-note">* some logged items don't carry this nutrient, so the total is a floor.</p>
      )}
    </div>
  );
}

function Suggestions({ day }) {
  const { data } = useWidgetData(`/api/food/suggest?day=${day}`, "food");
  if (!data) return null;
  return (
    <>
      <h4 className="people-section">next: {data.meal}</h4>
      {!data.plates.length && <p className="widget-empty">{data.why_empty}</p>}
      <ul className="food-plates">
        {data.plates.map((p) => (
          <li key={p.venue}>
            <span className="food-plate-venue">{p.venue_name}</span>
            <span className="food-plate-items">{p.items.map((i) => i.name).join(" + ")}</span>
            <span className="food-plate-n">
              {fmtN(p.adds.kcal)} kcal · +{fmtN(p.adds.protein)}g P
              {p.closes.length > 0 && <> · closes {p.closes.join(", ")}</>}
            </span>
            <button className="chip" onClick={() => Promise.all(p.items.map((i) =>
              postJSON("/api/food/log", { food: i.ref, meal: data.meal, venue: p.venue, day })))
              .then(foodChanged)}>ate it</button>
          </li>
        ))}
      </ul>
    </>
  );
}

function Menus({ today }) {
  const [which, setWhich] = useState(0);   // 0 today, 1 tomorrow
  const [hall, setHall] = useState(null);
  const [meal, setMeal] = useState(null);
  const day = today && (() => {
    const d = new Date(`${today}T12:00`);
    d.setDate(d.getDate() + which);
    return d.toISOString().slice(0, 10);
  })();
  // Kept with the day it's for, so flipping today/tomorrow shows nothing
  // stale while the other day loads.
  const [got, setGot] = useState(null);
  const data = got?.day === day ? got.data : null;
  const [error, setError] = useState(null);
  useEffect(() => {
    if (!day) return undefined;
    let live = true;
    const load = () => fetchJSON(`/api/food/menus?day=${day}`)
      .then((d) => { if (live) { setGot({ day, data: d }); setError(null); } })
      .catch((e) => live && setError(e.message));
    load();
    // New items are estimated in the background; look again while it works.
    const t = setInterval(() => { if (live) load(); }, 20000);
    return () => { live = false; clearInterval(t); };
  }, [day]);

  const halls = data?.halls || [];
  const h = halls.find((x) => x.slug === hall) || halls[0];
  const meals = h?.meals || [];
  const hourMeal = new Date().getHours() < 11 ? "Breakfast" : new Date().getHours() < 16 ? "Lunch" : "Dinner";
  const m = meals.find((x) => x.meal === meal) || meals.find((x) => x.meal === hourMeal) || meals[0];

  return (
    <div className="food-menus">
      <div className="chip-row">
        {["today", "tomorrow"].map((l, i) => (
          <button key={l} className={`chip ${which === i ? "active" : ""}`}
            onClick={() => setWhich(i)}>{l}</button>
        ))}
        <span className="food-sep" />
        {halls.map((x) => (
          <button key={x.slug} className={`chip ${h?.slug === x.slug ? "active" : ""}`}
            onClick={() => setHall(x.slug)}>{x.name.replace(/ (Residential )?Dining Hall$/, "")}</button>
        ))}
      </div>
      {meals.length > 0 && (
        <div className="chip-row">
          {meals.map((x) => (
            <button key={x.meal} className={`chip ${m?.meal === x.meal ? "active" : ""}`}
              onClick={() => setMeal(x.meal)}>{x.meal}</button>
          ))}
          {data?.estimating && <span className="food-note">estimating new items…</span>}
        </div>
      )}
      {error && <WidgetError error={error} />}
      {!data && !error && <><div className="skel" /><div className="skel w80" /></>}
      {data && !halls.length && <p className="widget-empty">No menus published for {day}.</p>}
      {m?.stations.map((st) => (
        <div key={st.station} className="food-station">
          <h4 className="people-section">{st.station}</h4>
          <ul className="food-menu-items">
            {st.items.map((it) => (
              <li key={it.item}>
                <span className="food-entry-name">
                  {it.item}
                  {it.prefs.map((p) => <span key={p} className="food-tag">{p.replace("-ingredients", "")}</span>)}
                </span>
                <span className="food-entry-n" title={it.serving_desc || ""}>
                  {it.kcal != null ? `${it.kcal} kcal · ${it.protein}g P` : "…"}
                </span>
                <button className="food-x" disabled={!it.food_id}
                  title={it.food_id ? `Log it — your usual amount, or 1 serving (${it.serving_desc})` : "Not estimated yet"}
                  onClick={() => logFood({ food: `f${it.food_id}`, meal: MEAL_OF[m.meal],
                    venue: h.slug, day }, `Logged ${it.item}`)}>
                  <Icon name="plus" size={12} />
                </button>
              </li>
            ))}
          </ul>
        </div>
      ))}
    </div>
  );
}

function Week() {
  const { data, error } = useWidgetData("/api/food/week", "food");
  const { data: today } = useWidgetData("/api/food/day", "food");
  if (error) return <WidgetError error={error} />;
  if (!data) return <div className="skel" />;
  const max = Math.max(...data.days.map((d) => Math.max(d.totals.kcal || 0, d.targets.kcal || 0)), 1);
  const avg = (k) => {
    const ds = data.days.filter((d) => d.logged && d.coverage[k] != null);
    return ds.length ? ds.reduce((s, d) => s + d.coverage[k], 0) / ds.length : null;
  };
  const micros = ["protein", "fiber", "potassium", "calcium", "iron", "magnesium", "zinc",
    "vit_a", "vit_c", "vit_d", "vit_b12", "folate", "omega3"];

  return (
    <div className="food-week">
      <h4 className="people-section">energy (bar) vs target (tick)</h4>
      <ul className="food-week-bars">
        {data.days.map((d) => (
          <li key={d.day} title={`${d.day}: ${fmtN(d.totals.kcal)} kcal, ${fmtN(d.totals.protein)} g protein`}>
            <span className="food-week-day">{shortDay(d.day)}{d.training ? " ·T" : ""}</span>
            <span className="food-bar-track">
              <span className="food-bar-fill" style={{ width: `${(d.totals.kcal || 0) / max * 100}%` }} />
              {d.targets.kcal && <span className="food-tick" style={{ left: `${d.targets.kcal / max * 100}%` }} />}
            </span>
            <span className="food-bar-value">
              {d.logged ? `${fmtN(d.totals.kcal)} · ${fmtN(d.totals.protein)}g P` : "—"}
              {d.closed ? " ✓" : ""}
            </span>
          </li>
        ))}
      </ul>
      <h4 className="people-section">average coverage, logged days</h4>
      <ul className="food-bars wide">
        {micros.filter((k) => avg(k) != null).map((k) => (
          <NutrientBar key={k} label={data.nutrients[k][0]} value={avg(k) * 100} target={100} unit="%" />
        ))}
      </ul>
      <h4 className="people-section">
        weight{today?.weight.avg7_kg ? ` · 7-day average ${(today.weight.avg7_kg / 0.45359237).toFixed(1)} lb` : ""}
      </h4>
      <ul className="food-weights">
        {data.weigh_ins.slice(-8).map((x) => (
          <li key={x.day}><span>{shortDay(x.day)}</span><span>{(x.kg / 0.45359237).toFixed(1)} lb</span></li>
        ))}
      </ul>
      <WeighIn placeholder="Weigh-in (lb)…" />
      {today && <p className="food-note">Weigh {today.weight.how}.</p>}
      {today && (
        <>
          <h4 className="people-section">scale check</h4>
          <p className={`food-cal-line ${today.calibration?.verdict || ""}`}>{today.calibration_line}</p>
        </>
      )}
      <p className="food-note">
        DD spent this week: ${data.days.reduce((s, d) => s + d.dd_spent, 0).toFixed(2)}
      </p>
    </div>
  );
}

const FIELDS = [
  ["sex", "Sex (DRI table)", "select", ["male", "female"]],
  ["birth_year", "Birth year", "number"],
  ["height_in", "Height (inches)", "number"],
  ["weight_lb", "Weight today (lb)", "number"],
  ["activity", "Day-to-day activity (gym excluded)", "select", ["sedentary", "light", "moderate", "very"]],
  ["goal", "Goal", "select", ["lean_bulk", "maintain", "cut"]],
  ["training_kcal", "kcal a gym session burns", "number"],
  ["dd_balance", "Dining Dollars balance now ($)", "number"],
  ["semester_end", "Semester ends (dollars expire)", "date"],
  ["door_price", "Dining hall entry ($ DD)", "number"],
];

function Setup({ data }) {
  const p = data.profile;
  const [f, setF] = useState(() => ({
    sex: p.sex || "", birth_year: p.birth_year || "",
    height_in: p.height_cm ? (p.height_cm / 2.54).toFixed(1) : "",
    weight_lb: data.weight.kg ? (data.weight.kg / 0.45359237).toFixed(1) : "",
    activity: p.activity || "light", goal: p.goal || "lean_bulk",
    training_kcal: p.training_kcal ?? 300, dd_balance: p.dd_balance ?? "",
    semester_end: p.semester_end || "", door_price: p.door_price ?? "",
  }));
  const [msg, setMsg] = useState(null);

  function save() {
    const num = (v) => (v === "" || v == null ? undefined : Number(v));
    const body = {
      sex: f.sex || undefined, birth_year: num(f.birth_year),
      height_cm: f.height_in ? +(Number(f.height_in) * 2.54).toFixed(1) : undefined,
      activity: f.activity, goal: f.goal, training_kcal: num(f.training_kcal),
      semester_end: f.semester_end || undefined, door_price: num(f.door_price),
    };
    // Only a changed balance resets the "as of" — re-saving the same number
    // must not forget the spending logged since.
    if (num(f.dd_balance) !== undefined && num(f.dd_balance) !== p.dd_balance) {
      body.dd_balance = num(f.dd_balance);
    }
    Object.keys(body).forEach((k) => body[k] === undefined && delete body[k]);
    const lb = num(f.weight_lb);
    const sameWeight = data.weight.kg && lb && Math.abs(lb * 0.45359237 - data.weight.kg) < 0.05;
    postJSON("/api/food/profile", body, "PUT")
      .then(() => (lb && !sameWeight ? postJSON("/api/food/weigh", { lb }) : null))
      .then(() => { setMsg("Saved."); foodChanged(); })
      .catch((e) => setMsg(e.message));
  }

  return (
    <div className="food-setup fin-setup">
      {FIELDS.map(([k, label, type, opts]) => (
        <label key={k} className="food-field">
          <span>{label}</span>
          {type === "select"
            ? <select value={f[k]} onChange={(e) => setF({ ...f, [k]: e.target.value })}>
                <option value="">—</option>
                {opts.map((o) => <option key={o} value={o}>{o.replace("_", " ")}</option>)}
              </select>
            : <input type={type} value={f[k]} onChange={(e) => setF({ ...f, [k]: e.target.value })} />}
        </label>
      ))}
      <p className="fin-setup-hint">
        Protein is 1.8 g/kg every day. Energy is maintenance on rest days and maintenance + the
        session + 300 on training days (Exercise checked, or a gym event on the calendar).
        Once there are 4+ weigh-ins over two weeks and 7 fully logged days, maintenance comes from
        your weight trend against what you log (phased in over four weeks), so a steady under- or
        over-log is corrected for; until then, weekly weigh-ins nudge it ±100 kcal.
      </p>
      <div className="fin-setup-row">
        <button className="chip active" onClick={save}>save</button>
        {msg && <span className="food-note">{msg}</span>}
      </div>
    </div>
  );
}
