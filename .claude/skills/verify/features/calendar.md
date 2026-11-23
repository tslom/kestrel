# Calendar (card `calendar`, sheet `calendar`) and Office hours (card `courses`)

## Sub-features
- Card: today / tomorrow toggle, past events folded as "N earlier", a leave-by ask, a link out to Google Calendar.
- Sheet: range picker, "Next up" countdowns, "Load" bars of booked hours, "Agenda" by day with "Xh free" gaps and all-day banners.
- Office hours card: today or all classes, approve / dismiss on proposed changes.

## How to get to it (user POV)
- `⌘K Calendar` opens the sheet (defaults to this week). The side-column card is `.row-line[data-card="calendar"]`.
- `⌘K Office hours` only jumps to the card (`.row-line[data-card="courses"]`); it has no sheet.

## Driving it with Playwright
- Card: `.seg button` ("today" / "tomorrow", `.on` marks the active one), `.event-zone`, `.cal-link`, `.event-list`, `.event-summary`. Calls `/api/calendar?range=today|next-day` and `/api/leave`.
- Sheet: `.cal-next`, `.cal-next-when`, `section.slice-day`, `.cal-booked`, `.cal-banners`, `.cal-gap`, `.cal-gap-label`. Range tabs are `.range-tab` (aria-pressed), labelled "this week" etc.
- Office hours: `.chip` "all classes" / "today", `.oh-proposal` with `.chip` "approve" / "dismiss", `.course-open`. `/api/courses`.
- Proof: `.slice-day` sections render, or the text "Nothing scheduled in this range.".

## Gotchas
- Data is live (school Google account). The OAuth refresh token expires ~7 days (External/Testing consent): a `.widget-error` may be that, not your change.
- Only today−7 … today+35 is kept synced; a colder range syncs from Google on first read (slow, and it writes the cache).
- Calendar has no metrics; use health or weather to exercise metric chips.
- "approve" on an office-hours proposal writes into the course note in the vault. Don't click it to verify.
- The `day:YYYY-MM-DD` sheet is Health's, not Calendar's (see health.md).
