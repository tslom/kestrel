import { fetchJSON } from "../lib/api";
import { useWidgetData } from "../lib/refresh";
import { NEEDS, HAS, QUIET } from "../lib/salience";
import WidgetError from "../ui/WidgetError.jsx";
import RefreshButton from "../ui/RefreshButton.jsx";
import Icon from "../ui/icons.jsx";
import { fmtTime, shortDay } from "../lib/time";
import { dragPin } from "../lib/drag.js";
import Folded from "../ui/Folded.jsx";

// Deliveries (§15) + travel (§16) on the desk. Both endpoints have existed
// since the extractors shipped and both already feed the brief — but with no
// card, a package arriving today was visible only if that morning's brief
// happened to mention it, and a flight only in prose.
//
// The two live on one card on purpose: each is empty most days, and two
// separate cards that are both usually blank is exactly the "everything
// visible at equal weight" the salience pass was built to end. Together they
// earn a slot on the days something is moving and collapse to a line when
// nothing is.

// A flight inside this window is the one you need to act on — the brief uses
// the same horizon for its Travel section.
const SOON_MS = 48 * 60 * 60 * 1000;

const STATUS_LABEL = {
  ordered: "ordered",
  shipped: "in transit",
  out_for_delivery: "out for delivery",
  delivered: "delivered",
};

function fmtDepart(iso) {
  const d = new Date(iso);
  return d.toLocaleDateString([], { month: "short", day: "numeric" }) + " · "
    + d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
}

// When a flight lands, appended to when it leaves. `dep_local` and `arr_local`
// are each local to their own airport (`app/tz.py`), which is how anyone reads
// a boarding pass — but it means the pair is not a duration, so the row says
// the two clock times and never the span between them. `+1` for a red-eye that
// lands on the next date, compared on the date strings so the zones stay put.
function arrival(t) {
  if (!t.arr_local) return null;
  const nextDay = t.arr_local.slice(0, 10) > (t.dep_local || "").slice(0, 10);
  return `–${fmtTime(t.arr_local)}${nextDay ? "+1" : ""}`;
}

function TripRow({ t, soon, off, onPin }) {
  const label = `flight · ${t.carrier}${t.flight_no}`;
  const lands = arrival(t);
  return (
    <li className={soon ? "soon" : ""} {...dragPin(label, t)}>
      <span className="travel-key">{t.carrier}{t.flight_no}</span>
      <span className="travel-desc">
        {/* JSX text, so the arrow has to be the character \u2014 an escape here is
            six literal characters, which is what the row was printing. */}
        {t.dep_airport}{"\u2192"}{t.arr_airport}
        {t.gate && <span className="travel-meta"> · gate {t.gate}</span>}
        {off && <span className="travel-alert"> · {t.status}</span>}
      </span>
      <span className="travel-when"
        title={lands ? `Departs ${t.dep_airport} local, lands ${t.arr_airport} local`
                     : undefined}>
        {fmtDepart(t.dep_local)}{lands}
      </span>
      <button className="pin-mini" onClick={() => onPin(label, t)}
        title="Pin this flight to your next console message"><Icon name="pin" /></button>
    </li>
  );
}

function PackageRow({ p, landing, onPin, onDelivered, onDrop }) {
  const label = `package · ${p.merchant || p.order_key}`;
  return (
    <li className={landing ? "soon" : ""} {...dragPin(label, p)}>
      <span className="travel-key">{p.merchant || p.carrier || "parcel"}</span>
      <span className="travel-desc" title={p.tracking_no || undefined}>
        {p.item_desc || p.order_id || p.tracking_no}
        <span className="travel-meta"> · {STATUS_LABEL[p.status] || p.status}</span>
      </span>
      <span className="travel-when">
        {landing ? "today" : p.eta_day ? shortDay(p.eta_day) : "\u2014"}
      </span>
      <button className="pin-mini" onClick={() => onPin(label, p)}
        title="Pin this package to your next console message"><Icon name="pin" /></button>
      {p.status !== "delivered" && (
        <button className="pin-mini" onClick={() => onDelivered(p)}
          title="Mark delivered — stays on the card 3 days for returns">
          <Icon name="check" /></button>
      )}
      <button className="pin-mini travel-drop" onClick={() => onDrop(p)}
        title="Remove — for a row that was never a real parcel, not one that arrived">
        <Icon name="close" /></button>
    </li>
  );
}

