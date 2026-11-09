#!/usr/bin/env python3
# This program source code file is part of KiCad, a free EDA CAD application.
#
# Copyright (C) 2026 Exowatt, Inc.
# Copyright The KiCad Developers, see AUTHORS.txt for contributors.
#
# This program is free software: you can redistribute it and/or modify it
# under the terms of the GNU General Public License as published by the
# Free Software Foundation, either version 3 of the License, or (at your
# option) any later version.
#
# This program is distributed in the hope that it will be useful, but
# WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
# General Public License for more details.
#
# You should have received a copy of the GNU General Public License along
# with this program.  If not, see <http://www.gnu.org/licenses/>.
"""Footprint libraries as *data*: pads, courtyards, and a re-emittable body.

Why this exists
---------------
`demos/teensy-sensor-suite/generate_board.py` reads stock footprints with four separate
regexes (`pad_offsets`, `pad_dims`, `pad_sizes`, `load_mod`).  Regex over s-expressions is
fine until a pad carries a nested `(options …)` or a footprint puts `(size …)` on something
that is not a pad — then it silently returns the wrong number, and a wrong pad centre is a
track that misses its pad by half a millimetre with no error anywhere.

This module parses the file once with `phicad/pylib/sexp.py` and answers every question from
the parse tree, so "what is pad 3's centre" has exactly one implementation.

What a caller gets
------------------
    lib = Library.default()                       # stock + any project .pretty dirs
    fp  = lib.resolve("Resistor_SMD:R_0805_2012Metric")
    fp.pads["1"].x, fp.pads["1"].y                # local pad centre, mm
    fp.extent_at(rot=90)                          # courtyard bbox in BOARD axes
    fp.body                                       # inner s-expr text, ready to re-emit

Everything is local, unrotated footprint space; `place.py` and `emit.py` own board space.
Nothing here writes files.
"""

from __future__ import annotations

import math
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

# phicad/pylib is a sibling of phicad/synth; importing by path keeps this module runnable
# as `python3 phicad/synth/library.py` and as `from phicad.synth import library` alike.
_PYLIB = Path(__file__).resolve().parent.parent / "pylib"
if str(_PYLIB) not in sys.path:
    sys.path.insert(0, str(_PYLIB))

import sexp  # noqa: E402  (path set above)

# KiCad's stock footprint set as installed by this fork's app bundle.  A project may add
# its own `.pretty` directories (the parts pipeline writes one per project); those are
# passed to Library() explicitly rather than discovered, so a build is reproducible.
STOCK_DIR = Path(os.path.expanduser(
    "~/Library/Application Support/kicad/stock/footprints"))

# Properties the library file carries that a *board* must not inherit: we emit our own
# Reference/Value, and keeping the library's leaves a stray "REF**" on the silkscreen that
# DRC then reports as silk-over-copper.
_DROP_PROPERTIES = ("Reference", "Value", "Datasheet", "Description", "Footprint")

# Header keys that are meaningful in a .kicad_mod file and meaningless (or actively wrong)
# inside a board's `(footprint …)` node.
_DROP_HEADS = ("version", "generator", "generator_version")

# Top-level node names a `.kicad_mod` may legally carry.  `module` is the pre-6.0 token, and
# it is NOT merely historical: `easyeda2kicad` -- the fetcher behind `phicad/parts` -- still
# writes it today, so every footprint the exact-parts pipeline produces arrives as `module`.
# KiCad's own reader accepts both (`pcb_io_kicad_sexpr_parser.cpp:1111`, `case T_module:
# // legacy token`), so refusing it here made synth stricter than the tool that has to load
# the board afterwards -- the parts ledger could fetch a real part that synth then called
# "not in any library".  The node shape below the header is identical; only the token differs.
FOOTPRINT_HEADS = ("footprint", "module")


class LibraryError(RuntimeError):
    """A footprint could not be found, read, or understood."""


