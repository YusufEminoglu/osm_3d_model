# -*- coding: utf-8 -*-
"""Regenerate the animated README illustrations.

    py -3 docs/assets/make_assets.py


Hand-written SVG with SMIL animation only: no <style> blocks and no script, both
of which GitHub's README sanitiser strips. Every colour is a presentation
attribute so the markup survives that pass intact.
"""
from __future__ import annotations

import io
import os

OUT = os.path.dirname(os.path.abspath(__file__))

INK = "#4d3d38"
INK_SOFT = "#7a655d"
ACCENT = "#b8978a"
ACCENT_DEEP = "#8a675c"

# The ten curated looks, straight from web/src/looks.js.
# swatch = [buildings, greens, roads, ground]
LOOKS = [
    ("Editorial Dusk", ["#ebd4c0", "#c2c5aa", "#7c5c43", "#fdfbf7"]),
    ("Nordic Snowfall", ["#b9c7c5", "#dde6e3", "#36433f", "#e6efec"]),
    ("Harbor Morning", ["#e3c3b5", "#5e9e7e", "#2f4a46", "#dfeae7"]),
    ("Golden Mediterranean", ["#f0d9bf", "#7bb069", "#6f655c", "#f4ead6"]),
    ("Neon Rain", ["#9fc7d6", "#2f9e7e", "#1a1d24", "#2b3038"]),
    ("Anime Noon", ["#bfe3f0", "#b6f2c6", "#7d8a96", "#e9f1e4"]),
    ("Violet Dawn", ["#d2c9e4", "#8a9e6e", "#2a2a30", "#efecf6"]),
    ("Desert Noon", ["#e3d6b6", "#8aa05e", "#46413a", "#efe7d4"]),
    ("Cartoon Playground", ["#f2b8b0", "#5fbf57", "#4a4540", "#fbe7c6"]),
    ("Vintage Postcard", ["#cdb89a", "#b9c79a", "#6b6052", "#e4d6b8"]),
]
CYCLE = "20s"   # 2s per look


def write(name: str, body: str) -> None:
    path = os.path.join(OUT, name)
    io.open(path, "w", encoding="utf-8", newline="\n").write(body)
    print(f"{name:26s} {len(body) / 1024:6.1f} KB")


def first(index: int, factor: float = 1.0) -> str:
    """The colour an element must already have before any animation runs."""
    colour = LOOKS[0][1][index]
    return colour if factor == 1.0 else shade(colour, factor)


def cyc(index: int, shift: float = 0.0) -> str:
    """A discrete 10-step fill animation over the look cycle."""
    values = ";".join(look[1][index] for look in LOOKS)
    begin = f' begin="{shift}s"' if shift else ""
    return (f'<animate attributeName="fill" values="{values}" dur="{CYCLE}" '
            f'calcMode="discrete" repeatCount="indefinite"{begin}/>')


def shade(hex_color: str, factor: float) -> str:
    r = int(hex_color[1:3], 16)
    g = int(hex_color[3:5], 16)
    b = int(hex_color[5:7], 16)
    f = lambda v: max(0, min(255, int(v * factor)))  # noqa: E731
    return f"#{f(r):02x}{f(g):02x}{f(b):02x}"


def cyc_shaded(index: int, factor: float) -> str:
    values = ";".join(shade(look[1][index], factor) for look in LOOKS)
    return (f'<animate attributeName="fill" values="{values}" dur="{CYCLE}" '
            f'calcMode="discrete" repeatCount="indefinite"/>')


# ===========================================================================
# 1. Hero — an isometric city that rises, then recolours through the ten looks
#    while the sun crosses the sky.
# ===========================================================================
def iso_prism(cx: float, cy: float, w: float, h: float, height: float,
              fill_anim: str, left_anim: str, right_anim: str,
              top_fill: str, left_fill: str, right_fill: str) -> str:
    """One isometric box. (cx, cy) is the centre of its base diamond."""
    top = f"{cx},{cy - height - h} {cx + w},{cy - height} {cx},{cy - height + h} {cx - w},{cy - height}"
    left = (f"{cx - w},{cy - height} {cx},{cy - height + h} {cx},{cy + h} {cx - w},{cy}")
    right = (f"{cx + w},{cy - height} {cx},{cy - height + h} {cx},{cy + h} {cx + w},{cy}")
    return (
        f'<polygon points="{left}" fill="{left_fill}">{left_anim}</polygon>'
        f'<polygon points="{right}" fill="{right_fill}">{right_anim}</polygon>'
        f'<polygon points="{top}" fill="{top_fill}">{fill_anim}</polygon>'
    )


