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
"""A maze router, so "all connected" stops being a thing a model does by hand.

The problem this solves
-----------------------
`pcbnew/router/` is the PNS push-and-shove router, and it is interactive-only:
`RunAction` is registered and then rejected headless (`api_handler_board.cpp:500-516`).
So routing in this fork has meant an LLM emitting one `Track` at a time over IPC —
`agents/pcb-router.md` is exactly that loop — with DRC as the only oracle and a 25 second
round trip per attempt.  On a dense board that is the dominant cost and the dominant risk.

This is a grid A* router instead.  It is not PNS: it does not shove, it has no topology
optimizer, and it will refuse a board that a good router would finish.  What it does have
is the property that matters here — when it says a net is routed, the net is routed, and
when it cannot, it says which connection it failed on instead of producing plausible
copper that DRC rejects.

Strategy for a 2-layer board with a ground pour
-----------------------------------------------
Ground is not routed as a tree.  Every ground pad gets a stitching via next to it and the
B.Cu pour does the connecting — which is what a person would do, and it removes ground
from the maze entirely.  On the example design that is 6 of 16 connections gone before the
router starts.  Through-hole ground pads need nothing at all: the hole already reaches the
pour.

Everything else is routed as a minimum spanning tree over its pads, shortest edge first,
on a grid whose cells are owned by whichever net claimed them.  A cell is passable for net
N if it is free or already owned by N, which is what makes same-net contact legal and
everything else a wall.

Why a grid and not a shape-based router
---------------------------------------
Clearance arithmetic on real pad shapes is where hand-written routing goes wrong — the
Teensy demo's `check_routing.py` exists because four separate classes of clearance bug
shipped past review.  Rasterizing every obstacle once, inflated by the clearance it needs,
turns all of that into a single question ("is this cell mine?") that cannot be got subtly
wrong per call site.  The cost is resolution: at the default 0.1 mm grid the router cannot
find a gap that a shape-based router would.  That trade is deliberate — a refusal is
recoverable, a clearance violation found at the fab is not.
"""

from __future__ import annotations

import heapq
import math
import sys
import uuid as _uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Set, Tuple

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from ir import Design, MIN_HOLE_SPACING  # noqa: E402
from library import Library, rotate_point  # noqa: E402
from place import Layout, Outline  # noqa: E402

# Grid pitch in mm.  0.1 mm resolves every geometry in the stock libraries (the finest pad
# pitch here is 0.65 mm) while keeping a 50 x 50 mm board at 500 x 500 cells per layer,
# which A* crosses in well under a second.
DEFAULT_GRID = 0.1

# Cost of changing layer, in grid steps.  High enough that the router lays a track around a
# small obstacle rather than diving under it, low enough that it will use the back layer
# when the front is genuinely blocked.
VIA_COST_STEPS = 12.0

# Cost added for a direction change, in grid steps.  Without it A* returns staircases:
# every diagonal costs the same as the equivalent dogleg, so it picks arbitrarily among
# thousands of equal-cost paths.  A small turn penalty makes the cheapest path also the
# straightest one, which is the one a person would draw.
TURN_COST_STEPS = 0.6

# The board's pad_to_mask_clearance, matching what project.py writes.  A pad's solder-mask
# opening is its copper grown by this much, and that window is why a via may not sit under
# a pad: the via itself is tented, but the PAD's mask opening exposes the barrel anyway, and
# at reflow paste wicks down an unfilled 0.4 mm hole -- a starved joint on top and a solder
# ball on the pour underneath.  KiCad has no via-in-pad rule at any severity, so nothing
# downstream catches it; six vias on the two example boards were sitting in pads before this
# check existed, including one on a regulator's output pin.
MASK_CLEARANCE = 0.05

_FREE = 0
_BLOCKED = -1

# Neighbour offsets: orthogonals first so equal-cost ties resolve to straight moves.
_STEPS: Tuple[Tuple[int, int, float], ...] = (
    (1, 0, 1.0), (-1, 0, 1.0), (0, 1, 1.0), (0, -1, 1.0),
    (1, 1, math.sqrt(2)), (1, -1, math.sqrt(2)),
    (-1, 1, math.sqrt(2)), (-1, -1, math.sqrt(2)),
)


@dataclass
class Track:
    """One copper segment, in board millimetres."""

    x1: float
    y1: float
    x2: float
    y2: float
    net: str
    layer: str
    width: float

    def length(self) -> float:
        return math.hypot(self.x2 - self.x1, self.y2 - self.y1)


@dataclass
class Via:
    x: float
    y: float
    net: str
    size: float
    drill: float


@dataclass
class Failure:
    """A connection the router could not make, and why."""

    net: str
    frm: str
    to: str
    reason: str

    def __str__(self) -> str:
        return f"{self.net}: {self.frm} -> {self.to}: {self.reason}"


@dataclass
class RouteResult:
    tracks: List[Track] = field(default_factory=list)
    vias: List[Via] = field(default_factory=list)
    failures: List[Failure] = field(default_factory=list)
    stitched: List[str] = field(default_factory=list)   # ground pads bonded to the pour
    necked: List[str] = field(default_factory=list)     # connections routed below nominal

    @property
    def ok(self) -> bool:
        return not self.failures

    def track_length(self) -> float:
        return sum(t.length() for t in self.tracks)

    def summary(self) -> str:
        parts = [f"{len(self.tracks)} segments", f"{len(self.vias)} vias",
                 f"{self.track_length():.1f} mm"]
        if self.stitched:
            parts.append(f"{len(self.stitched)} ground stitches")
        if self.necked:
            parts.append(f"{len(self.necked)} necked")
        if self.failures:
            parts.append(f"{len(self.failures)} UNROUTED")
        return ", ".join(parts)


# ---------------------------------------------------------------------------------------
# geometry
# ---------------------------------------------------------------------------------------

@dataclass
class PadSite:
    """One pad in board coordinates, with everything the router needs about it."""

    ref: str
    number: str
    net: str
    x: float
    y: float
    half_w: float
    half_h: float
    through: bool
    layer: str            # "F.Cu" or "B.Cu"; through-hole pads report "*"

    @property
    def pin(self) -> str:
        return f"{self.ref}.{self.number}"

    def on(self, layer: str) -> bool:
        return self.through or self.layer == layer


def pad_sites(design: Design, layout: Layout, library: Library) -> List[PadSite]:
    """Every pad of every placed part, in board coordinates."""
    owner = design.pin_owner()
    sites: List[PadSite] = []
    for ref in sorted(layout.parts):
        placed = layout.parts[ref]
        fp = library.resolve(design.parts[ref].footprint)
        for number in sorted(fp.pads):
            pad = fp.pads[number]
            dx, dy = rotate_point(pad.x, pad.y, placed.rot)
            ex, ey = pad.extent(placed.rot)
            layer = "F.Cu" if placed.side == "front" else "B.Cu"
            sites.append(PadSite(
                ref=ref, number=number, net=owner.get(f"{ref}.{number}", ""),
                x=placed.x + dx, y=placed.y + dy,
                half_w=ex / 2.0, half_h=ey / 2.0,
                through=pad.through,
                layer="*" if pad.through else layer))
    return sites


def _rotated_extent(w: float, h: float, rot: float) -> Tuple[float, float]:
    """Axis-aligned extent of a `w` x `h` rectangle rotated by `rot` degrees.

    Same rule as `Pad.extent`: exact on the right angles, bounding box otherwise, which
    over-covers and is therefore the safe direction for a keepout.
    """
    total = rot % 180.0
    if abs(total) < 1e-9:
        return (w, h)
    if abs(total - 90.0) < 1e-9:
        return (h, w)
    r = math.radians(total)
    c, s = abs(math.cos(r)), abs(math.sin(r))
    return (w * c + h * s, w * s + h * c)


def keepout_sites(design: Design, layout: Layout,
                  library: Library) -> List[Tuple[float, float, float, float]]:
    """Every placed footprint's track-keepout areas as board-coordinate (x, y, hw, hh).

    An RF module carries a keepout around its PCB antenna.  It is not advisory: copper there
    detunes the antenna, and KiCad's DRC reports any track crossing it as
    `items_not_allowed`.  A router that does not know about them takes the shortest path,
    which for a module sat on the board edge runs straight under the antenna.
    """
    out: List[Tuple[float, float, float, float]] = []
    for ref in sorted(layout.parts):
        placed = layout.parts[ref]
        fp = library.resolve(design.parts[ref].footprint)
        for x1, y1, x2, y2 in fp.keepouts:
            dx, dy = rotate_point((x1 + x2) / 2.0, (y1 + y2) / 2.0, placed.rot)
            w, h = _rotated_extent(x2 - x1, y2 - y1, placed.rot)
            out.append((placed.x + dx, placed.y + dy, w / 2.0, h / 2.0))
    return out


