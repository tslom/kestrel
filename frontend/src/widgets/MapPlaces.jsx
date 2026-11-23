import { useCallback, useEffect, useState } from "react";
import { fetchJSON } from "../lib/api";
import { fmtDur, shortDay } from "../lib/time";

// The two place panels under the map: which place each visit
// was, and naming a cluster the map drew. Split out of MapSheet.jsx so the map
// engine and the labelling UI read apart; the map still owns the picked place
// and hands it down.

const BUCKET_LABEL = {
  calendar: "your calendar",
  google_poi: "map landmarks",
  google_address: "street addresses",
  google_nearby: "nearby places",
  overpass: "OpenStreetMap",
  typed: "names you typed yourself",
};

// The lookups behind `unavailable` in a suggestion — sources, not buckets.
const SOURCE_LABEL = {
  google: "Google's geocoder",
  google_nearby: "Google Places",
  overpass: "OpenStreetMap",
};

// What the ranking currently believes, in a sentence rather than a table.
// Shown because a weight learned from four clicks is a number you should be
// able to see before you trust the order it put things in — and because until
// you have clicked anything it should say so, rather than implying it knows.
function learnedNote(weights) {
  if (!weights) return "";
  const shown = Object.values(weights).reduce((n, w) => n + w.shown, 0);
  const taken = Object.values(weights).reduce((n, w) => n + w.accepted, 0);
  if (!shown) {
    return "Ranked on starting weights — nothing learned yet. What you pick "
      + "here, and what you pass over, is what tunes it.";
  }
  const n = `${shown} candidate${shown === 1 ? "" : "s"}`;
  // Naming a winner off zero accepts would be a claim with nothing behind it:
  // every shortlist so far was closed, which says the ranking is not helping,
  // not which part of it helps most.
  if (!taken) {
    return `${n} shown so far, none kept — so the order is still the `
      + "starting weights, nudged down.";
  }
  const best = Object.entries(weights)
    .filter(([, w]) => w.accepted > 0)
    .sort((a, b) => b[1].weight - a[1].weight)[0];
  return `Learned from ${n} shown and ${taken} kept — `
    + `${BUCKET_LABEL[best[0]] || best[0]} has been the best guide `
    + `(${best[1].accepted}/${best[1].shown}).`;
}

// Below this much resemblance to the nearest labelled visit, a guess is drawn
// as a guess about nothing in particular. Measured on the hub cluster with six
// labels: a genuine match scores ~0.9, an evening visit against evening labels
// 0.6-0.9, and the two cases that motivated this — 09:00 scored against midday
// labels, and a 00:05 stay scored against evening ones — land near 0.2 while
// still taking ~100% of the vote, because nothing else was closer.
const WEAK_SUPPORT = 0.35;