def build_hero() -> str:
    W, H = 1200, 520
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" role="img" '
        'aria-labelledby="heroTitle heroDesc">',
        '<title id="heroTitle">3D OSM Model</title>',
        '<desc id="heroDesc">An OpenStreetMap study area rising into a procedural 3D city, '
        'recolouring through ten curated looks as the sun crosses the sky.</desc>',
        '<defs>',
        '<linearGradient id="sky" x1="0" y1="0" x2="0" y2="1">',
        '<stop offset="0" stop-color="#dcecf2">'
        '<animate attributeName="stop-color" '
        'values="#dcecf2;#cfe3ee;#f6dcc4;#2b3550;#101a2e;#2b3550;#f6dcc4;#dcecf2" '
        'dur="24s" repeatCount="indefinite"/></stop>',
        '<stop offset="1" stop-color="#fdf6f1">'
        '<animate attributeName="stop-color" '
        'values="#fdf6f1;#fbeadd;#f3c9a4;#4a4a68;#1d2740;#4a4a68;#f3c9a4;#fdf6f1" '
        'dur="24s" repeatCount="indefinite"/></stop>',
        '</linearGradient>',
        '<clipPath id="card"><rect x="0" y="0" width="1200" height="520" rx="26"/></clipPath>',
        '</defs>',
        '<g clip-path="url(#card)">',
        f'<rect width="{W}" height="{H}" fill="url(#sky)"/>',
    ]

    state = {"seed": 20261}

    def rnd(n):
        state["seed"] = (state["seed"] * 1103515245 + 12345) % (1 << 31)
        return (state["seed"] >> 7) % n

    for i in range(48):
        sx, sy = 40 + rnd(1120), 22 + rnd(200)
        r = 0.9 + rnd(100) / 90.0
        parts.append(
            f'<circle cx="{sx}" cy="{sy}" r="{r:.1f}" fill="#ffffff" opacity="0">'
            '<animate attributeName="opacity" values="0;0;0;0.85;0.95;0.85;0;0" '
            f'dur="24s" repeatCount="indefinite" begin="{i * 0.04:.2f}s"/></circle>'
        )

    parts.append(
        '<circle r="30" fill="#ffd9a8" opacity="0.95">'
        '<animate attributeName="fill" '
        'values="#ffe9c4;#ffd9a8;#ff9d5c;#dfe6f5;#eef2ff;#dfe6f5;#ff9d5c;#ffe9c4" '
        'dur="24s" repeatCount="indefinite"/>'
        '<animate attributeName="r" values="26;30;36;18;16;18;36;26" dur="24s" '
        'repeatCount="indefinite"/>'
        '<animateMotion dur="24s" repeatCount="indefinite" '
        'path="M 40 430 C 300 26 1000 26 1250 430 C 1260 470 1260 470 1260 470"/>'
        '</circle>'
    )

    # --- the isometric city -------------------------------------------------
    # Standard mapping: x grows with (col - row), y with (col + row).
    ox, oy = 700, 168
    cw, ch = 40, 20
    nc, nr = 7, 5
    heights = [
        [18, 40, 14, 30, 58, 22, 36],
        [46, 24, 54, 16, 28, 48, 14],
        [22, 60, 30, 44, 16, 34, 52],
        [38, 18, 46, 26, 52, 20, 30],
        [28, 50, 18, 56, 24, 40, 22],
    ]

    def iso(col, row):
        return ox + (col - row) * cw, oy + (col + row) * ch

    parts.append('<g>')
    c0, c1 = iso(-0.5, -0.5), iso(nc - 0.5, -0.5)
    c2, c3 = iso(nc - 0.5, nr - 0.5), iso(-0.5, nr - 0.5)
    plate = " ".join(f"{x:.0f},{y:.0f}" for x, y in (c0, c1, c2, c3))
    parts.append(f'<polygon points="{plate}" opacity="0.97" fill="{first(3)}">{cyc(3)}</polygon>')
    # A soft skirt below the plate, so it reads as a solid model base.
    skirt = (f"{c3[0]:.0f},{c3[1]:.0f} {c2[0]:.0f},{c2[1]:.0f} "
             f"{c2[0]:.0f},{c2[1] + 18:.0f} {c3[0]:.0f},{c3[1] + 18:.0f}")
    parts.append(f'<polygon points="{skirt}" fill="{first(3, 0.78)}">{cyc_shaded(3, 0.78)}</polygon>')
    parts.append(f'<polygon points="{plate}" fill="none" stroke="{ACCENT}" '
                 'stroke-opacity="0.4" stroke-width="2"/>')

    street_cols = (1.5, 4.5)
    street_rows = (2.5,)
    for col in street_cols:
        a, b = iso(col - 0.17, -0.5), iso(col - 0.17, nr - 0.5)
        c, d = iso(col + 0.17, nr - 0.5), iso(col + 0.17, -0.5)
        pts = " ".join(f"{x:.0f},{y:.0f}" for x, y in (a, b, c, d))
        parts.append(f'<polygon points="{pts}" opacity="0.92" fill="{first(2)}">{cyc(2)}</polygon>')
    for row in street_rows:
        a, b = iso(-0.5, row - 0.17), iso(nc - 0.5, row - 0.17)
        c, d = iso(nc - 0.5, row + 0.17), iso(-0.5, row + 0.17)
        pts = " ".join(f"{x:.0f},{y:.0f}" for x, y in (a, b, c, d))
        parts.append(f'<polygon points="{pts}" opacity="0.92" fill="{first(2)}">{cyc(2)}</polygon>')

    boxes = sorted(
        ((col + row, col, row, heights[row][col]) for row in range(nr) for col in range(nc)),
        key=lambda b: b[0],
    )
    for _, col, row, h in boxes:
        if any(abs(col - c) < 0.35 for c in street_cols):
            continue
        if any(abs(row - r) < 0.35 for r in street_rows):
            continue
        ix, iy = iso(col, row)
        if h <= 18:   # a green plot rather than a building
            pts = (f"{ix},{iy - ch * 0.8} {ix + cw * 0.8},{iy} "
                   f"{ix},{iy + ch * 0.8} {ix - cw * 0.8},{iy}")
            parts.append(f'<polygon points="{pts}" opacity="0.95" fill="{first(1)}">{cyc(1)}</polygon>')
            continue
        parts.append(iso_prism(ix, iy, cw * 0.47, ch * 0.47, h,
                               cyc(0), cyc_shaded(0, 0.72), cyc_shaded(0, 0.88),
                               first(0), first(0, 0.72), first(0, 0.88)))
    parts.append('</g>')

    # --- copy ---------------------------------------------------------------
    parts.append(
        f'<text x="56" y="104" font-family="Segoe UI,Inter,system-ui,sans-serif" font-size="48" '
        f'font-weight="800" fill="{INK}" letter-spacing="-1.2">3D OSM Model</text>'
        f'<text x="58" y="140" font-family="Segoe UI,Inter,system-ui,sans-serif" font-size="19" '
        f'fill="{INK_SOFT}">Pick an area. One click later you are standing in it.</text>'
    )

    # Live look badge. SMIL cannot animate text content, so ten labels take turns:
    # each is opaque for exactly its own two-second slot of the cycle.
    parts.append(
        f'<rect x="56" y="168" width="276" height="36" rx="18" fill="#ffffff" fill-opacity="0.86" '
        f'stroke="{ACCENT}" stroke-opacity="0.42"/>'
        f'<circle cx="78" cy="186" r="8" fill="{first(0)}">{cyc(0)}</circle>'
    )
    for i, (name, _) in enumerate(LOOKS):
        slots = ";".join("1" if j == i else "0" for j in range(len(LOOKS)))
        parts.append(
            f'<text x="96" y="191" font-family="Segoe UI,Inter,system-ui,sans-serif" '
            f'font-size="14.5" font-weight="700" fill="{ACCENT_DEEP}" opacity="0">{name}'
            f'<animate attributeName="opacity" values="{slots}" dur="{CYCLE}" '
            'calcMode="discrete" repeatCount="indefinite"/></text>'
        )

    tiles = [("293", "Buildings"), ("8", "Blocks"), ("3.0", "Avg. floors"), ("4,349", "Population")]
    for i, (value, label) in enumerate(tiles):
        x = 56 + i * 96
        parts.append(
            f'<rect x="{x}" y="244" width="86" height="70" rx="12" fill="#ffffff" '
            f'fill-opacity="0.8" stroke="{ACCENT}" stroke-opacity="0.3"/>'
            f'<text x="{x + 43}" y="278" text-anchor="middle" '
            f'font-family="Segoe UI,Inter,system-ui,sans-serif" font-size="22" font-weight="800" '
            f'fill="{INK}">{value}</text>'
            f'<text x="{x + 43}" y="298" text-anchor="middle" '
            f'font-family="Segoe UI,Inter,system-ui,sans-serif" font-size="11.5" '
            f'fill="{INK_SOFT}">{label}</text>'
        )
    parts.append(
        f'<text x="56" y="352" font-family="Segoe UI,Inter,system-ui,sans-serif" font-size="13.5" '
        f'fill="{INK_SOFT}">Buildings, roads, greens, water, trees and street furniture,</text>'
        f'<text x="56" y="372" font-family="Segoe UI,Inter,system-ui,sans-serif" font-size="13.5" '
        f'fill="{INK_SOFT}">read straight from their native OpenStreetMap tags.</text>'
    )

    chips = [
        ("One button", 56),
        ("10 curated looks", 186),
        ("Cinematic tour", 348),
        ("Walk at 1.73 m", 498),
        ("PNG · PDF · SVG · video", 658),
    ]
    for label, x in chips:
        w = 20 + len(label) * 7.5
        parts.append(
            f'<rect x="{x}" y="450" width="{w:.0f}" height="32" rx="16" fill="#ffffff" '
            f'fill-opacity="0.82" stroke="{ACCENT}" stroke-opacity="0.36"/>'
            f'<text x="{x + w / 2:.0f}" y="471" text-anchor="middle" '
            f'font-family="Segoe UI,Inter,system-ui,sans-serif" font-size="13" font-weight="600" '
            f'fill="{ACCENT_DEEP}">{label}</text>'
        )

    parts.append('</g>')
    parts.append(f'<rect x="1" y="1" width="{W - 2}" height="{H - 2}" rx="26" fill="none" '
                 f'stroke="{ACCENT}" stroke-opacity="0.45" stroke-width="2"/>')
    parts.append('</svg>')
    return "\n".join(parts)


