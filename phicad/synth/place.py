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
"""Placement: turn "C1 near U1" into coordinates, and shrink the board onto the result.

What this replaces
------------------
Hand-tuned constants.  The Teensy demo's placement is 400 lines of them, and its comments
are an honest log of what that costs:

    # -4.8, after two corrections.  -3.4 put C2's GND pad at the same y as J4's 3V3 tap
    # (0.01 mm); -4.1 moved it only to 0.69 mm where 0.9 mm is needed for a 1.0 mm pad.

Every one of those numbers was found by a DRC round trip.  The design intent behind them —
"the decoupling cap belongs next to the pin it decouples" — is one line in the IR, and
this module is what turns that line back into a coordinate, with the overlap arithmetic
done once, correctly, instead of per board.

How it works
------------
1. **Cluster.**  `near` and `group` constraints join parts into clusters.  A cluster is
   placed as a unit, so moving a connector moves its decoupling with it.
2. **Pack.**  Clusters are shelf-packed into rows, tallest first, against a target aspect
   ratio.  Edge-constrained clusters are reserved to their side first, because an edge is a
   boundary condition on everything else.
3. **Legalize.**  A deterministic separation sweep pushes apart anything whose courtyard
   still overlaps.  Courtyard, not pad box: `courtyards_overlap` is what DRC actually
   tests, so packing against the pad box produces boards that pass a private check and
   fail the real one.
4. **Shrink-wrap.**  The outline is the courtyard bounding box plus the design's margin —
   which is what "minimal footprint" means operationally: the board is a *result* of the
   placement, not a guess made before it.

Determinism is a hard requirement, not a nicety: every ordering here is by an explicit
sort key, never by dict iteration order, so the same design produces the same board and
`git diff` shows only real changes.
"""

from __future__ import annotations

import math
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Set, Tuple

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from ir import Design  # noqa: E402
from library import Library  # noqa: E402

# Gap left between two courtyards.  Courtyards already carry the library's own assembly
# clearance, so this only has to be enough that they do not touch — KiCad reports an
# overlap on intersection, and floating-point equality is not a safe place to sit.
DEFAULT_PART_GAP = 0.15

# Aspect ratio the packer aims for.  Slightly wider than square because boards are usually
# cut from panels and connectors tend to live on a long edge.
TARGET_ASPECT = 1.3

# Iterations of the legalization sweep.  Each pass separates every overlapping pair by half
# the overlap in each direction; three passes settle every arrangement produced here, and
# the cap guarantees termination rather than relying on convergence.
LEGALIZE_PASSES = 8


@dataclass
class Placed:
    """One part's resolved position, in board millimetres."""

    ref: str
    x: float
    y: float
    rot: float = 0.0
    side: str = "front"
    # Courtyard half-extents in board axes, carried so the outline and the overlap checks
    # do not have to re-derive them.
    half_w: float = 0.0
    half_h: float = 0.0
    # Offset from the part origin to the centre of its courtyard.  Footprint origins are
    # rarely at the courtyard centre (a connector's origin is pin 1), so packing the origin
    # instead of the centre leaves visible gaps on one side and overlaps on the other.
    cx_off: float = 0.0
    cy_off: float = 0.0
    locked: bool = False

    @property
    def centre(self) -> Tuple[float, float]:
        return (self.x + self.cx_off, self.y + self.cy_off)

    def box(self) -> Tuple[float, float, float, float]:
        """Courtyard bounding box in board coordinates."""
        cx, cy = self.centre
        return (cx - self.half_w, cy - self.half_h, cx + self.half_w, cy + self.half_h)

    def move_centre_to(self, cx: float, cy: float) -> None:
        self.x = cx - self.cx_off
        self.y = cy - self.cy_off


@dataclass
class Outline:
    """The board edge the placement earned."""

    left: float
    top: float
    right: float
    bottom: float
    chamfer: float = 0.0

    @property
    def width(self) -> float:
        return self.right - self.left

    @property
    def height(self) -> float:
        return self.bottom - self.top

    @property
    def area(self) -> float:
        return self.width * self.height

    def corners(self) -> List[Tuple[Tuple[float, float], Tuple[float, float]]]:
        """Edge segments, chamfered when the design asked for it."""
        left, top, right, bottom = self.left, self.top, self.right, self.bottom
        c = self.chamfer
        if c <= 0:
            return [((left, top), (right, top)), ((right, top), (right, bottom)),
                    ((right, bottom), (left, bottom)), ((left, bottom), (left, top))]
        return [
            ((left + c, top), (right - c, top)), ((right - c, top), (right, top + c)),
            ((right, top + c), (right, bottom - c)),
            ((right, bottom - c), (right - c, bottom)),
            ((right - c, bottom), (left + c, bottom)),
            ((left + c, bottom), (left, bottom - c)),
            ((left, bottom - c), (left, top + c)), ((left, top + c), (left + c, top)),
        ]


