#!/usr/bin/env python3
#
# This program source code file is part of KiCad, a free EDA CAD application.
#
# Copyright (C) 2026 Exowatt, Inc.
#
#  This program is free software; you can redistribute it and/or modify
#  it under the terms of the GNU General Public License as published by
#  the Free Software Foundation; either version 3 of the License, or
#  (at your option) any later version.
#
#  This program is distributed in the hope that it will be useful,
#  but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#  GNU General Public License for more details.
#
#  You should have received a copy of the GNU General Public License
#  along with this program.  If not, see <https://www.gnu.org/licenses/>.
#
"""Build InteractiveHtmlBom's *generic JSON* input from `kicad-cli` output only.

WHY THIS EXISTS
    InteractiveHtmlBom (ibom, MIT) renders an interactive assembly BOM, and its normal input
    path is `import pcbnew` -- the SWIG module.  This fork ships no SWIG module (see
    phicad/README.md), and ibom's IPC-API backend is still an open upstream PR, so the only
    supported way in is ibom's documented *generic JSON* format
    (InteractiveHtmlBom/ecad/genericjson.py + ecad/schema/genericjsonpcbdata_v1.schema).

    This module is the translator, and it is deliberately pure stdlib: it turns three text
    artifacts the fork's own `kicad-cli` already emits into that JSON.  Nothing here spawns a
    process, reads a board file or needs a venv, so `phicad check fast` can gate the whole
    shape of the document (phicad/mcp/tools_cli.py::pcb_export_ibom is the part that runs
    kicad-cli and then ibom as a subprocess).

THE THREE INPUTS AND WHAT EACH ONE CONTRIBUTES
    `kicad-cli pcb export pos --format csv --side both --units mm`
        Ref, Val, Package, PosX, PosY, Rot, Side -> one ibom component + footprint each.
        This is the only REQUIRED input; the component list is exactly its rows.
    `kicad-cli pcb export ipcd356`
        The board's own netlist WITH pad geometry: net name, refdes, pin, hole, access code,
        centre, size and rotation for every pad and via.  This is what gives the render real
        pads (and therefore real per-footprint bounding boxes) and the net list, without a
        schematic and without parsing the board.  Optional: without it every footprint falls
        back to a nominal square box and the BOM carries no nets.
    `kicad-cli pcb export svg -l Edge.Cuts --exclude-drawing-sheet --page-size-mode 1`
        The board outline in board millimetres -> ibom `edges` + `edges_bbox`.  Optional:
        without it the outline is a synthetic rectangle around the placements (flagged in
        `warnings`, because it is a guess, not the board).

COORDINATES
    pos CSV and IPC-D-356 both report Y in the "Y up" convention (a footprint at board
    y=99.695 mm is written as -99.695), while ibom, like the board file, has Y growing
    downwards.  Every parser here negates Y once, at the edge, so everything downstream of a
    `parse_*` call is in ibom/board millimetres.  IPC-D-356 lengths are 0.0001 inch integers.

WHAT IS NOT IN THE DOCUMENT
    Silkscreen and fabrication drawings (ibom renders them, but no CLI export gives them as
    geometry), tracks and zones (the schema makes them optional and mutually dependent), and
    pad shapes finer than rect/circle -- IPC-D-356 only carries a size pair. `warnings` on the
    result records every such gap, and pcb_export_ibom passes them to the caller.

Run standalone (handy when debugging a board):

    python3 phicad/pylib/ibom_json.py --pos b.pos.csv [--d356 b.d356] [--edges b-edge.svg] \
        [--title T] [--revision R] [--company C] [--date D] -o b.ibom.json
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import math
import xml.etree.ElementTree as ET
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

#: The only generic-JSON spec version ibom accepts (GenericJsonParser.COMPATIBLE_SPEC_VERSIONS).
SPEC_VERSION = 1

#: IPC-D-356 "UNITS CUST 0" is 0.0001 inch integers.
D356_UNIT_MM = 25.4 / 10000.0

#: Fallback half-size of a footprint box when no pad geometry is available, in mm.
DEFAULT_BOX_MM = 1.0

#: Margin added around a footprint's pads to make its highlight box, in mm.
BOX_MARGIN_MM = 0.15

#: Margin added around the placements when the board outline is unknown, in mm.
OUTLINE_MARGIN_MM = 2.0


# --------------------------------------------------------------------------------------
# Parsed inputs
# --------------------------------------------------------------------------------------


@dataclass
class Placement:
    """One row of `pcb export pos` in ibom coordinates (Y already flipped)."""

    ref: str
    val: str
    package: str
    x: float
    y: float
    rot: float
    side: str  # "top" | "bottom"

    @property
    def layer(self) -> str:
        return "B" if self.side == "bottom" else "F"


@dataclass
class PadRecord:
    """One IPC-D-356 feature record (a pad or a via) in ibom coordinates."""

    record: str          # "317" | "327" | "307" | "367"
    net: str             # "" when the record says N/C
    ref: str             # "VIA" for a via
    pin: str
    x: float
    y: float
    size_x: float
    size_y: float        # 0.0 means "round, diameter = size_x"
    rot: float
    drill: float         # 0.0 for an SMD pad
    access: int          # 0 = both sides, 1 = top, >1 = an inner or the bottom layer

    @property
    def is_via(self) -> bool:
        return self.ref.upper() == "VIA" or not self.pin

    @property
    def through(self) -> bool:
        return self.drill > 0.0 or self.access == 0

    @property
    def layers(self) -> list[str]:
        if self.through:
            return ["F", "B"]
        return ["F"] if self.access <= 1 else ["B"]


@dataclass
class BoardOutline:
    """Board edges as ibom drawing dicts, plus the bbox they span."""

    edges: list[dict[str, Any]] = field(default_factory=list)
    minx: float | None = None
    miny: float | None = None
    maxx: float | None = None
    maxy: float | None = None

    @property
    def known(self) -> bool:
        return bool(self.edges) and self.minx is not None


# --------------------------------------------------------------------------------------
# `pcb export pos --format csv`
# --------------------------------------------------------------------------------------

_POS_HEADER = ("Ref", "Val", "Package", "PosX", "PosY", "Rot", "Side")


def parse_pos_csv(text: str) -> list[Placement]:
    """Parse a `pcb export pos --format csv --units mm` file into placements.

    The header row is required (it is how a CSV pos file is told from an ASCII one) and its
    column order is honoured rather than assumed, because `--units in` and the gerber-style
    variants of this command reorder nothing but could. Rows whose coordinates do not parse
    are skipped: a pos file with a broken row is still worth a BOM.
    """
    rows = list(csv.reader(io.StringIO(text)))
    if not rows:
        raise ValueError("empty position file")
    head = [c.strip().lstrip("#").strip() for c in rows[0]]
    if "Ref" not in head or "PosX" not in head:
        raise ValueError(f"not a kicad-cli CSV position file (header: {head!r})")
    idx = {name: head.index(name) for name in _POS_HEADER if name in head}

    out: list[Placement] = []
    for row in rows[1:]:
        if not row or len(row) < len(head):
            continue

        def cell(name: str, default: str = "") -> str:
            pos = idx.get(name)
            return row[pos].strip() if pos is not None and pos < len(row) else default

        try:
            x = float(cell("PosX", "0"))
            y = float(cell("PosY", "0"))
            rot = float(cell("Rot", "0"))
        except ValueError:
            continue
        ref = cell("Ref")
        if not ref:
            continue
        side = cell("Side", "top").lower()
        out.append(Placement(ref=ref, val=cell("Val"), package=cell("Package"),
                             x=x, y=-y, rot=rot,
                             side="bottom" if side.startswith("b") else "top"))
    if not out:
        raise ValueError("position file has a header but no usable rows")
    return out


# --------------------------------------------------------------------------------------
# `pcb export ipcd356`
#
# The record layout is fixed-width, straight out of pcbnew/exporters/export_d356.cpp:
#   %03d record  %-14.14s net  3 spaces  %-6.6s refdes  '-'|' '  %-4.4s pin  'M'|' '
#   ("D%04d%c" hole | 6 spaces)  A%02d access  X%+07d  Y%+07d  X%04d  Y%04d  R%03d  S%d
# Columns are used, not a loose regex, because a net name may legitimately contain any
# character ibom would accept -- including something that looks like a field separator.
# --------------------------------------------------------------------------------------

_D356_MIN_LEN = 71


def parse_d356(text: str, aux_origin: tuple[float, float] = (0.0, 0.0)) -> list[PadRecord]:
    """Parse IPC-D-356, restoring the board auxiliary origin to absolute millimetres."""
    out: list[PadRecord] = []
    for line in text.splitlines():
        if len(line) < _D356_MIN_LEN or line[:3] not in ("317", "327", "307", "367"):
            continue
        try:
            net = line[3:17].strip()
            ref = line[20:26].strip()
            pin = line[27:31].strip()
            hole = line[32:38]
            drill = int(hole[1:5]) * D356_UNIT_MM if hole.startswith("D") else 0.0
            access = int(line[39:41])
            x = int(line[42:49]) * D356_UNIT_MM
            y = int(line[50:57]) * D356_UNIT_MM
            size_x = int(line[58:62]) * D356_UNIT_MM
            size_y = int(line[63:67]) * D356_UNIT_MM
            rot = float(int(line[68:71]))
        except ValueError:
            continue
        if net.upper() == "N/C":
            net = ""
        out.append(PadRecord(record=line[:3], net=net, ref=ref, pin=pin, x=x + aux_origin[0], y=-y + aux_origin[1],
                             size_x=size_x, size_y=size_y, rot=rot, drill=drill,
                             access=access))
    return out


def pads_by_ref(records: Iterable[PadRecord]) -> dict[str, list[PadRecord]]:
    """Group component pads by reference designator; vias are dropped."""
    grouped: dict[str, list[PadRecord]] = {}
    for rec in records:
        if rec.is_via:
            continue
        grouped.setdefault(rec.ref, []).append(rec)
    return grouped


# --------------------------------------------------------------------------------------
# `pcb export svg -l Edge.Cuts --exclude-drawing-sheet --page-size-mode 1`
#
# The plot is in board millimetres, so the numbers in a path's `d` attribute are usable as
# ibom coordinates with no transform.  Straight M/L paths become ibom `segment` drawings (the
# cheapest thing its renderer can draw); a path carrying arcs or curves is handed over
# verbatim as an `arc` with `svgpath`, which the schema allows and ibom strokes as-is.
# --------------------------------------------------------------------------------------

_SVG_NUM_RE = re.compile(r"[-+]?[0-9]*\.?[0-9]+(?:[eE][-+]?[0-9]+)?")
_SVG_CMD_RE = re.compile(r"([MmLlHhVvCcSsQqTtAaZz])([^MmLlHhVvCcSsQqTtAaZz]*)")



def _path_commands(d: str) -> list[tuple[str, list[float]]]:
    return [(cmd, [float(n) for n in _SVG_NUM_RE.findall(args)])
            for cmd, args in _SVG_CMD_RE.findall(d)]


def _path_points(cmds: list[tuple[str, list[float]]]) -> list[tuple[float, float]]:
    """Bounds for KiCad's absolute SVG paths, including circular-arc extrema."""
    pts: list[tuple[float, float]] = []
    current = (0.0, 0.0)
    for cmd, nums in cmds:
        if cmd == "A":
            for i in range(0, len(nums) - 6, 7):
                rx, ry, rotation, large, sweep, x, y = nums[i:i + 7]
                if rx != ry or rotation != 0:
                    raise ValueError("unsupported elliptical outline arc")
                dx, dy = (current[0] - x) / 2, (current[1] - y) / 2
                distance = math.hypot(dx, dy)
                radius = max(abs(rx), distance)
                if distance and rx:
                    factor = math.sqrt(max(0.0, radius * radius - distance * distance)) / distance
                    if bool(large) == bool(sweep):
                        factor = -factor
                    cx = (current[0] + x) / 2 + factor * dy
                    cy = (current[1] + y) / 2 - factor * dx
                    start = math.atan2(current[1] - cy, current[0] - cx)
                    end = math.atan2(y - cy, x - cx)
                    span = (end - start) % math.tau if sweep else (start - end) % math.tau
                    for angle in (0.0, math.pi / 2, math.pi, 3 * math.pi / 2):
                        offset = (angle - start) % math.tau if sweep else (start - angle) % math.tau
                        if offset <= span + 1e-10:
                            pts.append((cx + radius * math.cos(angle), cy + radius * math.sin(angle)))
                current = (x, y)
                pts.append(current)
        elif cmd in "MLCQS":
            points = [(nums[i], nums[i + 1]) for i in range(0, len(nums) - 1, 2)]
            pts += points
            if points:
                current = points[-1]
        elif cmd != "Z":
            raise ValueError(f"unsupported outline SVG command {cmd}")
    return pts


