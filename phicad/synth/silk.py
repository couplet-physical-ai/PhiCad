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
"""Where each reference designator goes — solved against the board, not typed into a table.

What this replaces
------------------
`demos/teensy-sensor-suite/generate_board.py` carries a `REF_POS` table of 17 hand-found
offsets, and the comment above it is the argument for this module:

    # A constant (0 -2.2) put every refdes 2.2 mm above its own origin, which on this
    # density lands on a neighbour's pads or on another refdes -- that was the whole
    # silk_over_copper / silk_overlap warning population.

The entries were found one DRC round trip at a time, and one of them needed 11 lines of
prose to explain a 0.11 mm feasible window.  None of that is design intent; it is the
arithmetic of "this rectangle must miss those rectangles", which is what this module does.

What DRC actually tests
-----------------------
Measured against this fork rather than assumed (probe boards under `synth/`'s scratch
runs), a silkscreen item collides with:

* **solder-mask apertures** — every pad grown by the board's `pad_to_mask_clearance`
  (`project.py` writes 0.05).  This is `silk_over_copper`, and it is the mask opening, not
  the copper, so a label that clears the pad by 0.04 mm still fails.
* **other silkscreen** — footprint outlines, other reference designators, board text.
  This is `silk_overlap`, and it is *not* exempt within one footprint: a label over its own
  part's silk counts.
* **the board edge** — but only when it *crosses* it.  Silk entirely off the board is
  reported by nothing (the check is `#if 0`'d in `drc_test_provider_edge_clearance.cpp`),
  so "no violation" is not the same as "on the board" and this module requires the
  stronger property.

`F.Cu`, `F.Fab` and `F.CrtYd` are in the test provider's layer-pair list but produce no
violation; they are ignored here, and a courtyard overlap is only ever a soft penalty
because a label over a neighbour's body is hard to read, not illegal.

Determinism
-----------
Candidates are enumerated in a fixed order and scored by a fixed cost; ties go to the
earlier candidate.  Same design, same board, and `git diff` shows only real changes.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Sequence, Tuple

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from ir import Design  # noqa: E402
from library import Library, rotate_point  # noqa: E402
from place import Layout, Outline, Placed  # noqa: E402

Box = Tuple[float, float, float, float]

# Must match `project.py`'s `(pad_to_mask_clearance …)`: the mask aperture is the pad grown
# by this, and the mask aperture is what `silk_over_copper` compares against.
MASK_EXPANSION = 0.05

# Our own margin on top of DRC's threshold, which the project sets to 0 (`min_silk_clearance`)
# and which therefore fires only on true intersection.  Two reasons not to sit on zero: the
# boxes here bound the glyph *cells* while DRC tests the inked strokes, so equality would be
# decided by rounding into board nanometres; and a label one micron off a pad reads as a
# mistake even when it passes.
SILK_CLEARANCE = 0.15

# `project.py` writes `min_text_height: 0.8`, and DRC reports anything below it as
# `text_height`.  Shrinking is a legal last resort only down to this floor — which means
# that at the default 0.8 there is no room to shrink at all, and the solver has to find
# space instead.
MIN_TEXT_HEIGHT = 0.8
TEXT_SIZE_STEP = 0.1

# KiCad's default pen for silk text, and the `min_text_thickness` floor under it.
THICKNESS_RATIO = 0.15
MIN_TEXT_THICKNESS = 0.1

# Edge.Cuts is stroked too (`emit.EDGE_WIDTH`), so the usable board is inset by half of it.
EDGE_HALF_WIDTH = 0.05

# How far out the search goes and in what increments.  Six steps of 0.4 mm reaches 2.5 mm
# past a courtyard; past that a label is closer to some other part than to its own and the
# board is more readable without it.
STANDOFF_STEP = 0.4
STANDOFF_RINGS = 7

# ---------------------------------------------------------------------------------------
# cost weights.  Anything DRC would report dwarfs every preference, so the solver's answer
# is "the legal placement a person would have picked", not a compromise between the two.
# ---------------------------------------------------------------------------------------
COST_VIOLATION = 100.0     # per colliding obstacle, per outline breach
COST_AMBIGUOUS = 25.0      # some other part is nearer to the label than the part it names
COST_RING = 2.0            # per 0.4 mm further from the part
COST_SLOT = 1.0            # per step down the N, S, W, E, … preference list
COST_SLIDE = 1.5           # per step off centre along the courtyard edge
COST_VERTICAL = 3.0        # a horizontal label reads without turning the board
COST_SHRINK = 6.0          # per size step below the requested one
COST_OVER_PART = 8.0       # per neighbour courtyard the label sits on top of

# Character advances of KiCad's stroke font, in 1/21 em — the unit the font itself uses
# (`STROKE_FONT_SCALE = 1.0 / 21.0`, common/font/stroke_font.cpp).  A glyph's first two
# encoded coordinates are its left and right bearing and their difference is the advance,
# so these are the font's own numbers.  They are also the reason not to reach for the
# often-quoted "0.6 * size per character": that is roughly half the truth for capitals,
# where 'M' advances 24/21 = 1.14 em, and a label sized from it lands on its neighbour.
_ADVANCE_21 = (
    16, 10, 16, 21, 20, 24, 26, 10,    # (space) ! " # $ % & '
    14, 14, 16, 26, 10, 26, 10, 22,    # ( ) * + , - . /
    20, 20, 20, 20, 20, 20, 20, 20,    # 0 1 2 3 4 5 6 7
    20, 20, 10, 10, 26, 26, 26, 18,    # 8 9 : ; < = > ?
    27, 18, 21, 21, 21, 19, 18, 21,    # @ A B C D E F G
    22, 10, 16, 21, 17, 24, 22, 22,    # H I J K L M N O
    21, 22, 21, 20, 16, 22, 18, 24,    # P Q R S T U V W
    20, 18, 20, 14, 14, 14, 12, 16,    # X Y Z [ \ ] ^ _
     8, 19, 19, 18, 19, 18, 12, 19,    # ` a b c d e f g
    19, 10, 10, 17, 11, 28, 19, 19,    # h i j k l m n o
    19, 19, 13, 17, 12, 19, 16, 22,    # p q r s t u v w
    17, 16, 17, 14, 20, 14, 15,        # x y z { | } ~
)
_ADVANCE = {chr(0x20 + i): v / 21.0 for i, v in enumerate(_ADVANCE_21)}
_WIDEST_ADVANCE = max(_ADVANCE_21) / 21.0

# The font's trailing gap, dropped from the last glyph when KiCad closes a text's box.
_INTER_CHAR = 0.2

# Candidate directions, in preference order.  N first because a label above its part is the
# convention every reader already has.
_SLOTS: Tuple[Tuple[str, int, int], ...] = (
    ("N", 0, -1), ("S", 0, 1), ("W", -1, 0), ("E", 1, 0),
    ("NW", -1, -1), ("NE", 1, -1), ("SW", -1, 1), ("SE", 1, 1),
)

# Sideways nudges along the courtyard edge, as multiples of the label's own half width.
_SLIDES = (0, 1, -1, 2, -2)


@dataclass
class RefLabel:
    """One reference designator's resolved placement.

    `x`/`y` are the footprint-local, *unrotated* offset — the value a footprint's
    `(property "Reference" … (at x y rot))` node holds, which KiCad reads through the
    footprint's own transform.  `rot` is the board-frame text angle, which is why it is 0
    even for a rotated part: the label stays horizontal and readable.

    `title_label()` reuses this type for the board title, which has no footprint to be
    local to — there `x`/`y` are board coordinates, as a `gr_text` stores them.
    """

    ref: str
    x: float
    y: float
    rot: float = 0.0
    size: float = MIN_TEXT_HEIGHT
    layer: str = "F.SilkS"
    thickness: float = 0.12
    box: Box = (0.0, 0.0, 0.0, 0.0)     # where it lands, in board millimetres
    slot: str = ""
    cost: float = 0.0

    @property
    def clean(self) -> bool:
        """True when nothing DRC tests for is being violated."""
        return self.cost < COST_VIOLATION


# ---------------------------------------------------------------------------------------
# text metrics
# ---------------------------------------------------------------------------------------

def stroke_thickness(size: float) -> float:
    """The pen width KiCad draws silk text with at a given height."""
    return round(max(size * THICKNESS_RATIO, MIN_TEXT_THICKNESS), 3)


def text_extents(text: str, size: float,
                 thickness: Optional[float] = None) -> Tuple[float, float]:
    """(width, height) in mm of one line of stroke text, pen width included.

    KiCad's own box is the glyph cells; the shape DRC collides with is the stroked path,
    which reaches half a pen width past them on every side.  Adding the whole pen width to
    each axis covers both sides at once.
    """
    if thickness is None:
        thickness = stroke_thickness(size)
    if not text:
        return (0.0, 0.0)
    advance = sum(_ADVANCE.get(ch, _WIDEST_ADVANCE) for ch in text)
    return ((advance - _INTER_CHAR) * size + thickness, size + thickness)


def _size_steps(size: float) -> List[float]:
    """The requested height, then each step down to the `min_text_height` floor."""
    steps = [size]
    while steps[-1] - TEXT_SIZE_STEP >= MIN_TEXT_HEIGHT - 1e-9:
        steps.append(round(steps[-1] - TEXT_SIZE_STEP, 3))
    return steps


# ---------------------------------------------------------------------------------------
# board geometry
# ---------------------------------------------------------------------------------------

def _overlap(a: Box, b: Box) -> bool:
    return not (a[2] <= b[0] or b[2] <= a[0] or a[3] <= b[1] or b[3] <= a[1])


def _inflate(box: Box, by: float) -> Box:
    return (box[0] - by, box[1] - by, box[2] + by, box[3] + by)


def _gap(a: Box, b: Box) -> float:
    """Edge-to-edge distance between two boxes; 0 where they touch or overlap."""
    dx = max(b[0] - a[2], a[0] - b[2], 0.0)
    dy = max(b[1] - a[3], a[1] - b[3], 0.0)
    return (dx * dx + dy * dy) ** 0.5


def _rotated_box(box: Box, rot: float) -> Box:
    """A local box's bounding box once the part is rotated into board axes."""
    corners = [rotate_point(x, y, rot)
               for x, y in ((box[0], box[1]), (box[2], box[1]),
                            (box[2], box[3]), (box[0], box[3]))]
    xs = [p[0] for p in corners]
    ys = [p[1] for p in corners]
    return (min(xs), min(ys), max(xs), max(ys))