def _outline_ok(outline: Outline, x: float, y: float, keep: float) -> bool:
    """Is (x, y) at least `keep` inside the board edge, chamfers included?

    Exact for the shapes `place.Outline` produces: an axis-aligned rectangle with equal
    45-degree corner chamfers.  Each chamfer is the line x + y = const in its own corner
    frame, so the inset is the constant shifted by keep*sqrt(2).
    """
    if not (outline.left + keep <= x <= outline.right - keep
            and outline.top + keep <= y <= outline.bottom - keep):
        return False
    c = outline.chamfer
    if c <= 0:
        return True
    shift = keep * math.sqrt(2.0)
    # Distance from each corner along the chamfer's normal must exceed the inset.
    if (x - outline.left) + (y - outline.top) < c + shift:
        return False
    if (outline.right - x) + (y - outline.top) < c + shift:
        return False
    if (x - outline.left) + (outline.bottom - y) < c + shift:
        return False
    if (outline.right - x) + (outline.bottom - y) < c + shift:
        return False
    return True


# ---------------------------------------------------------------------------------------
# the grid
# ---------------------------------------------------------------------------------------

class Grid:
    """Two layers of cell ownership, one int per cell.

    `_FREE` is unclaimed, `_BLOCKED` is unusable by anyone, and any other value is the
    index of the net that owns the cell.  Ownership rather than a boolean is what lets a
    track reach its own pad through the pad's own keepout.
    """

    def __init__(self, outline: Outline, layers: Sequence[str], step: float):
        self.step = step
        self.outline = outline
        self.layers = list(layers)
        self.x0 = outline.left
        self.y0 = outline.top
        self.nx = max(1, int(math.ceil(outline.width / step)) + 1)
        self.ny = max(1, int(math.ceil(outline.height / step)) + 1)
        self.cells: Dict[str, List[int]] = {
            layer: [_FREE] * (self.nx * self.ny) for layer in self.layers}
        # (cx, cy, half_w, half_h) of every surface pad's solder-mask opening.  A list
        # rather than a rasterized layer: there are tens of pads and via placement is
        # memoized, so a linear scan is cheaper than another full-board bitmap.
        self.pad_masks: List[Tuple[float, float, float, float]] = []

    # ------------------------------------------------------------------ coordinates

    def to_cell(self, x: float, y: float) -> Tuple[int, int]:
        return (int(round((x - self.x0) / self.step)),
                int(round((y - self.y0) / self.step)))

    def to_mm(self, ix: int, iy: int) -> Tuple[float, float]:
        return (self.x0 + ix * self.step, self.y0 + iy * self.step)

    def inside(self, ix: int, iy: int) -> bool:
        return 0 <= ix < self.nx and 0 <= iy < self.ny

    def _index(self, ix: int, iy: int) -> int:
        return iy * self.nx + ix

    def get(self, layer: str, ix: int, iy: int) -> int:
        return self.cells[layer][self._index(ix, iy)]

    def set(self, layer: str, ix: int, iy: int, value: int) -> None:
        self.cells[layer][self._index(ix, iy)] = value

    # ---------------------------------------------------------------------- stamping

    def claim(self, layer: str, ix: int, iy: int, net_id: int) -> None:
        """Mark a cell as belonging to `net_id`, or blocked if another net wants it too."""
        current = self.get(layer, ix, iy)
        if current == _BLOCKED or current == net_id:
            return
        self.set(layer, ix, iy, net_id if current == _FREE else _BLOCKED)

    def force(self, layer: str, ix: int, iy: int, net_id: int) -> None:
        """Claim a cell for `net_id` even if it was already blocked.

        Used for pad copper.  A pad's own body is reachable by definition — it *is* the
        net — but on a fine-pitch part its neighbours' keepouts blanket it, so a cell that
        is genuinely un-routable and a cell that is simply inside a pad look identical
        under `claim()`.  Without this an LGA-8 has no enterable pads at all and every net
        touching it reports "no clear path".
        """
        if self.inside(ix, iy):
            self.set(layer, ix, iy, net_id)

    def force_centre(self, layer: str, cx: float, cy: float, net_id: int) -> None:
        """Make just the pad's centre cell enterable by its own net.

        Deliberately one cell, not the pad's whole body.  Forcing the body looks more
        generous and is wrong: a pad's copper often reaches into a *neighbouring* pad's
        keepout, so a track allowed to wander across the body ends up running parallel to
        the neighbour at a fraction of the required clearance.  That produced exactly this
        set of DRC errors during development — six `clearance`, two `shorting_items` and
        two `solder_mask_bridge`, every one of them a 3V3 escape hugging one of U1's
        ground pads.  One cell is enough for A* to have a terminal; the escape direction
        is then chosen by real clearance, which is the whole point.
        """
        ix, iy = self.to_cell(cx, cy)
        self.force(layer, ix, iy, net_id)

    def stamp_rect(self, layer: str, cx: float, cy: float,
                   half_w: float, half_h: float, net_id: int) -> None:
        """Claim every cell whose centre falls inside a rectangle."""
        ix0, iy0 = self.to_cell(cx - half_w, cy - half_h)
        ix1, iy1 = self.to_cell(cx + half_w, cy + half_h)
        for iy in range(max(0, iy0), min(self.ny - 1, iy1) + 1):
            for ix in range(max(0, ix0), min(self.nx - 1, ix1) + 1):
                self.claim(layer, ix, iy, net_id)

    def stamp_disc(self, layer: str, cx: float, cy: float, r: float, net_id: int) -> None:
        ix0, iy0 = self.to_cell(cx - r, cy - r)
        ix1, iy1 = self.to_cell(cx + r, cy + r)
        r2 = r * r
        for iy in range(max(0, iy0), min(self.ny - 1, iy1) + 1):
            for ix in range(max(0, ix0), min(self.nx - 1, ix1) + 1):
                mx, my = self.to_mm(ix, iy)
                if (mx - cx) ** 2 + (my - cy) ** 2 <= r2:
                    self.claim(layer, ix, iy, net_id)

    def stamp_segment(self, layer: str, x1: float, y1: float, x2: float, y2: float,
                      r: float, net_id: int) -> None:
        """Claim the capsule swept by a segment of radius `r`.

        Rasterized in chunks: the segment is cut into pieces about 2r long and each
        piece's bounding box is scanned once, testing each cell's distance to the piece.

        Both simpler approaches are worse, and both were measured.  Stamping a disc every
        half cell along the segment re-visits each cell O(r/step) times, which is
        invisible on a 12 mm board and made a 23 x 35 mm one take minutes — the router
        stamps every segment it lays as an obstacle for the nets that follow, so the cost
        compounds with the copper already down.  Scanning one bounding box for the whole
        segment is worse again for a long diagonal, whose box has area O(L^2) against the
        capsule's O(L*r).  Chunking keeps it O(L*r) with no repeated visits.
        """
        length = math.hypot(x2 - x1, y2 - y1)
        pieces = max(1, int(length / max(2.0 * r, self.step)))
        for i in range(pieces):
            t0 = i / pieces
            t1 = (i + 1) / pieces
            self._stamp_capsule(layer,
                                x1 + (x2 - x1) * t0, y1 + (y2 - y1) * t0,
                                x1 + (x2 - x1) * t1, y1 + (y2 - y1) * t1,
                                r, net_id)

    def _stamp_capsule(self, layer: str, x1: float, y1: float, x2: float, y2: float,
                       r: float, net_id: int) -> None:
        dx, dy = x2 - x1, y2 - y1
        length2 = dx * dx + dy * dy
        ix0, iy0 = self.to_cell(min(x1, x2) - r, min(y1, y2) - r)
        ix1, iy1 = self.to_cell(max(x1, x2) + r, max(y1, y2) + r)
        r2 = r * r
        for iy in range(max(0, iy0), min(self.ny - 1, iy1) + 1):
            for ix in range(max(0, ix0), min(self.nx - 1, ix1) + 1):
                mx, my = self.to_mm(ix, iy)
                if length2 <= 0.0:
                    t = 0.0
                else:
                    t = ((mx - x1) * dx + (my - y1) * dy) / length2
                    t = 0.0 if t < 0.0 else (1.0 if t > 1.0 else t)
                ex = mx - (x1 + dx * t)
                ey = my - (y1 + dy * t)
                if ex * ex + ey * ey <= r2:
                    self.claim(layer, ix, iy, net_id)

    def block_outside(self, keep: float) -> None:
        """Block every cell closer than `keep` to the board edge, on every layer."""
        for iy in range(self.ny):
            for ix in range(self.nx):
                mx, my = self.to_mm(ix, iy)
                if not _outline_ok(self.outline, mx, my, keep):
                    for layer in self.layers:
                        self.set(layer, ix, iy, _BLOCKED)


