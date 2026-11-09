#!/usr/bin/env python3
#
# This program source code file is part of PhiCad, a fork of KiCad.
#
# Copyright (C) 2026 Exowatt, Inc.
# Copyright The KiCad Developers, see AUTHORS.txt for contributors.
#
# SPDX-License-Identifier: GPL-2.0-or-later
#
"""Plot a .kicad_pcb to SVG/PNG without KiCad.

`kicad-cli pcb render` is the real renderer and this is not a substitute for it.  This exists
for two situations that came up building this demo:

* **kicad-cli is unavailable.**  A wedged `opendirectoryd` makes every kicad-cli invocation
  abort with exit 134 and no message (see verify.sh), and then there is no way to *look* at
  the board being generated.  This reads the s-expression directly, so it depends on nothing
  but Python.
* **Reviewing the generator's intent.**  It draws exactly what the generator emitted -- F.Cu,
  B.Cu, pads, holes, refdes, edge cuts -- with layers colour-coded to the PhiCad Altium theme,
  which makes a corridor/lane plan legible in a way a photorealistic render does not.

Usage:
    python3 plot_board.py [board.kicad_pcb] [-o out.svg] [--png]

`--png` additionally rasterises via rsvg-convert or cairosvg if either is available.
"""

from __future__ import annotations

import argparse
import math
import re
import shutil
import subprocess
import sys
from pathlib import Path

# PhiCad Altium theme colours (phicad/resources/themes/phicad-altium.json).
COLORS = {
    "F.Cu":       "#EB2D28",
    "B.Cu":       "#3C64F0",
    "F.SilkS":    "#FFEB3C",
    "B.SilkS":    "#C8C864",
    "Edge.Cuts":  "#E641DC",
    "pad":        "#C8A03C",
    "hole":       "#1A1A1A",
    "bg":         "#0A0A0A",
    "via":        "#96A0A0",
}

SCALE = 12.0     # px per mm
MARGIN = 6.0     # mm


def rot(x: float, y: float, deg: float) -> tuple[float, float]:
    """KiCad's footprint rotation: +angle is counter-clockwise on a y-down board."""
    a = math.radians(deg)
    return (x * math.cos(a) + y * math.sin(a), -x * math.sin(a) + y * math.cos(a))