def parse_edge_svg(text: str) -> BoardOutline:
    """Read stroked KiCad outline geometry, excluding filled drill marks."""
    outline = BoardOutline()
    xs: list[float] = []
    ys: list[float] = []

    def walk(node, inherited):
        style = dict(inherited)
        for key in ("fill", "stroke", "stroke-width"):
            if key in node.attrib:
                style[key] = node.attrib[key]
        for setting in node.get("style", "").split(";"):
            if ":" in setting:
                key, value = setting.split(":", 1)
                style[key.strip()] = value.strip()
        yield node, style
        for child in node:
            yield from walk(child, style)

    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise ValueError(f"invalid outline SVG: {exc}") from exc
    for node, style in walk(root, {}):
        tag = node.tag.rsplit("}", 1)[-1]
        width = float(style.get("stroke-width", "0.1"))
        if tag == "circle":
            if style.get("fill", "none") != "none" or style.get("stroke") == "none":
                continue
            cx, cy, radius = (float(node.get(k, "0")) for k in ("cx", "cy", "r"))
            if radius <= 0:
                continue
            outline.edges.append({"type": "circle", "start": [cx, cy], "radius": radius, "width": width})
            xs.extend((cx - radius, cx + radius))
            ys.extend((cy - radius, cy + radius))
        elif tag == "path":
            d = node.get("d", "")
            cmds = _path_commands(d)
            pts = _path_points(cmds)
            if not pts:
                continue
            xs.extend(p[0] for p in pts)
            ys.extend(p[1] for p in pts)
            # Preserve subpath starts and closure instead of joining unrelated contours.
            simple = all(cmd in "ML" for cmd, _ in cmds) and sum(cmd == "M" for cmd, _ in cmds) == 1
            if not simple:
                outline.edges.append({"type": "arc", "width": width, "svgpath": " ".join(d.split())})
            else:
                for start, end in zip(pts, pts[1:]):
                    if start != end:
                        outline.edges.append({"type": "segment", "start": list(start),
                                              "end": list(end), "width": width})
    if xs and ys:
        outline.minx, outline.maxx = min(xs), max(xs)
        outline.miny, outline.maxy = min(ys), max(ys)
    return outline