# ===========================================================================
# 2. Pipeline — a packet travelling QGIS -> Overpass -> GeoJSON -> viewer
# ===========================================================================
def build_pipeline() -> str:
    W, H = 1120, 300
    cards = [
        ("Study area", "canvas extent or selection", "#e8f2ec"),
        ("Overpass", "3 mirrors, one retry each", "#f6e6dc"),
        ("GeoJSON", "native OSM tags kept", "#e7edf6"),
        ("Viewer", "procedural 3D city", "#efe7f4"),
    ]
    cw, gap, y0 = 232, 40, 96
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" role="img" '
        'aria-labelledby="pipeTitle pipeDesc">',
        '<title id="pipeTitle">Export pipeline</title>',
        '<desc id="pipeDesc">A study area travels through the Overpass API and a GeoJSON export '
        'into the Three.js city viewer.</desc>',
        '<defs>',
        '<linearGradient id="pipeBg" x1="0" y1="0" x2="1" y2="1">'
        '<stop offset="0" stop-color="#fdf8f5"/><stop offset="1" stop-color="#eef4f1"/>'
        '</linearGradient>',
        f'<path id="track" d="M 96 {y0 + 58} H 1024" fill="none"/>',
        '</defs>',
        f'<rect width="{W}" height="{H}" rx="24" fill="url(#pipeBg)"/>',
        f'<rect x="1" y="1" width="{W - 2}" height="{H - 2}" rx="24" fill="none" '
        f'stroke="{ACCENT}" stroke-opacity="0.4" stroke-width="2"/>',
        f'<text x="40" y="52" font-family="Segoe UI,Inter,system-ui,sans-serif" font-size="22" '
        f'font-weight="800" fill="{INK}">From a map view to a city you can walk through</text>',
    ]

    # Connector line behind the cards.
    parts.append(f'<path d="M 96 {y0 + 58} H 1024" stroke="{ACCENT}" stroke-opacity="0.3" '
                 'stroke-width="3" stroke-linecap="round" stroke-dasharray="7 9"/>')

    for i, (title, sub, tint) in enumerate(cards):
        x = 40 + i * (cw + gap)
        begin = i * 2.0
        parts.append(
            f'<g><rect x="{x}" y="{y0}" width="{cw}" height="116" rx="18" fill="#ffffff" '
            f'stroke="{ACCENT}" stroke-opacity="0.28" stroke-width="1.5"/>'
            f'<rect x="{x}" y="{y0}" width="{cw}" height="116" rx="18" fill="{tint}" opacity="0">'
            f'<animate attributeName="opacity" values="0;0.9;0" keyTimes="0;0.12;0.36" '
            f'dur="8s" begin="{begin}s" repeatCount="indefinite"/></rect>'
            f'<rect x="{x}" y="{y0}" width="{cw}" height="116" rx="18" fill="none" '
            f'stroke="{ACCENT_DEEP}" stroke-width="2.5" opacity="0">'
            f'<animate attributeName="opacity" values="0;1;0" keyTimes="0;0.1;0.3" '
            f'dur="8s" begin="{begin}s" repeatCount="indefinite"/></rect>'
            f'<text x="{x + 24}" y="{y0 + 48}" font-family="Segoe UI,Inter,system-ui,sans-serif" '
            f'font-size="20" font-weight="700" fill="{INK}">{title}</text>'
            f'<text x="{x + 24}" y="{y0 + 76}" font-family="Segoe UI,Inter,system-ui,sans-serif" '
            f'font-size="13.5" fill="{INK_SOFT}">{sub}</text>'
            f'<text x="{x + 24}" y="{y0 + 100}" font-family="Segoe UI,Inter,system-ui,sans-serif" '
            f'font-size="11" font-weight="700" fill="{ACCENT}" letter-spacing="1.4">'
            f'STEP {i + 1}</text></g>'
        )

    # The packet.
    parts.append(
        f'<circle r="9" fill="{ACCENT_DEEP}">'
        '<animateMotion dur="8s" repeatCount="indefinite" keyPoints="0;0.335;0.335;0.665;0.665;1;1;0" '
        'keyTimes="0;0.25;0.28;0.5;0.53;0.75;0.78;1" calcMode="linear">'
        '<mpath href="#track"/></animateMotion>'
        '</circle>'
        f'<circle r="9" fill="none" stroke="{ACCENT_DEEP}" stroke-width="2" opacity="0.6">'
        '<animate attributeName="r" values="9;22;9" dur="2s" repeatCount="indefinite"/>'
        '<animate attributeName="opacity" values="0.6;0;0.6" dur="2s" repeatCount="indefinite"/>'
        '<animateMotion dur="8s" repeatCount="indefinite" keyPoints="0;0.335;0.335;0.665;0.665;1;1;0" '
        'keyTimes="0;0.25;0.28;0.5;0.53;0.75;0.78;1" calcMode="linear">'
        '<mpath href="#track"/></animateMotion>'
        '</circle>'
    )

    parts.append(
        f'<text x="40" y="266" font-family="Segoe UI,Inter,system-ui,sans-serif" font-size="13.5" '
        f'fill="{INK_SOFT}">Downloads use QGIS\u2019s own network stack, so the plugin reaches the '
        'internet wherever QGIS does \u2014 proxy, TLS and certificates included.</text>'
    )
    parts.append('</svg>')
    return "\n".join(parts)


