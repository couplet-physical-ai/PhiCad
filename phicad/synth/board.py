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
"""A saved `.kicad_pcb` read back into the types `silk.solve()` reasons about.

Why this exists
---------------
`silk.py` solves reference-designator placement well — it took a 120-part generated board
to zero `silk_over_copper` / `silk_overlap` warnings — but it could only ever be pointed
at a board `synth` had just produced, because it asks for a `Design`, a `Layout`, a
`Library` and an `Outline`, and those are the *inputs* to generation.  Every board drawn
by a person or by some other script was out of reach, which is most boards.

Nothing in the solver actually needs the design intent.  Read against `silk._scene()`, it
wants exactly four things per part: where the footprint sits, how big its courtyard is,
where its pads and their mask apertures are, and where its own silkscreen is.  A
`.kicad_pcb` states all four, on every footprint, in the same s-expressions a `.kicad_mod`
uses.  So this module reads them straight off the board and hands the solver the shapes it
expects — no library lookup, no netlist, no design IR.

Reading the *instance* rather than the library entry is deliberate and is the only correct
choice: a board may carry a footprint that has since changed in the library, or one edited
in place, or one from a library this checkout cannot see (the r1 prototype's `private-lib:`
footprints are project-local).  The silkscreen has to clear the copper that will be
fabricated, which is the copper in the board file.

What the solver did not previously have to know
-----------------------------------------------
A generated board has no board-level graphics, and its footprints put `Value` on `F.Fab`.
An existing board has title text, fabrication notes, logos, and footprint text on the
silkscreen — all of it immovable, all of it something `silk_overlap` will report a label
for landing on.  `read()` collects it into `fixed_silk`, which `silk._scene()` now takes.
`pad_to_mask_clearance` is read off the board for the same reason: it is 0.05 on a `synth`
board and 0 on the r1 prototype, and it decides how far a mask aperture reaches.

What this cannot fix, and says so
---------------------------------
Moving a reference designator can only clear a violation that a reference designator is
part of.  Two footprints whose *outlines* overlap, a footprint whose own silk crosses its
own pad, board text over a test point — those are placement and library problems, and
`unfixable()` lists them rather than letting a caller read "still 21 warnings" as failure.

    python3 phicad/synth/board.py board.kicad_pcb            # report, change nothing
    python3 phicad/synth/board.py board.kicad_pcb --apply    # rewrite the references
"""

from __future__ import annotations

import argparse
import math
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))
_PYLIB = _HERE.parent / "pylib"
if str(_PYLIB) not in sys.path:
    sys.path.insert(0, str(_PYLIB))

import sexp  # noqa: E402
import silk as silk_mod  # noqa: E402
from library import Footprint, footprint_from_node  # noqa: E402
from place import Layout, Outline, Placed  # noqa: E402

Box = Tuple[float, float, float, float]

# KiCad's default when a board's `(setup)` omits it.  Named rather than inlined because
# getting it wrong is invisible: the labels simply come out 0.05 mm too brave.
DEFAULT_PAD_TO_MASK = 0.0

# Reference text height to aim for when a board's own references disagree.  KiCad's own
# default for a new footprint field, and what every footprint on the r1 prototype uses.
DEFAULT_TEXT_SIZE = 1.0

# Silkscreen layer names, in both the long and short spellings board files use.
_SILK_LAYERS = {"F.SilkS": "F", "B.SilkS": "B",
                "F.Silkscreen": "F", "B.Silkscreen": "B"}

# Graphics whose bounding box is worth taking as a silkscreen obstacle.  `gr_text` is here
# too but is measured through the stroke font instead of its own coordinates.
_GRAPHIC_HEADS = ("gr_line", "gr_rect", "gr_circle", "gr_arc", "gr_poly", "gr_curve")


class BoardError(RuntimeError):
    """The board file could not be read, or holds nothing to solve."""


# ---------------------------------------------------------------------------------------
# the shims silk.solve() type-hints for
# ---------------------------------------------------------------------------------------