@dataclass
class Pad:
    """One pad, in local (unrotated) footprint coordinates."""

    number: str
    x: float
    y: float
    rot: float = 0.0
    w: float = 0.0
    h: float = 0.0
    shape: str = "rect"
    kind: str = "smd"              # smd | thru_hole | np_thru_hole | connect
    drill: Optional[float] = None
    layers: Tuple[str, ...] = ()

    @property
    def through(self) -> bool:
        return self.kind in ("thru_hole", "np_thru_hole")

    @property
    def plated(self) -> bool:
        """A pad that can carry a net.  Two kinds cannot.

        An `np_thru_hole` is a bare mechanical hole.  And a pad whose layer list contains no
        copper at all — `(layers "F.Paste")` — is a solder-paste stencil aperture rather than a
        pin: module and QFN footprints routinely subdivide one big thermal pad into several such
        apertures so the stencil deposits the right paste volume.  KiCad does not treat them as
        pins, and neither can we, or every part with a subdivided thermal pad (every ESP32
        module, most QFNs) fails the symbol/footprint pad comparison against a symbol that is
        perfectly correct.  An empty layer list is left alone, since absent is not the same as
        "explicitly no copper".
        """
        if self.kind == "np_thru_hole":
            return False
        if self.layers and not any(_is_copper_layer(layer) for layer in self.layers):
            return False
        return True

    def extent(self, fp_rot: float = 0.0) -> Tuple[float, float]:
        """(x_extent, y_extent) in BOARD axes once the part is rotated by `fp_rot`.

        A 90 degree rotation swaps the two; anything else is treated as its bounding box,
        which is conservative and therefore safe for clearance arithmetic.
        """
        total = (self.rot + fp_rot) % 180.0
        if abs(total) < 1e-9:
            return (self.w, self.h)
        if abs(total - 90.0) < 1e-9:
            return (self.h, self.w)
        r = math.radians(total)
        c, s = abs(math.cos(r)), abs(math.sin(r))
        return (self.w * c + self.h * s, self.w * s + self.h * c)


@dataclass
class Footprint:
    """A parsed library footprint: geometry as data, body as re-emittable text."""

    lib: str
    name: str
    path: Path
    pads: Dict[str, Pad] = field(default_factory=dict)
    courtyard: Optional[Tuple[float, float, float, float]] = None   # local bbox
    attrs: Tuple[str, ...] = ()
    body: str = ""                  # inner text, header/properties stripped
    # Local bboxes of the footprint's own silkscreen strokes, pen width included.  One box
    # per *segment*, not per item: a header's outline is a hollow rectangle whose bounding
    # box would declare the entire body occupied, and the body is where a label on a large
    # part belongs.
    silk: Tuple[Tuple[float, float, float, float], ...] = ()
    # Local bboxes of the footprint's own `(zone ... (keepout (tracks not_allowed)))` areas.
    # RF modules carry these around their PCB antenna, and they are not advisory: copper in
    # that region detunes the antenna, and KiCad's DRC reports a track there as
    # `items_not_allowed`.  The router has to treat them as hard obstacles or it will
    # cheerfully route the shortest path straight under the antenna.
    keepouts: Tuple[Tuple[float, float, float, float], ...] = ()

    @property
    def libid(self) -> str:
        return f"{self.lib}:{self.name}"

    @property
    def smd(self) -> bool:
        return "smd" in self.attrs

    def pad(self, number) -> Pad:
        key = str(number)
        if key not in self.pads:
            known = ", ".join(sorted(self.pads)) or "(none)"
            raise LibraryError(f"{self.libid} has no pad {key!r}; it has: {known}")
        return self.pads[key]

    def pad_bbox(self) -> Tuple[float, float, float, float]:
        """Bounding box of the pads alone — the fallback when there is no courtyard."""
        if not self.pads:
            return (0.0, 0.0, 0.0, 0.0)
        xs: List[float] = []
        ys: List[float] = []
        for p in self.pads.values():
            xs += [p.x - p.w / 2, p.x + p.w / 2]
            ys += [p.y - p.h / 2, p.y + p.h / 2]
        return (min(xs), min(ys), max(xs), max(ys))

    def extent_at(self, rot: float = 0.0) -> Tuple[float, float, float, float]:
        """Courtyard bbox rotated into board axes, as (minx, miny, maxx, maxy) offsets.

        Courtyard, not pad box: courtyard overlap is what KiCad's DRC actually tests
        (`courtyards_overlap`), so a placer that packs against the pad box produces boards
        that pass its own check and fail the real one.
        """
        box = self.courtyard or self.pad_bbox()
        return _rotate_box(box, rot)


