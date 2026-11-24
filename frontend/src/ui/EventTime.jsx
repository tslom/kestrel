// An event row's clock columns: start, and the end far quieter at the trailing edge in `--paper-faint`.
// Not "2:00–3:20 PM" in one column: that doubles the times read while scanning, and an event crossing
// noon needs both meridiems, widening its column and misaligning every summary below.
export default function EventTime({ range, text, pre }) {
  return <span className="event-time">{pre}{text ?? range?.start}</span>;
}

// The trailing half: render it last in the row so its auto margin pins it to the edge.
export function EventEnd({ range }) {
  if (!range?.end) return null;
  return (
    <span className="event-ends"
      title={range.overnight ? `ends ${range.endLabel} the next day` : undefined}>
      {range.end}
    </span>
  );
}
