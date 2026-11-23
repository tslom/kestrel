# Email (card `email`, sheets `email` and `review`)

## Sub-features
- Card: queue count and unread, preview rows, a "Review N" button.
- Sheet: local search with a label filter, a "Search Gmail too" fallback, Mail / Senders / Messages bands, an in-app reader.
- Review queue: Archive / Unsub / Keep per message, a label picker, ↵ applies the recommendation on the hovered card, undo, Gmail sync.

## How to get to it (user POV)
- `⌘K Email` opens the sheet; card is `.row-line[data-card="email"]`.
- `⌘K Email review queue` opens key `review` (tab reads "Review queue"). The card's `.widget-footer-btn` and the ask's "Review" open the same.

## Driving it with Playwright
- Card: `.email-count`, `.email-peek`, `.email-from`, `.email-subject`.
- Search: `.mail-search input`, `select[aria-label="Only this label"]`. `/api/email/labels`, `/api/email/search?q=&label=&gmail=`.
- Rows: `button.email-open` (opens the reader dialog), `.mail-label`, `.email-gmail`.
- Reader: `.email-reader` (role=dialog, aria-label = subject), `.seg` "Formatted" / "Text", `.email-reader-btn`, `.email-body`. `/api/email/{id}/body?html=true`.
- Review: `.email-card`, `.email-act` (`.recommended` on the suggested one), `.email-rec`, `.email-label`, `.email-labels .chip`, `.email-undo`, `.email-done`; sync button has title "Sync Gmail + summarize new mail".
- Proof: search returns `.email-open` rows; opening one shows `.email-reader` with `.email-body`.

## Gotchas
- **Archive, Unsub and Keep act on the real Gmail account.** Unsubscribe fires when the undo toast expires and can't be taken back. Read-only verification: search and the reader only.
- ↵ while hovering an `.email-card` triggers its recommended action. Don't press Enter with the mouse over the queue.
- Sync calls Gmail plus Haiku (model usage), ~1 min. "Search Gmail too" is a live Gmail call.
