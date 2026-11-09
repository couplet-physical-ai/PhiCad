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
"""Board furniture the netlist cannot carry: the edge cuts, the ground pour, the title.

Why this is a text splice and not a serializer
----------------------------------------------
The board these items are added to was written by KiCad itself, seconds earlier, in a
format this fork owns.  Round-tripping it through any Python writer would drop tokens the
parser does not model — and a board that has quietly lost a `(zone_connect …)` or a custom
pad primitive is broken in a way DRC will not report.  So nothing here re-serializes: the
new items are appended before the closing paren and every existing byte is left alone.

This is the same discipline `swarm/merge_routes.py` uses when it splices routed copper
between boards, and for the same reason.

What belongs here vs. in the netlist
------------------------------------
The netlist owns everything with a net: parts, pads, connectivity.  This module owns
everything that has no schematic counterpart — the board edge, a copper pour, mounting
holes, the title block. Splitting them that way keeps a single source of truth for
connectivity, which is what makes the design-versus-board comparison meaningful.
"""

from __future__ import annotations

import re
import sys
import uuid as _uuid
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import silk  # noqa: E402
from ir import Design  # noqa: E402
from place import Layout, Outline  # noqa: E402

# Deterministic UUID namespace, distinct from netlist.py's so a component and a graphic
# with the same tag never collide.
_NS = _uuid.UUID("2b8c6f11-47ad-4d3e-b0a9-16c7e5d82a30")

EDGE_WIDTH = 0.1


def uid(tag: str) -> str:
    return str(_uuid.uuid5(_NS, tag))


def gr_line(x1: float, y1: float, x2: float, y2: float, tag: str,
            layer: str = "Edge.Cuts", width: float = EDGE_WIDTH) -> str:
    return "\n".join([
        "\t(gr_line",
        f"\t\t(start {x1:.4f} {y1:.4f})",
        f"\t\t(end {x2:.4f} {y2:.4f})",
        "\t\t(stroke",
        f"\t\t\t(width {width})",
        "\t\t\t(type solid)",
        "\t\t)",
        f'\t\t(layer "{layer}")',
        f'\t\t(uuid "{uid(tag)}")',
        "\t)",
    ])


def gr_text(text: str, x: float, y: float, tag: str,
            layer: str = "F.SilkS", size: float = 1.0, rot: float = 0.0) -> str:
    return "\n".join([
        f'\t(gr_text "{_escape(text)}"',
        f"\t\t(at {x:.4f} {y:.4f} {rot:g})",
        f'\t\t(layer "{layer}")',
        f'\t\t(uuid "{uid(tag)}")',
        "\t\t(effects",
        "\t\t\t(font",
        f"\t\t\t\t(size {size} {size})",
        f"\t\t\t\t(thickness {silk.stroke_thickness(size)})",
        "\t\t\t)",
        "\t\t)",
        "\t)",
    ])


def _escape(text: str) -> str:
    return str(text).replace("\\", "\\\\").replace('"', '\\"')


def outline_items(outline: Outline) -> List[str]:
    """Edge.Cuts segments for the board edge."""
    return [gr_line(a[0], a[1], b[0], b[1], f"edge-{i}")
            for i, (a, b) in enumerate(outline.corners())]


