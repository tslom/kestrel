# Location (card `location`, sheet `location`) and Location map (sheet `map`)

## Sub-features
- Card: today's lede, "Still home · X", a coverage line with a stale-fix warning.
- Location sheet bands: Distance (heroes + bars), Away (span bars), Distance this year (heatmap), Routine (series rows).
- Location map: canvas map on OSM tiles (pan / zoom / fit / fullscreen), places list, "This place" detail (click a row or a disc), "name this place" with candidates and radius, per-visit labels.

## How to get to it (user POV)
- `⌘K Location` → sheet `location`; card is `.row-line[data-card="location"]`.
- `⌘K Location map` → sheet `map` (not reachable from the card).

## Driving it with Playwright
- Map: `.map-frame`, `canvas.map-canvas`, `.map-overlay`, `.map-controls` (buttons titled "Zoom in", "Zoom out", "Fit to the range", "Full screen"), `.map-credit`.
- Places: `ul.map-list`, `.map-list-rank`, `.map-list-dwell`, `.map-list-where`, `.map-list-visits`, `.map-place`, `.place-namer`, `.place-cands`, `.place-name-input`, `.place-radius`, `.visit-list`, `ul.visits`.
- Card: `.loc-line`, `.loc-coverage`, `.loc-stale`.
- `/api/location?range=&metric=km_traveled|time_at_home_min|places|…` (default this week), `/api/location/trace?range=` (map default this month), `/api/tiles/{z}/{x}/{y}.png`, `/api/location/summary`.
- Proof: the map's `.band-note` reads "N days · M fixes" and `ul.map-list` has rows.

## Gotchas
- The map is a DPR-scaled 2D canvas: no DOM nodes for discs or paths, so judge it by screenshot, and in WebKit too (the .app is WKWebView).
- Tiles come from tile.openstreetmap.org on a miss, are cached in app.db `tile_cache`, and load async; offline is a blank paper background.
- Fullscreen fails silently headless / in WebKit. Canvas clicks pick a disc only within 18px.
- OwnTracks reports only movement, relayed through a Cloudflare Worker: "No fixes in this range…" at home is normal.
- Naming a place or labelling a visit writes app.db.