def plot(path: Path) -> tuple[str, tuple[float, float, float, float]]:
    text = path.read_text()
    body: list[str] = []
    xs: list[float] = []
    ys: list[float] = []

    def note(x: float, y: float) -> None:
        xs.append(x)
        ys.append(y)

    # ---- board outline -----------------------------------------------------------
    # NOTE the board is pretty-printed one key per line, so these patterns must span
    # newlines (re.S) and cannot rely on [^)]* to stay inside one element.  A single-line
    # pattern silently matches nothing and the outline just vanishes from the plot.
    for m in re.finditer(
        r'\(gr_line\s+\(start ([-\d.]+) ([-\d.]+)\)\s+\(end ([-\d.]+) ([-\d.]+)\)'
        r'.*?\(layer "Edge\.Cuts"\)', text, re.S
    ):
        x1, y1, x2, y2 = (float(v) for v in m.groups())
        note(x1, y1)
        note(x2, y2)
        body.append(f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" '
                    f'stroke="{COLORS["Edge.Cuts"]}" stroke-width="0.15"/>')

    # ---- copper zones ------------------------------------------------------------
    # Drawn first and translucent, so it reads as a plane behind the tracks.  Omitting zones
    # made the plot actively misleading: the whole B.Cu ground pour was invisible, so B.Cu
    # looked like four lonely lanes rather than a plane with lanes cut through it.
    for zm in re.finditer(r'\(zone\s+(.*?)\n\t\)', text, re.S):
        zb = zm.group(1)
        lm = re.search(r'\(layers? "([^"]+)"\)', zb)
        nm = re.search(r'\(net "([^"]*)"\)', zb)
        # Match the (pts ...) block by looking for the LAST (xy ...) before the closing
        # paren, not with a lazy (.*?)\)\s*\): that form swallows the final point's own
        # closing paren, so a 4-point rectangle silently became a 3-point triangle -- a zone
        # that looked like it covered half the board and spilled past the outline.
        pm = re.search(r'\(polygon\s+\(pts\s+((?:\(xy [-\d.]+ [-\d.]+\)\s*)+)', zb, re.S)
        if not (lm and pm):
            continue
        pts = re.findall(r'\(xy ([-\d.]+) ([-\d.]+)\)', pm.group(1))
        if len(pts) < 3:
            continue
        for px, py in pts:
            note(float(px), float(py))
        d = " ".join(f"{float(px):.3f},{float(py):.3f}" for px, py in pts)
        col = COLORS.get(lm.group(1), "#555")
        body.append(
            f'<polygon points="{d}" fill="{col}" fill-opacity="0.16" stroke="{col}" '
            f'stroke-opacity="0.5" stroke-width="0.12" stroke-dasharray="0.6 0.4">'
            f'<title>zone {nm.group(1) if nm else "?"} on {lm.group(1)}</title></polygon>')

    # ---- tracks: B.Cu first so F.Cu reads on top ---------------------------------
    tracks = list(re.finditer(
        r'\(segment\s+\(start ([-\d.]+) ([-\d.]+)\)\s+\(end ([-\d.]+) ([-\d.]+)\)'
        r'\s+\(width ([-\d.]+)\)\s+\(layer "([^"]+)"\).*?\(net "([^"]*)"\)',
        text, re.S))

    for layer in ("B.Cu", "F.Cu"):
        for m in tracks:
            x1, y1, x2, y2, w, lay, net = m.groups()
            if lay != layer:
                continue
            note(float(x1), float(y1))
            note(float(x2), float(y2))
            body.append(
                f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" '
                f'stroke="{COLORS.get(lay, "#888")}" stroke-width="{w}" '
                f'stroke-linecap="round" opacity="0.95"><title>{net} ({lay})</title></line>')

    # ---- vias --------------------------------------------------------------------
    for m in re.finditer(
        r'\(via\s+\(at ([-\d.]+) ([-\d.]+)\)\s+\(size ([\d.]+)\)\s+\(drill ([\d.]+)\)',
        text, re.S
    ):
        x, y, size, drill = (float(v) for v in m.groups())
        body.append(f'<circle cx="{x}" cy="{y}" r="{size / 2}" fill="{COLORS["via"]}"/>')
        body.append(f'<circle cx="{x}" cy="{y}" r="{drill / 2}" fill="{COLORS["hole"]}"/>')

    # ---- footprints: pads, holes, reference designators --------------------------
    for fm in re.finditer(r'\(footprint "[^"]+"(.*?)\n\t\)', text, re.S):
        fb = fm.group(1)
        tm = re.search(r'\(translate ([-\d.]+) ([-\d.]+)\)', fb)
        if not tm:
            continue
        ox, oy = float(tm.group(1)), float(tm.group(2))
        rm = re.search(r'\(rotate ([-\d.]+)\)', fb)
        frot = float(rm.group(1)) if rm else 0.0

        for chunk in re.split(r'(?=\(pad ")', fb)[1:]:
            am = re.search(r'\(at ([-\d.]+) ([-\d.]+)\s*([-\d.]*)\)', chunk, re.S)
            sm = re.search(r'\(size ([\d.]+) ([\d.]+)\)', chunk, re.S)
            if not am or not sm:
                continue
            lx, ly = float(am.group(1)), float(am.group(2))
            dx, dy = rot(lx, ly, frot)
            px, py = ox + dx, oy + dy
            pw, ph = float(sm.group(1)), float(sm.group(2))
            if (float(am.group(3) or 0) + frot) % 180 == 90:
                pw, ph = ph, pw
            note(px, py)
            circle = "circle" in chunk[:40] or "oval" in chunk[:40]
            if circle:
                body.append(f'<ellipse cx="{px}" cy="{py}" rx="{pw / 2}" ry="{ph / 2}" '
                            f'fill="{COLORS["pad"]}"/>')
            else:
                body.append(f'<rect x="{px - pw / 2}" y="{py - ph / 2}" width="{pw}" '
                            f'height="{ph}" fill="{COLORS["pad"]}" rx="0.1"/>')
            dm = re.search(r'\(drill ([\d.]+)\)', chunk)
            if dm:
                body.append(f'<circle cx="{px}" cy="{py}" r="{float(dm.group(1)) / 2}" '
                            f'fill="{COLORS["hole"]}"/>')

        pm = re.search(r'\(property "Reference" "([^"]+)"\s+\(at ([-\d.]+) ([-\d.]+)'
                       r'\s*([-\d.]*)\)', fb, re.S)
        if pm:
            ref = pm.group(1)
            dx, dy = rot(float(pm.group(2)), float(pm.group(3)), frot)
            tx, ty = ox + dx, oy + dy
            trot = (float(pm.group(4) or 0) + frot) % 360
            xf = f' transform="rotate({-trot} {tx} {ty})"' if trot % 180 else ""
            body.append(f'<text x="{tx}" y="{ty}" font-size="0.8" fill="{COLORS["F.SilkS"]}" '
                        f'text-anchor="middle" dominant-baseline="central" '
                        f'font-family="monospace"{xf}>{ref}</text>')

    x0, y0 = min(xs) - MARGIN, min(ys) - MARGIN
    x1, y1 = max(xs) + MARGIN, max(ys) + MARGIN
    w, h = x1 - x0, y1 - y0

    svg = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{w * SCALE:.0f}" '
        f'height="{h * SCALE:.0f}" viewBox="{x0:.2f} {y0:.2f} {w:.2f} {h:.2f}">',
        f'<rect x="{x0}" y="{y0}" width="{w}" height="{h}" fill="{COLORS["bg"]}"/>',
        *body,
        "</svg>",
    ]
    return "\n".join(svg), (x0, y0, w, h)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("board", nargs="?", default="teensy-sensor-suite.kicad_pcb")
    ap.add_argument("-o", "--out", default=None)
    ap.add_argument("--png", action="store_true")
    args = ap.parse_args()

    board = Path(args.board)
    if not board.exists():
        print(f"no such board: {board}", file=sys.stderr)
        return 2

    svg, (x0, y0, w, h) = plot(board)
    out = Path(args.out) if args.out else board.with_suffix(".svg")
    out.write_text(svg)
    print(f"{out}  ({w:.1f} x {h:.1f} mm incl. {MARGIN} mm margin)")

    if args.png:
        png = out.with_suffix(".png")
        if shutil.which("rsvg-convert"):
            subprocess.run(["rsvg-convert", "-o", str(png), str(out)], check=True)
            print(f"{png}  (rsvg-convert)")
        else:
            try:
                import cairosvg           # type: ignore[import-not-found]
                cairosvg.svg2png(url=str(out), write_to=str(png))
                print(f"{png}  (cairosvg)")
            except ImportError:
                print("no rasteriser (brew install librsvg) -- SVG only", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
