# Sources & status (sheet `status`)

## Sub-features
- Per source: state, detail, age; expand for origin, auth, notes, error, days behind, event history.
- connect / reconnect (OAuth or Plaid), an "app" infra list, a faults log with clear, re-check.

## How to get to it (user POV)
- `⌘K Sources & status` opens the sheet (its tab reads "Sources"). Settings → Connections → "Open" gets there too.

## Driving it with Playwright
- `ul.status-list`, `li.status-row` (`.ok` / `.stale` / `.broken` / `.off`), `button.status-row-head` (aria-expanded), `.status-name`, `.status-detail`, `.status-state`, `button.status-action`, `.status-more`, `.status-error`, `li.status-event`, `h3.status-group`, `.status-foot`.
- `GET /api/status`, `GET /api/failures`.
- Proof: every source has a `li.status-row`; expanding one shows `.status-more`.
- Backend-only check: `curl 'http://localhost:8788/api/providers'` for registry metadata, `curl 'http://localhost:8788/api/{source}?range=...&metric=...'` for slices. Errors are 400 `{"detail": "..."}`, rendered verbatim.

## Gotchas
- "connect" opens a real consent tab via the backend and polls for 5 min.
- "clear" deletes the fault log (derived, but gone). It is held for the undo toast: the faults section hides at once, and the DELETE is sent when the toast expires.
- The faults heading is absent when there are none.
- A stale aw-server row after a reboot is real (aw is the upstream; gaps are unrecoverable). Location comes from a Cloudflare relay, not Tailscale.
