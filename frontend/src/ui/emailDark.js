// Dark reading for mail that never chose its colours. All-or-nothing: if every background is
// transparent or white the whole message flips; any own ground (wrapper, header, button, image)
// means it was designed on its colours and none does (white-on-blue would go blue-on-blue).
// Read from the frame's computed styles, not the markup (bgcolor=, inline, <style>, <font>).

function rgba(s) {
  const m = /^rgba?\(([^)]+)\)$/.exec(s || "");
  if (!m) return null;                     // color(srgb …) etc.: nothing we set
  const p = m[1].split(/[\s,/]+/).filter(Boolean).map(Number);
  return [p[0], p[1], p[2], p.length > 3 ? p[3] : 1];
}

// White and template off-whites (#fafafa, #fdfdfd); not #f4f4f4, a wrapper design framing a white card.
const whiteish = ([r, g, b]) =>
  Math.min(r, g, b) >= 245 && Math.max(r, g, b) - Math.min(r, g, b) <= 8;

const all = (doc) => [doc.documentElement, doc.body, ...doc.body.querySelectorAll("*")];

export function isPlain(doc) {
  if (!doc?.body) return false;
  const win = doc.defaultView;
  for (const el of all(doc)) {
    const cs = win.getComputedStyle(el);
    if (cs.backgroundImage !== "none") return false;
    const c = rgba(cs.backgroundColor);
    if (c && c[3] > 0.05 && !whiteish(c)) return false;
  }
  return true;
}

function hslToRgb(h, s, l) {
  const k = (n) => (n + h / 30) % 12;
  const f = (n) => l - s * Math.min(l, 1 - l) * Math.max(-1, Math.min(k(n) - 3, 9 - k(n), 1));
  return [f(0), f(8), f(4)];
}

const luminance = ([r, g, b]) => {
  const lin = (c) => (c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4);
  return 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b);
};

// Lightness turned over, hue and saturation kept, squeezed into 0.1–0.9 (no pure white). `readable`
// text gets a brightness floor: HSL lightness isn't brightness (#0000EE flipped is still too deep).
function flip([r, g, b, a], readable = false) {
  r /= 255; g /= 255; b /= 255;
  const max = Math.max(r, g, b), min = Math.min(r, g, b);
  const l = (max + min) / 2;
  let h = 0, s = 0;
  if (max !== min) {
    const d = max - min;
    s = l > 0.5 ? d / (2 - max - min) : d / (max + min);
    h = max === r ? (g - b) / d + (g < b ? 6 : 0) : max === g ? (b - r) / d + 2 : (r - g) / d + 4;
    h *= 60;
  }
  let l2 = 0.9 - 0.8 * l;
  if (readable && l <= 0.5)
    while (l2 < 0.9 && luminance(hslToRgb(h, s, l2)) < 0.3) l2 += 0.02;
  return `hsla(${h.toFixed(1)}, ${(s * 100).toFixed(1)}%, ${(l2 * 100).toFixed(1)}%, ${a})`;
}

const SIDES = ["top", "right", "bottom", "left"];

// Read every computed value before writing any: colour inherits, so a parent written first would be flipped back in its children.
export function darken(doc, ground) {
  const win = doc.defaultView;
  const plan = all(doc).map((el) => {
    const cs = win.getComputedStyle(el);
    return {
      el,
      color: rgba(cs.color),
      bg: rgba(cs.backgroundColor),
      borders: SIDES.map((s) => parseFloat(cs.getPropertyValue(`border-${s}-width`)) > 0
        ? rgba(cs.getPropertyValue(`border-${s}-color`)) : null),
    };
  });
  for (const { el, color, bg, borders } of plan) {
    const st = el.style;
    if (bg && bg[3] > 0) st.setProperty("background-color", "transparent", "important");
    if (color) st.setProperty("color", flip(color, true), "important");
    borders.forEach((c, i) => {
      if (c) st.setProperty(`border-${SIDES[i]}-color`, flip(c), "important");
    });
  }
  const root = doc.documentElement.style;
  root.setProperty("background", ground, "important");
  root.setProperty("color-scheme", "dark");
}