def _side_of(placed: Placed) -> str:
    return "B" if placed.side == "back" else "F"


def _mask_sides(pad, side: str) -> Tuple[str, ...]:
    """Which solder masks a pad opens, from its own layer list.

    Read from the library rather than inferred from the pad kind: a thermal pad with only
    `F.Cu` and `F.Paste` opens no mask at all, and treating it as one costs a label the
    space it is entitled to.
    """
    sides: List[str] = []
    for layer in pad.layers:
        if not layer.endswith(".Mask"):
            continue
        prefix = layer.split(".", 1)[0]
        if prefix == "*":
            return ("F", "B")
        if prefix in ("F", "B"):
            sides.append(prefix)
    if sides:
        return tuple(sorted(set(sides)))
    if not pad.layers:
        # A pad with no layer list at all is a parse gap, not a maskless pad; assume the
        # aperture exists so the label keeps away from it.
        return ("F", "B") if pad.through else (side,)
    return ()


@dataclass
class _Scene:
    """Everything already on the silkscreen or the solder mask, per board side."""

    mask: Dict[str, List[Box]] = field(default_factory=lambda: {"F": [], "B": []})
    silk: Dict[str, List[Box]] = field(default_factory=lambda: {"F": [], "B": []})
    courtyards: Dict[str, Box] = field(default_factory=dict)

    def window(self, ref: str, side: str, reach: float) -> "_Window":
        """The obstacles a label for `ref` could possibly reach, and nothing else.

        Every candidate for one part lies inside its courtyard grown by `reach`, so scoring
        each of them against every pad on the board is quadratic work for an answer that
        cannot change.  Neighbouring courtyards get twice the margin: they are consulted
        for *distance* as well as overlap, and a part at more than `reach` can never be the
        nearer one when the label's own part is at most `reach` away.
        """
        court = self.courtyards[ref]
        near = _inflate(court, reach)
        far = _inflate(court, 2.0 * reach)
        return _Window(
            court=court,
            mask=[b for b in self.mask[side] if _overlap(near, b)],
            silk=[b for b in self.silk[side] if _overlap(near, b)],
            others=[b for other, b in sorted(self.courtyards.items())
                    if other != ref and _overlap(far, b)])