# ---------------------------------------------------------------------------------------
# A*
# ---------------------------------------------------------------------------------------

def _passable(grid: Grid, layer: str, ix: int, iy: int, net_id: int) -> bool:
    if not grid.inside(ix, iy):
        return False
    value = grid.get(layer, ix, iy)
    return value == _FREE or value == net_id


def _via_in_pad(grid: Grid, x: float, y: float, land_radius: float) -> bool:
    """Would a via land here overlap some pad's solder-mask opening?"""
    for cx, cy, hw, hh in grid.pad_masks:
        if abs(x - cx) <= hw + land_radius and abs(y - cy) <= hh + land_radius:
            return True
    return False


def _via_fits(grid: Grid, ix: int, iy: int, net_id: int, radius: float,
              cache: Dict[Tuple[int, int], bool], land_radius: float = 0.0) -> bool:
    """Can a via sit at this cell without violating anything?

    A via is much larger than the track that reaches it — 0.8 mm against 0.25 mm here — so
    "the track may pass" and "a via may land" are different questions, and A* asking only
    the first is what put 3V3 vias on top of U1's ground pads.  The answer is memoized
    because a layer change is attempted at most once per visited cell, but the disc scan
    behind it is not cheap.

    Every layer is checked, not just the two being joined: a through via occupies all of
    them.
    """
    key = (ix, iy)
    hit = cache.get(key)
    if hit is not None:
        return hit

    cx, cy = grid.to_mm(ix, iy)
    if land_radius and _via_in_pad(grid, cx, cy, land_radius):
        cache[key] = False
        return False
    # A cell and a half of slack.  The margin is exact arithmetic on a discrete grid, and
    # a via whose required clearance lands within a cell of the boundary rounds to
    # "passable" here and to a clearance error in DRC.  Three quarters of a cell was not
    # enough on the larger example -- a ground via came out 0.175 mm from an SCL track
    # where 0.2 mm was required, a 0.025 mm shortfall that only shows up once anchor cells
    # have been forced passable for their own net.
    radius = radius + grid.step * 1.5
    span = int(math.ceil(radius / grid.step))
    r2 = radius * radius
    ok = True
    for layer in grid.layers:
        for dy in range(-span, span + 1):
            for dx in range(-span, span + 1):
                px, py = ix + dx, iy + dy
                if not grid.inside(px, py):
                    ok = False
                    break
                mx, my = grid.to_mm(px, py)
                if (mx - cx) ** 2 + (my - cy) ** 2 > r2:
                    continue
                if not _passable(grid, layer, px, py, net_id):
                    ok = False
                    break
            if not ok:
                break
        if not ok:
            break

    cache[key] = ok
    return ok


def _astar(grid: Grid, net_id: int, start: Tuple[str, int, int],
           goals: Set[Tuple[str, int, int]],
           via_cost: float, via_radius: float = 0.0,
           via_land: float = 0.0) -> Optional[List[Tuple[str, int, int]]]:
    """Cheapest path from `start` to any cell in `goals`, or None.

    States carry the direction they were entered by, so a turn can be charged.  Without
    that the search returns whichever of thousands of equal-cost staircases the heap
    happened to pop first, and the emitted copper looks random.
    """
    if not goals:
        return None

    goal_cells = {(ix, iy) for _layer, ix, iy in goals}

    def heuristic(ix: int, iy: int) -> float:
        best = None
        for gx, gy in goal_cells:
            dx, dy = abs(ix - gx), abs(iy - gy)
            # Octile distance: the exact cost of an unobstructed 8-connected walk.
            d = (dx + dy) + (math.sqrt(2.0) - 2.0) * min(dx, dy)
            best = d if best is None or d < best else best
        return best or 0.0

    via_cache: Dict[Tuple[int, int], bool] = {}
    start_state = (start[0], start[1], start[2], 0, 0)
    open_heap: List[Tuple[float, int, Tuple]] = []
    counter = 0
    heapq.heappush(open_heap, (heuristic(start[1], start[2]), counter, start_state))
    best_cost: Dict[Tuple, float] = {start_state: 0.0}
    came: Dict[Tuple, Optional[Tuple]] = {start_state: None}

    while open_heap:
        _priority, _tie, state = heapq.heappop(open_heap)
        layer, ix, iy, dx_in, dy_in = state
        cost = best_cost[state]

        if (layer, ix, iy) in goals:
            path: List[Tuple[str, int, int]] = []
            cursor: Optional[Tuple] = state
            while cursor is not None:
                path.append((cursor[0], cursor[1], cursor[2]))
                cursor = came[cursor]
            path.reverse()
            return path

        for dx, dy, step_cost in _STEPS:
            nx_, ny_ = ix + dx, iy + dy
            if not _passable(grid, layer, nx_, ny_, net_id):
                continue
            if dx and dy:
                # Never cut a corner: a diagonal between two blocked orthogonals would
                # squeeze copper through a gap narrower than the clearance that created it.
                if not (_passable(grid, layer, ix + dx, iy, net_id)
                        and _passable(grid, layer, ix, iy + dy, net_id)):
                    continue
            turn = TURN_COST_STEPS if (dx_in or dy_in) and (dx, dy) != (dx_in, dy_in) else 0.0
            nxt = (layer, nx_, ny_, dx, dy)
            new_cost = cost + step_cost + turn
            if new_cost < best_cost.get(nxt, float("inf")):
                best_cost[nxt] = new_cost
                came[nxt] = state
                counter += 1
                heapq.heappush(open_heap,
                               (new_cost + heuristic(nx_, ny_), counter, nxt))

        for other in grid.layers:
            if other == layer or not _passable(grid, other, ix, iy, net_id):
                continue
            if via_radius > 0 and not _via_fits(grid, ix, iy, net_id, via_radius,
                                                via_cache, via_land):
                continue
            nxt = (other, ix, iy, 0, 0)
            new_cost = cost + via_cost
            if new_cost < best_cost.get(nxt, float("inf")):
                best_cost[nxt] = new_cost
                came[nxt] = state
                counter += 1
                heapq.heappush(open_heap, (new_cost + heuristic(ix, iy), counter, nxt))

    return None


def _path_to_copper(grid: Grid, path: Sequence[Tuple[str, int, int]], net: str,
                    width: float, via_size: float, via_drill: float,
                    start_at: Tuple[float, float],
                    end_at: Tuple[float, float]) -> Tuple[List[Track], List[Via]]:
    """Collapse a cell path into segments and vias.

    Collinear runs become one segment: a track per grid cell would be legal and would also
    make the board unreadable and the file enormous.  The first and last segments are
    anchored on the real pad centres rather than their grid cells, so connectivity does not
    depend on the rounding.
    """
    tracks: List[Track] = []
    vias: List[Via] = []
    if not path:
        return tracks, vias

    points: List[Tuple[str, float, float]] = []
    for layer, ix, iy in path:
        mx, my = grid.to_mm(ix, iy)
        points.append((layer, mx, my))

    # Anchor the ends on the pads.
    points[0] = (points[0][0], start_at[0], start_at[1])
    points[-1] = (points[-1][0], end_at[0], end_at[1])

    run_start = 0
    for i in range(1, len(points)):
        layer_prev, px, py = points[i - 1]
        layer_now, cx, cy = points[i]

        if layer_now != layer_prev:
            _flush_run(tracks, points, run_start, i - 1, net, width)
            vias.append(Via(x=px, y=py, net=net, size=via_size, drill=via_drill))
            run_start = i
            continue

        if i + 1 < len(points):
            layer_next, nx_, ny_ = points[i + 1]
            if layer_next == layer_now:
                a = (cx - px, cy - py)
                b = (nx_ - cx, ny_ - cy)
                # Cross product zero means the three points are collinear, so the middle
                # one is not a corner and can be dropped.
                if abs(a[0] * b[1] - a[1] * b[0]) < 1e-9:
                    continue
        _flush_run(tracks, points, run_start, i, net, width)
        run_start = i

    return tracks, vias


