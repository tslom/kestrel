// The card kept its numbers but couldn't refresh them — the post-wake case,
// where the lid opens, the app launches before DNS is back, and every provider
// read falls through to cache.
//
// Deliberately not a WidgetError: nothing is broken, nothing is being asked of
// you, and the numbers on the card are real. So it sits at the weight of the
// range label beside it, not at the weight of a failure — the reason only comes
// out on hover. The glance strip's status dot is what escalates if the source
// stays unreachable; this just stops you trusting a stale number as current.
export default function StaleBadge({ reason }) {
  if (!reason) return null;
  return (
    <span className="stale-badge" title={`Showing cached data — couldn't reach the source.\n${reason}`}>
      cached
    </span>
  );
}