@dataclass
class _Window:
    """One part's neighbourhood, pre-filtered so scoring a candidate is a local question."""

    court: Box
    mask: List[Box] = field(default_factory=list)
    silk: List[Box] = field(default_factory=list)
    others: List[Box] = field(default_factory=list)

    def hits(self, grown: Box) -> int:
        """How many things DRC tests for this box would collide with."""
        return (sum(1 for other in self.mask if _overlap(grown, other))
                + sum(1 for other in self.silk if _overlap(grown, other)))

    def over_parts(self, box: Box) -> int:
        return sum(1 for other in self.others if _overlap(box, other))

    def ambiguous(self, box: Box) -> bool:
        """Is some other part closer to this label than the one it names?

        The readability test the DRC rules cannot make: a legal label nearer to its
        neighbour than to its own part has been placed correctly and says the wrong thing.
        """
        mine = _gap(box, self.court)
        return any(_gap(box, other) <= mine - 1e-9 for other in self.others)


def _scene(design: Design, layout: Layout, library: Library,
           mask_expansion: float = MASK_EXPANSION,
           fixed_silk: Optional[Dict[str, Sequence[Box]]] = None) -> _Scene:
    """Fixed obstacles: pad mask apertures and every footprint's own silkscreen.

    `mask_expansion` is the board's `pad_to_mask_clearance`, which decides how far the
    aperture reaches past the copper.  It is a parameter rather than the module constant
    because an existing board sets its own — this fork's `project.py` writes 0.05 and the
    r1 prototype writes 0 — and a solver that assumed the wrong one either wastes 0.05 mm
    of usable board or lands a label 0.05 mm inside an aperture DRC will report.

    `fixed_silk` is `{side: [box, …]}` of silkscreen the solver must miss but cannot move:
    board-level graphics and text, and any footprint text that is not the reference
    designator.  A board `synth` generated has none of those, which is why the solver did
    not need them until it was pointed at a board somebody else drew.
    """
    scene = _Scene()
    for side, boxes in (fixed_silk or {}).items():
        scene.silk.setdefault(side, []).extend(boxes)

    for ref in sorted(layout.parts):
        placed = layout.parts[ref]
        fp = library.resolve(design.parts[ref].footprint)
        side = _side_of(placed)
        scene.courtyards[ref] = placed.box()

        for number in sorted(fp.pads):
            pad = fp.pads[number]
            px, py = rotate_point(pad.x, pad.y, placed.rot)
            ex, ey = pad.extent(placed.rot)
            aperture = (placed.x + px - ex / 2.0 - mask_expansion,
                        placed.y + py - ey / 2.0 - mask_expansion,
                        placed.x + px + ex / 2.0 + mask_expansion,
                        placed.y + py + ey / 2.0 + mask_expansion)
            for mask_side in _mask_sides(pad, side):
                scene.mask[mask_side].append(aperture)

        for box in fp.silk:
            rotated = _rotated_box(box, placed.rot)
            scene.silk[side].append((placed.x + rotated[0], placed.y + rotated[1],
                                     placed.x + rotated[2], placed.y + rotated[3]))
    return scene