def _flush_run(tracks: List[Track], points: Sequence[Tuple[str, float, float]],
               start: int, end: int, net: str, width: float) -> None:
    if end <= start:
        return
    layer, x1, y1 = points[start]
    _layer2, x2, y2 = points[end]
    if abs(x1 - x2) < 1e-9 and abs(y1 - y2) < 1e-9:
        return
    tracks.append(Track(x1=x1, y1=y1, x2=x2, y2=y2, net=net, layer=layer, width=width))


# ---------------------------------------------------------------------------------------
# net decomposition
# ---------------------------------------------------------------------------------------

def _spanning_edges(sites: Sequence[PadSite]) -> List[Tuple[int, int, float]]:
    """A minimum spanning tree over pads, as (i, j, length), shortest first (Prim's).

    A tree, not a chain: routing a net as "pad 1 to pad 2 to pad 3" makes the copper depend
    on the order the pads happened to be listed in, and produces a long detour whenever
    that order is not geometric.
    """
    n = len(sites)
    if n < 2:
        return []
    inside = {0}
    edges: List[Tuple[int, int, float]] = []
    while len(inside) < n:
        best: Optional[Tuple[float, int, int]] = None
        for i in sorted(inside):
            for j in range(n):
                if j in inside:
                    continue
                d = math.hypot(sites[i].x - sites[j].x, sites[i].y - sites[j].y)
                if best is None or d < best[0]:
                    best = (d, i, j)
        assert best is not None
        d, i, j = best
        inside.add(j)
        edges.append((i, j, d))
    return sorted(edges, key=lambda e: e[2])


# ---------------------------------------------------------------------------------------
# the router
# ---------------------------------------------------------------------------------------

@dataclass
class _Copper:
    """One piece of existing copper the grid has to model, in board coordinates."""

    kind: str                      # "pad" | "track" | "via"
    layers: Tuple[str, ...]
    net_id: int
    x: float
    y: float
    half_w: float = 0.0
    half_h: float = 0.0
    x2: float = 0.0
    y2: float = 0.0
    radius: float = 0.0


def _build_grid(outline: Outline, layers: Sequence[str], step: float,
                obstacles: Sequence[_Copper], keep: float,
                edge_keep: float) -> Grid:
    """A grid sized for one net's track width.

    Rebuilt per net rather than shared, because the keepout a track needs depends on its
    own width: a 0.4 mm power track and a 0.25 mm signal need 0.075 mm different clearance
    from every obstacle on the board.  Sharing one grid means using the widest track's
    keepout for everything, which walls off corridors the narrow nets could legally use —
    and on a fine-pitch part that is the difference between routable and not.
    """
    grid = Grid(outline, layers, step)
    grid.block_outside(edge_keep)

    for item in obstacles:
        for layer in item.layers:
            if layer not in grid.cells:
                continue
            if item.kind == "pad":
                grid.stamp_rect(layer, item.x, item.y,
                                item.half_w + keep, item.half_h + keep, item.net_id)
            elif item.kind == "track":
                grid.stamp_segment(layer, item.x, item.y, item.x2, item.y2,
                                   item.radius + keep, item.net_id)
            else:
                grid.stamp_disc(layer, item.x, item.y, item.radius + keep, item.net_id)

    for item in obstacles:
        if item.kind == "pad":
            grid.pad_masks.append((item.x, item.y,
                                   item.half_w + MASK_CLEARANCE,
                                   item.half_h + MASK_CLEARANCE))

    # Pad centres last, and forcibly: a pad is always a legal terminal for its own net.
    for item in obstacles:
        if item.kind != "pad" or item.net_id == _BLOCKED:
            continue
        for layer in item.layers:
            if layer in grid.cells:
                grid.force_centre(layer, item.x, item.y, item.net_id)
    return grid