@dataclass
class _InstancePart:
    """Stands in for `ir.Part`.  The solver reads exactly one field off it."""

    footprint: str


@dataclass
class _InstanceDesign:
    """Stands in for `ir.Design` — `parts` for the solver, the rest for `title_label()`.

    Not an `ir.Design`: that type is a *specification* with nets, rules and placement
    constraints, and building a fake one out of a board would be inventing intent the
    board does not record.  This carries what the solver reads and nothing else, so a
    future field the solver starts using fails loudly here instead of quietly reading a
    default somebody invented.
    """

    parts: Dict[str, _InstancePart] = field(default_factory=dict)
    project: str = ""
    title: str = ""
    rev: str = ""


class _InstanceLibrary:
    """Stands in for `Library`, resolving the ids `_InstanceDesign` hands out.

    Ids are `@board:<ref>` rather than the footprint's real `Lib:Name`, because two parts
    can share a library footprint and still need separate geometry here: one may be
    rotated, and (on a hand-edited board) one may have had a pad moved.  The `@board:`
    prefix cannot collide with a real library id, so a lookup that reaches the wrong
    resolver raises instead of silently returning a similarly-named library footprint.
    """

    def __init__(self, footprints: Dict[str, Footprint]):
        self._by_id = footprints

    def resolve(self, libid: str) -> Footprint:
        try:
            return self._by_id[libid]
        except KeyError:
            raise BoardError(f"no footprint instance {libid!r} on this board") from None


@dataclass
class BoardModel:
    """One board, in the shape `silk.solve()` takes.

    `notes` records every place the board said something this model could only
    approximate.  It is not diagnostics for its own sake: a caller that reports "0
    warnings" while a note says the outline was reduced to its bounding box has reported
    the wrong thing.
    """

    path: Path
    design: _InstanceDesign
    layout: Layout
    library: _InstanceLibrary
    outline: Outline
    fixed_silk: Dict[str, List[Box]]
    mask_expansion: float = DEFAULT_PAD_TO_MASK
    text_size: float = DEFAULT_TEXT_SIZE
    notes: List[str] = field(default_factory=list)
    # Refs whose reference field is hidden: not solved, and not an obstacle either, since
    # KiCad does not plot them and DRC does not test them.
    hidden: Tuple[str, ...] = ()

    def solve_silk(self) -> Dict[str, silk_mod.RefLabel]:
        """Run the solver against this board with the board's own parameters."""
        return silk_mod.solve(self.design, self.layout, self.library, self.outline,
                              text_size=self.text_size,
                              mask_expansion=self.mask_expansion,
                              fixed_silk=self.fixed_silk)


# ---------------------------------------------------------------------------------------
# reading
# ---------------------------------------------------------------------------------------

def _first_float(node, name: str, count: int) -> Optional[List[float]]:
    vals = sexp.floats(node, name)
    if vals is None or len(vals) < count:
        return None
    return vals[:count]


def _layer_side(node) -> Optional[str]:
    """'F' / 'B' if this node sits on a silkscreen layer, else None."""
    layer = sexp.scalar(node, "layer")
    if layer:
        return _SILK_LAYERS.get(layer)
    layers = sexp.child(node, "layers")
    if layers is not None:
        for atom in sexp.atoms(layers):
            side = _SILK_LAYERS.get(str(atom))
            if side:
                return side
    return None


def _is_hidden(node) -> bool:
    """`(hide yes)` in v8+, bare `(hide)` in older files."""
    h = sexp.child(node, "hide")
    if h is None:
        return False
    args = sexp.atoms(h)
    return not args or str(args[0]) in ("yes", "true")


def _stroke_half(node) -> float:
    stroke = sexp.child(node, "stroke")
    width = sexp.floats(stroke, "width") if stroke is not None else None
    if not width:
        width = sexp.floats(node, "width")
    return (width[0] / 2.0) if width else 0.06