def _half_planes(outline: Outline) -> List[Tuple[float, float, float]]:
    """The outline as inward half-planes `(nx, ny, c)` with `nx*x + ny*y >= c` inside.

    Valid because `Outline.corners()` only ever produces a rectangle or a chamfered one,
    both convex — stated here because the containment test below would be silently wrong
    for any outline that was not.
    """
    edges = outline.corners()
    cx = sum(a[0] for a, _ in edges) / len(edges)
    cy = sum(a[1] for a, _ in edges) / len(edges)

    planes: List[Tuple[float, float, float]] = []
    for (x1, y1), (x2, y2) in edges:
        dx, dy = x2 - x1, y2 - y1
        length = (dx * dx + dy * dy) ** 0.5
        if length < 1e-9:
            continue
        nx, ny = -dy / length, dx / length
        c = nx * x1 + ny * y1
        if nx * cx + ny * cy < c:               # point the normal at the board's interior
            nx, ny, c = -nx, -ny, -c
        planes.append((nx, ny, c))
    return planes


def _inside(box: Box, planes: Sequence[Tuple[float, float, float]], margin: float) -> bool:
    """Is every corner of `box` at least `margin` inside the outline?"""
    corners = ((box[0], box[1]), (box[2], box[1]), (box[2], box[3]), (box[0], box[3]))
    for nx, ny, c in planes:
        for x, y in corners:
            if nx * x + ny * y - c < margin:
                return False
    return True


