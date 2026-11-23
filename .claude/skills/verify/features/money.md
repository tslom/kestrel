# Money (card `finance`, sheet `finance`), Debts (card `debts`), Expenses (card `expenses`)

## Sub-features
- Finance card: balances and this month's spend; Plaid setup, connect, reconnect.
- Money sheet bands: Worth (net-worth chart + accounts), Flow (in / out; daily spend bars up to 31 days, weekly cashflow beyond), Due soon, Where it went (rank bars), Transactions by day with "+ Expense" claim buttons.
- Debts card: add ("Rowan 24 pizza", "Theo beer"), settle, delete, nudge.
- Expenses card: edit or drop claims, export .xlsx, Show in Finder, mark everything reviewed, paid / reopen per batch, add cash or Venmo items.

## How to get to it (user POV)
- `⌘K Finance` opens the sheet, whose tab reads "Money" (default range this month).
- `⌘K Debts` and `⌘K Expenses` jump to `.row-line[data-card="debts"]` / `.row-line[data-card="expenses"]` (no sheets). The Finance card is `.row-line[data-card="finance"]`.

## Driving it with Playwright
- Sheet: `svg.fin-chart-svg` (aria-labels "Net worth over time", "Daily spending with average line", "Weekly net cashflow (surplus green, deficit red)"), `.fin-hero-lg`, `.money-flow`, `.money-in`, `.money-out`, `.money-accounts`, `.rank-bars`, `li[data-txn]`, `button.txn-claim` (`.on`, `.sent`).
- Finance card: `.fin-accounts`, `form.fin-setup`, `.fin-reconnect`.
- Debts: `.debt-amount`, `.debt-nudge`, `.beer-head`. Expenses: `.expense-row`, `.expense-open`, `.check-drop`, `.expense-export`, `.expense-reveal`, `.expense-batches`, `.expense-batch`.
- `/api/finance?range=…&metric=balances|networth|cashflow|spend|transactions|bills_due`, `/api/expenses/marks?range=`, `/api/plaid/status`.
- Proof: Worth, Flow and Where it went `section.band` titles render and the Transactions `.band-note` shows a count.

## Gotchas
- Reads come from the SQLite cache; the sheet makes no Plaid calls. The data is live and personal: keep screenshots out of anything shared.
- Without Plaid: "Plaid not set up." on the card, "No accounts connected." in the sheet. "sandbox" in the subtitle means the sandbox env.
- Every claim, debt and expense button writes app.db. A debt's × is held for the undo toast (sent when it expires); a beer's × drops one at once. Export writes an .xlsx to ~/Downloads; reveal opens Finder.
- "Due soon" renders only when bills are due.
