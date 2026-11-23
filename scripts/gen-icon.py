"""Generate the Kestrel app icon -> scripts/dashboard.icns.

Design: Malar. A kestrel's face cut down to its two tells — the eye with its
bare yellow ring, and the black tear stripe (the malar stripe) that falls from
it — on a rufous squircle. A kestrel is the bird that hangs still over a field
and watches it, which is what the dashboard does over the vault; and its back
is already Flexoki orange, so the tile needs no colour the UI doesn't have.

Flat Flexoki fills, no gradient and no glow, because the desk doesn't have any
— the mark has to look like it came off the same page as the UI. It's drawn to
survive 32px in the Dock and 16px in a browser tab: at that size you're
reading an orange tile with a dark eye, which is enough.

Geometry is the same 100-unit box that public/favicon.svg and ui/Mark.jsx
draw in; keep the three in sync by hand. The stripe's cubics are sampled into
a polygon here because PIL has no Bézier primitive.

Rendered at 3x and downscaled for clean antialiasing, then compiled to .icns
via iconutil.

Run:  backend/.venv/bin/python scripts/gen-icon.py
The .icns is a committed static asset; make-app.sh just copies it into the
bundle, so Pillow is NOT a runtime/build dependency.
"""
import math
import subprocess
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw

SS = 3                       # supersample factor
S = 1024 * SS                # working canvas size
HERE = Path(__file__).resolve().parent

# Palette — Flexoki, the same values Mark.jsx and favicon.svg use ------------
RUFOUS = (0xBC, 0x52, 0x15)  # orange-600, the paper-side accent
INK    = (0x10, 0x0F, 0x0F)
PAPER  = (0xF2, 0xF0, 0xE5)
CERE   = (0xD0, 0xA2, 0x15)  # yellow-400, the bare ring round the eye

# Geometry — in the shared 100-unit box, mapped onto the icon body -----------
MARGIN = 96 * SS             # macOS keeps the art off the tile edge
BOX = (MARGIN, MARGIN, S - MARGIN, S - MARGIN)
W = BOX[2] - BOX[0]

EYE = (59, 39)
EYE_R = 11.5
RING_R, RING_W = 15, 4.5     # stroke centred on RING_R, as in SVG
GLINT, GLINT_R = (63, 35), 3.2
# M53 55 C51 66 47 77 42 91 C50 79 56 67 60 56 Z — two cubics meeting in a
# point at the bottom.
STRIPE = [
    ((53, 55), (51, 66), (47, 77), (42, 91)),
    ((42, 91), (50, 79), (56, 67), (60, 56)),
]


def _squircle():
    """macOS-style superellipse |x|^n + |y|^n = 1, as a filled mask.

    A rounded rectangle is visibly not this shape next to real app icons —
    the corners of a squircle keep curving all the way into the straights.
    """
    n, pts = 5.0, 1440
    cx = cy = S / 2
    r = W / 2
    poly = []
    for i in range(pts):
        t = 2 * math.pi * i / pts
        ct, st = math.cos(t), math.sin(t)
        poly.append((cx + r * math.copysign(abs(ct) ** (2 / n), ct),
                     cy + r * math.copysign(abs(st) ** (2 / n), st)))
    mask = Image.new("L", (S, S), 0)
    ImageDraw.Draw(mask).polygon(poly, fill=255)
    return mask


def _pt(p):
    """A 100-box point in canvas pixels."""
    return (BOX[0] + W * p[0] / 100, BOX[1] + W * p[1] / 100)


def _circle(d, c, r, fill):
    x, y = _pt(c)
    r = W * r / 100
    d.ellipse([x - r, y - r, x + r, y + r], fill=fill)


def _cubic(p0, p1, p2, p3, steps=64):
    out = []
    for i in range(steps + 1):
        t = i / steps
        u = 1 - t
        out.append(tuple(u**3 * a + 3 * u * u * t * b + 3 * u * t * t * c + t**3 * e
                         for a, b, c, e in zip(p0, p1, p2, p3)))
    return out


def build():
    body = Image.new("RGB", (S, S), RUFOUS)
    d = ImageDraw.Draw(body)

    # The closing Z edge is straight, so chaining the two sampled curves and
    # letting the polygon close itself reproduces the SVG path exactly.
    poly = [_pt(p) for seg in STRIPE for p in _cubic(*seg)]
    d.polygon(poly, fill=INK)

    _circle(d, EYE, RING_R + RING_W / 2, CERE)
    _circle(d, EYE, RING_R - RING_W / 2, RUFOUS)   # hollow the ring
    _circle(d, EYE, EYE_R, INK)
    _circle(d, GLINT, GLINT_R, PAPER)

    tile = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    tile.paste(body, (0, 0), _squircle())
    icon = tile.resize((1024, 1024), Image.LANCZOS)

    with tempfile.TemporaryDirectory() as tmp:
        iconset = Path(tmp) / "icon.iconset"
        iconset.mkdir()
        specs = [
            (16, "16x16"), (32, "16x16@2x"), (32, "32x32"), (64, "32x32@2x"),
            (128, "128x128"), (256, "128x128@2x"), (256, "256x256"),
            (512, "256x256@2x"), (512, "512x512"), (1024, "512x512@2x"),
        ]
        for px, name in specs:
            icon.resize((px, px), Image.LANCZOS).save(iconset / f"icon_{name}.png")
        out = HERE / "dashboard.icns"
        subprocess.run(
            ["iconutil", "-c", "icns", str(iconset), "-o", str(out)], check=True
        )
        icon.save(HERE / "dashboard-preview.png")  # for eyeballing only
    print(f"wrote {HERE / 'dashboard.icns'}")


if __name__ == "__main__":
    build()