# ===========================================================================
# 3. Looks gallery — a city strip recolouring through all ten looks
# ===========================================================================
def build_looks() -> str:
    W, H = 1120, 430
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" role="img" '
        'aria-labelledby="looksTitle looksDesc">',
        '<title id="looksTitle">Ten curated looks</title>',
        '<desc id="looksDesc">One city cycling through the ten curated looks: palette, textures, '
        'massing and light changed together by a single button.</desc>',
        '<defs>',
        '<linearGradient id="looksBg" x1="0" y1="0" x2="1" y2="1">'
        '<stop offset="0" stop-color="#fdf8f5"/><stop offset="1" stop-color="#f0eff5"/>'
        '</linearGradient>',
        '<clipPath id="strip"><rect x="40" y="96" width="640" height="252" rx="18"/></clipPath>',
        '</defs>',
        f'<rect width="{W}" height="{H}" rx="24" fill="url(#looksBg)"/>',
        f'<rect x="1" y="1" width="{W - 2}" height="{H - 2}" rx="24" fill="none" '
        f'stroke="{ACCENT}" stroke-opacity="0.4" stroke-width="2"/>',
        f'<text x="40" y="52" font-family="Segoe UI,Inter,system-ui,sans-serif" font-size="22" '
        f'font-weight="800" fill="{INK}">One button, ten curated looks</text>',
        f'<text x="40" y="76" font-family="Segoe UI,Inter,system-ui,sans-serif" font-size="13.5" '
        f'fill="{INK_SOFT}">Palette, road and ground textures, roof type, time of day, weather and '
        'fog \u2014 tuned together, never drawn at random.</text>',
    ]

    # --- the recolouring city strip ----------------------------------------
    parts.append('<g clip-path="url(#strip)">')
    parts.append('<rect x="40" y="96" width="640" height="252" fill="#f6f2ef"/>')
    horizon = 288
    # Ground, then the street the block fronts onto.
    parts.append(f'<rect x="40" y="{horizon}" width="640" height="60" fill="{first(3)}">{cyc(3)}</rect>')
    parts.append(f'<rect x="40" y="{horizon + 16}" width="640" height="30" fill="{first(2)}">{cyc(2)}</rect>')
    for cx in range(60, 690, 56):
        parts.append(f'<rect x="{cx}" y="{horizon + 29}" width="26" height="4" rx="2" fill="{first(3)}">{cyc(3)}</rect>')
    # A block of buildings standing on the ground line.
    block = [(58, 96), (94, 148), (132, 118), (170, 176), (208, 86),
             (250, 138), (288, 190), (326, 108), (364, 158), (402, 92),
             (448, 170), (486, 124), (524, 196), (562, 112), (600, 146), (638, 88)]
    for x, h in block:
        y = horizon - h
        parts.append(f'<rect x="{x}" y="{y}" width="32" height="{h}" fill="{first(0)}">{cyc(0)}</rect>')
        parts.append(f'<rect x="{x - 2}" y="{y - 6}" width="36" height="8" rx="2" fill="{first(0, 0.74)}">{cyc_shaded(0, 0.74)}</rect>')
        for wy in range(y + 14, horizon - 14, 20):
            for wx in (x + 6, x + 19):
                parts.append(f'<rect x="{wx}" y="{wy}" width="7" height="10" rx="1.5" '
                             'fill="#ffffff" fill-opacity="0.42"/>')
    # Street trees, so the greens in each palette are visible too.
    for tx in (232, 430, 620):
        parts.append(f'<rect x="{tx + 5}" y="{horizon - 26}" width="4" height="26">'
                     f'{cyc_shaded(2, 0.9)}</rect>')
        parts.append(f'<circle cx="{tx + 7}" cy="{horizon - 34}" r="16" fill="{first(1)}">{cyc(1)}</circle>')
        parts.append(f'<circle cx="{tx - 4}" cy="{horizon - 24}" r="11" fill="{first(1, 0.9)}">{cyc_shaded(1, 0.9)}</circle>')
        parts.append(f'<circle cx="{tx + 18}" cy="{horizon - 24}" r="11" fill="{first(1, 0.92)}">{cyc_shaded(1, 0.92)}</circle>')
    parts.append('</g>')
    parts.append(f'<rect x="40" y="96" width="640" height="252" rx="18" fill="none" '
                 f'stroke="{ACCENT}" stroke-opacity="0.34" stroke-width="1.5"/>')

    # --- the list of looks --------------------------------------------------
    lx, ly, row = 716, 104, 25
    step = 20.0 / len(LOOKS)
    parts.append(
        f'<rect x="{lx - 12}" y="{ly - 8}" width="20" height="{row - 3}" rx="7" '
        f'fill="{ACCENT}" fill-opacity="0.001"/>'
    )
    ys = ";".join(str(ly - 6 + i * row) for i in range(len(LOOKS)))
    parts.append(
        f'<rect x="{lx - 14}" y="{ly - 6}" width="382" height="{row - 4}" rx="8" '
        f'fill="{ACCENT}" fill-opacity="0.18">'
        f'<animate attributeName="y" values="{ys}" dur="{CYCLE}" calcMode="discrete" '
        'repeatCount="indefinite"/></rect>'
    )
    for i, (name, sw) in enumerate(LOOKS):
        y = ly + i * row
        for j, colour in enumerate(sw):
            parts.append(f'<rect x="{lx + j * 13}" y="{y - 1}" width="11" height="11" rx="2.5" '
                         f'fill="{colour}" stroke="{INK}" stroke-opacity="0.18"/>')
        parts.append(
            f'<text x="{lx + 62}" y="{y + 9}" font-family="Segoe UI,Inter,system-ui,sans-serif" '
            f'font-size="13.5" fill="{INK}" opacity="0.55">{name}'
            f'<animate attributeName="opacity" values="0.55;1;1;0.55" keyTimes="0;0.04;0.96;1" '
            f'dur="{step}s" begin="{i * step}s" repeatCount="indefinite"/></text>'
        )

    parts.append(
        f'<text x="40" y="386" font-family="Segoe UI,Inter,system-ui,sans-serif" font-size="13.5" '
        f'fill="{INK_SOFT}">Shuffle never returns the look already on screen, and never touches '
        'which layers are visible.</text>'
    )
    parts.append('</svg>')
    return "\n".join(parts)


