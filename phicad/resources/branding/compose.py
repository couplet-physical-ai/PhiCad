#!/usr/bin/env python3
"""PhiCad identity composition engine.

Renders every PhiCad brand asset from two inputs: the PhiCad mark
(mark.png, supplied by the project; vector source phicad-mark.svg) and one
24x24 glyph SVG per tool (glyphs/*.svg). `regen-icons.py` drives it over the
whole icon set.

Requires: Pillow, rsvg-convert (brew install librsvg).

Copyright (C) 2026 Exowatt, Inc.  GPLv3+ (same terms as the KiCad tree).
"""

import argparse
import re
import shutil
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
PNG_DIR = REPO / "resources/bitmaps_png/png"

# Palette follows the PhiCad mark (mark.png: teal #0F766E with white).  Tool tiles are a
# darker teal so the suite mark stays distinct; glyph strokes (#F0FDFA) and the one accent
# per glyph (#2DD4BF) are set in glyphs/*.svg.  Documents are neutral paper with teal ink.
TILE = (0x13, 0x4E, 0x4A, 255)          # dark teal tile
PAPER = (0xF8, 0xFA, 0xFA, 255)
PAPER_EDGE = (0xCB, 0xD5, 0xD4, 255)
INK = (0x13, 0x4E, 0x4A, 255)
DEV_DOT = (255, 255, 255, 230)
SS = 4                                   # supersample factor

# Mark badge is dropped below this logical size in every badged mode (tool,
# doc): at 16/24/32 px the badge is smaller than the glyph's stroke weight and
# only muddies it, so the glyph grows instead.
BADGE_MIN_LOGICAL = 48

# Doc mode: the dark-ink glyph is the primary read, the mark badge is a
# secondary provenance mark.  It stays fully inside the paper (never breaks the
# silhouette or the folded corner) and is pushed back with a touch of alpha and
# desaturation so it cannot out-compete the ink.
DOC_BADGE_FRAC = 0.17                    # of canvas (was 0.26 -> overhung paper)
DOC_BADGE_INSET = 0.035                  # clear paper kept inside the corner
DOC_BADGE_ALPHA = 0.85
DOC_BADGE_DESAT = 0.20
DOC_GLYPH_FRAC = 0.38                    # ink glyph, badge present
DOC_GLYPH_FRAC_SMALL = 0.46              # badge dropped -> glyph grows
DOC_RINGS_FRAC = 0.34                    # project doc: the mark IS the glyph

_rings_master = None


