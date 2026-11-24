import { useCallback, useEffect, useRef, useState } from "react";
import { fetchJSON } from "./api";
import { useTopic } from "./refresh";

// The read half of every bespoke sheet: N views of one source per range, with one Promise.all, one generation
// counter and one error rule here (the seq-guard was forgotten in six of seven copies). `views` must be a
// module-level constant (it's the effect's dependency: an inline array refetches forever); no metrics: `[null]`.

// The provider registry (/api/providers) is static per backend process: fetched once per page load and shared; a failed read isn't cached.
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
  // Only the newest request may write: reads sync their provider first, so a slow earlier answer would land over the latest click.
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
      // All reading one provider, all failing means one reason: show it once; a single failure stays in its own band.
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
    // For RangePicker: null while the registry loads, [] for a source with no ranges.
    ranges: meta ? (meta.ranges || []) : null,
    pickRange: (token) => { setRange(token); load(token); },
    reload: () => load(range),
    rows: (m) => payloads?.[m || ""]?.data || [],
    refreshedAt: views.map((m) => payloads?.[m || ""]?.refreshed_at).find(Boolean),
  };
}
