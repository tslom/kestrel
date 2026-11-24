// The card kept its numbers but couldn't refresh them (post-wake: DNS isn't back, reads fall to cache).
// Not a WidgetError: nothing is broken, so it has the range label's weight and the reason on hover.
// The glance strip's status dot escalates if the source stays unreachable.
export default function StaleBadge({ reason }) {
  if (!reason) return null;
  return (
    <span className="stale-badge" title={`Showing cached data — couldn't reach the source.\n${reason}`}>
      cached
    </span>
  );
}
