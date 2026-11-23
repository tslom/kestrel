import { useCallback, useEffect, useRef, useState } from "react";
import { fetchJSON } from "./api";
import { useTopic } from "./refresh";

// The read half of every bespoke sheet (IDEAS §1).
//
// A sheet is several views of one source on screen at once, which means N
// reads per range instead of the provider panel's one. Doing that N times in
// seven files is how the seq-guard gets forgotten in six of them, so it lives
// here: one `Promise.all`, one generation counter, one rule for what counts as
// an error.
//
// `views` must be a module-level constant. It's the effect's dependency, so an
// array literal written inline would be a new array every render and the sheet
// would refetch forever. A source with no metrics passes `[null]`.

// The provider registry (/api/providers: each source's ranges, metrics, …) is
// static for the life of the backend process, and every sheet open used to
// refetch it. Fetched once per page load and shared, the way googleLinks.js
// caches the OAuth addresses. A failed read isn't cached — the next sheet to
// open asks again rather than inheriting an outage.
let registry = null;   // Promise<{ [source]: meta }>

export function getProviders() {
  if (!registry) {
    registry = fetchJSON("/api/providers");
    registry.catch(() => { registry = null; });
  }
  return registry;
}

export function useSheetData(source, views, initialRange) {
  const [meta, setMeta] = useState(null);
  const [range, setRange] = useState(initialRange);
  const [payloads, setPayloads] = useState(null);   // { metric: envelope }
  const [error, setError] = useState(null);
  const [loading, setLoading] = useState(false);
  // Only the newest request may write. Several of these reads sync their
  // provider first (calendar, email, health), so clicking through ranges can
  // land a slow earlier answer on top of the one you asked for last — and it
  // would sit there looking authoritative until you clicked again.
  const seq = useRef(0);

  const load = useCallback((token) => {
    setLoading(true);
    const mine = ++seq.current;
    Promise.all(views.map((m) => {
      const q = new URLSearchParams({ range: token });
      if (m) q.set("metric", m);
      return fetchJSON(`/api/${source}?${q}`)
        .then((p) => [m || "", p])
        .catch((e) => [m || "", { error: e.message }]);
    })).then((pairs) => {
      if (mine !== seq.current) return;
      // They all read the same provider, so when they all fail they failed for
      // one reason — not authorized, nothing synced — and N copies of that
      // sentence is N-1 too many. A single failure is left to its own band,
      // which knows what it was missing.
      const failed = pairs.filter(([, p]) => p.error);
      setError(failed.length === pairs.length ? failed[0][1].error : null);
      setPayloads(Object.fromEntries(pairs));
      setLoading(false);
    });
  }, [source, views]);

  useEffect(() => {
    getProviders()
      .then((all) => setMeta(all[source]))
      .catch(() => setMeta({ ranges: [] }));   // chips can wait; the data can't
    load(initialRange);
  }, [source, initialRange, load]);

  // A source's topic is its name, so an open sheet moves with its card.
  useTopic(source, () => load(range));

  return {
    meta, range, error, loading, payloads,
    // What RangePicker takes: null while the registry is loading (it shows
    // nothing yet), [] for a source with no ranges.
    ranges: meta ? (meta.ranges || []) : null,
    pickRange: (token) => { setRange(token); load(token); },
    reload: () => load(range),
    rows: (m) => payloads?.[m || ""]?.data || [],
    refreshedAt: views.map((m) => payloads?.[m || ""]?.refreshed_at).find(Boolean),
  };
}