def _rotate_box(box: Sequence[float], rot: float) -> Tuple[float, float, float, float]:
    """Rotate a local bbox into board axes and return its new bbox.

    Rotation sense matches the board format: local -x maps to +y at rot 90.  Verified in
    `generate_board.rotate()` against a board this fork wrote.
    """
    minx, miny, maxx, maxy = box
    if abs(rot % 360.0) < 1e-9:
        return (minx, miny, maxx, maxy)
    r = math.radians(rot)
    c, s = math.cos(r), math.sin(r)
    xs: List[float] = []
    ys: List[float] = []
    for x, y in ((minx, miny), (maxx, miny), (maxx, maxy), (minx, maxy)):
        xs.append(x * c + y * s)
        ys.append(-x * s + y * c)
    return (min(xs), min(ys), max(xs), max(ys))


def rotate_point(x: float, y: float, rot: float) -> Tuple[float, float]:
    """Rotate a footprint-local offset into board space."""
    if abs(rot % 360.0) < 1e-9:
        return (x, y)
    r = math.radians(rot)
    c, s = math.cos(r), math.sin(r)
    return (x * c + y * s, -x * s + y * c)


# ---------------------------------------------------------------------------------------
# parsing
# ---------------------------------------------------------------------------------------

def _floats(node, name: str, count: int) -> Optional[List[float]]:
    vals = sexp.floats(node, name)
    if vals is None or len(vals) < count:
        return None
    return vals[:count]


def _parse_pad(node) -> Optional[Pad]:
    args = sexp.atoms(node)
    if len(args) < 2:
        return None
    number = str(args[0])
    kind = str(args[1])
    shape = str(args[2]) if len(args) > 2 else "rect"

    at = sexp.floats(node, "at") or [0.0, 0.0]
    size = _floats(node, "size", 2) or [0.0, 0.0]

    drill = None
    dnode = sexp.child(node, "drill")
    if dnode is not None:
        dvals = [float(a) for a in sexp.atoms(dnode)
                 if _is_number(a)]
        if dvals:
            drill = max(dvals)

    layers: Tuple[str, ...] = ()
    lnode = sexp.child(node, "layers")
    if lnode is not None:
        layers = tuple(str(a) for a in sexp.atoms(lnode))

    return Pad(number=number, x=at[0], y=at[1],
               rot=at[2] if len(at) > 2 else 0.0,
               w=size[0], h=size[1], shape=shape, kind=kind,
               drill=drill, layers=layers)


def _is_copper_layer(layer: str) -> bool:
    """True for `F.Cu`, `B.Cu`, `In7.Cu` and the `*.Cu` wildcard; false for paste and mask."""
    return layer.endswith(".Cu")


def _is_number(text) -> bool:
    try:
        float(text)
    except (TypeError, ValueError):
        return False
    return True


def _keepout_boxes(doc) -> Tuple[Tuple[float, float, float, float], ...]:
    """Local bboxes of every zone in `doc` that forbids tracks.

    The bounding box rather than the polygon: a keepout is a region nothing may enter, so
    over-blocking is the safe direction, and the alternative is point-in-polygon tests in
    the router's inner loop for a handful of rectangles.
    """
    boxes: List[Tuple[float, float, float, float]] = []
    for node in doc:
        if not isinstance(node, list) or sexp.head(node) != "zone":
            continue
        keepout = sexp.child(node, "keepout")
        if keepout is None:
            continue
        # `(keepout (tracks not_allowed) (vias not_allowed) ...)` — only the track rule
        # concerns the router; a copper-pour keepout is the fill stage's business.
        blocks = False
        for rule in keepout:
            if not isinstance(rule, list) or sexp.head(rule) not in ("tracks", "vias"):
                continue
            if any(str(a) == "not_allowed" for a in sexp.atoms(rule)):
                blocks = True
        if not blocks:
            continue
        xs: List[float] = []
        ys: List[float] = []
        for poly in sexp.children(node, "polygon"):
            pts = sexp.child(poly, "pts")
            if pts is None:
                continue
            for xy in sexp.children(pts, "xy"):
                vals = [float(a) for a in sexp.atoms(xy) if _is_number(a)]
                if len(vals) >= 2:
                    xs.append(vals[0])
                    ys.append(vals[1])
        if xs and ys:
            boxes.append((min(xs), min(ys), max(xs), max(ys)))
    return tuple(boxes)