@dataclass
class Layout:
    """The whole result: where everything goes and how big the board came out."""

    parts: Dict[str, Placed] = field(default_factory=dict)
    outline: Optional[Outline] = None
    notes: List[str] = field(default_factory=list)

    def positions(self) -> Dict[str, Tuple[float, float, float]]:
        """`{ref: (x, y, rot)}`, the shape `session.place()` wants."""
        return {ref: (p.x, p.y, p.rot) for ref, p in sorted(self.parts.items())}

    def overlaps(self, gap: float = 0.0) -> List[Tuple[str, str, float]]:
        """Pairs whose courtyards are closer than `gap`, worst first.

        The placement's own gate.  An empty list is the claim that DRC's
        `courtyards_overlap` will find nothing — cheap enough to run every pass, where DRC
        costs about 25 seconds.
        """
        found: List[Tuple[str, str, float]] = []
        items = [self.parts[ref] for ref in sorted(self.parts)]
        for i, a in enumerate(items):
            for b in items[i + 1:]:
                if a.side != b.side:
                    continue           # opposite sides of the board cannot collide
                overlap = _overlap_amount(a.box(), b.box(), gap)
                if overlap > 0:
                    found.append((a.ref, b.ref, overlap))
        return sorted(found, key=lambda t: -t[2])


def _overlap_amount(a: Sequence[float], b: Sequence[float], gap: float) -> float:
    """How deep two boxes intersect once `gap` is required between them; 0 if clear."""
    dx = min(a[2], b[2]) - max(a[0], b[0]) + gap
    dy = min(a[3], b[3]) - max(a[1], b[1]) + gap
    if dx <= 0 or dy <= 0:
        return 0.0
    return min(dx, dy)


# ---------------------------------------------------------------------------------------
# clustering
# ---------------------------------------------------------------------------------------

def _clusters(design: Design) -> List[List[str]]:
    """Group parts joined by `group` names or `near` references, via union-find.

    `near` is directional in the IR ("C1 near U1") but the cluster is not: whichever way it
    was written, the two parts travel together.
    """
    parent: Dict[str, str] = {ref: ref for ref in design.parts}

    def find(a: str) -> str:
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            # Union by name keeps the representative deterministic.
            lo, hi = sorted((ra, rb))
            parent[hi] = lo

    by_group: Dict[str, List[str]] = {}
    for ref in sorted(design.placement):
        spec = design.placement[ref]
        if ref not in parent:
            continue
        if spec.group:
            by_group.setdefault(spec.group, []).append(ref)
        if spec.near:
            target = spec.near.split(".")[0]
            if target in parent:
                union(ref, target)

    for members in by_group.values():
        for other in members[1:]:
            union(members[0], other)

    groups: Dict[str, List[str]] = {}
    for ref in sorted(parent):
        groups.setdefault(find(ref), []).append(ref)
    return [sorted(members) for _, members in sorted(groups.items())]


def _connection_count(design: Design) -> Dict[str, int]:
    """How many nets each part touches — the packing order's tiebreak.

    Parts on many nets belong near the middle: every net they miss is a track that has to
    cross the board.
    """
    counts: Dict[str, int] = {ref: 0 for ref in design.parts}
    for pins in design.nets.values():
        for ref in {pin.split(".")[0] for pin in pins}:
            if ref in counts:
                counts[ref] += 1
    return counts


# ---------------------------------------------------------------------------------------
# the placer
# ---------------------------------------------------------------------------------------