def _graphic_points(node) -> List[Tuple[float, float]]:
    """Every defining point of a board graphic, in board coordinates.

    Arcs and circles reduce to the box around their defining points: exact for a full
    circle, loose for a fillet, and loose is the safe direction for an obstacle.
    """
    pts: List[Tuple[float, float]] = []
    kind = sexp.head(node)

    if kind == "gr_circle":
        centre = _first_float(node, "center", 2)
        edge = _first_float(node, "end", 2)
        if centre and edge:
            r = math.hypot(edge[0] - centre[0], edge[1] - centre[1])
            return [(centre[0] - r, centre[1] - r), (centre[0] + r, centre[1] + r)]
        return []

    for key in ("start", "mid", "end", "center"):
        pt = _first_float(node, key, 2)
        if pt:
            pts.append((pt[0], pt[1]))

    pts_node = sexp.child(node, "pts")
    if pts_node is not None:
        for xy in sexp.children(pts_node, "xy"):
            vals = [float(a) for a in sexp.atoms(xy) if _is_number(a)]
            if len(vals) >= 2:
                pts.append((vals[0], vals[1]))
    return pts


def _is_number(text) -> bool:
    try:
        float(text)
    except (TypeError, ValueError):
        return False
    return True


def _text_box(node, text: str, origin: Tuple[float, float] = (0.0, 0.0),
              parent_rot: float = 0.0) -> Optional[Box]:
    """Where a text item's ink lands, in board coordinates.

    Sized through `silk.text_extents()` — the same stroke-font metrics the solver sizes
    its own labels with, so an existing label and a solved one are measured by one ruler.
    A rotated text is taken as its bounding box, which for the 90 degree case every board
    actually uses is exact.
    """
    at = sexp.floats(node, "at") or []
    if len(at) < 2:
        return None
    x, y = at[0], at[1]
    rot = (at[2] if len(at) > 2 else 0.0) + parent_rot

    effects = sexp.child(node, "effects")
    font = sexp.child(effects, "font") if effects is not None else None
    size = _first_float(font, "size", 2) if font is not None else None
    height = size[1] if size else DEFAULT_TEXT_SIZE
    thick = sexp.floats(font, "thickness") if font is not None else None
    thickness = thick[0] if thick else silk_mod.stroke_thickness(height)

    w, h = silk_mod.text_extents(text, height, thickness)
    if w <= 0.0 or h <= 0.0:
        return None

    # A footprint-local text rides its parent's transform; a board-level one does not.
    if origin != (0.0, 0.0) or parent_rot:
        from library import rotate_point                          # noqa: PLC0415
        lx, ly = rotate_point(x, y, parent_rot)
        x, y = origin[0] + lx, origin[1] + ly

    if abs(rot % 180.0) > 1e-9:
        r = math.radians(rot)
        c, s = abs(math.cos(r)), abs(math.sin(r))
        w, h = w * c + h * s, w * s + h * c

    return (x - w / 2.0, y - h / 2.0, x + w / 2.0, y + h / 2.0)


def _outline(doc, notes: List[str]) -> Outline:
    """The board edge, as the rectangle `silk.Outline` models.

    KiCad allows any closed set of segments and arcs; `Outline` is a rectangle with an
    optional chamfer, and reconciling the two is not this module's job.  So the outline
    here is the *bounding box* of `Edge.Cuts`, which is exact for the rectangular boards
    this is aimed at and optimistic for anything else — a label could then sit inside the
    box but outside a cut-out.  When the edge is not a plain rectangle that goes in
    `notes`, because a caller reporting "no violations" needs to know the containment test
    was the weaker one.
    """
    xs: List[float] = []
    ys: List[float] = []
    shapes = 0

    def collect(nodes) -> None:
        nonlocal shapes
        for node in nodes:
            if not isinstance(node, list):
                continue
            if sexp.scalar(node, "layer") != "Edge.Cuts":
                continue
            pts = _graphic_points(node)
            if not pts:
                continue
            shapes += 1
            if sexp.head(node) in ("gr_rect", "fp_rect"):
                shapes += 3         # a rect is four edges; count it as such
            for px, py in pts:
                xs.append(px)
                ys.append(py)

    collect(doc)
    for fp in sexp.children(doc, "footprint"):
        collect(fp)                 # board edges drawn inside a footprint (card edges)

    if not xs or not ys:
        raise BoardError("board has nothing on Edge.Cuts; cannot bound the silkscreen")

    if shapes != 4:
        notes.append(
            f"Edge.Cuts is {shapes} segment(s), not a 4-sided rectangle; the outline was "
            "reduced to its bounding box, so labels are only checked against that box")

    return Outline(left=min(xs), top=min(ys), right=max(xs), bottom=max(ys))