def _courtyard_box(doc) -> Optional[Tuple[float, float, float, float]]:
    """Bounding box of every F.CrtYd / B.CrtYd graphic in the footprint."""
    xs: List[float] = []
    ys: List[float] = []

    def on_courtyard(node) -> bool:
        layer = sexp.scalar(node, "layer")
        return bool(layer) and layer.endswith("CrtYd")

    for node in doc:
        if not isinstance(node, list):
            continue
        kind = sexp.head(node)
        if kind not in ("fp_line", "fp_rect", "fp_poly", "fp_circle", "fp_arc"):
            continue
        if not on_courtyard(node):
            continue

        if kind in ("fp_line", "fp_rect", "fp_arc"):
            for key in ("start", "end", "mid"):
                pt = _floats(node, key, 2)
                if pt:
                    xs.append(pt[0])
                    ys.append(pt[1])
        elif kind == "fp_circle":
            centre = _floats(node, "center", 2)
            edge = _floats(node, "end", 2)
            if centre and edge:
                r = math.hypot(edge[0] - centre[0], edge[1] - centre[1])
                xs += [centre[0] - r, centre[0] + r]
                ys += [centre[1] - r, centre[1] + r]
        elif kind == "fp_poly":
            pts = sexp.child(node, "pts")
            if pts is not None:
                for xy in sexp.children(pts, "xy"):
                    vals = [float(a) for a in sexp.atoms(xy) if _is_number(a)]
                    if len(vals) >= 2:
                        xs.append(vals[0])
                        ys.append(vals[1])

    if not xs or not ys:
        return None
    return (min(xs), min(ys), max(xs), max(ys))


def _stroke_half_width(node) -> float:
    """Half the pen width a graphic is drawn with; DRC tests the stroked shape, not the path."""
    stroke = sexp.child(node, "stroke")
    width = sexp.floats(stroke, "width") if stroke is not None else None
    if not width:
        width = sexp.floats(node, "width")          # pre-7.0 footprints put it at the top
    return (width[0] / 2.0) if width else 0.06


def _points(node, *keys: str) -> List[Tuple[float, float]]:
    out: List[Tuple[float, float]] = []
    for key in keys:
        pt = _floats(node, key, 2)
        if pt:
            out.append((pt[0], pt[1]))
    return out


def _silk_boxes(doc) -> Tuple[Tuple[float, float, float, float], ...]:
    """Bounding boxes of every silkscreen stroke, in local coordinates.

    Arcs and circles are reduced to the box around their defining points rather than to a
    swept outline.  That is exact for a full circle and slightly loose for the small corner
    fillets footprints actually use, and loose is the safe direction: the boxes exist to
    keep a reference designator off this geometry.
    """
    boxes: List[Tuple[float, float, float, float]] = []

    def add(pts: Sequence[Tuple[float, float]], pad: float) -> None:
        if not pts:
            return
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        boxes.append((min(xs) - pad, min(ys) - pad, max(xs) + pad, max(ys) + pad))

    for node in doc:
        if not isinstance(node, list):
            continue
        kind = sexp.head(node)
        layer = sexp.scalar(node, "layer")
        if not layer or not layer.endswith("SilkS"):
            continue
        half = _stroke_half_width(node)

        if kind == "fp_line":
            add(_points(node, "start", "end"), half)
        elif kind == "fp_rect":
            pts = _points(node, "start", "end")
            if len(pts) == 2:
                (x1, y1), (x2, y2) = pts
                # Four edges, so the hollow middle stays available.
                add([(x1, y1), (x2, y1)], half)
                add([(x2, y1), (x2, y2)], half)
                add([(x2, y2), (x1, y2)], half)
                add([(x1, y2), (x1, y1)], half)
        elif kind == "fp_arc":
            add(_points(node, "start", "mid", "end"), half)
        elif kind == "fp_circle":
            pts = _points(node, "center", "end")
            if len(pts) == 2:
                (cx, cy), (ex, ey) = pts
                r = math.hypot(ex - cx, ey - cy)
                add([(cx - r, cy - r), (cx + r, cy + r)], half)
        elif kind == "fp_poly":
            pts_node = sexp.child(node, "pts")
            if pts_node is None:
                continue
            pts = []
            for xy in sexp.children(pts_node, "xy"):
                vals = [float(a) for a in sexp.atoms(xy) if _is_number(a)]
                if len(vals) >= 2:
                    pts.append((vals[0], vals[1]))
            # One box for the whole polygon: silk polygons are filled markers (pin-1 dots,
            # polarity arrows), so the interior is ink too.
            add(pts, half)

    return tuple(boxes)


