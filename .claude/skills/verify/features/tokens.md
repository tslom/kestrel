# Model usage (sheet `tokens`)

## Sub-features
- Range chips: 5h window, week window (both start at the live quota window's reset when the usage chips know it, else rolling), today, 30 days, 90 days, all.
- Group chips: by tab (session), model, provider (Claude, Gemini, ChatGPT (Codex)), mode, project, day. Gemini and Codex rows come from the delegates log and are tokens only, unpriced.
- Lede: API-equivalent cost, tokens, sessions, prompt-caching savings; a "By provider" line when anything besides Claude ran in the range; a line repricing Claude's tokens on Opus 5.5 / Fable 5.2 / Sonnet 5.5 ("mix saves N%" / "mix costs N% more").
- Table: label with a second line (mode · project · worktree · models, or sessions · calls), output tokens, input tokens (fresh + cache read + cache write), cost, a share bar.

## How to get to it (user POV)
- `⌘K Model usage`, or Settings → Usage → Model usage → Open. No card.

## Driving it with Playwright
- `.tu-sheet`, `.chip-row .chip` (`.active`, role=radio), `p.usage-lede`, `.tu-big`, `table.tu-table`, `.tu-label`, `.tu-sub`, `.tu-share`, `.tu-good` / `.tu-bad`.
- `GET /api/token-usage/summary?range=5h|week|today|7d|30d|90d|all&group=tab|model|provider|mode|project|day`.
- Proof: the lede names the range ("in the current 5h window") and switching a group chip changes the first column's header and rows.

## Gotchas
- Read-only. The first load after a backend start reads every transcript under `~/.claude/projects` (about 3.6GB, 5–8s); later loads re-read only files that grew.
- Backend changes need a worktree backend on :8799 (SKILL.md, Launch). Point it at a copy of app.db (`DASHBOARD_DATA_DIR`) when the branch adds a migration, so the real one isn't migrated before the merge.
- Figures include the verifier's own session, which is live while you drive it.