def _placed_from(fp, ref: str, footprint: Footprint) -> Placed:
    """A board footprint node as a `Placed`, matching `place.solve()`'s conventions.

    The courtyard offsets are the ones `place.py` computes: half-extents of the rotated
    courtyard box, plus the offset from the footprint origin to that box's centre.  A
    footprint origin is rarely its courtyard centre, and the solver hangs candidate labels
    off the courtyard, so getting this wrong moves every label on the part.
    """
    at = sexp.floats(fp, "at") or [0.0, 0.0]
    x, y = at[0], at[1]
    rot = at[2] if len(at) > 2 else 0.0
    layer = sexp.scalar(fp, "layer") or "F.Cu"
    side = "back" if layer.startswith("B.") else "front"

    minx, miny, maxx, maxy = footprint.extent_at(rot)
    return Placed(ref=ref, x=x, y=y, rot=rot, side=side,
                  half_w=(maxx - minx) / 2.0, half_h=(maxy - miny) / 2.0,
                  cx_off=(minx + maxx) / 2.0, cy_off=(miny + maxy) / 2.0,
                  locked=True)


def _absolute_pad_angles(footprint: Footprint, fp_rot: float) -> None:
    """Rewrite each pad's angle so it survives `Pad.extent(fp_rot)` unchanged.

    A pad's `(at x y angle)` angle in a BOARD file is board-absolute: KiCad writes the
    footprint's own rotation into every pad when a footprint is rotated (checked across
    `demos/`: `video.kicad_pcb` C3 at 270 has both pads at 270, `cm5_minima` U405 at 180
    has all five at 180).  A pad's *position* is still footprint-local and does rotate.
    `Pad.extent()` composes `pad.rot + fp_rot`, which is right for a `.kicad_mod` being
    instantiated and wrong here — it would rotate the shape a second time.

    Measured rather than assumed.  On a board with a footprint at 90 degrees whose pad is
    `(at 0 0) (size 1 1.45)` with no angle, a probe track 0.75 mm from the pad centre in
    board Y draws `clearance … actual 0.0250 mm`: the pad reaches 0.725, so the 1.45 mm
    dimension stayed on Y and the shape did not rotate.  The same probe in board X reports
    nothing, so the 1.0 mm dimension stayed on X.  Both hold at board file version
    20260206 and 20260831.

    Subtracting `fp_rot` here means `extent(fp_rot)` recomposes exactly the file's angle,
    for a pad that carries one and for a pad that does not.
    """
    if abs(fp_rot % 360.0) < 1e-9:
        return
    for pad in footprint.pads.values():
        pad.rot = pad.rot - fp_rot


def _fallback_box(footprint: Footprint) -> Optional[Box]:
    """A stand-in courtyard for a footprint that declares none.

    `Footprint.extent_at()` already falls back to the pad box; this covers the case with
    no pads either (fiducials, logos, a mounting hole drawn as graphics), where the pad
    box is the degenerate (0,0,0,0) and every candidate would be scored against a point.
    """
    if footprint.pads:
        return None
    if footprint.silk:
        xs = [v for b in footprint.silk for v in (b[0], b[2])]
        ys = [v for b in footprint.silk for v in (b[1], b[3])]
        return (min(xs), min(ys), max(xs), max(ys))
    return None