# --------------------------------------------------------------------------------------
# The document
# --------------------------------------------------------------------------------------


def _pad_dict(rec: PadRecord) -> dict[str, Any]:
    round_pad = rec.size_y <= 0.0
    size = [rec.size_x, rec.size_x if round_pad else rec.size_y]
    pad: dict[str, Any] = {
        "layers": rec.layers,
        "pos": [rec.x, rec.y],
        "size": size,
        "angle": rec.rot,
        "shape": "circle" if round_pad else "rect",
        "type": "th" if rec.through else "smd",
        "net": rec.net,
    }
    if pad["type"] == "th":
        drill = rec.drill if rec.drill > 0 else min(size) / 2.0
        pad["drillshape"] = "circle"
        pad["drillsize"] = [drill, drill]
    if rec.pin in ("1", "A1", "A"):
        pad["pin1"] = 1
    return pad


def _pad_extent(pads: list[PadRecord]) -> tuple[float, float, float, float]:
    """Axis-aligned extent of a footprint's pads (pad rotation is ignored on purpose).

    A rotated rectangular pad's true extent needs its corners; using the unrotated half-sizes
    can under-cover a 45-degree pad by up to 40%.  The box is a click target and a highlight,
    not a courtyard, so the cheap version plus BOX_MARGIN_MM is the right trade -- and it is
    never smaller than the pad centres it has to contain.
    """
    xs0 = [p.x - max(p.size_x, p.size_y or p.size_x) / 2.0 for p in pads]
    xs1 = [p.x + max(p.size_x, p.size_y or p.size_x) / 2.0 for p in pads]
    ys0 = [p.y - max(p.size_x, p.size_y or p.size_x) / 2.0 for p in pads]
    ys1 = [p.y + max(p.size_x, p.size_y or p.size_x) / 2.0 for p in pads]
    return min(xs0), min(ys0), max(xs1), max(ys1)


