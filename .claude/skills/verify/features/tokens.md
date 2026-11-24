# Model usage (sheet `tokens`)

## Sub-features
- Range chips: 5h window, week window (both start at the live quota window's reset when the usage chips know it, else rolling), today, 30 days, 90 days, all.
- Group chips: by tab (session), model, provider (Claude, Antigravity, ChatGPT (Codex): the registry's labels), account (each outside provider's account), mode, project, day. The non-Claude rows come from the delegates log and are priced from the same live lists (app/model_prices.py).
- Lede: API-equivalent cost, tokens, sessions, prompt-caching savings, "N calls unpriced" when any are; a "By provider" line when anything besides Claude ran in the range; a line repricing every priced call on the newest Opus / Fable / Sonnet and each outside provider's first-listed model ("mix saves N%" / "mix costs N% more").
- Table: label with a second line (mode · project · worktree · models, or sessions · calls; by model, which price list it came from), output tokens, input tokens (fresh + cache read + cache write), cost (or "unpriced", never $0), a share bar.
- Prices: every model in the range with $/M in, out, cached, the list it came from and how long ago it was fetched; an unpriced model says why. A note names a list whose last fetch failed.

## How to get to it (user POV)
- `⌘K Model usage`, or Settings → Usage → Model usage → Open. No card.

## Driving it with Playwright
- `.tu-sheet`, `.chip-row .chip` (`.active`, role=radio), `p.usage-lede`, `.tu-big`, `table.tu-table`, `.tu-label`, `.tu-sub`, `.tu-share`, `.tu-good` / `.tu-bad`, `.tu-unpriced`, `table.tu-prices`, `.tu-from`.
- Nothing in range is unpriced on real data most days; to see the flag, `page.route` the summary and push a row with `price: null` and `unpriced > 0`.
- `GET /api/token-usage/summary?range=5h|week|today|7d|30d|90d|all&group=tab|model|provider|account|mode|project|day`. By account: each outside account is its own row ("Antigravity · Google (first sign-in)"); Claude is one row, "Claude (all accounts)", since transcripts don't say which login wrote them.
- Proof: the lede names the range ("in the current 5h window") and switching a group chip changes the first column's header and rows.

## Gotchas
- Read-only, except that a load past a day refetches the price lists (LiteLLM's is about 5MB) into `model_prices`. The first load after a backend start reads every transcript under `~/.claude/projects` (about 3.6GB, 5–8s); later loads re-read only files that grew.
- Backend changes need a worktree backend on :8799 (SKILL.md, Launch). Point it at a copy of app.db (`DASHBOARD_DATA_DIR`) when the branch adds a migration, so the real one isn't migrated before the merge.
- Figures include the verifier's own session, which is live while you drive it.