def routing_gap(design: Design) -> float:
    """How far apart to hold courtyards so a track can still get between two parts.

    One nominal track with clearance on either side.  The old default was a flat 0.15 mm,
    which is legal — DRC only tests whether courtyards *overlap* — and useless: parts ended
    up 0.15 mm apart with no channel between them, and the router then could not get a
    single pad of the part in the middle out to anywhere.  A placement that packs below
    this is optimising the one number (area) that nobody is asking about at the expense of
    the one that decides whether the board exists.
    """
    if design.board.part_gap is not None:
        return max(design.board.part_gap, 0.05)
    rules = design.rules
    # The WIDEST track, not the nominal one.  A channel sized for a 0.25 mm signal is
    # 0.15 mm too narrow for a 0.4 mm power net, and the net that then cannot get out is
    # ground -- whose pads are the ones most likely to be in the middle of a fine-pitch
    # part with nowhere else to go.
    widest = max([rules.track_width] + list(rules.track_widths.values()))
    channel = 2.0 * rules.clearance + widest
    # A channel also has to fit a VIA, not only a track.  On a two-layer board every
    # surface ground pad reaches the pour through a stitching via, and a fine-pitch part's
    # middle pads can only be via'd in the channel beside the part -- there is no room
    # between their own pads.  Sizing channels for tracks alone leaves those pads
    # permanently unconnected on a board that otherwise routes.
    if design.board.pour_ground:
        channel = max(channel, rules.via_diameter_min + 2.0 * rules.clearance)
        # Deliberately NOT also `+ track_width` to let a track past the via.  It sounds
        # strictly better and measures worse: widening the channel repacks the whole
        # placement, and the arrangement that comes out failed on BOTH example designs
        # where 1.0 mm routes one of them.  Routing success is sensitive to the exact
        # arrangement rather than monotone in the spacing, so a design that needs more
        # room asks for it with `board.part_gap` instead of everyone paying for it.
    return max(DEFAULT_PART_GAP, channel)


def solve(design: Design, library: Library,
          part_gap: Optional[float] = None) -> Layout:
    """Resolve the design's placement intent into coordinates and a board outline."""
    layout = Layout()
    gap = routing_gap(design) if part_gap is None else max(part_gap, 0.05)

    # --- 1. every part's courtyard, rotated into board axes ---------------------------
    for ref in sorted(design.parts):
        spec = design.placement.get(ref)
        rot = spec.rot if spec else 0.0
        side = spec.side if spec else "front"
        fp = library.resolve(design.parts[ref].footprint)
        minx, miny, maxx, maxy = fp.extent_at(rot)
        layout.parts[ref] = Placed(
            ref=ref, x=0.0, y=0.0, rot=rot, side=side,
            half_w=(maxx - minx) / 2.0, half_h=(maxy - miny) / 2.0,
            cx_off=(minx + maxx) / 2.0, cy_off=(miny + maxy) / 2.0,
            locked=bool(spec and (spec.at is not None or spec.lock)),
        )

    # --- 2. explicit positions win outright ------------------------------------------
    for ref, spec in sorted(design.placement.items()):
        if spec.at is not None and ref in layout.parts:
            placed = layout.parts[ref]
            placed.x, placed.y = spec.at

    # --- 3. pack the rest -------------------------------------------------------------
    clusters = _clusters(design)
    counts = _connection_count(design)

    free_clusters: List[List[str]] = []
    for members in clusters:
        if all(layout.parts[ref].locked for ref in members):
            continue
        free_clusters.append(members)

    _pack(design, layout, free_clusters, counts, gap)

    # --- 4. legalize ------------------------------------------------------------------
    _legalize(layout, gap)

    # --- 5. the outline the placement earned ------------------------------------------
    layout.outline = _fit_outline(design, layout)

    # --- 6. re-origin so the board sits where the design asked ------------------------
    _translate_to_origin(design, layout)

    residual = layout.overlaps(gap * 0.5)
    if residual:
        layout.notes.append(
            f"{len(residual)} courtyard overlaps remain after legalization; worst is "
            f"{residual[0][0]}/{residual[0][1]} by {residual[0][2]:.3f} mm")
    return layout