def _footprint_dict(place: Placement, pads: list[PadRecord]) -> dict[str, Any]:
    if pads:
        minx, miny, maxx, maxy = _pad_extent(pads)
        minx -= BOX_MARGIN_MM
        miny -= BOX_MARGIN_MM
        maxx += BOX_MARGIN_MM
        maxy += BOX_MARGIN_MM
    else:
        minx, miny = place.x - DEFAULT_BOX_MM, place.y - DEFAULT_BOX_MM
        maxx, maxy = place.x + DEFAULT_BOX_MM, place.y + DEFAULT_BOX_MM
    return {
        "ref": place.ref,
        "center": [place.x, place.y],
        # angle 0 keeps relpos in board coordinates, which is what these numbers are.
        "bbox": {"pos": [place.x, place.y],
                 "relpos": [minx - place.x, miny - place.y],
                 "size": [maxx - minx, maxy - miny],
                 "angle": 0.0},
        "pads": [_pad_dict(p) for p in pads],
        "drawings": [],
        "layer": place.layer,
    }


def _rect_edges(minx: float, miny: float, maxx: float, maxy: float,
                width: float = 0.1) -> list[dict[str, Any]]:
    corners = [(minx, miny), (maxx, miny), (maxx, maxy), (minx, maxy), (minx, miny)]
    return [{"type": "segment", "start": [a[0], a[1]], "end": [b[0], b[1]], "width": width}
            for a, b in zip(corners, corners[1:])]