def _body_text(text: str) -> str:
    """The footprint's inner lines, with file-only headers and stock properties removed.

    Text surgery rather than a re-serialize on purpose: a round trip through any writer
    would drop tokens this fork understands and the parser does not, and a board that
    loses a `(zone_connect …)` or a custom pad primitive is corrupt in a way DRC will not
    report.  Copying the library's own bytes cannot lose anything.
    """
    body = text[text.index("\n") + 1:].rstrip()
    if body.endswith(")"):
        body = body[:-1].rstrip()

    keep: List[str] = []
    skip_depth: Optional[int] = None
    depth = 0

    for line in body.splitlines():
        stripped = line.strip()

        if skip_depth is None and stripped.startswith('(property "') and any(
            f'(property "{name}"' in stripped for name in _DROP_PROPERTIES
        ):
            skip_depth = depth

        if skip_depth is None and not stripped.startswith(
                tuple(f"({head} " for head in _DROP_HEADS)):
            keep.append(line)

        depth += line.count("(") - line.count(")")

        if skip_depth is not None and depth <= skip_depth:
            skip_depth = None

    return "\n".join(keep)


def footprint_from_node(doc, lib: str = "", name: str = "",
                        path: Optional[Path] = None, body: str = "") -> Footprint:
    """Build a Footprint from an already-parsed `(footprint …)` node.

    Split out of `parse_footprint` so a footprint *instance* lifted from a `.kicad_pcb`
    can go through exactly the same geometry code as a `.kicad_mod` from a library.  The
    two node shapes are identical below the header — same `(pad …)`, same `(fp_line …)`,
    same courtyard — and the alternative was a second copy of the pad, courtyard and silk
    readers that would drift from this one.

    `body` stays empty for a board instance: it is the re-emittable text `emit` splices
    into a new board, and an instance is already in one.
    """
    if sexp.head(doc) not in FOOTPRINT_HEADS:
        where = f"{path}: " if path else ""
        raise LibraryError(f"{where}not a footprint (top node is {sexp.head(doc)!r}, "
                           f"expected one of {', '.join(FOOTPRINT_HEADS)})")

    pads: Dict[str, Pad] = {}
    for node in sexp.children(doc, "pad"):
        pad = _parse_pad(node)
        if pad is None:
            continue
        # Duplicate pad numbers are legal (thermal pads, jumpers); the first wins for
        # geometry questions, which matches how the demo generator resolved them.
        pads.setdefault(pad.number, pad)

    attrs: Tuple[str, ...] = ()
    attr_node = sexp.child(doc, "attr")
    if attr_node is not None:
        attrs = tuple(str(a) for a in sexp.atoms(attr_node))

    return Footprint(
        lib=lib,
        name=name,
        path=path if path is not None else Path(),
        pads=pads,
        courtyard=_courtyard_box(doc),
        attrs=attrs,
        body=body,
        silk=_silk_boxes(doc),
        keepouts=_keepout_boxes(doc),
    )