def route(design: Design, layout: Layout, library: Library,
          grid_step: float = DEFAULT_GRID) -> RouteResult:
    """Route every net the ground pour does not already handle."""
    result = RouteResult()
    if layout.outline is None:
        raise ValueError("layout has no outline — run place.solve() first")

    rules = design.rules
    layers = ["F.Cu", "B.Cu"] if design.board.layers >= 2 else ["F.Cu"]
    sites = pad_sites(design, layout, library)

    ground = design.board.ground_net
    pour = design.board.pour_ground and ground in design.nets and len(layers) > 1
    pour_layer = "B.Cu" if pour else ""

    # Net ids start at 1.  Ground keeps one even when poured, because its pads still have
    # to block everyone else.
    net_ids: Dict[str, int] = {name: i + 1 for i, name in enumerate(sorted(design.nets))}

    obstacles: List[_Copper] = []
    for site in sites:
        nid = net_ids.get(site.net, _BLOCKED) if site.net else _BLOCKED
        on = tuple(layer for layer in layers if site.on(layer))
        obstacles.append(_Copper(kind="pad", layers=on, net_id=nid,
                                 x=site.x, y=site.y,
                                 half_w=site.half_w, half_h=site.half_h))

    # A keepout belongs to no net, so `_BLOCKED` keeps every net out of it on every layer.
    for kx, ky, khw, khh in keepout_sites(design, layout, library):
        obstacles.append(_Copper(kind="pad", layers=tuple(layers), net_id=_BLOCKED,
                                 x=kx, y=ky, half_w=khw, half_h=khh))

    def keep_for(width: float) -> float:
        return rules.clearance + width / 2.0

    def edge_keep_for(width: float) -> float:
        return max(rules.edge_clearance, 0.0) + width / 2.0

    # --- reserve one escape per pad before anything routes ------------------------------
    # Whoever routes first otherwise takes the space every other pad needs to get out.
    # That is not a tie-break detail: on the example design SDA and 3V3 between them
    # enclosed one of U1's ground pads so completely that nothing could reach it, at any
    # width, on any grid.  Reserving a narrow corridor out of every pad first costs almost
    # no board area and makes the order the nets happen to be routed in stop mattering.
    obstacles.extend(_reserve_escapes(sites, layout, layers, grid_step, net_ids, rules))

    # --- signals and power first, ground last -------------------------------------------
    # Ground is the one net that does not need a path: wherever its via lands, the pour
    # connects it.  Stitching it first spends that freedom on nothing and takes space the
    # other nets do need.  Three variants of "help ground earlier" were measured against
    # the two example designs and every one is a loss:
    #
    #   stitch ground first        walls in U1's 3V3 pads completely — unroutable at
    #                              0.15 mm on a 0.05 mm grid
    #   reserve a via site per     3 s and fully routed becomes 99 s with a connection
    #     ground pad up front      missing; a via site is far larger than a corridor
    #   pre-stitch only the pads   6.7 s becomes 103 s, and 3V3 loses an escape instead
    #     on dense parts
    #
    # So everything with a real routing problem gets first pick of the board, and ground
    # takes what is left.  The residual cost is documented in the README: one ground pad
    # in the middle of a fine-pitch part can still end up with nowhere to go, and the fix
    # for that is rip-up-and-reroute, which this router does not have.
    by_net: Dict[str, List[PadSite]] = {}
    for site in sites:
        if site.net:
            by_net.setdefault(site.net, []).append(site)

    routable = [name for name in sorted(by_net) if not (pour and name == ground)]
    # Fewest pads first, then shortest: an easy net that locks in early is copper the hard
    # nets can plan around, and a hard net routed first tends to sprawl across the board.
    routable.sort(key=lambda name: (len(by_net[name]),
                                    sum(e[2] for e in _spanning_edges(by_net[name])),
                                    name))

    for name in routable:
        pads = sorted(by_net[name], key=lambda s: (s.x, s.y, s.pin))
        nid = net_ids[name]
        nominal = rules.track_widths.get(name, rules.track_width)

        # Widths to try, in order.  A power net given a wide nominal cannot escape a
        # fine-pitch part — a 0.4 mm track between two ground pads on a 0.65 mm pitch does
        # not fit at 0.2 mm clearance, and the router is right to refuse it.  Real layouts
        # neck down at the pad; retrying the whole connection at the design's own minimum
        # width is the conservative version of that, and it stays clearance-correct
        # because the grid is rebuilt for the width actually emitted.
        widths = [nominal]
        if rules.min_track_width < nominal:
            widths.append(rules.min_track_width)

        # Attempts in increasing order of desperation.  The fine grid comes last because
        # it costs four times the cells: a 0.1 mm grid can miss a corridor only 0.25 mm
        # wide purely by where its cells happen to land, so halving the pitch is a real
        # second chance rather than a repeat of the same search.
        attempts = ([(w, grid_step) for w in widths]
                    + [(w, grid_step / 2.0) for w in widths])

        grids: Dict[Tuple[float, float], Grid] = {}

        def grid_for(width: float, step: float) -> Grid:
            key = (width, step)
            if key not in grids:
                grids[key] = _build_grid(layout.outline, layers, step, obstacles,
                                         keep_for(width), edge_keep_for(width))
            return grids[key]

        for i, j, _length in _spanning_edges(pads):
            a, b = pads[i], pads[j]
            path = None
            used_width = nominal
            grid = grid_for(nominal, grid_step)

            for width, step in attempts:
                grid = grid_for(width, step)
                keep = keep_for(width)
                starts = _cells_for(grid, a, layers)
                goals = _cells_for(grid, b, layers)
                if not starts or not goals:
                    continue
                for start in starts:
                    path = _astar(grid, nid, start, set(goals), VIA_COST_STEPS,
                                  via_radius=_via_margin(rules, width),
                                  via_land=rules.via_diameter / 2.0)
                    if path:
                        break
                if path:
                    used_width = width
                    break

            if not path:
                result.failures.append(Failure(
                    name, a.pin, b.pin,
                    f"no clear path at width {min(widths):g} mm even on a "
                    f"{grid_step / 2.0:g} mm grid; the parts around it are packed "
                    "tighter than clearance plus track width allows"))
                continue

            if used_width != nominal:
                result.necked.append(
                    f"{name}: {a.pin} -> {b.pin} at {used_width:g} mm "
                    f"(nominal {nominal:g} mm would not fit)")

            keep = keep_for(used_width)
            tracks, vias = _path_to_copper(
                grid, path, name, used_width, rules.via_diameter, rules.via_drill,
                (a.x, a.y), (b.x, b.y))
            result.tracks.extend(tracks)
            result.vias.extend(vias)

            # Claim the new copper on every grid this net is using, and record it so
            # later nets see it too.
            for track in tracks:
                for candidate in grids.values():
                    candidate.stamp_segment(track.layer, track.x1, track.y1,
                                            track.x2, track.y2,
                                            used_width / 2.0 + keep, nid)
                obstacles.append(_Copper(kind="track", layers=(track.layer,), net_id=nid,
                                         x=track.x1, y=track.y1,
                                         x2=track.x2, y2=track.y2,
                                         radius=used_width / 2.0))
            for via in vias:
                for candidate in grids.values():
                    for layer in layers:
                        candidate.stamp_disc(layer, via.x, via.y,
                                             via.size / 2.0 + keep, nid)
                obstacles.append(_Copper(kind="via", layers=tuple(layers), net_id=nid,
                                         x=via.x, y=via.y, radius=via.size / 2.0))

    # --- ground: stitch into whatever space is left -------------------------------------
    if pour:
        keep = keep_for(rules.track_widths.get(ground, rules.track_width))
        grid = _build_grid(layout.outline, layers, grid_step, obstacles, keep,
                           edge_keep_for(rules.via_diameter))
        result.stitched, stitch_vias, stitch_stubs = _stitch_ground(
            design, grid, sites, net_ids[ground], ground, rules, keep, pour_layer)
        result.vias.extend(stitch_vias)
        result.tracks.extend(stitch_stubs)

        for via in stitch_vias:
            obstacles.append(_Copper(kind="via", layers=tuple(layers),
                                     net_id=net_ids[ground], x=via.x, y=via.y,
                                     radius=via.size / 2.0))
        for stub in stitch_stubs:
            obstacles.append(_Copper(kind="track", layers=(stub.layer,),
                                     net_id=net_ids[ground], x=stub.x1, y=stub.y1,
                                     x2=stub.x2, y2=stub.y2, radius=stub.width / 2.0))

        # A pad with no room beside it for its own via is not stranded: it only has to
        # reach ground copper that *is* in the pour.  Routing it to the nearest stitched
        # pad or via is cheaper than a via anyway, and it is what saves the corner pads of
        # a fine-pitch part, which are the ones most likely to be boxed in.
        _reach_ground(result, design, layout, sites, obstacles, layers, grid_step,
                      net_ids[ground], ground, keep_for, edge_keep_for, rules)

    # Same-net copper is passable while routing, which permits two otherwise useful things
    # that KiCad quite correctly refuses at fabrication time: overlapping drills, and a track
    # merely grazing a via land.  Repair both without deleting the tracks that reached them.
    result.vias, repairs = _repair_same_net_contacts(
        result.vias, result.tracks, layers, rules)
    result.tracks.extend(repairs)
    return result


def _reach_ground(result: RouteResult, design: Design, layout: Layout,
                  sites: Sequence[PadSite], obstacles: List[_Copper],
                  layers: Sequence[str], grid_step: float, net_id: int, ground: str,
                  keep_for, edge_keep_for, rules) -> None:
    """Connect ground pads that could not take a via to ground copper that already can."""
    done = set(result.stitched)
    stranded = [s for s in sites if s.net == ground and s.pin not in done]
    if not stranded:
        return

    nominal = rules.track_widths.get(ground, rules.track_width)
    # Same ladder the signal nets get.  A ground pad boxed in by a neighbour's keepout
    # usually has an escape that a narrow track fits and a wide one does not, and a thin
    # ground stub is a far better outcome than a pad that never reaches the pour.
    widths = [nominal] + ([rules.min_track_width]
                          if rules.min_track_width < nominal else [])
    attempts = ([(w, grid_step) for w in widths]
                + [(w, grid_step / 2.0) for w in widths])
    grids: Dict[Tuple[float, float], Grid] = {}

    def grid_for(width: float, step: float) -> Grid:
        key = (width, step)
        if key not in grids:
            grids[key] = _build_grid(layout.outline, layers, step, obstacles,
                                     keep_for(width), edge_keep_for(width))
        return grids[key]

    anchors: List[Tuple[float, float, Tuple[str, ...]]] = []
    for via in result.vias:
        if via.net == ground:
            anchors.append((via.x, via.y, tuple(layers)))
    for site in sites:
        if site.net == ground and site.pin in done:
            anchors.append((site.x, site.y,
                            tuple(layer for layer in layers if site.on(layer))))

    for site in sorted(stranded, key=lambda s: (s.ref, s.number)):
        path = None
        used_width = nominal
        grid = grid_for(nominal, grid_step)

        # First: walk out to anywhere a via fits and drop one there.  Requiring the via to
        # sit *beside* the pad is what strands the middle pad of a fine-pitch row — its
        # immediate surroundings are its neighbours' keepouts, but two millimetres away
        # there is open board.  A short track to a via out there is an ordinary layout
        # move, and it is the difference between a stranded pad and a routed one.
        for width, step in attempts:
            grid = grid_for(width, step)
            spot = None
            size, drill = rules.via_diameter, rules.via_drill
            for size, drill in via_ladder(rules):
                spot = _bfs_to_via_site(grid, site, net_id,
                                        _via_margin(rules, width, size),
                                        land_radius=size / 2.0)
                if spot:
                    break
            if spot:
                used_width = width
                path = None
                points, (vx, vy) = spot
                tracks: List[Track] = []
                for (x1, y1), (x2, y2) in zip(points, points[1:]):
                    tracks.append(Track(x1=x1, y1=y1, x2=x2, y2=y2, net=ground,
                                        layer=site.layer if site.layer != "*"
                                        else layers[0], width=width))
                via = Via(x=vx, y=vy, net=ground, size=size, drill=drill)
                result.tracks.extend(_collapse(tracks))
                result.vias.append(via)
                result.stitched.append(site.pin)
                if width != nominal:
                    result.necked.append(
                        f"{ground}: {site.pin} -> pour at {width:g} mm "
                        f"(nominal {nominal:g} mm would not fit)")
                keep = keep_for(width)
                for track in _collapse(tracks):
                    for candidate in grids.values():
                        candidate.stamp_segment(track.layer, track.x1, track.y1,
                                                track.x2, track.y2,
                                                width / 2.0 + keep, net_id)
                    obstacles.append(_Copper(kind="track", layers=(track.layer,),
                                             net_id=net_id, x=track.x1, y=track.y1,
                                             x2=track.x2, y2=track.y2,
                                             radius=width / 2.0))
                for candidate in grids.values():
                    for layer in layers:
                        candidate.stamp_disc(layer, vx, vy, size / 2.0 + keep, net_id)
                obstacles.append(_Copper(kind="via", layers=tuple(layers), net_id=net_id,
                                         x=vx, y=vy, radius=size / 2.0))
                anchors.append((vx, vy, tuple(layers)))
                break
        if result.stitched and result.stitched[-1] == site.pin:
            continue

        for width, step in attempts:
            grid = grid_for(width, step)
            goals = set()
            for ax, ay, on in anchors:
                ix, iy = grid.to_cell(ax, ay)
                if grid.inside(ix, iy):
                    for layer in on:
                        grid.force(layer, ix, iy, net_id)
                        goals.add((layer, ix, iy))
            if not goals:
                break
            for start in _cells_for(grid, site, layers):
                path = _astar(grid, net_id, start, goals, VIA_COST_STEPS,
                              via_radius=_via_margin(rules, width),
                              via_land=rules.via_diameter / 2.0)
                if path:
                    break
            if path:
                used_width = width
                break

        if not path:
            result.failures.append(Failure(
                ground, site.pin, "pour",
                "no room beside the pad for a stitching via and no clear path to ground "
                "copper that has one"))
            continue

        if used_width != nominal:
            result.necked.append(
                f"{ground}: {site.pin} -> pour at {used_width:g} mm "
                f"(nominal {nominal:g} mm would not fit)")

        keep = keep_for(used_width)
        _end_layer, end_ix, end_iy = path[-1]
        tracks, vias = _path_to_copper(
            grid, path, ground, used_width, rules.via_diameter, rules.via_drill,
            (site.x, site.y), grid.to_mm(end_ix, end_iy))
        result.tracks.extend(tracks)
        result.vias.extend(vias)
        result.stitched.append(site.pin)

        for track in tracks:
            for candidate in grids.values():
                candidate.stamp_segment(track.layer, track.x1, track.y1,
                                        track.x2, track.y2,
                                        used_width / 2.0 + keep, net_id)
            obstacles.append(_Copper(kind="track", layers=(track.layer,), net_id=net_id,
                                     x=track.x1, y=track.y1, x2=track.x2, y2=track.y2,
                                     radius=used_width / 2.0))
        anchors.append((site.x, site.y,
                        tuple(layer for layer in layers if site.on(layer))))