def build_document(placements: list[Placement],
                   pad_records: Iterable[PadRecord] | None = None,
                   outline: BoardOutline | None = None,
                   metadata: dict[str, str] | None = None) -> dict[str, Any]:
    """Assemble the generic-JSON document. Pure data in, pure data out.

    The returned dict carries one extra, non-schema key, `_phicad`, with the warnings and
    counts a caller wants to report; `strip_private()` removes it before the file is written
    (ibom's schema sets additionalProperties=false at the top level).
    """
    if not placements:
        raise ValueError("no placements: refusing to build an empty BOM")

    grouped = pads_by_ref(pad_records or [])
    warnings: list[str] = []
    if not grouped:
        warnings.append("no IPC-D-356 netlist: footprint boxes are nominal and nets are absent")

    footprints: list[dict[str, Any]] = []
    components: list[dict[str, Any]] = []
    missing_pads: list[str] = []
    for place in placements:
        pads = grouped.get(place.ref, [])
        if grouped and not pads:
            missing_pads.append(place.ref)
        footprints.append(_footprint_dict(place, pads))
        attr = None
        if pads:
            attr = "th" if any(p.through for p in pads) else "smd"
        comp: dict[str, Any] = {"ref": place.ref, "val": place.val,
                                "footprint": place.package, "layer": place.layer}
        if attr:
            comp["attr"] = attr
        components.append(comp)

    if missing_pads:
        warnings.append("no pads in the netlist for: " + ", ".join(sorted(missing_pads)[:12]))

    if outline is not None and outline.known:
        edges = list(outline.edges)
        bbox = {"minx": outline.minx, "miny": outline.miny,
                "maxx": outline.maxx, "maxy": outline.maxy}
    else:
        xs = [f["bbox"]["pos"][0] for f in footprints]
        ys = [f["bbox"]["pos"][1] for f in footprints]
        bbox = {"minx": min(xs) - OUTLINE_MARGIN_MM, "miny": min(ys) - OUTLINE_MARGIN_MM,
                "maxx": max(xs) + OUTLINE_MARGIN_MM, "maxy": max(ys) + OUTLINE_MARGIN_MM}
        edges = _rect_edges(bbox["minx"], bbox["miny"], bbox["maxx"], bbox["maxy"])
        warnings.append("board outline unknown: edges are a rectangle around the placements")

    nets = sorted({p.net for p in (pad_records or []) if p.net})
    meta = dict(metadata or {})
    pcbdata: dict[str, Any] = {
        "edges_bbox": bbox,
        "edges": edges,
        # ibom always renders these two layer groups; no kicad-cli export hands them over as
        # geometry, so they are present and empty rather than absent and invalid.
        "drawings": {"silkscreen": {"F": [], "B": []},
                     "fabrication": {"F": [], "B": []}},
        "footprints": footprints,
        "metadata": {"title": meta.get("title", ""), "revision": meta.get("revision", ""),
                     "company": meta.get("company", ""), "date": meta.get("date", "")},
    }
    if meta.get("variant"):
        pcbdata["metadata"]["variant"] = meta["variant"]
    if nets:
        pcbdata["nets"] = nets
    warnings.append("silkscreen/fabrication drawings and tracks/zones are not exported by "
                    "kicad-cli, so the render shows pads and outline only")

    doc: dict[str, Any] = {"spec_version": SPEC_VERSION, "pcbdata": pcbdata,
                           "components": components}
    doc["_phicad"] = {
        "warnings": warnings,
        "components": len(components),
        "footprints": len(footprints),
        "pads": sum(len(f["pads"]) for f in footprints),
        "nets": len(nets),
        "sides": sorted({c["layer"] for c in components}),
        "top": sum(1 for c in components if c["layer"] == "F"),
        "bottom": sum(1 for c in components if c["layer"] == "B"),
        "edges": len(edges),
        "outline_from_svg": bool(outline is not None and outline.known),
    }
    return doc