def parse_footprint(path: Path, lib: str = "", name: str = "") -> Footprint:
    """Read one `.kicad_mod` into a Footprint."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise LibraryError(f"cannot read footprint {path}: {exc}") from None

    try:
        doc = sexp.parse(text)
    except ValueError as exc:
        raise LibraryError(f"{path} is not a parseable footprint: {exc}") from None

    return footprint_from_node(
        doc,
        lib=lib or path.parent.stem.replace(".pretty", ""),
        name=name or path.stem,
        path=path,
        body=_body_text(text),
    )


# ---------------------------------------------------------------------------------------
# the library index
# ---------------------------------------------------------------------------------------

class Library:
    """Resolves `Lib:Name` against a list of roots holding `*.pretty` directories."""

    def __init__(self, roots: Iterable[Path]):
        self.roots: List[Path] = [Path(r) for r in roots]
        self._cache: Dict[str, Footprint] = {}

    @classmethod
    def default(cls, extra: Optional[Iterable[Path]] = None) -> "Library":
        """Stock footprints, plus any project-local roots (searched first)."""
        roots = [Path(p) for p in (extra or [])]
        override = os.environ.get("PHICAD_FOOTPRINT_DIR")
        if override:
            # An explicit root is authoritative: a typo must not select another install.
            roots.append(Path(override).expanduser())
        elif sys.platform.startswith("linux"):
            roots.extend((Path("/usr/local/share/kicad/footprints"),
                          Path("/usr/share/kicad/footprints")))
        else:
            roots.append(STOCK_DIR)
        return cls(roots)

    def _find(self, lib: str, name: str) -> Path:
        for root in self.roots:
            candidate = root / f"{lib}.pretty" / f"{name}.kicad_mod"
            if candidate.is_file():
                return candidate
        searched = "\n  ".join(str(r / f"{lib}.pretty") for r in self.roots)
        raise LibraryError(
            f"footprint {lib}:{name} not found. Searched:\n  {searched}\n"
            "Set PHICAD_FOOTPRINT_DIR to a root containing Library.pretty directories.")

    def resolve(self, libid: str) -> Footprint:
        """`"Resistor_SMD:R_0805_2012Metric"` -> Footprint (cached)."""
        if libid in self._cache:
            return self._cache[libid]
        if ":" not in libid:
            raise LibraryError(
                f"footprint id {libid!r} must be 'Library:Name' "
                "(e.g. 'Resistor_SMD:R_0805_2012Metric')")
        lib, name = libid.split(":", 1)
        fp = parse_footprint(self._find(lib, name), lib=lib, name=name)
        self._cache[libid] = fp
        return fp

    def has(self, libid: str) -> bool:
        try:
            self.resolve(libid)
        except LibraryError:
            return False
        return True

    def suggest(self, fragment: str, limit: int = 12) -> List[str]:
        """Footprint ids resembling `fragment` — for "did you mean" errors.

        Progressively shortens the needle: a typo is usually in the tail
        ("R_0805_2012Metrics"), so an exact-substring search on the whole string finds
        nothing precisely when help is most wanted.  Six characters is the floor, below
        which the matches stop being suggestions and start being noise.
        """
        needle = fragment.strip().lower()
        while len(needle) >= 6:
            hits: List[str] = []
            for root in self.roots:
                if not root.is_dir():
                    continue
                for pretty in sorted(root.glob("*.pretty")):
                    lib = pretty.stem
                    for mod in sorted(pretty.glob("*.kicad_mod")):
                        if needle in mod.stem.lower() or needle in lib.lower():
                            hits.append(f"{lib}:{mod.stem}")
                            if len(hits) >= limit:
                                return hits
            if hits:
                return hits
            needle = needle[:-1]
        return []


if __name__ == "__main__":
    # Smoke test:  python3 phicad/synth/library.py Resistor_SMD:R_0805_2012Metric
    ids = sys.argv[1:] or ["Resistor_SMD:R_0805_2012Metric",
                           "Package_LGA:Bosch_LGA-8_2.5x2.5mm_P0.65mm_ClockwisePinNumbering"]
    library = Library.default()
    for libid in ids:
        f = library.resolve(libid)
        print(f"{f.libid}  pads={len(f.pads)}  attrs={f.attrs}  courtyard={f.courtyard}")
        for number in sorted(f.pads)[:8]:
            p = f.pads[number]
            print(f"    pad {number:>3}  at ({p.x:+.4f}, {p.y:+.4f})  "
                  f"size {p.w}x{p.h}  {p.kind}/{p.shape}  drill={p.drill}")
