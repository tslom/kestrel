import { useCallback, useEffect, useState } from "react";
import { fetchJSON } from "../lib/api";
import { fmtDur, shortDay } from "../lib/time";

// The two place panels under the map: VisitList (which place each visit was) and
// PlaceNamer (naming a cluster). MapSheet owns the picked place.
// Design: DESIGN.md → `frontend/src/widgets/MapSheet.jsx`.

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

// What the ranking currently believes, in a sentence; says so when nothing is learned yet.
function learnedNote(weights) {
  if (!weights) return "";
  const shown = Object.values(weights).reduce((n, w) => n + w.shown, 0);
  const taken = Object.values(weights).reduce((n, w) => n + w.accepted, 0);
  if (!shown) {
    return "Ranked on starting weights — nothing learned yet. What you pick "
      + "here, and what you pass over, is what tunes it.";
  }
  const n = `${shown} candidate${shown === 1 ? "" : "s"}`;
  // No winner can be named off zero accepts.
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

// Below this support a guess is drawn weak. Measured: real match ~0.9, evening vs evening
// labels 0.6-0.9, mismatched-hour visits ~0.2 (while still taking ~100% of the vote).
const WEAK_SUPPORT = 0.35;

// Which place each visit was. Predictions are shown greyed and never stored.
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
                  {/* Calendar event during the visit (28% of visits): a chip to click, never a label. */}
                  {!v.name && v.event?.where && !used.includes(v.event.where) && (
                    <button className="chip guess"
                      title={`on your calendar: ${v.event.summary || v.event.where}`}
                      disabled={busy === v.started_at}
                      onClick={() => put(v, v.event.where)}>
                      {/* Truncated for the chip; the label it writes is the whole name. */}
                      {v.event.where.length > 22
                        ? `${v.event.where.slice(0, 21)}…` : v.event.where} · cal
                    </button>
                  )}
                  {!v.name && v.predicted && (
                    // `support`, not `share`, sets the weight: share is relative (who won the vote).
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

// Naming a cluster: ranked shortlist fills an always-editable field; nothing applies without a click
// (the top candidate is right ~1/3 of the time and a name changes `time_at_home_min`).
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

  // Fire-and-forget: a learning signal must never fail the save.
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
        // Deliberately no `from`: dating it to the viewed range would stop it resolving earlier fixes.
      }),
    })
      .then(() => {
        // Only after the note is written.
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