# ===========================================================================
# 4. Walk mode — a 1.85 m body moving through the street
# ===========================================================================
def build_walk() -> str:
    W, H = 1120, 340
    ground = 268
    m = 62.0           # pixels per metre
    eye = ground - 1.73 * m
    head = ground - 1.85 * m
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" role="img" '
        'aria-labelledby="walkTitle walkDesc">',
        '<title id="walkTitle">Walk mode</title>',
        '<desc id="walkDesc">Walk mode seen from a 1.85 metre body: the eyes sit at 1.73 metres '
        'and the walker moves at 1.8 metres per second past solid building walls.</desc>',
        '<defs>',
        '<linearGradient id="walkBg" x1="0" y1="0" x2="0" y2="1">'
        '<stop offset="0" stop-color="#eaf1f5"/><stop offset="1" stop-color="#fdf8f5"/>'
        '</linearGradient>',
        '<clipPath id="street"><rect x="18" y="70" width="1084" height="230" rx="16"/></clipPath>',
        '</defs>',
        f'<rect width="{W}" height="{H}" rx="24" fill="url(#walkBg)"/>',
        f'<rect x="1" y="1" width="{W - 2}" height="{H - 2}" rx="24" fill="none" '
        f'stroke="{ACCENT}" stroke-opacity="0.4" stroke-width="2"/>',
        f'<text x="40" y="48" font-family="Segoe UI,Inter,system-ui,sans-serif" font-size="22" '
        f'font-weight="800" fill="{INK}">Walk mode is a body, not a camera</text>',
    ]

    parts.append('<g clip-path="url(#street)">')
    # Scrolling background: a 360 px period repeated, so the loop is seamless.
    parts.append('<g><animateTransform attributeName="transform" type="translate" '
                 'from="0 0" to="-360 0" dur="5s" repeatCount="indefinite"/>')
    facades = [(0, 150, "#d8c6bb"), (120, 208, "#c9d4cf"), (250, 176, "#dccfc2")]
    for rep in range(5):
        ox = rep * 360
        for fx, fh, colour in facades:
            x = 18 + ox + fx
            y = ground - fh
            parts.append(f'<rect x="{x}" y="{y}" width="96" height="{fh}" rx="3" fill="{colour}"/>')
            parts.append(f'<rect x="{x}" y="{y}" width="96" height="8" rx="3" fill="{shade(colour, 0.82)}"/>')
            for wy in range(int(y) + 22, ground - 26, 30):
                for wx in (x + 14, x + 42, x + 70):
                    parts.append(f'<rect x="{wx}" y="{wy}" width="14" height="18" rx="2" '
                                 f'fill="#8fa6b5" fill-opacity="0.55"/>')
        # kerb dashes
        for k in range(6):
            parts.append(f'<rect x="{18 + ox + k * 60}" y="{ground + 16}" width="34" height="4" '
                         f'rx="2" fill="{ACCENT}" fill-opacity="0.45"/>')
    parts.append('</g>')

    # Ground and sidewalk.
    parts.append(f'<rect x="18" y="{ground}" width="1084" height="8" fill="{ACCENT_DEEP}" fill-opacity="0.55"/>')
    parts.append(f'<rect x="18" y="{ground + 8}" width="1084" height="32" fill="{ACCENT}" fill-opacity="0.16"/>')

    # --- the walker, fixed in frame, bobbing --------------------------------
    wx = 560
    parts.append(f'<g transform="translate({wx} 0)">')
    parts.append('<g><animateTransform attributeName="transform" type="translate" '
                 'values="0 0;0 -3.2;0 0;0 -3.2;0 0" dur="0.86s" repeatCount="indefinite"/>')
    body = ground
    parts.append(
        f'<circle cx="0" cy="{head + 13:.0f}" r="13" fill="{INK}" fill-opacity="0.88"/>'
        f'<rect x="-4" y="{head + 24:.0f}" width="8" height="10" fill="{INK}" fill-opacity="0.88"/>'
        f'<rect x="-11" y="{head + 32:.0f}" width="22" height="{(ground - 40) - (head + 32):.0f}" '
        f'rx="10" fill="{INK}" fill-opacity="0.88"/>'
    )
    # Legs. The rotation lives on its own group inside the translate, because an
    # animateTransform on an element that already carries a transform attribute
    # replaces it rather than composing with it - which folded both legs onto the
    # same spot.
    hip = body - 44
    for opacity, phase in ((0.92, "0s"), (0.6, "0.43s")):
        parts.append(
            f'<g transform="translate(0 {hip:.0f})"><g>'
            '<animateTransform attributeName="transform" type="rotate" '
            f'values="-26;26;-26" dur="0.86s" begin="{phase}" repeatCount="indefinite"/>'
            f'<rect x="-6" y="-2" width="12" height="40" rx="6" fill="{INK}" fill-opacity="{opacity}"/>'
            '</g></g>'
        )
    parts.append('</g>')
    # Eye marker and sight line.
    parts.append(
        f'<circle cx="6" cy="{eye:.0f}" r="4" fill="#ffffff" stroke="{ACCENT_DEEP}" stroke-width="2"/>'
    )
    parts.append('</g>')

    # Eye-height guide across the frame.
    parts.append(
        f'<line x1="18" y1="{eye:.0f}" x2="1102" y2="{eye:.0f}" stroke="{ACCENT_DEEP}" '
        'stroke-width="1.5" stroke-dasharray="6 8" opacity="0.75"/>'
    )
    parts.append('</g>')

    # --- dimensions ---------------------------------------------------------
    dx = 470
    parts.append(
        f'<line x1="{dx}" y1="{head:.0f}" x2="{dx}" y2="{ground}" stroke="{INK}" stroke-width="1.4" opacity="0.8"/>'
        f'<line x1="{dx - 6}" y1="{head:.0f}" x2="{dx + 6}" y2="{head:.0f}" stroke="{INK}" stroke-width="1.4"/>'
        f'<line x1="{dx - 6}" y1="{ground}" x2="{dx + 6}" y2="{ground}" stroke="{INK}" stroke-width="1.4"/>'
        f'<text x="{dx - 12}" y="{(head + ground) / 2:.0f}" text-anchor="end" '
        f'font-family="Segoe UI,Inter,system-ui,sans-serif" font-size="13" font-weight="700" '
        f'fill="{INK}">1.85 m body</text>'
    )
    parts.append(
        f'<rect x="700" y="{eye - 26:.0f}" width="150" height="24" rx="12" fill="#ffffff" '
        f'fill-opacity="0.94" stroke="{ACCENT}" stroke-opacity="0.4"/>'
        f'<text x="775" y="{eye - 9:.0f}" text-anchor="middle" '
        f'font-family="Segoe UI,Inter,system-ui,sans-serif" '
        f'font-size="13" font-weight="700" fill="{ACCENT_DEEP}">1.73 m eye height</text>'
    )

    facts = [
        ("1.8 m/s", "walking pace, 6.5 km/h"),
        ("3.6 m/s", "Shift to run"),
        ("solid", "walls stop you"),
        ("1.3 cm", "head rise per step"),
    ]
    for i, (big, small) in enumerate(facts):
        x = 40 + i * 268
        parts.append(
            f'<text x="{x}" y="{H - 34}" font-family="Segoe UI,Inter,system-ui,sans-serif" '
            f'font-size="19" font-weight="800" fill="{ACCENT_DEEP}">{big}</text>'
            f'<text x="{x}" y="{H - 16}" font-family="Segoe UI,Inter,system-ui,sans-serif" '
            f'font-size="12.5" fill="{INK_SOFT}">{small}</text>'
        )
    parts.append('</svg>')
    return "\n".join(parts)


if __name__ == "__main__":
    write("github-hero.svg", build_hero())
    write("pipeline.svg", build_pipeline())
    write("looks-gallery.svg", build_looks())
    write("walk-mode.svg", build_walk())