def _reserve_escapes(sites: Sequence[PadSite], layout: Layout, layers: Sequence[str],
                     grid_step: float, net_ids: Dict[str, int], rules) -> List[_Copper]:
    """One reserved corridor per pad, from the pad out to open board.

    "Open board" means a cell no pad's keepout covers — the first place a track can turn
    without being wedged between two parts.  The corridor is the design's minimum track
    width, so it costs the least area that can still carry a track, and it belongs to the
    pad's own net, which means the pad's net can use it and every other net treats it as
    copper to route around.

    Pads are processed by reference designator so the result does not depend on dict
    order; the corridor already reserved for one pad is visible to the next, so two pads
    of different nets cannot be handed the same cells.

    Deliberately NOT reserved here: the site of a ground pad's stitching via.  Reserving
    one per ground pad was tried and is a clear loss — a via site is far larger than a
    corridor, and taking that much board away before anything routes made the example
    design go from 3 s and fully routed to 99 s with a connection missing.  Ground keeps
    yielding its space and gets stitched at the end.
    """
    assert layout.outline is not None
    keep = rules.clearance + rules.min_track_width / 2.0
    pads = [_Copper(kind="pad",
                    layers=tuple(layer for layer in layers if site.on(layer)),
                    net_id=net_ids.get(site.net, _BLOCKED) if site.net else _BLOCKED,
                    x=site.x, y=site.y, half_w=site.half_w, half_h=site.half_h)
            for site in sites]
    grid = _build_grid(layout.outline, layers, grid_step, pads, keep,
                       max(rules.edge_clearance, 0.0) + rules.min_track_width / 2.0)

    # Most-constrained pad first.  Reserving in reference order hands the corridors to
    # whichever pad happens to be named earliest, and the pad that then finds nothing left
    # is reliably the one that had least to begin with — the corner pad of a fine-pitch
    # part, flanked on two sides and up against its own courtyard.  Ordering by how much
    # open space a pad has gives the scarce corridors to the pads that cannot route
    # without them, and costs the roomy pads nothing: they have alternatives.
    ranked = sorted((s for s in sites if s.net),
                    key=lambda s: (_openness(grid, layers, s, net_ids[s.net]),
                                   s.ref, s.number))

    reserved: List[_Copper] = []
    for site in ranked:
        net_id = net_ids[site.net]
        layer = site.layer if site.layer != "*" else layers[0]
        if layer not in grid.cells:
            continue

        placed = layout.parts.get(site.ref)
        path = _bfs_to_open(grid, layer, site, net_id,
                            placed.box() if placed else None)
        if not path or len(path) < 2:
            continue

        for (x1, y1), (x2, y2) in zip(path, path[1:]):
            reserved.append(_Copper(kind="track", layers=(layer,), net_id=net_id,
                                    x=x1, y=y1, x2=x2, y2=y2,
                                    radius=rules.min_track_width / 2.0))
            grid.stamp_segment(layer, x1, y1, x2, y2,
                               rules.min_track_width / 2.0 + keep, net_id)

    return reserved


def _repair_same_net_contacts(vias: Sequence[Via], tracks: Sequence[Track],
                              layers: Sequence[str], rules) -> Tuple[List[Via], List[Track]]:
    """Merge drill-spacing violations and turn narrow same-net kisses into real joints.

    The grid lets a net reuse its own cells.  That is necessary for trees to join, but it also
    means two layer changes can land 0.1 mm apart, and a later track can graze an earlier via.
    KiCad reports those as `hole_to_hole` and `connection_width` even though connectivity is
    complete.

    A naïve close-via de-duplication is unsafe: deleting a via leaves the tracks that reached its
    centre on their original layers.  Here every removed centre is bridged to the surviving via
    on *both* copper layers.  The via-clearance discs already reserved the bridge corridor, so it
    cannot invade another net.  Likewise, a same-net track that only touches the rim of a via is
    joined to its nearest point at the track's full width.
    """
    kept: List[Via] = []
    bridges: List[Track] = []
    ordered = sorted(vias, key=lambda via: (via.net, -via.size, via.x, via.y))
    for via in ordered:
        survivor = None
        for other in kept:
            if other.net != via.net:
                continue
            limit = other.drill / 2.0 + via.drill / 2.0 + MIN_HOLE_SPACING
            if math.hypot(other.x - via.x, other.y - via.y) < limit - 1.0e-9:
                survivor = other
                break
        if survivor is None:
            kept.append(via)
            continue

        if math.hypot(survivor.x - via.x, survivor.y - via.y) > 1.0e-9:
            nominal = rules.track_widths.get(via.net, rules.track_width)
            width = max(rules.min_track_width,
                        min(nominal, survivor.size, via.size))
            for layer in layers:
                bridges.append(Track(survivor.x, survivor.y, via.x, via.y,
                                     via.net, layer, width))

    # A track whose centreline passes well inside the via land already has at least the DRC
    # minimum connection width.  Only bridge the annular fringe where the two shapes touch but
    # do not share a full-width throat.
    for via in kept:
        full_joint_radius = max(0.0, via.size / 2.0 - rules.min_track_width / 2.0)
        for track in tracks:
            if track.net != via.net:
                continue
            qx, qy = _nearest_point_on_track(via.x, via.y, track)
            distance = math.hypot(qx - via.x, qy - via.y)
            touch_radius = via.size / 2.0 + track.width / 2.0
            if distance <= full_joint_radius + 1.0e-9:
                continue
            if distance > touch_radius + 1.0e-9:
                continue
            bridges.append(Track(via.x, via.y, qx, qy, via.net, track.layer,
                                 max(track.width, rules.min_track_width)))

    return kept, _unique_bridges(bridges, tracks)


def _nearest_point_on_track(x: float, y: float, track: Track) -> Tuple[float, float]:
    """Nearest point on a finite track centreline to `(x, y)`."""
    dx, dy = track.x2 - track.x1, track.y2 - track.y1
    length_sq = dx * dx + dy * dy
    if length_sq < 1.0e-18:
        return (track.x1, track.y1)
    position = ((x - track.x1) * dx + (y - track.y1) * dy) / length_sq
    position = min(1.0, max(0.0, position))
    return (track.x1 + position * dx, track.y1 + position * dy)


