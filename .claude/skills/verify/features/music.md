# Music (card `music`, sheet `spotify`)

## Sub-features
- Card: today's lede (Listened, Tracks) and the most-repeated track.
- Sheet bands: Listening (Plays, Artists, New artists, Longest sitting + daily minute bars), When (24-hour bars), Artists, Tracks, On repeat, Plays (browser by day), Genres (Last.fm).

## How to get to it (user POV)
- `⌘K Music` opens sheet key `spotify` (tab "Music"). Card is `.row-line[data-card="music"]`.

## Driving it with Playwright
- Card: `.music-line`, `.music-track`, `.music-artist`, `.music-count`.
- Sheet: `.hero-row`, `.hero-value`, `.hero-label`, `svg.chart-svg` (aria-label "bar chart, N bars, max …"), `.bar-seg.seg-music`, `.slice-day`, `.day-note`, `.range-tab`.
- `/api/spotify/summary` (card), `/api/spotify?range=…&metric=plays|listen_min|novelty|top_repeat|shape` (default this week), `/api/lastfm?metric=genres|coverage`.
- Proof: the Listening band's Plays `.hero-value` is a number; Artists and Tracks bands render.

## Gotchas
- Live Spotify. Not connected → the card says to run `python -m app.providers.spotify authorize`.
- Genres renders nothing until Last.fm answers; its failure shows only inside that band.
- "first heard this week" under New artists ignores the picked range (known label quirk).
- Re-importing the Extended history export is idempotent (memory: spotify export re-import).