# ---------------------------------------------------------------------------------------
# the solver
# ---------------------------------------------------------------------------------------

@dataclass
class _Candidate:
    box: Box                 # the label's own extents, board mm
    cost: float
    size: float
    rot: float
    slot: str


def _candidates(court: Box, text: str, sizes: Sequence[float]) -> Iterator[_Candidate]:
    """Every position considered for one label, in preference order.

    Positions hang off the part's courtyard rather than its origin: footprint origins sit
    at pin 1 as often as at the centre, so an offset from the origin means something
    different on every part — which is exactly what a hand-written table has to encode.
    """
    left, top, right, bottom = court
    mid_x, mid_y = (left + right) / 2.0, (top + bottom) / 2.0

    for size_index, size in enumerate(sizes):
        thickness = stroke_thickness(size)
        raw_w, raw_h = text_extents(text, size, thickness)
        for rot in (0.0, 90.0):
            half_w, half_h = ((raw_w, raw_h) if rot == 0.0 else (raw_h, raw_w))
            half_w /= 2.0
            half_h /= 2.0
            slide_step = max(half_w, half_h)
            for ring in range(STANDOFF_RINGS):
                gap = SILK_CLEARANCE + ring * STANDOFF_STEP
                for slot_index, (name, sx, sy) in enumerate(_SLOTS):
                    cx = mid_x if sx == 0 else (right + gap + half_w if sx > 0
                                                else left - gap - half_w)
                    cy = mid_y if sy == 0 else (bottom + gap + half_h if sy > 0
                                                else top - gap - half_h)
                    for slide_index, slide in enumerate(_SLIDES):
                        if slide and sx != 0 and sy != 0:
                            continue        # a diagonal is already an offset in both axes
                        # Slide along the courtyard edge the label is hanging off, never
                        # into it: N/S nudge sideways, E/W nudge up and down.
                        dx = slide * slide_step if sx == 0 else 0.0
                        dy = slide * slide_step if sx != 0 else 0.0
                        x, y = cx + dx, cy + dy
                        cost = (COST_RING * ring
                                + COST_SLOT * slot_index
                                + COST_SLIDE * slide_index
                                + (COST_VERTICAL if rot else 0.0)
                                + COST_SHRINK * size_index)
                        yield _Candidate(
                            box=(x - half_w, y - half_h, x + half_w, y + half_h),
                            cost=cost, size=size, rot=rot, slot=name)


def _order(layout: Layout, scene: _Scene, reach: float) -> List[str]:
    """Crowded parts choose first.

    A part with neighbours on three sides has a handful of legal slots and an isolated one
    has hundreds; letting the isolated part pick first is how a greedy pass paints the
    crowded one into a corner.
    """
    def crowding(ref: str) -> Tuple[int, str]:
        court = scene.courtyards[ref]
        probe = _inflate(court, reach)
        near = sum(1 for other, box in scene.courtyards.items()
                   if other != ref and _overlap(probe, box))
        return (-near, ref)

    return sorted(layout.parts, key=crowding)


