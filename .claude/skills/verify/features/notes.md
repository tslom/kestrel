# Vault notes (sheet `notes`), note tabs (`note:<path>`), Generated inbox (card `generated`)

## Sub-features
- Notes sheet: search (2+ chars, 150ms debounce), Pinned (Obsidian bookmarks), "Written by the console" (06-Generated).
- Note tab: rendered body, Links to / Linked from chips (each opens a tab), unresolved links, Pin to console, Open in Obsidian.
- Generated inbox card: reviewed / adopt per note.

## How to get to it (user POV)
- `⌘K Vault notes`. Typing 2+ characters in ⌘K also lists notes under a "Notes" group; Enter opens the note as a tab titled by its filename.
- `⌘K Generated inbox` jumps to `.row-line[data-card="generated"]` (no sheet).

## Driving it with Playwright
- Sheet: `.vault-notes`, `input.note-search`, `h3.note-group`, `button.note-row` (title = vault path), `.note-title`, `.note-folder`, `.note-snippet`.
- Note tab: `article.note-tab`, `header.note-tab-head`, `.note-path`, `.note-tab-acts .pbtn`, `.note-excerpt`, `button.note-chip`, `.note-unresolved`.
- Generated: `ul.gen-list`, `.gen-title`, `.gen-actions button`.
- `GET /api/vault/search`, `/api/vault/pinned`, `/api/vault/recent?folder=06-Generated`, `/api/vault/note?path=`, `GET /api/generated`.
- Proof: search, click a `button.note-row`, see `article.note-tab` with `.note-excerpt`.

## Gotchas
- "Open in Obsidian" launches the desktop app.
- **Adopt rewrites frontmatter and can move the file out of 06-Generated**, then rebuilds index.db: a real vault write with no undo. Don't use it to verify.
- Search reads index.db (derived); a missing note may be a stale index, not a UI bug.