// Which place each visit was.
//
// A cluster is not a place at campus density — the busiest unnamed one here
// spans 168m and holds five buildings — and nothing automatic separates them
// (app/visits.py has the three measurements). So the label comes from you,
// one visit at a time, and what gets learned is the *shape*: once a few are
// labelled, "evening and long" and "midday and short" pull apart on their own.
//
// A prediction is shown greyed in the picker and is never stored. Nothing is
// true here because the learner thought so — it is true when you click it.
export function VisitList({ place, range }) {
  const [data, setData] = useState(null);
  const [busy, setBusy] = useState(null);
  const [adding, setAdding] = useState(null);

  const load = useCallback(() => {
    fetchJSON(`/api/location/place/visits?lat=${place.lat}&lon=${place.lon}&range=${range}`)
      .then(setData)
      .catch(() => setData(null));
  }, [place.lat, place.lon, range]);

  useEffect(() => { load(); }, [load]);

  const put = (visit, name) => {
    setBusy(visit.started_at);
    fetchJSON("/api/location/visits/label", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        started_at: visit.started_at, lat: place.lat, lon: place.lon, name,
      }),
    })
      .then(() => { setBusy(null); setAdding(null); load(); })
      .catch(() => setBusy(null));
  };

  if (!data) return null;
  const { visits, used_before: used, labelled, min_to_predict: need } = data;

  return (
    <div className="visit-list">
      <p className="map-note">
        {data.place.visits} visits here
        {data.place.sightings > 0 && (
          <>
            {", "}<strong>{data.place.sightings}</strong> of them a single fix
            followed by silence — listed, but not a stay and never learned from
          </>
        )}.
        {" "}
        {labelled < need
          ? `Label ${need - labelled} more and the rest get a guess.`
          : `Learning from ${labelled} labelled visit${labelled === 1 ? "" : "s"}.`}
      </p>
      <ul className="visits">
        {visits.map((v) => (
          <li key={v.started_at} className={v.sighting ? "is-sighting" : ""}>
            <span className="visit-when">
              {shortDay(v.day)} {v.started_at.slice(11, 16)}
            </span>
            <span className="visit-dur">{fmtDur(v.minutes)}</span>
            <span className="visit-pick">
              {adding === v.started_at ? (
                <input
                  className="place-name-input" autoFocus
                  placeholder="which place?"
                  onKeyDown={(e) => {
                    if (e.key === "Enter") put(v, e.target.value);
                    if (e.key === "Escape") setAdding(null);
                  }} />
              ) : (
                <>
                  {used.map((n) => (
                    <button
                      key={n} disabled={busy === v.started_at}
                      className={`chip${v.name === n ? " active" : ""}`}
                      onClick={() => put(v, v.name === n ? "" : n)}>
                      {n}
                    </button>
                  ))}
                  <button className="chip" disabled={busy === v.started_at}
                    onClick={() => setAdding(v.started_at)}>+</button>
                  {/* What was on your calendar during this visit. Offered
                      because typing is the expensive part and 28% of real
                      visits have one — but it names what you were *doing*,
                      and only sometimes does that name where you were, so it
                      is a chip to click and never a label. */}
                  {!v.name && v.event?.where && !used.includes(v.event.where) && (
                    <button className="chip guess"
                      title={`on your calendar: ${v.event.summary || v.event.where}`}
                      disabled={busy === v.started_at}
                      onClick={() => put(v, v.event.where)}>
                      {/* Real ones run to "United Airlines Field at the LA
                          Memorial Coliseum". The chip is a thing to click;
                          the whole name is in the tooltip and in the label
                          it writes. */}
                      {v.event.where.length > 22
                        ? `${v.event.where.slice(0, 21)}…` : v.event.where} · cal
                    </button>
                  )}
                  {!v.name && v.predicted && (
                    // A guess, and legible as one: it sits outside the chips,
                    // carries what it rests on, and takes a click to become
                    // a label like any other.
                    //
                    // `support` and not `share` decides how strongly it is
                    // drawn. Share is relative — it only says which label won
                    // the vote — so a 09:00 visit scored against midday labels
                    // and a 00:00 one scored against evening labels both come
                    // back near 100% share while resembling nothing much.
                    // Support is the part that knows that, so a weak guess has
                    // to *look* weak.
                    <button
                      className={`chip guess${v.predicted.support < WEAK_SUPPORT ? " weak" : ""}`}
                      title={`${Math.round(v.predicted.share * 100)}% of the vote, `
                        + `${Math.round(v.predicted.support * 100)}% like the closest of `
                        + `${v.predicted.total_labels} labelled visits`}
                      onClick={() => put(v, v.predicted.name)}>
                      {v.predicted.name}?
                      {v.predicted.support < WEAK_SUPPORT ? " · unlike your labels"
                        : ` · ${v.predicted.from_labels}`}
                    </button>
                  )}
                  {v.sighting && <span className="visit-why">one fix</span>}
                </>
              )}
            </span>
          </li>
        ))}
      </ul>
    </div>
  );
}

