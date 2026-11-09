#!/usr/bin/env python3
"""Regenerate the whole PhiCad icon set from mark.png + glyphs/*.svg via compose.py.

Every app, document and in-app icon PhiCad replaces in the KiCad tree is listed by the rules
below; each output keeps its existing pixel dimensions and container format. Run from any
directory:

    python3 phicad/resources/branding/regen-icons.py [--list]

Requires: Pillow, rsvg-convert (librsvg), and on macOS `iconutil` for the .icns files.

Copyright (C) 2026 Exowatt, Inc.  GPLv3+ (same terms as the KiCad tree).
"""

import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import compose as C  # noqa: E402

REPO = C.REPO
GLYPHS = HERE / "glyphs"

# Tool name (as used in each file name) -> recipe "mode[:glyph]".
TOOL = {
    "3d": "tool:3d", "bitmap2component": "tool:bitmap2component", "cvpcb": "tool:cvpcb",
    "eeschema": "tool:eeschema", "gerbview": "tool:gerbview", "libedit": "tool:libedit",
    "modedit": "tool:modedit", "fpedit": "tool:modedit", "pagelayout_editor": "tool:pagelayout_editor",
    "pcbcalculator": "tool:pcbcalculator", "pcb_calculator": "tool:pcbcalculator",
    "pcbnew": "tool:pcbnew", "pcm": "tool:pcm", "kicad": "rings", "kicad_nightly": "nightly",
}
# Document kind -> the tool glyph drawn on the paper (project = the mark itself).
DOC = {"footprint": "modedit", "pcb": "pcbnew", "project": None, "schematic": "eeschema",
       "symbol": "libedit", "worksheet": "pagelayout_editor"}
FAMILIES = {f"icon_{k}": v for k, v in TOOL.items() if k not in ("fpedit", "pcb_calculator")}
FAMILIES["project_kicad"] = "doc"
# <dir>/<tool>.icns -> recipe of its <tool>_doc.icns companion (None: no document icon).
ICNS = {
    "bitmap2component/bitmap2component": None, "cvpcb/cvpcb": "doc:cvpcb",
    "eeschema/eeschema": "doc:eeschema", "eeschema/libedit": "doc:libedit",
    "gerbview/gerbview": "doc:gerbview", "kicad/kicad": "doc",
    "pagelayout_editor/pagelayout_editor": "doc:pagelayout_editor",
    "pcb_calculator/pcb_calculator": None, "pcbnew/fpedit": "doc:modedit", "pcbnew/pcbnew": "doc:pcbnew",
}
HICOLOR = REPO / "resources/linux/icons/hicolor"
HICOLOR_NIGHTLY = REPO / "resources/linux/icons-nightly/hicolor"
APPS = ["bitmap2component", "eeschema", "gerbview", "kicad", "pcbcalculator", "pcbnew"]
HICOLOR_SIZES = [16, 24, 32, 48, 64, 128]


def recipe(r):
    mode, _, glyph = r.partition(":")
    return mode, (str(GLYPHS / f"{glyph}.svg") if glyph else None)


def doc(kind):
    g = DOC[kind]
    return f"doc:{g}" if g else "doc"


def targets():
    """(path, recipe, kind, px) for every generated asset."""
    out = []
    fam_rx = re.compile(r"^(?P<fam>.+?)(?:_(?P<a>\d+))?(?:_dark)?_(?P<b>\d+)\.png$")
    for p in sorted(C.PNG_DIR.glob("*.png")):
        m = fam_rx.match(p.name)
        if m and m["fam"] in FAMILIES:
            logical = int(m["a"]) if m["a"] else int(m["b"])
            out.append((p, FAMILIES[m["fam"]], "family", (int(m["b"]), logical)))
    for base, doc_recipe in ICNS.items():
        out.append((REPO / f"{base}.icns", TOOL[base.split("/")[1]], "icns", None))
        if doc_recipe:
            out.append((REPO / f"{base}_doc.icns", doc_recipe, "icns", None))
    out.append((REPO / "phicad/resources/macos/phicad.icns", "rings", "icns", None))
    icons = REPO / "resources/bitmaps_png/icons"
    for t in ("3d", "bitmap2component", "cvpcb", "eeschema", "gerbview", "kicad", "kicad_nightly",
              "pagelayout_editor", "pcbcalculator", "pcbnew", "pcm"):
        out.append((icons / f"icon_{t}.ico", TOOL[t], "ico", None))
    for k in DOC:
        out.append((icons / f"fileicon_{k}.ico", doc(k), "ico", None))
    out += [(icons / "icon_kicad.png", "rings", "png", 128), (icons / "icon_kicad_64.png", "rings", "png", 64),
            (icons / "icon_kicad.xpm", "rings", "xpm", 128), (icons / "icon_kicad_64.xpm", "rings", "xpm", 64)]
    for s in HICOLOR_SIZES:
        for a in APPS:
            out.append((HICOLOR / f"{s}x{s}/apps/{a}.png", TOOL[a], "png", s))
        for k in DOC:
            out.append((HICOLOR / f"{s}x{s}/mimetypes/application-x-kicad-{k}.png", doc(k), "png", s))
        out.append((HICOLOR_NIGHTLY / f"{s}x{s}/apps/kicad.png", "nightly", "png", s))
    for a in APPS:
        out.append((HICOLOR / f"scalable/apps/{a}.svg", TOOL[a], "svg", None))
    for k in DOC:
        for suffix in ("", "-16", "-24", "-32", "-48", "-64"):
            out.append((HICOLOR / f"scalable/mimetypes/application-x-kicad-{k}{suffix}.svg", doc(k), "svg", None))
    out.append((HICOLOR_NIGHTLY / "scalable/apps/kicad.svg", "nightly", "svg", None))
    docs = REPO / "phicad/docs-site/docs/assets"
    out += [(docs / "phicad-logo.png", "rings", "png", 512), (docs / "favicon.png", "rings", "png", 64)]
    return out


def main():
    items = targets()
    if "--list" in sys.argv:
        for p, r, kind, _ in items:
            print(f"{p.relative_to(REPO)}\t{r}\t{kind}")
        return 0
    for p, r, kind, arg in items:
        mode, glyph = recipe(r)
        p.parent.mkdir(parents=True, exist_ok=True)
        if kind == "family":
            px, logical = arg
            C.compose(mode, px, glyph=glyph, logical=logical, tile_frac=1.0).save(p)
        elif kind == "png":
            C.cmd_png(mode, glyph, arg, str(p))
        elif kind == "xpm":
            C.cmd_xpm(mode, glyph, arg, str(p), p.stem)
        elif kind == "ico":
            C.cmd_ico(mode, glyph, str(p))
        elif kind == "icns":
            C.cmd_icns(mode, glyph, str(p))
        elif kind == "svg":
            C.cmd_svg(mode, glyph, str(p))
    print(f"regenerated {len(items)} icon files from {HERE / 'mark.png'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