def strip_private(doc: dict[str, Any]) -> dict[str, Any]:
    """The document as ibom must see it: no `_phicad` key (additionalProperties is false)."""
    return {k: v for k, v in doc.items() if not k.startswith("_")}


def summary(doc: dict[str, Any]) -> dict[str, Any]:
    """The `_phicad` block, or a recomputed equivalent for a document read back from disk."""
    if "_phicad" in doc:
        return dict(doc["_phicad"])
    comps = doc.get("components", [])
    fps = doc.get("pcbdata", {}).get("footprints", [])
    return {"warnings": [], "components": len(comps), "footprints": len(fps),
            "pads": sum(len(f.get("pads", [])) for f in fps),
            "nets": len(doc.get("pcbdata", {}).get("nets", [])),
            "sides": sorted({c.get("layer") for c in comps}),
            "top": sum(1 for c in comps if c.get("layer") == "F"),
            "bottom": sum(1 for c in comps if c.get("layer") == "B"),
            "edges": len(doc.get("pcbdata", {}).get("edges", [])),
            "outline_from_svg": None}


# --------------------------------------------------------------------------------------
# Validation
#
# ibom validates with jsonschema, which is not in the stdlib and therefore not available to
# `phicad check fast`.  This is the subset of that schema which actually decides whether ibom
# accepts the file: the required keys, the component/footprint length equality its _verify()
# checks, the two-element coordinates, and the per-shape/-type conditionals.  A document that
# passes here has never yet been rejected by the real schema; when that changes, the fix is a
# rule added here, not a hand-edit of the JSON.
# --------------------------------------------------------------------------------------

_LAYERS = ("F", "B")


def _is_coord(value: Any) -> bool:
    return (isinstance(value, (list, tuple)) and len(value) == 2
            and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in value))


