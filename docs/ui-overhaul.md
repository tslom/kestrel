# UI overhaul — research, audit, plan (round 2 of the look lab)

2026-11-13/14. Brief: make Kestrel look production-ready, "not kitschy",
functional without being overloaded, modeled on teams that do product UX for
a living. This is round 2 of the look-lab loop (IDEAS.md → "The redesign is a
look-lab loop"): round 1's vote (Cupertino over Studio, twice: "likes the
colors from the Cupertino") picked the palette direction, and this round turns
it into a token system the whole app is drawn from. The before is kept as the
`flexoki` look, one ⌥⇧L away.

**Where things live:** tokens are the `:root` block at the top of
`frontend/src/styles.css`; looks are `frontend/src/looks/`.

---

## 1. What the references say (concrete rules, with sources)

Pages were read in full on 11-13 (JS-rendered ones through headless Chrome,
Apple's HIG through its JSON data). Where a rule below has a number, the
number is from the source.

### Type

| Source | Rule |
|---|---|
| Apple HIG, Typography (macOS table) — [link](https://developer.apple.com/design/human-interface-guidelines/typography) | macOS text styles: Large Title 26/32, Title 1 22/26, Title 2 17/22, Title 3 15/20, Headline 13/16 **bold**, Body 13/16, Callout 12/15, Subheadline 11/14, Footnote and Caption 10/13. 10pt is the smallest style. |
| Vercel Geist, Typography — [link](https://vercel.com/geist/typography) | Two families of styles: **Label** (single line, "ample line-height for … icons"; Label 14 is the "most common text style of all", Label 13 "secondary line", Label 12 with caps "for tertiary level text in busy views") and **Copy** (multi-line; Copy 14 "most commonly used", Copy 13 "where space is a premium"). Mono only pairs with labels, at 12–14. Tabular figures "when conveying numbers". |
| IBM Carbon, Type sets — [link](https://carbondesignsystem.com/elements/typography/type-sets/) | Product UI uses the **productive** set: body-compact-01 is 14/18, for "short paragraphs with no more than four lines"; fixed (not fluid) headings because product pages are dense. |
| Atlassian, Typography — [link](https://atlassian.design/foundations/typography) | `font.body` 14/20 is "the default size in components"; body.small 12/16 "should be used sparingly"; Medium weight in most components, Bold "sparingly". |
| Linear, How we redesigned the Linear UI (part II) — [link](https://linear.app/now/how-we-redesigned-the-linear-ui) | Inter Display for headings, Inter for body: hierarchy from one family, not from mixing faces. |

**Rule adopted:** the macOS scale, exactly: 10, 11, 12, 13, 15, 17, 22, 26
(+32 for the one display number). Nothing between steps. One UI face (the
system sans) and one code face (JetBrains Mono), mono only for code, paths and
kbd. Numbers get tabular figures instead of a mono face.

### Spacing

| Source | Rule |
|---|---|
| Carbon, Spacing — [link](https://carbondesignsystem.com/elements/spacing/overview/) | 2, 4, 8, 12, 16, 24, 32, 40, 48, 64, 80, 96, 160; "deviating from the scale should be avoided whenever possible"; "elements that have more spacing around them tend to be perceived as higher in importance". |
| Atlassian, Spacing — [link](https://atlassian.design/foundations/spacing) | 8px base with 2, 4, 6, 8, 12, 16, 20, 24, 32… Small 0–8 for icon gaps and input padding, medium 12–24 for buttons and card insides, large 32–80 for layout. |
| Radix Themes, Spacing — [link](https://www.radix-ui.com/themes/docs/theme/spacing) | 4, 8, 12, 16, 24, 32, 40, 48, 64. |

**Rule adopted:** 4px grid with 2 and 6 as half-steps for tight controls;
named `--sp-1…6` (4–32) for layout gaps.

### Colour, contrast, dark mode

| Source | Rule |
|---|---|
| Linear (above) | Theme from **three inputs** (base, accent, contrast) instead of 98 hand-set variables, generated in **LCH** because it is perceptually uniform. They deliberately limited how much accent "chrome" goes into neutrals for "a more neutral and timeless appearance". |
| Linear, A calmer interface for a product in motion (2026-03) — [link](https://linear.app/now/behind-the-latest-design-refresh) | "Don't compete for attention you haven't earned": sidebar dimmed, coloured icon backgrounds removed, icons smaller. "Structure should be felt not seen": separators softened. Greys moved off blue toward neutral, warning that too warm reads "muddy". They A/B'd the refresh against the old UI behind a flag — the look lab is the same idea. |
| Stripe, Designing accessible color systems — [link](https://stripe.com/blog/accessible-color-systems) | HSL's lightness is "flawed" (yellow and blue at the same L don't look equally light); they built the palette in a perceptual space so contrast is predictable per step. |
| Radix Colors, Understanding the scale — [link](https://www.radix-ui.com/colors/docs/palette-composition/understanding-the-scale) | 12 steps by job: 1–2 backgrounds, 3–5 component fills (rest/hover/pressed), 6–8 borders (6 non-interactive, 7 interactive, 8 strong + focus), 9–10 solid, 11–12 text. |
| Geist, Colors — [link](https://vercel.com/geist/colors) | Two page backgrounds ("use Background 2 sparingly"); colours 4–6 are default/hover/active borders; "draw each border once"; shadows translucent, never opaque grey. |
| Primer, Color usage — [link](https://primer.style/product/getting-started/foundations/color-usage/) | Functional roles (fg default/muted, bg default/muted, border); semantic colour is for "status, action, or emphasis". |
| Apple HIG, Dark Mode — [link](https://developer.apple.com/design/human-interface-guidelines/dark-mode) | Dimmer backgrounds, brighter foregrounds, "not necessarily inversions"; contrast no lower than 4.5:1, "strive for 7:1, especially in small text". |
| WCAG 2.2 — [1.4.3](https://www.w3.org/WAI/WCAG22/Understanding/contrast-minimum.html), [1.4.11](https://www.w3.org/WAI/WCAG22/Understanding/non-text-contrast.html) | Text 4.5:1 (large 3:1); UI component boundaries and required graphics 3:1. |
| NN/g, Dashboards: preattentive — [link](https://www.nngroup.com/articles/dashboards-preattentive/) | Colour is for categories, not magnitude, and best as a *secondary* cue paired with shape or position. |

**Rule adopted:** neutrals are one OKLCH hue (286, macOS's faint cool) at
chroma ≤ 0.008, stepped by lightness; one accent (blue, from the vote) at the
same OKLCH lightness logic; status green/red only with words. Every pairing
was computed (`oklch → sRGB`, WCAG ratio):

| Pair | Light | Dark |
|---|---|---|
| text on surface | 17.5 | 15.4 |
| secondary (`--paper-mid`) on desk | 5.5 | 7.7 |
| tertiary (`--paper-faint`) on surface | 3.9 | 4.1 |
| accent on surface | 4.95 | 5.6 |
| console text / secondary | 14.6 / 7.0 | — / 7.8 |
| good / bad | 5.2 / 5.9 | 8.2 / 5.4 |
| data (`--data`) on surface | 5.2 | 6.5 |
| input boundary (`--paper-line-3`) | 3.4 | 3.5 |

`--paper-faint` misses 4.5:1 on purpose and is reserved for incidental text
(timestamps, "later", placeholders), which 1.4.3 exempts. White on the
dark-mode blue is only 3.1:1, so accent fills take `--on-accent` (dark text,
6.4:1) there. Tokens are written as hex with the OKLCH source in a comment
because the map's canvas reads them with `hexToRgb`.

### Elevation: borders vs shadows

| Source | Rule |
|---|---|
| Atlassian, Elevation — [link](https://atlassian.design/foundations/elevation) | Four levels (sunken, default, raised, overlay). Default cards: "pair with a border". Raised is "not to group content when a border or white space would suffice". Overlay (menus, modals) always gets its shadow. Dark: higher = lighter surface. |
| Material 3, Applying elevation — [link](https://m3.material.io/styles/elevation/applying-elevation) | "Deliberately limited to just a handful of levels"; by default surfaces separate by **tonal difference**, shadow and scrim are the alternatives. |

**Rule adopted:** surfaces at rest sit on a hairline (`--paper-line`) and a
tone step; only floating things (⌘K, menus, toasts, a dragged pin) get
`--shadow-pop`. Eleven ad-hoc popover shadows collapsed into that one token.

### Motion

| Source | Rule |
|---|---|
| NN/g, Animation duration — [link](https://www.nngroup.com/articles/animation-duration/) | Most animations 100–500ms; simple feedback ~100ms; big screen changes 200–300; "400ms … only for big movements"; entering slightly longer than exiting; ease-out most often right. |
| Carbon, Motion — [link](https://carbondesignsystem.com/elements/motion/overview/) | fast-01 70, fast-02 110, moderate-01 150, moderate-02 240, slow-01 400; productive standard curve `cubic-bezier(0.2, 0, 0.38, 0.9)`; micro-interactions 90–120ms ease-out. |
| Apple HIG, Motion — [link](https://developer.apple.com/design/human-interface-guidelines/motion) | "Don't add motion for the sake of adding motion"; "generally avoid adding motion to UI interactions that occur frequently"; make motion optional. |

**Rule adopted:** `--dur-1/2/3` = 100/160/240ms on the Carbon productive
curve. Looping animation only while something is live (a turn running, the
mic open); the unread ping now runs three times, not forever.
`prefers-reduced-motion` already zeroes transitions.

### Density, hierarchy, disclosure

| Source | Rule |
|---|---|
| NN/g, Progressive disclosure — [link](https://www.nngroup.com/articles/progressive-disclosure/) | Show "a few of the most important options" first, the rest on request; more than two levels of disclosure "typically have low usability". |
| NN/g, Minimize cognitive load — [link](https://www.nngroup.com/articles/minimize-cognitive-load/) | "Avoid visual clutter: redundant links, irrelevant images, and meaningless typography flourishes slow users down." |
| NN/g, Aesthetic and minimalist design — [link](https://www.nngroup.com/articles/aesthetic-minimalist-design/) | Minimalist means no "irrelevant or rarely needed" information, not the visual trend; "having too few elements would inhibit utility"; "clarity will always win over visual flourish". |
| NN/g, Dashboards (above) | A dashboard is read "with a minimum of interaction or cognitive processing". |
| Raycast API, List — [link](https://developers.raycast.com/api-reference/user-interface/list) | When an item has a detail pane, drop its row accessories and put that info in the detail: the row stays one line. |

Things and Raycast don't publish design systems, so they inform this only as
observed patterns (one accent, plain sentence-case labels, a row that holds
one line, a detail pane for the rest), not as cited rules.

**Rule adopted:** Kestrel already practises this ("rest shows what bears on
you, reach shows the rest", DESIGN.md → Layout). The overhaul applies the
same rule to emphasis: **only one thing per region wears the accent**, and
only when it wants you.

### Focus, targets, keyboard

WCAG 2.2 [2.5.8 Target size (minimum)](https://www.w3.org/WAI/WCAG22/Understanding/target-size-minimum.html):
24×24 CSS px or spacing equivalent. [2.4.13 Focus appearance](https://www.w3.org/WAI/WCAG22/Understanding/focus-appearance.html)
(AAA, used as a target): a 2px perimeter at 3:1 against the unfocused state.
Kestrel already draws a 2px accent `:focus-visible` ring globally; the
action buttons now have a 24px minimum height.

---

## 2. Audit of the app as it was (11-13)

Counted in `styles.css`:

| What | Count | Why it reads kitschy or loud |
|---|---|---|
| distinct font sizes | **20**, six of them half-pixels (9.5, 10.5, 11.5, 12.5, 13.5, 14.5) | No scale, so nothing reads as a level. Geist/Apple/Carbon each use ~8. |
| distinct radii | 13 (2, 3, 4, 5, 6px, var, pill, 50%…) | Corners disagree between neighbours. |
| uppercase tracked labels | 46 rules, **9 different letter-spacings** (0.03–0.3em) | Wide-tracked caps in a mono face read as a terminal costume. |
| mono as the UI face | 147 rules | Every number, chip, header and the whole console in JetBrains Mono: the strongest "hacker dashboard" signal. |
| distinct padding values | 124 (odd px: 3, 5, 7, 9, 11, 13 used 160+ times) | No rhythm; neighbouring rows sat 1–2px off each other. |
| paper grain | noise SVG on the desk | Decoration standing in for structure. |
| Needs you column | 8 identical orange-outlined buttons | When every row's action wears the accent, none stands out (the dilution Linear's "don't compete for attention you haven't earned" is about). |
| map | orange contour tints, blue "has" rings, orange "needs" rings | Two hues competing on the most decorative surface. |
| overdue | red dot **and** red reason line per row | Four red lines read as an alarm for routine overdue tasks. |
| ad-hoc shadows | 11 popover shadow recipes | Floating surfaces each with their own depth. |
| hard-coded Flexoki hex | 38, several in the console | Bypassed the tokens, so a palette change couldn't reach them. |

What was already right and stays: one window that never scrolls; the
three-depth card model (row → pane → full view); hover-reveal for tools you
reach for; a global focus ring; reduced-motion handling; skeleton/empty/error
states drawn for every card (DESIGN.md → Card anatomy).

---

## 3. Plan, prioritized

### P0 — token foundation (done, this round)

- [x] Neutral OKLCH palette, light and dark, plus the console's ink set;
      accent, data, status, `--on-accent`, a 3:1 input-boundary line.
- [x] Type scale `--text-2xs…3xl` (macOS sizes); every `font-size` and
      `font:` shorthand rewritten onto it (502 declarations). One stray 7px
      glyph is left as-is.
- [x] Radius `--r0/1/2/pill` (3/6/10/999); 84 declarations mapped.
- [x] Spacing snapped to the 4px grid with 2/6 half-steps (308 declarations).
- [x] One caps tracking `--caps-track` (41 rules).
- [x] Faces: sans for UI, `--code` for code, kbd, paths, shell, diffs
      (25 selectors kept mono); tabular figures on `html`.
- [x] `--shadow-1` (hairline) / `--shadow-pop` (floating); stray shadows and
      Flexoki hex routed through tokens; `--dur-*`, `--ease`.
- [x] Grain off.

### P1 — component cleanup (done, this round)

- [x] Needs you: the primary action is neutral at rest and takes the accent
      on the row you're on (hover, keyboard cursor, focus). 24px minimum
      target.
- [x] Today list: row labels in sentence case, not caps.
- [x] Overdue: red dot keeps "late", the reason line is grey (its words say
      "N d overdue"); broken sources stay red.
- [x] Map: "has" nodes in ink, so the accent is the only hue; contour tint
      roughly halved.
- [x] Unread ping: three pulses, not an endless loop.

### P2 — layout and hierarchy (needs the user; not done)

These would change how the app is used, so they are proposals:

1. **The map's footprint.** It takes the middle third of a 1512px window and
   is the most decorative surface. Options: (a) leave it; (b) narrow it to a
   fixed ~360px column so the console gets the width; (c) fold it behind the
   KEY button and give the Needs you column the space. Recommendation: (b),
   tested as a round-3 look.
2. **Console header: two rows of chrome** (status, usage, view, +, expand;
   then model, mode, docs, resume, new, ⋯). Merge into one row and fold
   docs/resume/new into ⋯. Recommendation: do it, also as a look first.
3. **Glance strip.** Date, weather, next event and the fault pill on one
   line plus the brief under it. Fine as is; the fault pill is the one red
   thing and should stay.
4. **Caps labels elsewhere** (section heads like "Next up", "Agenda" on
   sheets). Kept as caps for now per Geist's tertiary-label rule; sentence
   case needs JSX text changes, since several labels are lowercase in source.

### P3 — follow-ups

- Sheets (Calendar, Money…) inherit the tokens but were drawn for the old
  look; a `/ui-pass` per sheet would catch local leftovers.
- Diff and mode-chip colours in the console are still hand-picked hex
  (categorical, on ink); move to tokens if a second console theme appears.

---

## 4. Decisions for the user

| Decision | Default now | How to choose |
|---|---|---|
| Accent colour | **blue** until voted | Round 3: blue, amber, green, violet, rose looks (`looks/accents.css`); pin any two, flip with ⌥⇧L, vote. |
| New vs old overall | new (`baseline`) | Pin `baseline` and `flexoki (before)`. |
| Density | one density: 13px body, 4px-grid spacing | 11-14: no preference, left as is. |
| Map footprint | even split until voted | Round 3 look `map-narrow`: a fixed 360px column (option b). |
| Console header | **one row** (done 11-14, in the baseline) | The bar lives in the status bar; docs/resume/new and the set-and-leave controls are in ⋯. |

## 5. Verification

- `npm run build` passes (no type-checker in this JS project; the build is
  the static check).
- Rendered in headless Chrome at 1512×920, light and dark: home, an open
  card, Calendar, Money, ⌘K (screenshots not checked in: live data).
- The flexoki look renders the old palette on the new scales (see its file
  header for what it does and doesn't restore).
- Backend suite: 2253 passed (no backend change; run per CLAUDE.md).
- Not checked: WKWebView itself (the .app). Headless Chrome stands in; the
  tokens use nothing WebKit lacks (hex colours, `color-mix`, system fonts).

### Before / after

The before/after screenshots were taken against live personal data, so they
are not checked in. Re-take them against a demo vault to publish any.

### Seen along the way, not styling

The Calendar sheet's Load chart shows 169–176h booked on single days
(Nov 12–15) and 696h for the week: something multi-day is being counted per
day. Left alone here; it's a data bug for its own fix.
