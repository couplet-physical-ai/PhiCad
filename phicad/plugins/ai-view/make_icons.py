#!/usr/bin/env python3
#
# This program source code file is part of PhiCad, a fork of KiCad.
#
# Copyright (C) 2026 Exowatt, Inc.
# Copyright The KiCad Developers, see AUTHORS.txt for contributors.
#
# SPDX-License-Identifier: GPL-2.0-or-later
#
"""Regenerate the toolbar icons (icons/ai-view-{24,48}-{light,dark}.png).

Two four-point "AI sparkles": a big one in the theme's ink colour and a small one in
accent orange.  Drawn 16x supersampled, LANCZOS-downsampled.  The PNGs are committed;
this script only exists so they are reproducible:

    build/venv/bin/python make_icons.py
"""

from __future__ import annotations

import os

from PIL import Image, ImageDraw

HERE = os.path.dirname(os.path.abspath(__file__))
SS = 16  # supersampling factor

INK = {"light": (58, 58, 58, 255), "dark": (232, 232, 232, 255)}
ORANGE = (247, 148, 29, 255)  # accent orange


def sparkle(cx: float, cy: float, r: float, waist: float = 0.20) -> list[tuple[float, float]]:
    """8-point polygon for a concave four-point star centred on (cx, cy)."""
    q = r * waist
    return [
        (cx, cy - r), (cx + q, cy - q), (cx + r, cy), (cx + q, cy + q),
        (cx, cy + r), (cx - q, cy + q), (cx - r, cy), (cx - q, cy - q),
    ]


def render(size: int, theme: str) -> Image.Image:
    big = size * SS
    img = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    u = big / 24.0  # design grid: 24 units

    draw.polygon(sparkle(10.4 * u, 13.2 * u, 8.6 * u), fill=INK[theme])
    draw.polygon(sparkle(18.2 * u, 5.6 * u, 4.6 * u), fill=ORANGE)

    return img.resize((size, size), Image.LANCZOS)


def render_new_project(size: int, theme: str) -> Image.Image:
    """New AI Project launcher tile: a bare board outline with the orange AI sparkle on it."""
    big = size * SS
    img = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    u = big / 24.0
    ink = INK[theme]

    # Board outline: rounded rectangle, stroke only.
    draw.rounded_rectangle([2.0 * u, 3.5 * u, 22.0 * u, 20.5 * u], radius=2.2 * u,
                           outline=ink, width=int(1.5 * u))
    # Two pads on the west edge (a connector), like the boards synth makes.
    for cy in (9.0, 15.0):
        draw.rectangle([4.4 * u, (cy - 1.1) * u, 7.0 * u, (cy + 1.1) * u], fill=ink)
    # The AI sparkle, in accent orange, sitting on the board.
    draw.polygon(sparkle(14.8 * u, 12.0 * u, 5.6 * u), fill=ORANGE)

    return img.resize((size, size), Image.LANCZOS)


def main() -> None:
    outdir = os.path.join(HERE, "icons")
    os.makedirs(outdir, exist_ok=True)
    for theme in ("light", "dark"):
        for size in (24, 48):
            path = os.path.join(outdir, f"ai-view-{size}-{theme}.png")
            render(size, theme).save(path)
            print(f"wrote {path}")
        # Launcher tiles render at 48 px (the bundle's default size is its smallest
        # bitmap, so only 48/96 are listed in the manifest for this action).
        for size in (48, 96):
            path = os.path.join(outdir, f"new-project-{size}-{theme}.png")
            render_new_project(size, theme).save(path)
            print(f"wrote {path}")


if __name__ == "__main__":
    main()