def _to_local(placed: Placed, box: Box) -> Tuple[float, float]:
    """A board-space label centre as the footprint-local, unrotated offset KiCad stores."""
    cx = (box[0] + box[2]) / 2.0 - placed.x
    cy = (box[1] + box[3]) / 2.0 - placed.y
    return rotate_point(cx, cy, -placed.rot)


def solve(design: Design, layout: Layout, library: Library, outline: Outline,
          text_size: float = MIN_TEXT_HEIGHT,
          mask_expansion: float = MASK_EXPANSION,
          fixed_silk: Optional[Dict[str, Sequence[Box]]] = None) -> Dict[str, RefLabel]:
    """Choose a position and size for every part's reference designator.

    The result is keyed by ref and holds footprint-local offsets, ready for
    `emit.set_references()`.

    `mask_expansion` and `fixed_silk` describe a board that already exists rather than one
    about to be generated; see `_scene()`.  `board.py` fills both from a saved
    `.kicad_pcb`, and the defaults are what a `synth` board has.

    The board title is *not* reserved first.  It is the one silkscreen item a board can do
    without, and a title laid along a 2 mm margin sterilises that whole side for every part
    beside it — which cost a label its own part and bought a caption.  `title_label()` runs
    afterwards, against these labels.
    """
    scene = _scene(design, layout, library, mask_expansion, fixed_silk)
    planes = _half_planes(outline)
    edge_margin = SILK_CLEARANCE + EDGE_HALF_WIDTH
    sizes = _size_steps(max(text_size, MIN_TEXT_HEIGHT))

    # How far the furthest candidate can land from a courtyard: the outermost ring, plus a
    # label that has been slid two of its own widths along the edge it hangs off.
    longest = max((text_extents(ref, sizes[0])[0] for ref in layout.parts), default=0.0)
    reach = (SILK_CLEARANCE + (STANDOFF_RINGS - 1) * STANDOFF_STEP
             + 3.0 * longest + SILK_CLEARANCE)

    labels: Dict[str, RefLabel] = {}
    for ref in _order(layout, scene, reach=2.0 * sizes[0] + SILK_CLEARANCE):
        placed = layout.parts[ref]
        side = _side_of(placed)
        window = scene.window(ref, side, reach)
        best: Optional[_Candidate] = None

        for cand in _candidates(window.court, ref, sizes):
            cost = cand.cost + COST_VIOLATION * window.hits(
                _inflate(cand.box, SILK_CLEARANCE))
            if not _inside(cand.box, planes, edge_margin):
                cost += COST_VIOLATION
            cost += COST_OVER_PART * window.over_parts(cand.box)
            if window.ambiguous(cand.box):
                cost += COST_AMBIGUOUS

            if best is None or cost < best.cost:
                best = _Candidate(cand.box, cost, cand.size, cand.rot, cand.slot)
                if cost == 0.0:
                    break               # nothing later in the order can beat a free slot

        assert best is not None
        lx, ly = _to_local(placed, best.box)
        labels[ref] = RefLabel(
            ref=ref, x=round(lx, 4), y=round(ly, 4), rot=best.rot, size=best.size,
            layer=f"{side}.SilkS", thickness=stroke_thickness(best.size),
            box=best.box, slot=best.slot, cost=best.cost)
        # Placed labels are obstacles for the labels that follow.
        scene.silk[side].append(_inflate(best.box, SILK_CLEARANCE))

    return labels


# ---------------------------------------------------------------------------------------
# the board title
# ---------------------------------------------------------------------------------------

def title_text(design: Design) -> str:
    return f"{design.title or design.project}  rev {design.rev}"