def _unique_bridges(tracks: Sequence[Track], existing: Sequence[Track]) -> List[Track]:
    """Deduplicate undirected repairs and omit segments the route already contains."""
    def key_for(track: Track):
        first = (round(track.x1, 9), round(track.y1, 9))
        second = (round(track.x2, 9), round(track.y2, 9))
        return (track.net, track.layer, min(first, second), max(first, second))

    existing_keys = {key_for(track) for track in existing}
    by_key: Dict[Tuple[str, str, Tuple[float, float], Tuple[float, float]], Track] = {}
    for track in tracks:
        key = key_for(track)
        if key[2] == key[3] or key in existing_keys:
            continue
        if key not in by_key or track.width > by_key[key].width:
            by_key[key] = track
    return [by_key[key] for key in sorted(by_key)]


def _collapse(tracks: Sequence[Track]) -> List[Track]:
    """Merge consecutive collinear segments so a path is not one track per grid cell."""
    out: List[Track] = []
    for track in tracks:
        if out:
            last = out[-1]
            same = (last.layer == track.layer and last.net == track.net
                    and abs(last.x2 - track.x1) < 1e-9 and abs(last.y2 - track.y1) < 1e-9)
            if same:
                a = (last.x2 - last.x1, last.y2 - last.y1)
                b = (track.x2 - track.x1, track.y2 - track.y1)
                if abs(a[0] * b[1] - a[1] * b[0]) < 1e-9:
                    last.x2, last.y2 = track.x2, track.y2
                    continue
        out.append(Track(x1=track.x1, y1=track.y1, x2=track.x2, y2=track.y2,
                         net=track.net, layer=track.layer, width=track.width))
    return [t for t in out if t.length() > 1e-9]


def _bfs_to_via_site(grid: Grid, site: PadSite, net_id: int,
                     via_margin: float, land_radius: float = 0.0,
                     max_reach: float = 4.0) -> Optional[Tuple[List[Tuple[float, float]],
                                                               Tuple[float, float]]]:
    """Nearest reachable point where a via fits, and the path to it.

    Breadth-first from the pad through cells its own net may use, stopping at the first
    cell that can also hold a via on every layer.  The path is the stub; the cell is where
    the via goes.
    """
    layer = site.layer if site.layer != "*" else grid.layers[0]
    if layer not in grid.cells:
        return None
    start = grid.to_cell(site.x, site.y)
    if not grid.inside(*start):
        return None

    seen = {start}
    queue: List[Tuple[int, int]] = [start]
    came: Dict[Tuple[int, int], Optional[Tuple[int, int]]] = {start: None}
    cache: Dict[Tuple[int, int], bool] = {}
    limit = int(max_reach / grid.step)
    head = 0

    while head < len(queue):
        cell = queue[head]
        head += 1
        ix, iy = cell

        if _via_fits(grid, ix, iy, net_id, via_margin, cache, land_radius):
            points: List[Tuple[float, float]] = []
            cursor: Optional[Tuple[int, int]] = cell
            while cursor is not None:
                points.append(grid.to_mm(*cursor))
                cursor = came[cursor]
            points.reverse()
            points[0] = (site.x, site.y)
            return (points, grid.to_mm(ix, iy))

        if abs(ix - start[0]) > limit or abs(iy - start[1]) > limit:
            continue

        for dx, dy, _cost in _STEPS:
            nxt = (ix + dx, iy + dy)
            if nxt in seen or not _passable(grid, layer, nxt[0], nxt[1], net_id):
                continue
            seen.add(nxt)
            came[nxt] = cell
            queue.append(nxt)
    return None


def _openness(grid: Grid, layers: Sequence[str], site: PadSite, net_id: int) -> int:
    """How many cells around a pad its own net could still use.

    A cheap proxy for "how much trouble is this pad in", used only to order escape
    reservation.  Counted on a ring rather than a filled disc so a large pad does not
    score highly just for being large.
    """
    layer = site.layer if site.layer != "*" else layers[0]
    if layer not in grid.cells:
        return 0
    ix, iy = grid.to_cell(site.x, site.y)
    span = int(math.ceil((max(site.half_w, site.half_h) + 0.6) / grid.step))
    count = 0
    for dy in (-span, span):
        for dx in range(-span, span + 1):
            if _passable(grid, layer, ix + dx, iy + dy, net_id):
                count += 1
    for dx in (-span, span):
        for dy in range(-span + 1, span):
            if _passable(grid, layer, ix + dx, iy + dy, net_id):
                count += 1
    return count


def _bfs_to_open(grid: Grid, layer: str, site: PadSite, net_id: int,
                 courtyard: Optional[Tuple[float, float, float, float]] = None
                 ) -> List[Tuple[float, float]]:
    """Shortest cell path from a pad's centre to open board OUTSIDE the part.

    Breadth-first rather than A*: there is no target to aim at, only a condition to
    satisfy, and the first cell reached is by construction the closest one.

    The courtyard test is what makes this an escape rather than a walk.  The middle of a
    fine-pitch part is unclaimed space — an LGA-8's two pad rows are 2 mm apart — so
    "the nearest cell nothing else claims" is happily satisfied by a pocket in the centre
    of the chip that leads nowhere.  Reserving a corridor into it wastes the reservation
    and leaves the pad with no way out, which is exactly how the middle ground pad of the
    example design ended up unroutable while every other net had already taken the space
    below the part.
    """
    start = grid.to_cell(site.x, site.y)
    if not grid.inside(*start):
        return []

    seen = {start}
    queue: List[Tuple[int, int]] = [start]
    came: Dict[Tuple[int, int], Optional[Tuple[int, int]]] = {start: None}
    head = 0
    # A pad on a crowded part can need a few tenths of a millimetre to get clear; beyond
    # roughly two millimetres the escape is no longer an escape, it is a route, and the
    # maze router should own it.
    limit = int(2.0 / grid.step)

    while head < len(queue):
        cell = queue[head]
        head += 1
        ix, iy = cell

        mx, my = grid.to_mm(ix, iy)
        outside = courtyard is None or not (courtyard[0] <= mx <= courtyard[2]
                                            and courtyard[1] <= my <= courtyard[3])
        if grid.get(layer, ix, iy) == _FREE and cell != start and outside:
            path: List[Tuple[float, float]] = []
            cursor: Optional[Tuple[int, int]] = cell
            while cursor is not None:
                path.append(grid.to_mm(*cursor))
                cursor = came[cursor]
            path.reverse()
            path[0] = (site.x, site.y)
            return path

        if abs(ix - start[0]) > limit or abs(iy - start[1]) > limit:
            continue

        for dx, dy, _cost in _STEPS:
            nxt = (ix + dx, iy + dy)
            if nxt in seen or not _passable(grid, layer, nxt[0], nxt[1], net_id):
                continue
            seen.add(nxt)
            came[nxt] = cell
            queue.append(nxt)
    return []


def _cells_for(grid: Grid, site: PadSite,
               layers: Sequence[str]) -> List[Tuple[str, int, int]]:
    """The grid cells a track may start from or end on for this pad."""
    ix, iy = grid.to_cell(site.x, site.y)
    if not grid.inside(ix, iy):
        return []
    return [(layer, ix, iy) for layer in layers if site.on(layer)]


def _stitch_ground(design: Design, grid: Grid, sites: Sequence[PadSite],
                   net_id: int, ground: str, rules, keep: float,
                   pour_layer: str) -> Tuple[List[str], List[Via], List[Track]]:
    """Bond every surface ground pad to the pour with a via and a stub beside it.

    The stub is not optional.  A via placed next to a pad with nothing joining them is
    dangling copper: DRC reports `via_dangling`, and the pad stays unconnected — which is
    exactly what happened on the first run of this code, three dangling vias and six
    connections still missing.

    Through-hole ground pads are skipped entirely: the hole already reaches the pour
    layer, so a via beside one would connect nothing to nothing.
    """
    stitched: List[str] = []
    vias: List[Via] = []
    stubs: List[Track] = []
    width = rules.track_widths.get(ground, rules.track_width)

    for site in sorted([s for s in sites if s.net == ground],
                       key=lambda s: (s.ref, s.number)):
        if site.through:
            stitched.append(site.pin)
            continue

        spot = None
        size, drill = rules.via_diameter, rules.via_drill
        for size, drill in via_ladder(rules):
            spot = _free_spot_near(grid, site, size / 2.0 + keep,
                                   _via_margin(rules, width, size), net_id, pour_layer,
                                   placed=[(v.x, v.y) for v in vias],
                                   hole_pitch=size + 0.25, size=size)
            if spot is not None:
                break
        if spot is None:
            continue
        vx, vy = spot
        vias.append(Via(x=vx, y=vy, net=ground, size=size, drill=drill))
        stubs.append(Track(x1=site.x, y1=site.y, x2=vx, y2=vy, net=ground,
                           layer=site.layer if site.layer != "*" else "F.Cu",
                           width=width))
        stitched.append(site.pin)

        # Claim the new copper immediately: the next pad's via must not land on it.
        for layer in grid.layers:
            grid.stamp_disc(layer, vx, vy, size / 2.0 + keep, net_id)
            grid.stamp_segment(layer, site.x, site.y, vx, vy, width / 2.0 + keep, net_id)

    return stitched, vias, stubs