export function useTravelCard() {
  // payload: { packages, trips, day }
  const { data: payload, error, setError, reload, busy: refreshing } = useWidgetData(() =>
    Promise.all([fetchJSON("/api/packages"), fetchJSON("/api/trips")])
      .then(([p, t]) => ({ packages: p.data, trips: t.data, day: p.day })), "travel");

  // The two corrections a delivery card actually needs. Both reload rather
  // than patching `payload` in place: marking delivered doesn't drop the row
  // (it stays three days for returns) and a delete can free the merchant's
  // one open order for the extractor's next follow-up match, so what the
  // list looks like afterwards is the backend's answer, not one we can guess.
  const pkgPath = (p) => `/api/packages/${encodeURIComponent(p.order_key)}`;

  function markDelivered(p) {
    fetchJSON(`${pkgPath(p)}/delivered`, { method: "POST" })
      .then(reload).catch((e) => setError(e.message));
  }

  // Not "it arrived" — that's markDelivered. This is for a row that was never
  // a real parcel: a promotion the extractor read as an order, or one parcel
  // showing up twice because the confirmation and the shipping notice shared
  // no id. No confirm step, same as the other list cards; a later email about
  // the order re-opens the row anyway.
  function drop(p) {
    fetchJSON(pkgPath(p), { method: "DELETE" })
      .then(reload).catch((e) => setError(e.message));
  }

  const packages = payload?.packages || [];
  const trips = payload?.trips || [];

  // "Today" is the backend's day_key (4am rollover), not the browser's — an
  // eta_day is a day_key, so comparing it against a local `new Date()` would
  // disagree with the rest of the app between midnight and 4am.
  const landingToday = (p) =>
    p.status !== "delivered"
    && (p.status === "out_for_delivery" || p.eta_day === payload?.day);
  const departingSoon = (t) => new Date(t.dep_local) - Date.now() <= SOON_MS;
  // Anything AeroAPI came back with that isn't a normal confirmed flight —
  // a delay, a gate change, a cancellation.
  const offNominal = (t) => t.status && !/^(confirmed|on time)$/i.test(t.status);

  const urgent = packages.some(landingToday) || trips.some(departingSoon)
    || trips.some(offNominal);

  // What the card renders at full weight: a parcel landing today, a flight
  // inside the 48h horizon, anything AeroAPI flagged. A parcel that arrives
  // next Tuesday is real, and it is not something you do anything about
  // today — so it folds instead of taking a row.
  const liveTrips = trips.filter((t) => departingSoon(t) || offNominal(t));
  const laterTrips = trips.filter((t) => !liveTrips.includes(t));
  const livePkgs = packages.filter(landingToday);
  const laterPkgs = packages.filter((p) => !livePkgs.includes(p));
  const later = laterTrips.length + laterPkgs.length;
  const laterLine = [laterPkgs.length && `${laterPkgs.length} in transit`,
                     laterTrips.length && `${laterTrips.length} trip${laterTrips.length > 1 ? "s" : ""} booked`]
    .filter(Boolean).join(" · ");

  // Counts, not the nearest item: a parcel and a flight are different enough
  // that "2 parcels · 2 flights" beats naming one of them and hiding the rest.
  const landing = packages.filter(landingToday).length;
  const soon = trips.filter((t) => departingSoon(t) || offNominal(t)).length;
  const level = !payload ? null : urgent ? NEEDS : packages.length + trips.length ? HAS : QUIET;
  const summary = urgent
    ? [landing && `${landing} landing today`, soon && `${soon} flight${soon > 1 ? "s" : ""} to watch`]
        .filter(Boolean).join(" · ")
    : packages.length + trips.length
      ? [packages.length && `${packages.length} in transit`,
         trips.length && `${trips.length} trip${trips.length > 1 ? "s" : ""} booked`]
          .filter(Boolean).join(" · ")
      : "nothing in transit";

  return { payload, error, reload, refreshing, markDelivered, drop,
           packages, trips, departingSoon, offNominal, urgent, liveTrips, laterTrips,
           livePkgs, laterPkgs, later, laterLine, level, summary };
}

export default function TravelWidget({ card, onPin }) {
  const { payload, error, reload, refreshing, markDelivered, drop,
          packages, trips, departingSoon, offNominal, urgent, liveTrips, laterTrips,
          livePkgs, laterPkgs, later, laterLine } = card;

  return (
    <section className="widget" {...dragPin("deliveries & travel", payload)}>
      <header>
        <h2>Deliveries &amp; travel</h2>
        <div className="widget-actions">
          <RefreshButton onClick={reload} busy={refreshing} />
          <button className="pin" disabled={!payload}
            onClick={() => onPin("deliveries & travel", payload)}
            title="Attach this data to your next console message"><Icon name="pin" /> pin</button>
        </div>
      </header>
      {error && <WidgetError error={error} />}
      {!payload && !error && <><div className="skel" /><div className="skel w60" /></>}
      {payload && !packages.length && !trips.length && (
        <p className="widget-empty">Nothing in transit.</p>
      )}
      {liveTrips.length > 0 && (
        // Flights first: a departure is a harder deadline than a parcel.
        <ul className="travel-list" {...dragPin("flights · to watch", liveTrips)}>
          {liveTrips.map((t) => <TripRow key={`${t.pnr}-${t.flight_no}-${t.dep_day}`}
            t={t} soon={departingSoon(t)} off={offNominal(t)} onPin={onPin} />)}
        </ul>
      )}
      {livePkgs.length > 0 && (
        <ul className="travel-list" {...dragPin("packages · landing today", livePkgs)}>
          {livePkgs.map((p) => <PackageRow key={p.order_key} p={p} landing
            onPin={onPin} onDelivered={markDelivered} onDrop={drop} />)}
        </ul>
      )}
      {later > 0 && (
        <Folded summary={laterLine} hideLabel="hide the rest"
          open={!liveTrips.length && !livePkgs.length}>
          {laterTrips.length > 0 && (
            <ul className="travel-list" {...dragPin("flights · booked", laterTrips)}>
              {laterTrips.map((t) => <TripRow key={`${t.pnr}-${t.flight_no}-${t.dep_day}`}
                t={t} off={offNominal(t)} onPin={onPin} />)}
            </ul>
          )}
          {laterPkgs.length > 0 && (
            <ul className="travel-list" {...dragPin("packages · in transit", laterPkgs)}>
              {laterPkgs.map((p) => <PackageRow key={p.order_key} p={p}
                onPin={onPin} onDelivered={markDelivered} onDrop={drop} />)}
            </ul>
          )}
        </Folded>
      )}
    </section>
  );
}