def _cluster_layout(design: Design, layout: Layout, members: Sequence[str],
                    gap: float) -> Tuple[float, float]:
    """Arrange one cluster around its anchor; return the cluster's (width, height).

    Positions are set relative to (0, 0) and translated by the packer.  The anchor is the
    most-connected part — satellites are what move to reach it, not the other way round.
    """
    counts = _connection_count(design)
    ordered = sorted(members, key=lambda r: (-counts.get(r, 0),
                                             -(layout.parts[r].half_w
                                               * layout.parts[r].half_h), r))
    anchor = ordered[0]
    layout.parts[anchor].move_centre_to(0.0, 0.0)

    if len(ordered) == 1:
        p = layout.parts[anchor]
        return (2 * p.half_w, 2 * p.half_h)

    # Satellites go on a ring around the anchor, at the angle that keeps the cluster
    # compact: alternating sides rather than a spiral, so a two-part cluster ends up
    # side by side instead of diagonal.
    order = [0, 2, 1, 3, 4, 5, 6, 7]          # E, W, N, S, then diagonals
    anchor_p = layout.parts[anchor]
    for i, ref in enumerate(ordered[1:]):
        p = layout.parts[ref]
        spec = design.placement.get(ref)
        want = spec.within if spec and spec.within else 3.0
        slot = order[i % len(order)]
        angle = slot * (math.pi / 4.0)
        # Distance is the two half-extents plus the gap, so satellites touch the anchor's
        # courtyard rather than sitting at an arbitrary radius; `within` only ever pushes
        # them further out, never closer than legal.
        reach_x = anchor_p.half_w + p.half_w + gap
        reach_y = anchor_p.half_h + p.half_h + gap
        ring = 1 + i // len(order)
        dx = math.cos(angle) * reach_x * ring
        dy = math.sin(angle) * reach_y * ring
        if want and math.hypot(dx, dy) > want:
            # `within` is a request, not a licence to overlap: honour it only when it is
            # still outside both courtyards.
            scale = want / math.hypot(dx, dy)
            if abs(dx) * scale >= reach_x - 1e-9 or abs(dy) * scale >= reach_y - 1e-9:
                dx, dy = dx * scale, dy * scale
        p.move_centre_to(dx, dy)

    boxes = [layout.parts[r].box() for r in ordered]
    left = min(b[0] for b in boxes)
    top = min(b[1] for b in boxes)
    right = max(b[2] for b in boxes)
    bottom = max(b[3] for b in boxes)

    # Normalise so the cluster's own bounding box starts at (0, 0).
    for ref in ordered:
        p = layout.parts[ref]
        p.x -= left
        p.y -= top
    return (right - left, bottom - top)


def _pack(design: Design, layout: Layout, clusters: List[List[str]],
          counts: Dict[str, int], gap: float) -> None:
    """Shelf-pack clusters into rows, edge-constrained ones to their side first."""
    if not clusters:
        return

    sized: List[Tuple[List[str], float, float, str]] = []
    for members in clusters:
        w, h = _cluster_layout(design, layout, members, gap)
        edge = ""
        for ref in members:
            spec = design.placement.get(ref)
            if spec and spec.edge:
                edge = spec.edge
                break
        sized.append((members, w, h, edge))

    # Rough target width: enough to hold the total area at the target aspect, and never
    # narrower than the widest single cluster.
    total_area = sum(w * h for _, w, h, _ in sized)
    widest = max(w for _, w, _, _ in sized)
    target_w = max(widest, math.sqrt(max(total_area, 1e-6) * TARGET_ASPECT))

    # West/north clusters lead, east/south trail, and the rest sit in the middle ordered
    # by connectivity so the busiest parts land centrally.
    def rank(entry: Tuple[List[str], float, float, str]) -> Tuple[int, float, str]:
        members, w, h, edge = entry
        side = {"west": 0, "north": 0, "": 1, "east": 2, "south": 2}.get(edge, 1)
        busiest = -max((counts.get(r, 0) for r in members), default=0)
        return (side, busiest, members[0])

    cursor_x = 0.0
    cursor_y = 0.0
    row_height = 0.0
    for members, w, h, _edge in sorted(sized, key=rank):
        if cursor_x > 0 and cursor_x + w > target_w:
            cursor_x = 0.0
            cursor_y += row_height + gap
            row_height = 0.0
        for ref in members:
            layout.parts[ref].x += cursor_x
            layout.parts[ref].y += cursor_y
        cursor_x += w + gap
        row_height = max(row_height, h)


