// Hand-rolled sparkline: polyline over {day, value} points. Nulls split the
// line into segments (a gap stays a gap); lone points render as dots. The
// only chart primitive in the app — no chart library.

const fmt = (v) =>
  Math.abs(v) >= 1000 ? `${(v / 1000).toFixed(1)}k` : +v.toFixed(1);

// `last={false}` for a caller that prints the latest value itself, formatted
// with its unit — SeriesRow did, and the row read "13.8 13.8 km".
export default function Sparkline({ data, width = 180, height = 34, last = true }) {
  const vals = data
    .map((d) => d.value)
    .filter((v) => v != null && Number.isFinite(v));
  if (!vals.length) return <span className="spark-empty">no data</span>;

  const min = Math.min(...vals);
  const max = Math.max(...vals);
  const span = max - min || 1;
  const x = (i) =>
    data.length === 1 ? width / 2 : 1 + (i / (data.length - 1)) * (width - 2);
  const y = (v) => height - 3 - ((v - min) / span) * (height - 6);

  const segs = [];
  let cur = [];
  data.forEach((d, i) => {
    if (d.value == null || !Number.isFinite(d.value)) {
      if (cur.length) segs.push(cur);
      cur = [];
    } else {
      cur.push([x(i), y(d.value)]);
    }
  });
  if (cur.length) segs.push(cur);

  return (
    <span className="spark-row">
      <svg viewBox={`0 0 ${width} ${height}`} height={height} preserveAspectRatio="none"
        className="spark" role="img"
        aria-label={`sparkline, min ${fmt(min)}, max ${fmt(max)}`}>
        {segs.map((pts, i) =>
          pts.length === 1 ? (
            <circle key={i} cx={pts[0][0]} cy={pts[0][1]} r="1.6" />
          ) : (
            <polyline key={i} points={pts.map((p) => p.join(",")).join(" ")} />
          )
        )}
      </svg>
      <span className="spark-minmax">{fmt(min)}–{fmt(max)}</span>
      {last && <strong className="spark-last">{fmt(vals[vals.length - 1])}</strong>}
    </span>
  );
}