def ground_zone(design: Design, outline: Outline, layers: Sequence[str],
                inset: Optional[float] = None) -> str:
    """A ground pour inset from the board edge.

    The inset must be at least the project's `min_copper_edge_clearance`, or every pour
    reports `copper_edge_clearance` against the outline it was derived from — a violation
    that looks like a routing problem and is really an arithmetic one.
    """
    gap = design.rules.clearance
    keep = inset if inset is not None else max(design.rules.edge_clearance, 0.5)
    left = outline.left + keep
    top = outline.top + keep
    right = outline.right - keep
    bottom = outline.bottom - keep
    c = max(outline.chamfer - keep, 0.0)

    if c > 0:
        pts = [(left + c, top), (right - c, top), (right, top + c), (right, bottom - c),
               (right - c, bottom), (left + c, bottom), (left, bottom - c), (left, top + c)]
    else:
        pts = [(left, top), (right, top), (right, bottom), (left, bottom)]

    lines = [
        "\t(zone",
        f'\t\t(net "{_escape(design.board.ground_net)}")',
        "\t\t(layers" + "".join(f' "{layer}"' for layer in layers) + ")",
        f'\t\t(uuid "{uid("zone-" + design.board.ground_net)}")',
        f'\t\t(name "{_escape(design.board.ground_net)}")',
        "\t\t(hatch edge 0.5)",
        "\t\t(connect_pads",
        f"\t\t\t(clearance {gap})",
        "\t\t)",
        f"\t\t(min_thickness {design.rules.min_track_width})",
        "\t\t(filled_areas_thickness no)",
        "\t\t(fill yes",
        "\t\t\t(thermal_gap 0.5)",
        "\t\t\t(thermal_bridge_width 0.5)",
        "\t\t)",
        "\t\t(polygon",
        "\t\t\t(pts",
    ]
    for x, y in pts:
        lines.append(f"\t\t\t\t(xy {x:.4f} {y:.4f})")
    lines += ["\t\t\t)", "\t\t)", "\t)"]
    return "\n".join(lines)


def board_items(design: Design, layout: Layout,
                title: Optional["silk.RefLabel"] = None) -> List[str]:
    """Everything to splice into the board after the netlist import placed the parts.

    `title` is the placement `silk.title_label()` chose, or None for a board with no room
    for one.  It is a parameter rather than something computed here because the title has
    to miss the reference designators, and those are not known until after `silk.solve()`.
    """
    if layout.outline is None:
        raise ValueError("layout has no outline — run place.solve() first")

    items: List[str] = outline_items(layout.outline)

    if design.board.pour_ground and design.board.ground_net in design.nets:
        stack = ["F.Cu", "B.Cu"] if design.board.layers >= 2 else ["F.Cu"]
        # On a 2-layer board the pour goes on the back so the front stays available for
        # signal routing; on 4+ layers both outer layers are poured and the inner planes
        # are the caller's business.
        pour_layers = ["B.Cu"] if design.board.layers == 2 else stack
        items.append(ground_zone(design, layout.outline, pour_layers))

    if title is not None:
        items.append(title_item(title))

    return items


def title_item(title: "silk.RefLabel") -> str:
    """The board title as `silk` placed it."""
    return gr_text(title.ref, title.x, title.y, "title-silk",
                   layer=title.layer, size=title.size, rot=title.rot)


# ---------------------------------------------------------------------------------------
# splicing
# ---------------------------------------------------------------------------------------

_TAGGED_UUID = re.compile(r'\(uuid "([0-9a-f-]{36})"\)')


def splice(board_path, items: Sequence[str]) -> int:
    """Append items just before the board's final closing paren.  Returns how many landed.

    Idempotent by UUID: an item whose uuid is already in the file is skipped, so running
    the pipeline twice does not stack two outlines on top of each other — which would
    silently double every edge segment and pass DRC, because a duplicated edge is still a
    closed outline.
    """
    path = Path(board_path)
    text = path.read_text(encoding="utf-8")

    existing = set(_TAGGED_UUID.findall(text))
    fresh = [item for item in items
             if not (set(_TAGGED_UUID.findall(item)) & existing)]
    if not fresh:
        return 0

    close = text.rstrip()
    if not close.endswith(")"):
        raise ValueError(f"{path} does not end with a closing paren — not a board file?")
    body = close[:-1].rstrip("\n")

    path.write_text(body + "\n" + "\n".join(fresh) + "\n)\n", encoding="utf-8")
    return len(fresh)