def title_label(design: Design, layout: Layout, outline: Outline,
                labels: Optional[Dict[str, RefLabel]] = None,
                text_size: float = 1.0) -> Optional[RefLabel]:
    """Place the board title in the margin the placement left, or report that there is none.

    The margin band is outside every courtyard, so a title that fits there needs no pad or
    footprint-silk test — the courtyards already contain both.  What it does have to miss
    is the reference designators, which reach *into* the margin: pass the result of
    `solve()` as `labels`.

    Returns None when the title fits nowhere at any legal height, which is a real outcome
    on a shrink-wrapped board: the caller then leaves the title off rather than shipping
    silkscreen that DRC reports as clipped by the board edge.
    """
    text = title_text(design)
    if not text.strip():
        return None

    planes = _half_planes(outline)
    edge_margin = SILK_CLEARANCE + EDGE_HALF_WIDTH
    taken = [_inflate(label.box, SILK_CLEARANCE) for label in (labels or {}).values()]
    boxes = [p.box() for p in layout.parts.values()]

    # How much clear board there is between each edge and the nearest courtyard.
    if boxes:
        bands = {
            "S": outline.bottom - max(b[3] for b in boxes),
            "N": min(b[1] for b in boxes) - outline.top,
            "W": min(b[0] for b in boxes) - outline.left,
            "E": outline.right - max(b[2] for b in boxes),
        }
    else:
        bands = {"S": outline.height / 2.0, "N": outline.height / 2.0,
                 "W": outline.width / 2.0, "E": outline.width / 2.0}

    for size in _size_steps(max(text_size, MIN_TEXT_HEIGHT)):
        thickness = stroke_thickness(size)
        raw_w, raw_h = text_extents(text, size, thickness)
        # South first (a title under the board is the convention), then north; the vertical
        # bands only when the horizontal ones cannot hold the line.
        for name in ("S", "N", "W", "E"):
            band = bands[name]
            rot = 0.0 if name in ("S", "N") else 90.0
            half_w, half_h = ((raw_w, raw_h) if rot == 0.0 else (raw_h, raw_w))
            half_w /= 2.0
            half_h /= 2.0
            if band < 2 * (half_h if rot == 0.0 else half_w) + 2 * SILK_CLEARANCE:
                continue
            if name == "S":
                x, y = _centre_x(outline), outline.bottom - band / 2.0
            elif name == "N":
                x, y = _centre_x(outline), outline.top + band / 2.0
            elif name == "W":
                x, y = outline.left + band / 2.0, _centre_y(outline)
            else:
                x, y = outline.right - band / 2.0, _centre_y(outline)

            box = (x - half_w, y - half_h, x + half_w, y + half_h)
            if not _inside(box, planes, edge_margin):
                continue
            grown = _inflate(box, SILK_CLEARANCE)
            if any(_overlap(grown, other) for other in taken):
                continue
            return RefLabel(ref=text, x=x, y=y, rot=rot, size=size, layer="F.SilkS",
                            thickness=thickness, box=box, slot=name)
    return None


def _centre_x(outline: Outline) -> float:
    return outline.left + outline.width / 2.0


def _centre_y(outline: Outline) -> float:
    return outline.top + outline.height / 2.0


if __name__ == "__main__":
    # Smoke test:  python3 phicad/synth/silk.py design.json
    import place as place_mod                                        # noqa: PLC0415
    from ir import load                                              # noqa: PLC0415

    if len(sys.argv) < 2:
        print("usage: silk.py <design.json>", file=sys.stderr)
        raise SystemExit(2)

    _design = load(sys.argv[1])
    _library = Library.default([Path(sys.argv[1]).resolve().parent / "lib"])
    _layout = place_mod.solve(_design, _library)
    assert _layout.outline is not None

    _labels = solve(_design, _layout, _library, _layout.outline)
    _title = title_label(_design, _layout, _layout.outline, _labels)
    print("title:", "dropped (no margin wide enough)" if _title is None
          else f"{_title.slot} at ({_title.x:.2f}, {_title.y:.2f}) "
               f"@ {_title.rot:g} deg, size {_title.size}")

    for _ref, _label in sorted(_labels.items()):
        print(f"  {_ref:<4} local ({_label.x:+7.3f}, {_label.y:+7.3f}) "
              f"@ {_label.rot:g} deg  size {_label.size}  {_label.slot:<2}  "
              f"cost {_label.cost:.1f}")
    _dirty = [r for r, l in sorted(_labels.items()) if not l.clean]
    print("labels with a violation:", ", ".join(_dirty) or "none")
    raise SystemExit(1 if _dirty else 0)
