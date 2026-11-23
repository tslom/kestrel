// The two clock columns of an event row: when it starts, and — far quieter,
// at the row's trailing edge — when it ends.
//
// Rows used to carry a start and nothing else, which answers "what's next" and
// nothing else: not whether the 3pm is still running, not whether the gap
// before dinner is real, not whether you can still walk into office hours. The
// end time is the second question every one of these rows gets asked, so it
// belongs on the row rather than one click away in the sheet.
//
// It just can't sit next to the start. Printing "2:00–3:20 PM" in the time
// column doubles the number of clock times you read while scanning for the
// next thing, and the one event that crosses noon needs both meridiems, so its
// column is wider and every summary below it starts on a different line. So
// the start column stays exactly the width it always was, and the end goes to
// the far right in `--paper-faint`: a second aligned column, out of the scan
// path, there when you look for it.
export default function EventTime({ range, text, pre }) {
  return <span className="event-time">{pre}{text ?? range?.start}</span>;
}

// The trailing half. Rendered last in the row — after the summary and after
// whatever else a card puts there (an "open now" chip, a room) — so its
// auto margin can pin it to the edge.
export function EventEnd({ range }) {
  if (!range?.end) return null;
  return (
    <span className="event-ends"
      title={range.overnight ? `ends ${range.endLabel} the next day` : undefined}>
      {range.end}
    </span>
  );
}