# The three numbers inside a `(property "Reference" …)` node that `silk` owns.  Scoped to
# that node before they are applied: `(at …)` and `(size …)` appear on nearly every item in
# a board file, and a global substitution would move the footprint itself.
_AT_NODE = re.compile(r"\(at\s+-?[\d.]+\s+-?[\d.]+(?:\s+-?[\d.]+)?\s*\)")
_SIZE_NODE = re.compile(r"\(size\s+-?[\d.]+\s+-?[\d.]+\s*\)")
_THICKNESS_NODE = re.compile(r"\(thickness\s+-?[\d.]+\s*\)")


def set_references(board_path, labels) -> int:
    """Move each footprint's reference designator to the position `silk` chose.

    Text surgery for the same reason as `splice()`: the board was written by KiCad seconds
    ago and re-serializing it through a Python writer would drop whatever this fork stores
    that the parser does not model.  Only the three numbers change; the layer, the uuid and
    the visibility the netlist import set are left exactly as they are.

    Returns how many references were rewritten.
    """
    path = Path(board_path)
    text = path.read_text(encoding="utf-8")
    changed = 0

    for ref in sorted(labels):
        label = labels[ref]
        marker = f'(property "Reference" "{_escape(ref)}"'
        start = text.find(marker)
        if start < 0:
            continue
        # `start` already IS the property node's opening paren, so `_node_start()` must not
        # be used here: it walks OUT to the enclosing node, which for a reference field is
        # the footprint.  The slice then began at the footprint header, and the first
        # `(at …)` in it — the one `_AT_NODE` rewrites — was the footprint's own position.
        # Every part on the board moved to its label's offset.
        end = _node_end(text, start)
        if end is None:
            continue

        node = text[start:end]
        node, hits = _AT_NODE.subn(
            f"(at {label.x:.4f} {label.y:.4f} {label.rot:g})", node, count=1)
        if not hits:
            continue
        node = _SIZE_NODE.subn(f"(size {label.size} {label.size})", node, count=1)[0]
        node, hits = _THICKNESS_NODE.subn(
            f"(thickness {label.thickness})", node, count=1)
        if not hits:
            # No explicit pen means KiCad derives one, and the width `silk` sized the label
            # against would then be a guess about that derivation; state it instead.
            node = _SIZE_NODE.sub(
                lambda m: f"{m.group(0)}\n\t\t\t\t\t(thickness {label.thickness})",
                node, count=1)

        text = text[:start] + node + text[end:]
        changed += 1

    if changed:
        path.write_text(text, encoding="utf-8")
    return changed


def strip_generated(board_path) -> int:
    """Remove items this module previously spliced, so a rerun replaces rather than adds.

    Matches on the deterministic UUIDs above; anything a human or another tool added is
    untouched, because it cannot have one of these UUIDs.
    """
    path = Path(board_path)
    text = path.read_text(encoding="utf-8")
    removed = 0
    for tag in _known_tags():
        marker = f'(uuid "{uid(tag)}")'
        while marker in text:
            start = _node_start(text, text.index(marker))
            end = _node_end(text, start)
            if start is None or end is None:
                break
            text = text[:start] + text[end:]
            removed += 1
    if removed:
        path.write_text(text, encoding="utf-8")
    return removed


def _known_tags() -> List[str]:
    return ([f"edge-{i}" for i in range(8)] + ["title-silk"]
            + [f"zone-{name}" for name in ("GND", "AGND", "DGND", "VSS")])


def _node_start(text: str, marker_at: int) -> Optional[int]:
    """Walk back to the opening paren of the node CONTAINING `marker_at`.

    Starting the scan one character before the marker matters: `marker_at` points at the
    `(` of `(uuid …)` itself, so a scan that includes it finds that paren at depth 0 and
    reports the uuid line as the node.  `strip_generated` then deleted the uuid and left
    the graphic behind — while still counting it as removed, so a rerun spliced a second
    copy of every edge segment on top of the first.
    """
    depth = 0
    i = marker_at - 1
    while i >= 0:
        if text[i] == ")":
            depth += 1
        elif text[i] == "(":
            if depth == 0:
                # Include the leading whitespace on that line.
                line_start = text.rfind("\n", 0, i)
                return line_start + 1 if line_start >= 0 else 0
            depth -= 1
        i -= 1
    return None