def _legalize(layout: Layout, gap: float) -> None:
    """Push overlapping courtyards apart, deterministically, until they clear.

    Locked parts do not move: an explicit `at` in the design is a statement about the
    world (a connector aligned to an enclosure cutout), and silently sliding it would make
    the generated board wrong in a way nothing downstream could detect.
    """
    for _ in range(LEGALIZE_PASSES):
        collisions = layout.overlaps(gap)
        if not collisions:
            return
        for ref_a, ref_b, _depth in collisions:
            a, b = layout.parts[ref_a], layout.parts[ref_b]
            if a.locked and b.locked:
                continue
            ax, ay = a.centre
            bx, by = b.centre
            need_x = a.half_w + b.half_w + gap
            need_y = a.half_h + b.half_h + gap
            push_x = need_x - abs(ax - bx)
            push_y = need_y - abs(ay - by)
            if push_x <= 0 or push_y <= 0:
                continue
            # Separate along the cheaper axis — the one already closest to clearing.
            if push_x <= push_y:
                step = push_x / (1.0 if (a.locked or b.locked) else 2.0)
                sign = 1.0 if ax >= bx else -1.0
                if not a.locked:
                    a.move_centre_to(ax + sign * step, ay)
                if not b.locked:
                    b.move_centre_to(bx - sign * step, by)
            else:
                step = push_y / (1.0 if (a.locked or b.locked) else 2.0)
                sign = 1.0 if ay >= by else -1.0
                if not a.locked:
                    a.move_centre_to(ax, ay + sign * step)
                if not b.locked:
                    b.move_centre_to(bx, by - sign * step)


def _fit_outline(design: Design, layout: Layout) -> Outline:
    """The board edge: shrink-wrapped to the placement, or the size the design demanded."""
    board = design.board
    boxes = [p.box() for p in layout.parts.values()]
    if not boxes:
        boxes = [(0.0, 0.0, 10.0, 10.0)]

    left = min(b[0] for b in boxes)
    top = min(b[1] for b in boxes)
    right = max(b[2] for b in boxes)
    bottom = max(b[3] for b in boxes)

    if board.outline == "rect" and board.width and board.height:
        # A stated size is a constraint from outside the design (an enclosure, a panel), so
        # it wins — but say so when the parts do not fit, rather than quietly cropping.
        needed_w = right - left + 2 * board.margin
        needed_h = bottom - top + 2 * board.margin
        if needed_w > board.width + 1e-6 or needed_h > board.height + 1e-6:
            layout.notes.append(
                f"parts need {needed_w:.2f} x {needed_h:.2f} mm but board.outline is "
                f"{board.width:g} x {board.height:g} mm — the placement overhangs the edge")
        return Outline(0.0, 0.0, board.width, board.height, board.chamfer)

    margin = max(board.margin, design.rules.edge_clearance)
    return Outline(left - margin, top - margin, right + margin, bottom + margin,
                   board.chamfer)


def _translate_to_origin(design: Design, layout: Layout) -> None:
    """Move the whole layout so the outline's top-left sits at the design's origin.

    Board coordinates are absolute in KiCad and the sheet's usable area starts near
    (0, 0); placing a board at the origin puts half of it off the page in the editor's
    default view, so designs state where they want it.
    """
    if layout.outline is None:
        return
    # An explicit `at` is absolute board coordinates -- a connector aligned to an enclosure
    # cutout, a mounting hole on a stated pitch.  Re-origining the board would slide it
    # somewhere else while still reporting the position the design asked for, so a design
    # that pins anything decides where the board sits and this pass stands down.
    if any(placed.locked for placed in layout.parts.values()):
        return
    ox, oy = design.board.origin
    dx = ox - layout.outline.left
    dy = oy - layout.outline.top
    if abs(dx) < 1e-12 and abs(dy) < 1e-12:
        return
    for placed in layout.parts.values():
        placed.x += dx
        placed.y += dy
    layout.outline = Outline(layout.outline.left + dx, layout.outline.top + dy,
                             layout.outline.right + dx, layout.outline.bottom + dy,
                             layout.outline.chamfer)


if __name__ == "__main__":
    # Smoke test:  python3 phicad/synth/place.py design.json
    from ir import load                                            # noqa: PLC0415

    if len(sys.argv) < 2:
        print("usage: place.py <design.json>", file=sys.stderr)
        raise SystemExit(2)
    d = load(sys.argv[1])
    result = solve(d, Library.default())
    assert result.outline is not None
    print(f"board {result.outline.width:.2f} x {result.outline.height:.2f} mm "
          f"({result.outline.area:.1f} mm^2)")
    for ref, placed in sorted(result.parts.items()):
        print(f"  {ref:<4} ({placed.x:8.3f}, {placed.y:8.3f}) @ {placed.rot:g} deg")
    collisions = result.overlaps(0.0)
    print("courtyard overlaps:", len(collisions))
    for note in result.notes:
        print("note:", note)
    raise SystemExit(1 if collisions else 0)