def _free_spot_near(grid: Grid, site: PadSite, radius: float, via_margin: float,
                    net_id: int, pour_layer: str, placed: Sequence[Tuple[float, float]] = (),
                    hole_pitch: float = 0.0, size: float = 0.0,
                    max_reach: float = 1.5) -> Optional[Tuple[float, float]]:
    """The nearest spot beside a pad where a via fits *and* the stub can reach it.

    The stub is the trap.  Checking only that the via lands somewhere legal accepts a spot
    on the far side of the part, and the straight stub drawn to it then ploughs through
    every pad and track in between — during development that produced two GND tracks of
    4.35 mm and 4.75 mm crossing the whole chip, and with them six clearance errors, five
    shorts and four mask bridges.  A via is only a stitch if a track can get to it.

    `max_reach` bounds the search for the same reason: past a millimetre or two this stops
    being a stitch and becomes a route, and routing is `_reach_ground`'s job, where the
    maze router will find a path around obstacles instead of straight through them.
    """
    step = grid.step
    # Start just outside the pad's own copper plus the clearance the via needs.
    base = max(site.half_w, site.half_h) + radius
    rings = max(1, int(max_reach / step))
    for ring in range(0, rings):
        distance = base + ring * step
        for dx, dy in ((0.0, -1.0), (0.0, 1.0), (-1.0, 0.0), (1.0, 0.0),
                       (-0.7071, -0.7071), (0.7071, -0.7071),
                       (-0.7071, 0.7071), (0.7071, 0.7071)):
            vx = site.x + dx * distance
            vy = site.y + dy * distance
            # Same-net vias do not block each other on the grid -- ownership is the whole
            # point -- so nothing stops two ground stitches landing a tenth of a
            # millimetre apart, which DRC reports as hole_to_hole.  Keep them apart here.
            if any((vx - px) ** 2 + (vy - py) ** 2 < hole_pitch * hole_pitch
                   for px, py in placed):
                continue
            # Same three-quarter-cell slack as _via_fits: exact clearance arithmetic on a
            # discrete grid rounds the wrong way often enough to matter.
            if not _spot_clear(grid, vx, vy, via_margin + grid.step * 1.5, net_id):
                continue
            # The via must land on the pour layer, or it bonds to nothing.
            if pour_layer and not _spot_clear(grid, vx, vy,
                                              via_margin + grid.step * 1.5, net_id,
                                              pour_layer):
                continue
            if _via_in_pad(grid, vx, vy, size / 2.0):
                continue
            stub_layer = site.layer if site.layer != "*" else grid.layers[0]
            if not _segment_clear(grid, stub_layer, site.x, site.y, vx, vy, net_id):
                continue
            return (vx, vy)
    return None


def _segment_clear(grid: Grid, layer: str, x1: float, y1: float, x2: float, y2: float,
                   net_id: int) -> bool:
    """Can a track run straight between two points?

    Radius zero on purpose: the grid was built with this net's own track half-width folded
    into every keepout, so a passable cell already means "a track of that width centred
    here clears everything".  Adding the width again asks for twice the room the track
    needs, which silently rejects legal stubs.
    """
    steps = max(1, int(math.hypot(x2 - x1, y2 - y1) / (grid.step * 0.5)))
    for i in range(steps + 1):
        t = i / steps
        if not _spot_clear(grid, x1 + (x2 - x1) * t, y1 + (y2 - y1) * t,
                           0.0, net_id, layer):
            return False
    return True


def via_ladder(rules) -> List[Tuple[float, float]]:
    """Via sizes to try, largest first: the standard one, then the fine-pitch one."""
    sizes = [(rules.via_diameter, rules.via_drill)]
    if rules.via_diameter_min < rules.via_diameter:
        sizes.append((rules.via_diameter_min, rules.via_drill_min))
    return sizes


def _via_margin(rules, track_width: float, diameter: Optional[float] = None) -> float:
    """How much clear room a via needs *beyond* what its track already reserved.

    The grid marks a cell passable when a track of `track_width` centred there clears
    every obstacle by the design clearance.  A via at the same spot is fatter, and only
    the difference is unaccounted for.  Asking for the full via radius plus clearance
    double-counts and rejects perfectly legal via sites — which is how a ground pad ends
    up with nowhere to stitch on a board that has room for it.
    """
    size = rules.via_diameter if diameter is None else diameter
    return max(0.0, size / 2.0 - track_width / 2.0)


def _spot_clear(grid: Grid, cx: float, cy: float, radius: float, net_id: int,
                only_layer: str = "") -> bool:
    layers = [only_layer] if only_layer else grid.layers
    ix0, iy0 = grid.to_cell(cx - radius, cy - radius)
    ix1, iy1 = grid.to_cell(cx + radius, cy + radius)
    # The cell the point itself falls in, always.  Without it a radius of 0 checks nothing
    # at all — the disc test rejects every cell whose centre is not exactly on the point,
    # so the loop below finds none and the caller is told the spot is clear.  That is how
    # ground stubs ended up drawn straight through 3V3 pads and tracks.
    home = grid.to_cell(cx, cy)
    r2 = radius * radius
    for layer in layers:
        if not grid.inside(*home) or not _passable(grid, layer, home[0], home[1], net_id):
            return False
        for iy in range(iy0, iy1 + 1):
            for ix in range(ix0, ix1 + 1):
                if not grid.inside(ix, iy):
                    return False
                mx, my = grid.to_mm(ix, iy)
                if (mx - cx) ** 2 + (my - cy) ** 2 > r2:
                    continue
                if not _passable(grid, layer, ix, iy, net_id):
                    return False
    return True


# ---------------------------------------------------------------------------------------
# emission
# ---------------------------------------------------------------------------------------

_COPPER_NS = _uuid.UUID("9d4e2a86-51bc-4a07-8e33-c1f70b95d2ae")


def _uid(tag: str) -> str:
    return str(_uuid.uuid5(_COPPER_NS, tag))


def copper_items(result: RouteResult) -> List[str]:
    """The routed copper as board s-expressions, ready for `emit.splice()`.

    Nets are referenced by NAME only — this fork writes no numeric net table, so a net
    exists purely by being named on a pad, segment or via (`pylib/sexp.py:167`).  UUIDs are
    derived from the geometry so re-running the router over an unchanged design produces a
    byte-identical board and `splice()` recognises its own previous output.
    """
    items: List[str] = []
    for track in result.tracks:
        tag = (f"seg-{track.net}-{track.layer}-{track.x1:.4f},{track.y1:.4f}-"
               f"{track.x2:.4f},{track.y2:.4f}")
        items.append("\n".join([
            "\t(segment",
            f"\t\t(start {track.x1:.4f} {track.y1:.4f})",
            f"\t\t(end {track.x2:.4f} {track.y2:.4f})",
            f"\t\t(width {track.width})",
            f'\t\t(layer "{track.layer}")',
            f'\t\t(net "{_escape(track.net)}")',
            f'\t\t(uuid "{_uid(tag)}")',
            "\t)",
        ]))
    for via in result.vias:
        tag = f"via-{via.net}-{via.x:.4f},{via.y:.4f}"
        items.append("\n".join([
            "\t(via",
            f"\t\t(at {via.x:.4f} {via.y:.4f})",
            f"\t\t(size {via.size})",
            f"\t\t(drill {via.drill})",
            '\t\t(layers "F.Cu" "B.Cu")',
            f'\t\t(net "{_escape(via.net)}")',
            f'\t\t(uuid "{_uid(tag)}")',
            "\t)",
        ]))
    return items


def _escape(text: str) -> str:
    return str(text).replace("\\", "\\\\").replace('"', '\\"')