def read(board_path) -> BoardModel:
    """Read a `.kicad_pcb` into everything `silk.solve()` needs."""
    path = Path(board_path)
    try:
        doc = sexp.parse_file(path)
    except (OSError, ValueError) as exc:
        raise BoardError(f"cannot read {path}: {exc}") from None
    if sexp.head(doc) != "kicad_pcb":
        raise BoardError(f"{path} is not a board (top node is {sexp.head(doc)!r})")

    notes: List[str] = []
    setup = sexp.child(doc, "setup")
    mask = sexp.floats(setup, "pad_to_mask_clearance") if setup is not None else None
    mask_expansion = mask[0] if mask else DEFAULT_PAD_TO_MASK

    design = _InstanceDesign()
    layout = Layout(outline=None)
    footprints: Dict[str, Footprint] = {}
    fixed: Dict[str, List[Box]] = {"F": [], "B": []}
    hidden: List[str] = []
    sizes: List[float] = []
    seen: Dict[str, int] = {}

    for fp in sexp.children(doc, "footprint"):
        ref = sexp.fp_property(fp, "Reference")
        if not ref:
            notes.append("a footprint has no Reference field; skipped")
            continue
        seen[ref] = seen.get(ref, 0) + 1
        if seen[ref] > 1:
            # Two footprints with one refdes is a broken board, and `set_references()`
            # rewrites by first match, so solving the second would move the first.
            notes.append(f"{ref} appears {seen[ref]} times; only the first is solved")
            continue

        libid = sexp.atoms(fp)[0] if sexp.atoms(fp) else ""
        footprint = footprint_from_node(fp, lib="@board", name=ref, path=path)
        fp_at = sexp.floats(fp, "at") or [0.0, 0.0]
        _absolute_pad_angles(footprint, fp_at[2] if len(fp_at) > 2 else 0.0)
        fallback = _fallback_box(footprint)
        if fallback is not None:
            footprint.courtyard = fallback
            notes.append(f"{ref} ({libid}) has no courtyard and no pads; "
                         "its own silkscreen was used as the body")

        placed = _placed_from(fp, ref, footprint)
        _collect_footprint_silk(fp, placed, fixed, sizes, hidden, ref, notes)

        key = f"@board:{ref}"
        footprints[key] = footprint
        design.parts[ref] = _InstancePart(footprint=key)
        layout.parts[ref] = placed

    for ref in hidden:
        # A hidden reference is not plotted, so it is neither solvable nor an obstacle.
        design.parts.pop(ref, None)
        layout.parts.pop(ref, None)

    if not layout.parts:
        raise BoardError(f"{path} has no footprint with a visible reference designator")

    _collect_board_silk(doc, fixed)

    outline = _outline(doc, notes)
    layout.outline = outline

    title_block = sexp.child(doc, "title_block")
    design.title = (sexp.scalar(title_block, "title") or "") if title_block else ""
    design.rev = (sexp.scalar(title_block, "rev") or "") if title_block else ""
    design.project = path.stem

    text_size = max(sizes) if sizes else DEFAULT_TEXT_SIZE
    return BoardModel(path=path, design=design, layout=layout,
                      library=_InstanceLibrary(footprints), outline=outline,
                      fixed_silk=fixed, mask_expansion=mask_expansion,
                      text_size=text_size, notes=notes, hidden=tuple(hidden))


def _collect_footprint_silk(fp, placed: Placed, fixed: Dict[str, List[Box]],
                            sizes: List[float], hidden: List[str], ref: str,
                            notes: List[str]) -> None:
    """Footprint text on the silkscreen: the reference's size, everything else as fixed.

    The reference field itself is deliberately NOT an obstacle — the solver is about to
    replace it, and leaving it in the scene would make every part refuse the position it
    already has.  `Value`, user text and vendor fields are obstacles: nothing here moves
    them.
    """
    for node in list(sexp.children(fp, "property")) + list(sexp.children(fp, "fp_text")):
        args = sexp.atoms(node)
        if len(args) < 2:
            continue
        key, text = str(args[0]), str(args[1])
        is_reference = (sexp.head(node) == "property" and key == "Reference") or \
                       (sexp.head(node) == "fp_text" and key == "reference")

        if _is_hidden(node):
            if is_reference:
                hidden.append(ref)
            continue

        side = _layer_side(node)
        if side is None:
            continue

        if is_reference:
            effects = sexp.child(node, "effects")
            font = sexp.child(effects, "font") if effects is not None else None
            size = _first_float(font, "size", 2) if font is not None else None
            if size:
                sizes.append(size[1])
            continue

        box = _text_box(node, text, origin=(placed.x, placed.y), parent_rot=placed.rot)
        if box is not None:
            fixed[side].append(box)


