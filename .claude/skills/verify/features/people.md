# People / CRM (card `people`, sheet `people`)

## Sub-features
- Card: birthdays, a "waiting on reply" count, preview rows, a dismiss tick.
- Sheet: overview / conversations views, name search, birthdays, watch-due, waiting rows with Pin / Draft / Remind / Dismiss / Mute.
- Dismissed and muted lists with undo; person detail (history, edit form, open in Messages); group conversations have their own detail.

## How to get to it (user POV)
- `⌘K People` opens the sheet; card is `.row-line[data-card="people"]`.

## Driving it with Playwright
- Card: `.people-count`, `.people-peek`, `.people-name`, `.people-days`, `.people-dismiss`.
- Sheet: `.chip-row .chip` ("overview" / "conversations"), `input.people-search`, `.people-row`, `.people-section`.
- Waiting-row actions are `.people-mute` buttons told apart by aria-label: "Pin to console", "Draft a reply", "Remind me today", "Dismiss this thread", "Mute this person", "Bring this thread back", "Unmute". Drafts land as `.people-draft` / `.people-draft-note`.
- Detail: `.people-detail`, `.people-back`, `.people-detail-name`, `.people-open-messages`, `.people-history`, `.people-edit`, `.form-save-btn`.
- Reads: `/api/people/waiting`, `/api/people/signals`, `/api/people/conversations`, `/api/people/search`, `/api/people/{key}`.
- Proof: typing in `input.people-search` filters `.people-row`; clicking one shows `.people-detail-name`.

## Gotchas
- Remind creates a real task; Mute, Dismiss and the edit form write app.db; the card's dismiss is optimistic.
- Draft is a model call (seconds, usage). "Open in Messages" launches Messages.app.
- `unanswered` collapses per person on purpose; group chats and shortcodes are excluded, so a missing thread isn't a bug by default.
- chat.db needs Full Disk Access for the process reading it; a stale grant empties the CRM.