def _node_end(text: str, start: int) -> Optional[int]:
    depth = 0
    i = text.index("(", start)
    while i < len(text):
        if text[i] == "(":
            depth += 1
        elif text[i] == ")":
            depth -= 1
            if depth == 0:
                end = text.find("\n", i)
                return (end + 1) if end >= 0 else len(text)
        i += 1
    return None

# ---------------------------------------------------------------------------------------
# placement
# ---------------------------------------------------------------------------------------

def set_positions(board_path, positions) -> int:
    """Move footprints by rewriting their `(transform …)` in place.  Returns how many moved.

    Why this is a text edit and not `board.update_items()` over IPC
    --------------------------------------------------------------
    Because kipy's footprint round trip is LOSSY, and measurably so.  A board straight out
    of `ImportNetlist` carries the 3D model reference every stock footprint declares:

        after import   bytes=26165  model=6  fp_line=34  property=31  pad=20
        after place    bytes=25029  model=0  fp_line=34  property=31  pad=20

    Reading each footprint and writing it back destroyed every `(model …)` node and nothing
    else.  The board still passes DRC — DRC does not care about 3D — and the 3D viewer then
    shows bare pads on an empty board, which is exactly how it was found.  Anything that
    moves a footprint through kipy has this bug, not just this pipeline.

    Rewriting the transform touches the four numbers that placement actually changes and
    leaves every other byte alone, which is the same discipline `splice()` above follows and
    `swarm/merge_routes.py` follows for copper.
    """
    path = Path(board_path)
    text = path.read_text(encoding="utf-8")
    moved = 0

    for ref, (x, y, rot) in sorted(positions.items()):
        span = _footprint_span(text, ref)
        if span is None:
            continue
        start, end = span
        block = text[start:end]
        rewritten = _rewrite_transform(block, x, y, rot)
        if rewritten == block:
            continue
        text = text[:start] + rewritten + text[end:]
        moved += 1

    if moved:
        path.write_text(text, encoding="utf-8")
    return moved


def _footprint_span(text: str, ref: str):
    """(start, end) of the `(footprint …)` block whose Reference property is `ref`."""
    marker = f'(property "Reference" "{ref}"'
    at = text.find(marker)
    while at != -1:
        start = text.rfind("\n\t(footprint ", 0, at)
        if start == -1:
            return None
        start += 1
        end = _node_end(text, start)
        if end is not None and start < at < end:
            return (start, end)
        at = text.find(marker, at + 1)
    return None


_TRANSLATE = re.compile(r"\(translate\s+[-\d.eE]+\s+[-\d.eE]+\s*\)")
_ROTATE = re.compile(r"\(rotate\s+[-\d.eE]+\s*\)")


def _rewrite_transform(block: str, x: float, y: float, rot: float) -> str:
    """Replace the translate and rotate inside one footprint's `(transform …)` node."""
    open_at = block.find("(transform")
    if open_at == -1:
        return block
    close_at = _node_end(block, block.rfind("\n", 0, open_at) + 1)
    if close_at is None:
        return block
    head, node, tail = block[:open_at], block[open_at:close_at], block[close_at:]

    node, n = _TRANSLATE.subn(f"(translate {x:.6f} {y:.6f})", node, count=1)
    if not n:
        return block
    if _ROTATE.search(node):
        node = _ROTATE.sub(f"(rotate {rot:g})", node, count=1)
    elif abs(rot) > 1e-9:
        # A footprint imported at zero rotation carries no (rotate …) at all; add one
        # before (scale …), which is where the writer puts it.
        node = node.replace("(scale", f"(rotate {rot:g})\n\t\t\t(scale", 1)
    return head + node + tail