def _collect_board_silk(doc, fixed: Dict[str, List[Box]]) -> None:
    """Board-level silkscreen: graphics and text that belong to no footprint."""
    for node in doc:
        if not isinstance(node, list):
            continue
        kind = sexp.head(node)
        side = _layer_side(node)
        if side is None:
            continue

        if kind == "gr_text":
            args = sexp.atoms(node)
            if not args:
                continue
            box = _text_box(node, str(args[0]))
            if box is not None:
                fixed[side].append(box)
        elif kind in _GRAPHIC_HEADS:
            pts = _graphic_points(node)
            if not pts:
                continue
            half = _stroke_half(node)
            xs = [p[0] for p in pts]
            ys = [p[1] for p in pts]
            fixed[side].append((min(xs) - half, min(ys) - half,
                                max(xs) + half, max(ys) + half))


# ---------------------------------------------------------------------------------------
# reporting
# ---------------------------------------------------------------------------------------

def unfixable(violations: Sequence[dict]) -> List[dict]:
    """The silk violations no reference-designator move can clear.

    A violation is fixable here only if a reference field is one of its two items; DRC
    names them "Reference field of R1".  Everything else — two footprint outlines
    overlapping, a footprint's own silk over its own pad, board text over a test point —
    is a placement or library problem, and reporting it as a leftover of this pass would
    be blaming the wrong thing.
    """
    out: List[dict] = []
    for v in violations:
        if v.get("type") not in ("silk_over_copper", "silk_overlap"):
            continue
        items = v.get("items") or []
        if any("Reference field" in (i.get("description") or "") for i in items):
            continue
        out.append(v)
    return out


def report(model: BoardModel, labels: Dict[str, silk_mod.RefLabel]) -> List[str]:
    """Human-readable lines: what moved, what the solver could not place cleanly."""
    lines = [f"{model.path.name}: {len(labels)} reference designators solved"]
    if model.hidden:
        lines.append(f"  hidden references, left alone: {', '.join(sorted(model.hidden))}")
    dirty = sorted(ref for ref, label in labels.items() if not label.clean)
    if dirty:
        lines.append(f"  no clear slot found for {len(dirty)}: {', '.join(dirty)}")
    else:
        lines.append("  every label found a slot the solver scores as clear")
    shrunk = sorted(ref for ref, label in labels.items()
                    if label.size < model.text_size - 1e-9)
    if shrunk:
        lines.append(f"  shrunk to fit ({len(shrunk)}): {', '.join(shrunk)}")
    for note in model.notes:
        lines.append(f"  note: {note}")
    return lines


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Solve reference-designator placement on an existing board.")
    parser.add_argument("board", help="path to a .kicad_pcb")
    parser.add_argument("--apply", action="store_true",
                        help="rewrite the board's reference positions in place")
    parser.add_argument("--verbose", action="store_true",
                        help="print every solved label")
    args = parser.parse_args(argv)

    try:
        model = read(args.board)
    except BoardError as exc:
        print(f"board: {exc}", file=sys.stderr)
        return 2

    labels = model.solve_silk()
    for line in report(model, labels):
        print(line)

    if args.verbose:
        for ref, label in sorted(labels.items()):
            print(f"  {ref:<6} local ({label.x:+7.3f}, {label.y:+7.3f}) "
                  f"size {label.size}  {label.slot:<2}  cost {label.cost:.1f}")

    if args.apply:
        import emit                                              # noqa: PLC0415
        changed = emit.set_references(model.path, labels)
        print(f"  rewrote {changed} reference position(s) in {model.path}")

    return 1 if any(not label.clean for label in labels.values()) else 0


if __name__ == "__main__":
    sys.exit(main())
