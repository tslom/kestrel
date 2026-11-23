# Brain (card `brain`, sheet `brain`)

## Sub-features
- Card: confirm queue for the first page with pending facts (up to 8): ✓ / ✗ per fact, "✓ all N", page list linking into the vault.
- Sheet: page nav, ✓ all per page, "next:" page, per-fact edit, "Earlier" history.

## How to get to it (user POV)
- `⌘K Brain` opens the sheet; card is `.row-line[data-card="brain"]`; its "review every page" `.widget-footer-btn` opens the sheet.

## Driving it with Playwright
- Card: `.brain-queue`, `.brain-queue-head`, `ul.brain-facts`, `li.brain-fact` (`.pending` / `.active` / `.superseded`), `.brain-claim`, `.brain-core`, `.gen-actions button` (titles "That's right" / "Wrong…"), `.brain-err`, `ul.brain-pages`.
- Sheet: `.brain-sheet`, `nav.brain-nav button` (`.on`), `.brain-count`, `.brain-title`, `.brain-edit`, `.brain-sub`.
- `GET /api/brain`, `GET /api/brain/page/{slug}`.
- Proof: clicking a `nav.brain-nav button` changes `.brain-title`.

## Gotchas
- **`GET /api/brain` can start a background ingest** (`agent.ask`) unless yesterday already ran, one is in flight, or usage is blocked. A full catch-up is ~1.5M Sonnet tokens; just loading the desk can trigger it. Warn before anything resets `brain_runs`.
- Verdict, confirm and edit rewrite `07-Brain/*.md` in the vault. ✓ and edit have no undo. ✗ (`[aria-label="Forget this fact"]`) is held for the undo toast and sent when it expires; it leaves a tombstone so ingest never relearns the fact, and can revive a fact it had superseded.
- Only core facts reach the console prompt; a fact being visible here says nothing about the prompt.
