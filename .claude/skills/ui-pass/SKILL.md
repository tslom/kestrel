---
name: ui-pass
description: Kestrel's layer on the global ui-review skill. Says how to capture a Kestrel
  view (dark mode, the /verify harness), what's off-limits, and how the capped reviewer
  loop runs here. Use with ui-review whenever judging or changing how a Kestrel sheet,
  card, console or Look lab variant looks. The reviewer loop itself runs only when
  the user asks for a UI pass or polish pass, because it spends model usage.
---

# UI pass (Kestrel)

The method is in the global `ui-review` skill. This file only adds what's specific to Kestrel.

**Mode: Operate.** The user is in this for hours a day, so restraint beats expression.

## Capture

- Use the `/verify` harness: Launch, Doctor, and Drive through the real palette path.
- **Dark mode only**, set the way `/verify` shows (`colorScheme: "dark"` and `theme=dark` in localStorage, both). Light variants get no polish until the app ships to other people.
- Shoot at 1440px and at a narrow width: under 1200px, where the side column closes. The other breakpoints are in verify's `features/map.md`.
- Use real data and full sheets. An empty sheet means the journey failed; check `/api/status` before blaming the change.
- Pixel and canvas work also gets a WebKit capture, because the .app is WKWebView.
- Save the first capture as `before/`.

## Kestrel's own tells

Each has come up in a review here, on top of the list in `ui-review`.

- A label, badge or chip added to explain something the layout already shows. Cut it. Clutter is the standing complaint, so remove before you add.
- A card that restates a number another card already shows. One owner per number.

## Off-limits unless the user asks

- the palette
- the map
- the rooms layout
- the type families

A pass that restyles the product has gone wrong.

## The loop, when asked

Say which view(s) it covers and that it draws on the shared usage window, then run the review loop in `ui-review`. Never run it unprompted or across the whole app. References are screens from apps the user likes that do the same job; ask if none are on hand. A Look lab variant can't be its own reference.

## Finish

Make the annotated before/after (see `ui-review`), then follow CLAUDE.md's Git bar: a change was rendered, not just built.