def validate_document(doc: Any) -> list[str]:
    """Structural check against ibom's generic-JSON schema. Returns a list of problems."""
    problems: list[str] = []
    if not isinstance(doc, dict):
        return ["document is not an object"]
    if doc.get("spec_version") != SPEC_VERSION:
        problems.append(f"spec_version must be {SPEC_VERSION}, got {doc.get('spec_version')!r}")
    for key in ("pcbdata", "components"):
        if key not in doc:
            problems.append(f"missing required key {key!r}")
    for key in doc:
        if key not in ("spec_version", "pcbdata", "components") and not key.startswith("_"):
            problems.append(f"unexpected top-level key {key!r} (additionalProperties is false)")

    pcbdata = doc.get("pcbdata")
    components = doc.get("components")
    if not isinstance(pcbdata, dict) or not isinstance(components, list):
        problems.append("pcbdata must be an object and components an array")
        return problems

    for key in ("edges_bbox", "edges", "drawings", "footprints", "metadata"):
        if key not in pcbdata:
            problems.append(f"pcbdata is missing required key {key!r}")
    bbox = pcbdata.get("edges_bbox", {})
    if isinstance(bbox, dict):
        for key in ("minx", "miny", "maxx", "maxy"):
            if not isinstance(bbox.get(key), (int, float)):
                problems.append(f"edges_bbox.{key} must be a number")
    else:
        problems.append("edges_bbox must be an object")
    meta = pcbdata.get("metadata", {})
    if isinstance(meta, dict):
        for key in ("title", "revision", "company", "date"):
            if not isinstance(meta.get(key), str):
                problems.append(f"metadata.{key} must be a string")
    else:
        problems.append("metadata must be an object")
    drawings = pcbdata.get("drawings", {})
    if isinstance(drawings, dict):
        for group, sides in drawings.items():
            if not isinstance(sides, dict) or set(sides) != set(_LAYERS):
                problems.append(f"drawings.{group} must have exactly F and B arrays")
    else:
        problems.append("drawings must be an object")
    if ("tracks" in pcbdata) != ("zones" in pcbdata):
        problems.append("tracks and zones must be present together")

    footprints = pcbdata.get("footprints")
    if not isinstance(footprints, list):
        problems.append("pcbdata.footprints must be an array")
        return problems
    if len(footprints) != len(components):
        problems.append(f"components ({len(components)}) and footprints ({len(footprints)}) "
                        "must be the same length (ibom's own _verify check)")

    for i, fp in enumerate(footprints):
        where = f"footprints[{i}]"
        if not isinstance(fp, dict):
            problems.append(f"{where} is not an object")
            continue
        for key in ("ref", "center", "bbox", "pads", "drawings", "layer"):
            if key not in fp:
                problems.append(f"{where} is missing {key!r}")
        if fp.get("layer") not in _LAYERS:
            problems.append(f"{where}.layer must be 'F' or 'B', got {fp.get('layer')!r}")
        if not _is_coord(fp.get("center")):
            problems.append(f"{where}.center must be [x, y]")
        fbox = fp.get("bbox")
        if isinstance(fbox, dict):
            for key in ("pos", "relpos", "size"):
                if not _is_coord(fbox.get(key)):
                    problems.append(f"{where}.bbox.{key} must be [x, y]")
            if not isinstance(fbox.get("angle"), (int, float)):
                problems.append(f"{where}.bbox.angle must be a number")
        else:
            problems.append(f"{where}.bbox must be an object")
        problems += _pad_problems(fp.get("pads"), where)

    for i, comp in enumerate(components):
        where = f"components[{i}]"
        if not isinstance(comp, dict):
            problems.append(f"{where} is not an object")
            continue
        for key in ("ref", "val", "footprint", "layer"):
            if not isinstance(comp.get(key), str):
                problems.append(f"{where}.{key} must be a string")
        if comp.get("layer") not in _LAYERS:
            problems.append(f"{where}.layer must be 'F' or 'B', got {comp.get('layer')!r}")
        for key in comp:
            if key not in ("ref", "val", "footprint", "layer", "attr", "extra_fields"):
                problems.append(f"{where} has unexpected key {key!r}")

    problems += _edge_problems(pcbdata.get("edges"))
    return problems


_PAD_SHAPES = ("rect", "circle", "oval", "roundrect", "chamfrect", "custom")


def _pad_problems(pads: Any, where: str) -> list[str]:
    problems: list[str] = []
    if not isinstance(pads, list):
        return [f"{where}.pads must be an array"]
    for j, pad in enumerate(pads):
        at = f"{where}.pads[{j}]"
        if not isinstance(pad, dict):
            problems.append(f"{at} is not an object")
            continue
        layers = pad.get("layers")
        if (not isinstance(layers, list) or not 1 <= len(layers) <= 2
                or any(lay not in _LAYERS for lay in layers)):
            problems.append(f"{at}.layers must be one or two of F/B, got {layers!r}")
        for key in ("pos", "size"):
            if not _is_coord(pad.get(key)):
                problems.append(f"{at}.{key} must be [x, y]")
        if pad.get("shape") not in _PAD_SHAPES:
            problems.append(f"{at}.shape must be one of {_PAD_SHAPES}, got {pad.get('shape')!r}")
        if pad.get("type") not in ("smd", "th"):
            problems.append(f"{at}.type must be 'smd' or 'th', got {pad.get('type')!r}")
        if pad.get("type") == "th":
            if pad.get("drillshape") not in ("circle", "oblong", "rect"):
                problems.append(f"{at}.drillshape is required for a through pad")
            if not _is_coord(pad.get("drillsize")):
                problems.append(f"{at}.drillsize must be [x, y] for a through pad")
        if pad.get("shape") == "roundrect" and not isinstance(pad.get("radius"), (int, float)):
            problems.append(f"{at}.radius is required for a roundrect pad")
        if pad.get("shape") == "custom" and "svgpath" not in pad and "polygons" not in pad:
            problems.append(f"{at} is custom but carries neither svgpath nor polygons")
    return problems