// Naming a cluster the map drew.
//
// The shape is deliberate: a ranked shortlist, an editable field, and nothing
// that happens without a click. The backend's own measurements (see
// app/place_suggest.py) put the top candidate right about a third of the time
// — good enough to save typing, nowhere near good enough to apply on its own,
// since an accepted name changes `time_at_home_min` and every place count
// after it. So the suggestion fills the box and you press the button.
//
// The field is always editable and never a dropdown, because the best
// candidate is often nearly right in a way only you can finish: the calendar
// knows the room ("SAL 101") when what you want is the building.
export function PlaceNamer({ place, range, onNamed }) {
  const [open, setOpen] = useState(false);
  const [sug, setSug] = useState(null);
  const [name, setName] = useState("");
  const [radius, setRadius] = useState(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState(null);

  const ask = () => {
    setOpen(true);
    setErr(null);
    setSug("loading");
    fetchJSON(`/api/location/place/suggest?lat=${place.lat}&lon=${place.lon}&range=${range}`)
      .then((s) => {
        setSug(s);
        setName(s.candidates[0]?.name || "");
        setRadius(s.place.suggested_radius_m);
      })
      .catch((e) => { setSug(null); setErr(e.message); });
  };

  // What became of the shortlist. Fire-and-forget on purpose: this is how the
  // ranking learns, and a learning signal that could fail your save would be
  // a worse trade than a weight that misses one observation.
  const tellBackend = (chosen) => {
    if (!sug || sug === "loading") return;
    fetchJSON("/api/places/feedback", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        lat: sug.place.lat,
        lon: sug.place.lon,
        shown: sug.candidates.map((c) => ({ name: c.name, bucket: c.bucket })),
        chosen,
      }),
    }).catch(() => {});
  };

  const cancel = () => { tellBackend(null); setOpen(false); };

  const save = () => {
    if (!name.trim() || busy) return;
    setBusy(true);
    setErr(null);
    fetchJSON("/api/places", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        name: name.trim(),
        lat: sug?.place.lat ?? place.lat,
        lon: sug?.place.lon ?? place.lon,
        radius_m: radius || 100,
        // Deliberately no `from`. The first day this place appears is the
        // first day of whatever range you happen to be looking at, not the
        // day the building went up — dating it to that would stop the place
        // resolving its own earlier fixes. A plain place is simply true;
        // `from`/`until` are for a home that moved, and you write those in
        // the note (see app/places.py).
      }),
    })
      .then(() => {
        // Only after the note is actually written: a name that failed to save
        // is not a name you accepted.
        tellBackend(name.trim());
        setBusy(false);
        setOpen(false);
        onNamed();
      })
      .catch((e) => { setBusy(false); setErr(e.message); });
  };

  if (!open) {
    return <button className="chip" onClick={ask}>name this place</button>;
  }

  return (
    <div className="place-namer">
      {sug === "loading" && <p className="map-note">looking it up…</p>}
      {sug && sug !== "loading" && (
        <>
          {sug.candidates.length === 0 && (
            <p className="map-note">
              Nothing recognisable here — the calendar says nothing about these
              visits and the map has no named feature this close. Type a name.
            </p>
          )}
          {sug.unavailable?.length > 0 && (
            <p className="map-note">
              {sug.unavailable.map((s) => SOURCE_LABEL[s] || s).join(" and ")}{" "}
              didn't answer, so this list is short — reopen in a few minutes
              to ask again.
            </p>
          )}
          <ul className="place-cands">
            {sug.candidates.map((c) => (
              <li key={`${c.source}:${c.name}`}>
                <button
                  className={`chip${c.name === name ? " active" : ""}`}
                  onClick={() => setName(c.name)}>
                  {c.name}
                </button>
                <span className="place-why">{c.why}</span>
              </li>
            ))}
          </ul>
          <div className="place-namer-row">
            <input
              className="place-name-input"
              value={name}
              placeholder="what is this place called?"
              onChange={(e) => setName(e.target.value)}
              onKeyDown={(e) => { if (e.key === "Enter") save(); }} />
            <label className="place-radius">
              radius
              <input
                type="number" min="20" max="500" step="10"
                value={radius ?? ""}
                onChange={(e) => setRadius(Number(e.target.value))} />
              m
            </label>
          </div>
          <p className="map-note">
            {sug.place.visits} visits over {sug.place.days} days, spread{" "}
            {sug.place.span_m}m. Saving writes a note to 02-Areas/places — edit
            or delete it there.
          </p>
          <p className="map-note">{learnedNote(sug.weights)}</p>
        </>
      )}
      {err && <p className="map-note map-err">{err}</p>}
      <button className="chip" disabled={busy || !name.trim()} onClick={save}>
        {busy ? "saving…" : "save place"}
      </button>{" "}
      <button className="chip" onClick={cancel}>cancel</button>
    </div>
  );
}