def rings_master():
    """The PhiCad mark (mark.png), alpha-trimmed to its bounding box, square-padded."""
    global _rings_master
    if _rings_master is None:
        im = Image.open(HERE / "mark.png").convert("RGBA")
        bbox = im.getchannel("A").getbbox()
        im = im.crop(bbox)
        side = max(im.size)
        sq = Image.new("RGBA", (side, side), (0, 0, 0, 0))
        sq.paste(im, ((side - im.width) // 2, (side - im.height) // 2))
        _rings_master = sq
    return _rings_master


def rings(px):
    return rings_master().resize((px, px), Image.LANCZOS)


def render_svg(svg_path, px):
    """Render an SVG to a px*px RGBA image via rsvg-convert."""
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tf:
        out = tf.name
    subprocess.run(
        ["rsvg-convert", "-w", str(px), "-h", str(px), "-o", out, str(svg_path)],
        check=True, capture_output=True)
    im = Image.open(out).convert("RGBA")
    Path(out).unlink()
    return im


def recolor(im, rgba):
    """Repaint every non-transparent pixel with rgba, keeping alpha."""
    solid = Image.new("RGBA", im.size, rgba)
    solid.putalpha(im.getchannel("A"))
    return solid


def recede(im, alpha=1.0, desat=0.0):
    """Push a raster element back in the hierarchy: blend RGB toward its own
    greyscale by `desat`, then scale alpha by `alpha`."""
    out = im.copy()
    if desat > 0:
        rgb = out.convert("RGB")
        rgb = Image.blend(rgb, rgb.convert("L").convert("RGB"), desat)
        a = out.getchannel("A")
        out = rgb.convert("RGBA")
        out.putalpha(a)
    if alpha < 1.0:
        out.putalpha(out.getchannel("A").point(
            lambda v: int(v * alpha + 0.5)))
    return out


def rounded_tile(side_ss, radius_frac=0.225, color=TILE):
    im = Image.new("RGBA", (side_ss, side_ss), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.rounded_rectangle([0, 0, side_ss - 1, side_ss - 1],
                        radius=int(side_ss * radius_frac), fill=color)
    return im


def paste_center(base, im, cx, cy):
    base.alpha_composite(im, (int(cx - im.width / 2), int(cy - im.height / 2)))


def compose(mode, px, glyph=None, logical=None, tile_frac=1.0):
    """Render one canvas. mode: tool|rings|nightly|doc. glyph: svg path."""
    logical = logical or px
    c = px * SS
    canvas = Image.new("RGBA", (c, c), (0, 0, 0, 0))

    if mode == "doc":
        pw, ph = int(c * 0.72), int(c * 0.88)
        x0, y0 = (c - pw) // 2, (c - ph) // 2
        fold = int(pw * 0.28)
        d = ImageDraw.Draw(canvas)
        r = int(c * 0.045)
        d.rounded_rectangle([x0, y0, x0 + pw, y0 + ph], radius=r, fill=PAPER,
                            outline=PAPER_EDGE, width=max(1, c // 128))
        # folded corner (top-right)
        d.polygon([(x0 + pw - fold, y0), (x0 + pw, y0 + fold),
                   (x0 + pw - fold, y0 + fold)], fill=PAPER_EDGE)
        d.rectangle([x0 + pw - fold, y0, x0 + pw, y0 + fold // 3], fill=(0, 0, 0, 0))
        if glyph and Path(glyph).exists():
            badge = logical >= BADGE_MIN_LOGICAL
            gfrac = DOC_GLYPH_FRAC if badge else DOC_GLYPH_FRAC_SMALL
            g = recolor(render_svg(glyph, int(c * gfrac)), INK)
        else:
            badge = False       # project doc: mark centered, never badged
            g = rings(int(c * DOC_RINGS_FRAC))
        paste_center(canvas, g, c * 0.5, c * 0.52)
        if badge:
            bs = int(c * DOC_BADGE_FRAC)
            b = recede(rings(bs), DOC_BADGE_ALPHA, DOC_BADGE_DESAT)
            off = c * DOC_BADGE_INSET + bs / 2
            paste_center(canvas, b, x0 + pw - off, y0 + ph - off)
    else:
        t = int(c * tile_frac)
        toff = (c - t) // 2
        if mode not in ("rings", "nightly"):
            canvas.alpha_composite(rounded_tile(t), (toff, toff))
        cx = cy = c / 2
        if mode in ("rings", "nightly"):
            # The mark is itself a rounded tile: the suite icon IS the mark.
            paste_center(canvas, rings(t), cx, cy)
            if mode == "nightly":
                d = ImageDraw.Draw(canvas)
                ds = int(t * 0.16)
                mx = toff + t - int(t * 0.14) - ds // 2
                my = toff + int(t * 0.14) + ds // 2
                d.ellipse([mx - ds // 2, my - ds // 2, mx + ds // 2, my + ds // 2],
                          fill=DEV_DOT)
        else:  # tool
            badge = logical >= BADGE_MIN_LOGICAL
            gfrac = 0.58 if badge else 0.66
            g = render_svg(glyph, int(t * gfrac))
            paste_center(canvas, g, cx, cy)
            if badge:
                b = rings(int(t * 0.30))
                paste_center(canvas, b, toff + t - t * 0.17, toff + t - t * 0.17)

    return canvas.resize((px, px), Image.LANCZOS)


# ---------------------------------------------------------------- families

FNAME_RE_T = r"^{fam}(?:_(?P<a>\d+))?(?P<dark>_dark)?_(?P<b>\d+)\.png$"


def family_inventory(fam):
    """Existing PNG inventory for a family, from the checked-in tree."""
    rx = re.compile(FNAME_RE_T.format(fam=re.escape(fam)))
    out = []
    for p in sorted(PNG_DIR.glob(f"{fam}_*.png")):
        m = rx.match(p.name)
        if m:
            out.append((p, int(m["a"]) if m["a"] else int(m["b"]), int(m["b"])))
    return out


def cmd_family(fam, mode, glyph):
    inv = family_inventory(fam)
    if not inv:
        sys.exit(f"no existing PNGs match family '{fam}' in {PNG_DIR}")
    for path, logical, px in inv:
        im = compose(mode, px, glyph=glyph, logical=logical, tile_frac=1.0)
        im.save(path)
        print(f"  {path.name}  {px}px (logical {logical})")
    print(f"{fam}: {len(inv)} PNGs regenerated")


# ---------------------------------------------------------------- OS icons

ICNS_SIZES = [16, 32, 128, 256, 512]
ICO_SIZES = [16, 24, 32, 48, 64, 128, 256]


def cmd_icns(mode, glyph, out):
    with tempfile.TemporaryDirectory() as td:
        iconset = Path(td) / "phicad.iconset"
        iconset.mkdir()
        for s in ICNS_SIZES:
            compose(mode, s, glyph=glyph, logical=s, tile_frac=0.80).save(
                iconset / f"icon_{s}x{s}.png")
            compose(mode, s * 2, glyph=glyph, logical=s, tile_frac=0.80).save(
                iconset / f"icon_{s}x{s}@2x.png")
        subprocess.run(["iconutil", "-c", "icns", str(iconset), "-o", out],
                       check=True)
    print(f"icns: {out}")


def cmd_ico(mode, glyph, out):
    frames = [compose(mode, s, glyph=glyph, logical=s, tile_frac=0.80)
              for s in ICO_SIZES]
    frames[-1].save(out, format="ICO",
                    sizes=[(s, s) for s in ICO_SIZES],
                    append_images=frames[:-1])
    print(f"ico: {out}")


def cmd_png(mode, glyph, px, out, tile_frac=0.80):
    compose(mode, px, glyph=glyph, logical=px, tile_frac=tile_frac).save(out)
    print(f"png: {out}")


def cmd_xpm(mode, glyph, px, out, name):
    im = compose(mode, px, glyph=glyph, logical=px, tile_frac=0.80)
    rgb = im.convert("RGB").quantize(colors=60).convert("RGB")
    alpha = im.getchannel("A")
    pal, order = {}, []
    chars = ("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
             "0123456789#$%&*+-/<=>?@^_~")
    rows = []
    for y in range(im.height):
        row = ""
        for x in range(im.width):
            if alpha.getpixel((x, y)) < 128:
                key = None
            else:
                key = rgb.getpixel((x, y))
            if key not in pal:
                pal[key] = chars[len(pal)]
                order.append(key)
            row += pal[key]
        rows.append(row)
    lines = [f"/* XPM */", f"static char const *{name}[] = {{",
             f'"{im.width} {im.height} {len(pal)} 1",']
    for key in order:
        ch = pal[key]
        col = "None" if key is None else "#%02X%02X%02X" % key
        lines.append(f'"{ch} c {col}",')
    lines += [f'"{r}",' for r in rows]
    lines[-1] = lines[-1][:-1]  # drop trailing comma
    lines.append("};")
    Path(out).write_text("\n".join(lines) + "\n")
    print(f"xpm: {out} ({len(pal)} colors)")


def cmd_svg(mode, glyph, out):
    """Scalable SVG (hicolor/scalable): vector tile + embedded raster art."""
    import base64, io
    art = compose(mode, 512, glyph=glyph, logical=512, tile_frac=0.80)
    buf = io.BytesIO()
    art.save(buf, format="PNG")
    b64 = base64.b64encode(buf.getvalue()).decode()
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" '
           f'viewBox="0 0 512 512" width="512" height="512">\n'
           f'  <image width="512" height="512" xlink:href="data:image/png;base64,{b64}"/>\n'
           f'</svg>\n')
    Path(out).write_text(svg)
    print(f"svg: {out} ({len(svg)//1024}KB)")


def cmd_lint(glyph):
    txt = Path(glyph).read_text()
    errs = []
    if 'viewBox="0 0 24 24"' not in txt:
        errs.append('missing viewBox="0 0 24 24"')
    for bad in ("inkscape:", "sodipodi:", "<metadata"):
        if bad in txt:
            errs.append(f"editor metadata: {bad}")
    if len(txt) > 6000:
        errs.append(f"too big ({len(txt)}B > 6000B)")
    try:
        render_svg(glyph, 48)
    except subprocess.CalledProcessError as e:
        errs.append(f"rsvg-convert failed: {e.stderr.decode()[:200]}")
    if errs:
        print("LINT FAIL:", *errs, sep="\n  ")
        sys.exit(1)
    print(f"lint ok: {glyph}")


def cmd_sheet(out):
    """Contact sheet of every glyph in tool mode + suite marks, on both bgs."""
    glyphs = sorted(HERE.glob("glyphs/*.svg"))
    cells = [("rings", None, "PhiCad"), ("nightly", None, "nightly")]
    cells += [("tool", g, g.stem) for g in glyphs]
    cells += [("doc", None, "project doc")]
    px, pad = 96, 12
    cols = 7
    rows = -(-len(cells) // cols)
    W = cols * (px + pad) + pad
    H = rows * 2 * (px + pad) + pad
    sheet = Image.new("RGB", (W, H), (240, 240, 240))
    for i, (mode, g, _label) in enumerate(cells):
        im = compose(mode, px, glyph=g, logical=px, tile_frac=0.9)
        cx, cy = i % cols, i // cols
        x = pad + cx * (px + pad)
        sheet.paste(im, (x, pad + cy * 2 * (px + pad)), im)
        dark = Image.new("RGB", (px, px), (30, 30, 30))
        dark.paste(im, (0, 0), im)
        sheet.paste(dark, (x, pad + (cy * 2 + 1) * (px + pad)))
    sheet.save(out)
    print(f"sheet: {out} ({len(cells)} marks)")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("family", help="regenerate one bitmap-store PNG family")
    p.add_argument("fam"); p.add_argument("--mode", default="tool")
    p.add_argument("--glyph")

    p = sub.add_parser("icns"); p.add_argument("--mode", default="tool")
    p.add_argument("--glyph"); p.add_argument("out")

    p = sub.add_parser("ico"); p.add_argument("--mode", default="tool")
    p.add_argument("--glyph"); p.add_argument("out")

    p = sub.add_parser("png"); p.add_argument("--mode", default="tool")
    p.add_argument("--glyph"); p.add_argument("--px", type=int, default=256)
    p.add_argument("--tile-frac", type=float, default=0.80)
    p.add_argument("out")

    p = sub.add_parser("xpm"); p.add_argument("--mode", default="rings")
    p.add_argument("--glyph"); p.add_argument("--px", type=int, default=64)
    p.add_argument("--name", default="icon_kicad_xpm"); p.add_argument("out")

    p = sub.add_parser("svg"); p.add_argument("--mode", default="rings")
    p.add_argument("--glyph"); p.add_argument("out")

    p = sub.add_parser("lint-glyph"); p.add_argument("glyph")

    p = sub.add_parser("sheet"); p.add_argument("out")

    a = ap.parse_args()
    if a.cmd == "family":
        cmd_family(a.fam, a.mode, a.glyph)
    elif a.cmd == "icns":
        cmd_icns(a.mode, a.glyph, a.out)
    elif a.cmd == "ico":
        cmd_ico(a.mode, a.glyph, a.out)
    elif a.cmd == "png":
        cmd_png(a.mode, a.glyph, a.px, a.out, a.tile_frac)
    elif a.cmd == "xpm":
        cmd_xpm(a.mode, a.glyph, a.px, a.out, a.name)
    elif a.cmd == "svg":
        cmd_svg(a.mode, a.glyph, a.out)
    elif a.cmd == "lint-glyph":
        cmd_lint(a.glyph)
    elif a.cmd == "sheet":
        cmd_sheet(a.out)


if __name__ == "__main__":
    main()