def _edge_problems(edges: Any) -> list[str]:
    problems: list[str] = []
    if not isinstance(edges, list):
        return ["pcbdata.edges must be an array"]
    if not edges:
        problems.append("pcbdata.edges is empty: ibom would render no board")
    for i, dwg in enumerate(edges):
        at = f"edges[{i}]"
        if not isinstance(dwg, dict) or "type" not in dwg:
            problems.append(f"{at} must be an object with a type")
            continue
        kind = dwg["type"]
        if kind == "segment":
            for key in ("start", "end"):
                if not _is_coord(dwg.get(key)):
                    problems.append(f"{at}.{key} must be [x, y]")
            if not isinstance(dwg.get("width"), (int, float)):
                problems.append(f"{at}.width must be a number")
        elif kind == "arc":
            if not isinstance(dwg.get("width"), (int, float)):
                problems.append(f"{at}.width must be a number")
            if "svgpath" not in dwg and not all(
                    k in dwg for k in ("start", "radius", "startangle", "endangle")):
                problems.append(f"{at} needs svgpath or start/radius/startangle/endangle")
        elif kind not in ("rect", "circle", "curve", "polygon"):
            problems.append(f"{at}.type {kind!r} is not a generic-JSON drawing type")
    return problems


# --------------------------------------------------------------------------------------
# One-call convenience + CLI
# --------------------------------------------------------------------------------------


def build_from_text(pos_csv: str, d356: str | None = None, edge_svg: str | None = None,
                    metadata: dict[str, str] | None = None,
                    aux_origin: tuple[float, float] = (0.0, 0.0)) -> dict[str, Any]:
    """pos CSV (+ optional IPC-D-356 and Edge.Cuts SVG) text -> generic-JSON document."""
    placements = parse_pos_csv(pos_csv)
    records = parse_d356(d356, aux_origin=aux_origin) if d356 else []
    outline = parse_edge_svg(edge_svg) if edge_svg else None
    return build_document(placements, records, outline, metadata)


def write_document(doc: dict[str, Any], path: Path) -> int:
    """Write the ibom-facing document (no `_phicad`); returns the byte count."""
    text = json.dumps(strip_private(doc), indent=1, sort_keys=False)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return len(text.encode("utf-8"))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--pos", required=True, help="kicad-cli pcb export pos --format csv output")
    ap.add_argument("--d356", help="kicad-cli pcb export ipcd356 output")
    ap.add_argument("--edges", help="Edge.Cuts SVG plot (board millimetres)")
    ap.add_argument("--title", default="")
    ap.add_argument("--revision", default="")
    ap.add_argument("--company", default="")
    ap.add_argument("--date", default="")
    ap.add_argument("-o", "--output", required=True)
    args = ap.parse_args(argv)

    def maybe(path: str | None) -> str | None:
        return Path(path).read_text(encoding="utf-8", errors="replace") if path else None

    doc = build_from_text(Path(args.pos).read_text(encoding="utf-8", errors="replace"),
                          maybe(args.d356), maybe(args.edges),
                          {"title": args.title, "revision": args.revision,
                           "company": args.company, "date": args.date})
    problems = validate_document(doc)
    if problems:
        for p in problems[:20]:
            print(f"invalid: {p}", file=sys.stderr)
        return 1
    size = write_document(doc, Path(args.output))
    print(json.dumps({"output": args.output, "bytes": size, **summary(doc)}, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
