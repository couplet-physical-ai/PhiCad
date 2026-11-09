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
"""Design IR -> `.kicad_sch`: the schematic the netlist has been naming all along.

Why emitted text, not IPC
-------------------------
The schematic API works headless (OpenDocument/CreateItems/SaveDocument were verified
live against this fork), but generation is the wrong job for it: the headless context has
no current sheet, so paper and title block cannot be set over the wire; `CreateItems`
builds a symbol's LIB_SYMBOL from the protobuf alone, so the client must ship every pin
and graphic itself; and the saved file then needs its `lib_symbols` post-processed anyway
to avoid `lib_symbol_mismatch`.  Emitting the file directly needs none of that, and the
result was verified against the fork's own tools: `kicad-cli sch erc` reports 0 errors
and `sch export netlist` reproduces the design's connectivity pin for pin.  The file
version is **20260830** — the schematic counter, not the board's 20260831; declaring the
board's version makes the file unloadable (`FUTURE_FORMAT_ERROR`,
`sch_io_kicad_sexpr_parser.cpp:3196`).

Connectivity: one global label per pin, no wires
------------------------------------------------
Every netted pin gets a global label placed exactly on its connection point.  A label
coincident with a PIN_END is not dangling (`sch_label.cpp:1240`), a labelled single-pin
subgraph does not raise pin_not_connected (`connection_graph.cpp:5718`), and global (not
local) labels give nets their bare names — local labels prefix the sheet path (`/GND`),
which would break agreement with the `.net` and the board.  Wires and junctions would buy
nothing here and add their own dangling-endpoint failure modes.  Symbols sit on the
1.27 mm connection grid, or every pin costs an `endpoint_off_grid` warning.

ERC also needs, and gets: a `(no_connect)` marker on each IR `no_connect` pad, and one
`power:PWR_FLAG` symbol per net that touches a `power_in` pin (a net is a power net the
moment one pin on it is power_in, and only a power_out pin may drive it —
`erc.cpp:130-140`; the flag is that pin).  Never `power:GND`-style symbols: they bring
their own power_in pin that itself needs a driver, and they collide with the labels.

Determinism: every UUID is uuid5 of a fixed namespace plus project and item identity, so
an unchanged design regenerates a byte-identical file — unlike the `.kicad_pcb`, this
file is honestly diffable.
"""

from __future__ import annotations

import hashlib
import re
import sys
import uuid as _uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from ir import Design  # noqa: E402
from library import Library  # noqa: E402
from symbols import Symbol, SymbolLibrary  # noqa: E402

SCH_VERSION = "20260830"            # eeschema/sch_file_versions.h — NOT the board's
GENERATOR = "phicad-synth"
GENERATOR_VERSION = "10.99"

# The schematic connection grid (50 mil).  Everything that can carry a connection —
# symbol origins, therefore pin ends and labels — must land on it.
GRID = 1.27

# Deterministic UUID namespace, distinct from netlist.py's and emit.py's so a schematic
# item and a board item with the same tag never collide.
_NS = _uuid.UUID("c4a7d9e3-58b1-4f26-9d02-7e94a1c6f085")

_POWER_FLAG = "power:PWR_FLAG"


class SchematicError(RuntimeError):
    """The design cannot be drawn — a symbol failed to resolve after validation."""


def uid(project: str, tag: str) -> str:
    return str(_uuid.uuid5(_NS, f"{project}/{tag}"))


def _q(text) -> str:
    s = "" if text is None else str(text)
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _snap(v: float) -> float:
    return round(round(v / GRID) * GRID, 4)


def _num(v: float) -> str:
    """A coordinate with no trailing noise — grid multiples are exact in 4 decimals."""
    s = f"{v:.4f}".rstrip("0").rstrip(".")
    return s if s not in ("-0", "") else "0"


# ---------------------------------------------------------------------------------------
# layout
# ---------------------------------------------------------------------------------------

@dataclass
class Placed:
    """One symbol on the sheet: reference, resolved symbol, origin (schematic mm)."""

    ref: str
    symbol: Symbol
    x: float
    y: float

    def pin_at(self, number: str) -> Tuple[float, float]:
        """A pin's connection point.  Symbol space is Y-up; the sheet is Y-down."""
        pin = self.symbol.pins[number]
        return (self.x + pin.x, self.y - pin.y)

    def pin_outward(self, number: str) -> float:
        """The direction a label should read towards, degrees, 0=east 90=up on screen.

        A pin's stored angle points from the connection point toward the body, so
        outward is its opposite.  The Y flip cancels: lib up is screen up.
        """
        return (self.symbol.pins[number].angle + 180.0) % 360.0


@dataclass
class Sheet:
    """The solved sheet: placements plus everything the emitter derives from them."""

    placed: Dict[str, Placed] = field(default_factory=dict)
    flags: List[Tuple[str, float, float]] = field(default_factory=list)   # (net, x, y)
    paper: str = "A4"
    # Pin pairs joined by a drawn wire instead of a pair of labels:
    # (net, (x1, y1), (x2, y2), ("REF.PIN", "REF.PIN")).
    wires: List[Tuple[str, Tuple[float, float], Tuple[float, float],
                     Tuple[str, str]]] = field(default_factory=list)
    # "REF.PIN" endpoints a wire reaches, so the emitter can drop their labels.
    wired_pins: Set[str] = field(default_factory=set)
    # Corner points of L-shaped runs; KiCad needs no junction for a 2-segment corner,
    # but recording them keeps the emitter's midpoint labelling honest.
    junctions: List[Tuple[float, float]] = field(default_factory=list)


# Past this a drawn wire sprawls across the sheet and a net label reads better.
_WIRE_MAX_MM = 80.0


def _segment_hits_body(x1: float, y1: float, x2: float, y2: float,
                       sheet: "Sheet", allow: Tuple[str, str]) -> bool:
    """Would an axis-aligned wire cross any symbol body, or reach a foreign pin?

    A schematic wire that clips another symbol is not a cosmetic problem: touching a pin
    connects it, so a badly drawn wire is a short that ERC will not necessarily catch in
    the way a human would notice. This refuses anything it cannot prove is clear.
    """
    lo_x, hi_x = (x1, x2) if x1 <= x2 else (x2, x1)
    lo_y, hi_y = (y1, y2) if y1 <= y2 else (y2, y1)
    # The two symbols being joined are excluded: a wire necessarily starts on its own
    # pin, so its endpoints' bodies always "overlap" and would reject every route. Their
    # *other* pins are still checked, which is the case that matters -- a wire must not
    # graze pin 2 on the way out of pin 1.
    endpoints = {p.partition(".")[0] for p in allow}
    for ref, placed in sheet.placed.items():
        minx, miny, maxx, maxy = placed.symbol.bbox()
        # Symbol space is Y-up, the sheet is Y-down: maxy maps above the origin.
        bx1, bx2 = placed.x + minx, placed.x + maxx
        by1, by2 = placed.y - maxy, placed.y - miny
        pad = 0.254                                  # keep off the ink
        if ref not in endpoints and (lo_x <= bx2 + pad and hi_x >= bx1 - pad
                                     and lo_y <= by2 + pad and hi_y >= by1 - pad):
            return True
        for number in placed.symbol.pins:
            if f"{ref}.{number}" in allow:
                continue
            px, py = placed.pin_at(number)
            if (lo_x - pad <= px <= hi_x + pad) and (lo_y - pad <= py <= hi_y + pad):
                return True
    return False


def _route_wires(design: Design, sheet: "Sheet") -> None:
    """Draw real wires for the two-pin nets whose pins face each other cleanly.

    Machine-generated schematics usually label every pin, which is electrically correct
    and visually useless: you cannot follow a signal without reading text. Power and
    multi-pin nets stay on labels, where a net name genuinely beats a rat's nest of
    lines. A two-pin net is the case a wire always reads better, so it gets one -- but
    only when the wire can be proven not to touch anything else, because a wire that
    clips a foreign pin is a short.

    Partial on purpose: an undrawable net keeps its labels, and the netlist is identical
    either way.
    """
    pins_of: Dict[str, List[str]] = {}
    for pin, net in design.pin_owner().items():
        pins_of.setdefault(net, []).append(pin)

    flagged = {net for net, _, _ in sheet.flags}
    for net in sorted(pins_of):
        endpoints = sorted(pins_of[net])
        if len(endpoints) != 2 or net in flagged:
            continue                          # labels are the better answer
        a, b = endpoints
        ra, _, na = a.partition(".")
        rb, _, nb = b.partition(".")
        pa, pb = sheet.placed.get(ra), sheet.placed.get(rb)
        if pa is None or pb is None or na not in pa.symbol.pins or nb not in pb.symbol.pins:
            continue
        (x1, y1), (x2, y2) = pa.pin_at(na), pb.pin_at(nb)

        # A straight run when the pins already line up, otherwise an L through one of the
        # two corners. Column layout means pins are rarely collinear -- they miss by a
        # grid step -- so straight-only would draw almost nothing.
        runs: List[List[Tuple[float, float]]] = []
        if abs(x1 - x2) <= 1e-6 or abs(y1 - y2) <= 1e-6:
            runs.append([(x1, y1), (x2, y2)])
        else:
            runs.append([(x1, y1), (x2, y1), (x2, y2)])
            runs.append([(x1, y1), (x1, y2), (x2, y2)])

        # Only accept a path long enough to read as a connection but short enough not to
        # sprawl across the sheet; past that a net name is genuinely the clearer notation.
        for path in runs:
            length = sum(abs(path[i + 1][0] - path[i][0]) + abs(path[i + 1][1] - path[i][1])
                         for i in range(len(path) - 1))
            if length > _WIRE_MAX_MM:
                continue
            if any(_segment_hits_body(path[i][0], path[i][1],
                                      path[i + 1][0], path[i + 1][1], sheet, (a, b))
                   for i in range(len(path) - 1)):
                continue
            for i in range(len(path) - 1):
                sheet.wires.append((net, path[i], path[i + 1], (a, b)))
            if len(path) > 2:
                sheet.junctions.append(path[1])
            sheet.wired_pins.update((a, b))
            break


def _column_of(ref: str, symbol: Symbol) -> int:
    """Left to right: connectors, ICs, everything else.  Signal flow is not solved here;
    a stable, readable arrangement is the whole ambition (a sheet is not a board)."""
    if symbol.lib.startswith("Connector") or symbol.prefix in ("J", "P", "X"):
        return 0
    if symbol.prefix == "U":
        return 1
    return 2


def _label_extents(design: Design, ref: str,
                   symbol: Symbol) -> Tuple[float, float, float]:
    """(horizontal, top, bottom) room the labels on this symbol's pins will occupy.

    A label extends outward from its pin by roughly 1.0 mm per character of 1.27 mm
    text; a symbol with labelled vertical pins therefore needs that much clear sheet
    above/below its body, or the labels overlap the neighbouring row's text.
    """
    owner = design.pin_owner()
    horiz = top = bottom = 0.0
    for number, pin in symbol.pins.items():
        net = owner.get(f"{ref}.{number}", "")
        reach = len(net) * 1.0 + 2.54 if net else 0.0
        outward = (pin.angle + 180.0) % 360.0
        if outward in (0.0, 180.0):
            horiz = max(horiz, reach)
        elif outward == 90.0:
            top = max(top, reach)
        else:
            bottom = max(bottom, reach)
    return (horiz + 5.08, top, bottom)


def layout(design: Design, footprints: Dict[str, object],
           symbols: SymbolLibrary) -> Sheet:
    """Columns by kind, stacked downward, on the connection grid.

    Margins are driven by each symbol's own bbox and the length of the net names on its
    pins, so two columns can never interleave their labels.  Nothing fancier is
    attempted on purpose: schematic placement quality is a human aesthetic problem, and
    a predictable grid beats a clever arrangement that is different on every run.
    """
    resolved = design.resolve_symbols(footprints, symbols)
    missing = [ref for ref in sorted(design.parts) if ref not in resolved]
    if missing:
        raise SchematicError(
            "symbols did not resolve for: " + ", ".join(missing)
            + " — validate() reports the specifics")

    columns: Dict[int, List[Tuple[str, Symbol]]] = {}
    for ref in sorted(resolved):
        symbol = resolved[ref]
        columns.setdefault(_column_of(ref, symbol), []).append((ref, symbol))

    sheet = Sheet()
    x_cursor = 30.0
    max_y = 30.0
    # Columns taller than this wrap into a fresh column — an A4 landscape sheet is
    # 210 mm high, and a strip of parts running off the paper is not a schematic.
    wrap_at = 165.0
    for col in sorted(columns):
        col_width = 0.0
        y_cursor = 30.0
        for ref, symbol in columns[col]:
            minx, miny, maxx, maxy = symbol.bbox()
            margin, top_ext, bottom_ext = _label_extents(design, ref, symbol)
            # Reference and Value stack above the body, above any upward labels, so the
            # cell reserves two text lines there on top of the label reach.
            head = top_ext + 6.35
            height = (maxy - miny) + head + bottom_ext + 7.62
            if y_cursor > 30.0 and y_cursor + height > wrap_at:
                x_cursor += col_width
                col_width = 0.0
                y_cursor = 30.0
            # Origin such that the body's left edge (plus label room) starts at the
            # column x; Y grows downward, symbol-space maxy maps to the top.
            ox = _snap(x_cursor + margin - minx)
            oy = _snap(y_cursor + head + maxy)
            sheet.placed[ref] = Placed(ref=ref, symbol=symbol, x=ox, y=oy)
            width = (maxx - minx) + 2 * margin
            col_width = max(col_width, width)
            y_cursor += height
            max_y = max(max_y, y_cursor)
        x_cursor += col_width

    # Power flags in a bottom row: one per net that has a power_in pin and no
    # power_out pin.  A net a regulator's output already drives needs no flag, and
    # adding one anyway costs a "Power output and Power output" warning.
    owner = design.pin_owner()
    needs_flag: Dict[str, bool] = {}
    for ref, placed in sorted(sheet.placed.items()):
        for number, pin in placed.symbol.pins.items():
            net = owner.get(f"{ref}.{number}")
            if not net:
                continue
            if pin.etype == "power_in":
                needs_flag.setdefault(net, True)
            elif pin.etype == "power_out":
                needs_flag[net] = False
    power_nets = [net for net, needed in needs_flag.items() if needed]
    fy = _snap(max_y + 12.7)
    fx = 30.0
    for net in sorted(power_nets):
        sheet.flags.append((net, _snap(fx), fy))
        fx += max(len(net) * 1.0 + 15.24, 20.32)

    _route_wires(design, sheet)
    return sheet


# ---------------------------------------------------------------------------------------
# emission
# ---------------------------------------------------------------------------------------

def _indent(text: str, tabs: int) -> str:
    pad = "\t" * tabs
    return "\n".join(pad + line for line in text.splitlines())


def _property(name: str, value: str, x: float, y: float,
              hide: bool = False, tabs: int = 2) -> str:
    t = "\t" * tabs
    lines = [f"{t}(property {_q(name)} {_q(value)}",
             f"{t}\t(at {_num(x)} {_num(y)} 0)"]
    if hide:
        lines.append(f"{t}\t(hide yes)")
    lines += [f"{t}\t(effects",
              f"{t}\t\t(font",
              f"{t}\t\t\t(size 1.27 1.27)",
              f"{t}\t\t)",
              f"{t}\t)",
              f"{t})"]
    return "\n".join(lines)


def _label_block(project: str, net: str, x: float, y: float, outward: float,
                 tag: str) -> str:
    """A global label whose anchor is exactly the pin's connection point.

    Angle and justification travel together the way eeschema stores spin styles: the
    anchor stays on the pin and the text body extends along `outward`.
    """
    if outward in (0.0, 90.0):
        angle, justify = outward, "left"
    elif outward == 180.0:
        angle, justify = 0.0, "right"
    else:                               # 270: reads bottom-up, anchored at the top
        angle, justify = 90.0, "right"
    return "\n".join([
        f"\t(global_label {_q(net)}",
        "\t\t(shape passive)",
        f"\t\t(at {_num(x)} {_num(y)} {_num(angle)})",
        "\t\t(effects",
        "\t\t\t(font",
        "\t\t\t\t(size 1.27 1.27)",
        "\t\t\t)",
        f"\t\t\t(justify {justify})",
        "\t\t)",
        f"\t\t(uuid {_q(uid(project, tag))})",
        "\t)",
    ])


def _symbol_block(design: Design, ref: str, libid: str, symbol: Symbol,
                  x: float, y: float, value: str, footprint: str,
                  extra_fields: Dict[str, str], in_bom: bool, dnp: bool,
                  root_uuid: str) -> str:
    project = design.project
    minx, miny, maxx, maxy = symbol.bbox()
    # Reference on top, Value beneath it, both clear of any labels on upward pins —
    # the labels sit on the pins and cannot move, so the text does.
    _, top_ext, _ = _label_extents(design, ref, symbol)
    ytop = y - maxy - top_ext
    out = [
        "\t(symbol",
        f"\t\t(lib_id {_q(libid)})",
        f"\t\t(at {_num(x)} {_num(y)} 0)",
        "\t\t(unit 1)",
        "\t\t(exclude_from_sim no)",
        f"\t\t(in_bom {'yes' if in_bom else 'no'})",
        "\t\t(on_board yes)",
        f"\t\t(dnp {'yes' if dnp else 'no'})",
        f"\t\t(uuid {_q(uid(project, f'symbol:{ref}'))})",
        _property("Reference", ref, x, ytop - 5.08),
        _property("Value", value, x, ytop - 2.54),
        _property("Footprint", footprint, x, y, hide=True),
        _property("Datasheet", design.parts[ref].datasheet if ref in design.parts
                  else "", x, y, hide=True),
        _property("Description", design.parts[ref].description if ref in design.parts
                  else "", x, y, hide=True),
    ]
    for name in sorted(extra_fields):
        out.append(_property(name, extra_fields[name], x, y, hide=True))
    for number in sorted(symbol.pins, key=lambda n: (len(n), n)):
        out.append(f"\t\t(pin {_q(number)}\n"
                   f"\t\t\t(uuid {_q(uid(project, f'pin:{ref}.{number}'))})\n"
                   "\t\t)")
    out += [
        "\t\t(instances",
        f"\t\t\t(project {_q(project)}",
        f"\t\t\t\t(path {_q('/' + root_uuid)}",
        f"\t\t\t\t\t(reference {_q(ref)})",
        "\t\t\t\t\t(unit 1)",
        "\t\t\t\t)",
        "\t\t\t)",
        "\t\t)",
        "\t)",
    ]
    return "\n".join(out)


def emit(design: Design, sheet: Sheet, symbols: SymbolLibrary) -> str:
    """The whole schematic as text."""
    project = design.project
    root_uuid = uid(project, "sheet:/")
    owner = design.pin_owner()

    used: Dict[str, Symbol] = {}
    for placed in sheet.placed.values():
        used.setdefault(placed.symbol.libid, placed.symbol)
    flag_symbol: Optional[Symbol] = None
    if sheet.flags:
        flag_symbol = symbols.resolve(_POWER_FLAG)
        used.setdefault(_POWER_FLAG, flag_symbol)

    out: List[str] = [
        "(kicad_sch",
        f"\t(version {SCH_VERSION})",
        f"\t(generator {_q(GENERATOR)})",
        f"\t(generator_version {_q(GENERATOR_VERSION)})",
        f"\t(uuid {_q(root_uuid)})",
        f"\t(paper {_q(sheet.paper)})",
        "\t(title_block",
        f"\t\t(title {_q(design.title or project)})",
        f"\t\t(rev {_q(design.rev)})",
        f"\t\t(company {_q(design.company)})",
        "\t\t(comment 1 \"generated by phicad-synth from the design IR\")",
        "\t)",
        "\t(lib_symbols",
    ]
    for libid in sorted(used):
        out.append(_indent(used[libid].embedded(), 2))
    out.append("\t)")

    # No-connect markers before labels/symbols, mirroring the serializer's grouping.
    for pin in sorted(design.no_connect):
        ref, _, number = pin.partition(".")
        placed = sheet.placed.get(ref)
        if placed is None or number not in placed.symbol.pins:
            continue                         # validate() already reported the specifics
        px, py = placed.pin_at(number)
        out.append("\t(no_connect\n"
                   f"\t\t(at {_num(px)} {_num(py)})\n"
                   f"\t\t(uuid {_q(uid(project, f'nc:{pin}'))})\n"
                   "\t)")

    for ref in sorted(sheet.placed):
        placed = sheet.placed[ref]
        for number in sorted(placed.symbol.pins, key=lambda n: (len(n), n)):
            net = owner.get(f"{ref}.{number}")
            if not net:
                continue
            if f"{ref}.{number}" in sheet.wired_pins:
                continue                     # a drawn wire already carries this net
            px, py = placed.pin_at(number)
            out.append(_label_block(project, net, px, py,
                                    placed.pin_outward(number),
                                    f"label:{ref}.{number}"))
    for net, fx, fy in sheet.flags:
        # The flag's pin 1 is at its origin; the matching label lands on the same point.
        out.append(_label_block(project, net, fx, fy, 0.0, f"label:flag:{net}"))

    # Wires last among the connection items: one net, two pins, a straight run proven
    # clear of every other body and pin. The net name rides along as a label at the
    # midpoint so the drawing still says what the wire is.
    longest: Dict[Tuple[str, str], Tuple[float, str, float, float, float]] = {}
    for seq, (net, (x1, y1), (x2, y2), (a, b)) in enumerate(sheet.wires):
        out.append("\t(wire\n"
                   f"\t\t(pts (xy {_num(x1)} {_num(y1)}) (xy {_num(x2)} {_num(y2)}))\n"
                   "\t\t(stroke\n\t\t\t(width 0)\n\t\t\t(type default)\n\t\t)\n"
                   f"\t\t(uuid {_q(uid(project, f'wire:{a}:{b}:{seq}'))})\n"
                   "\t)")
        # One label per *net*, on its longest segment: a label per segment would put two
        # copies of the same name on one L.
        span = abs(x2 - x1) + abs(y2 - y1)
        prior = longest.get((a, b))
        if prior is None or span > prior[0]:
            longest[(a, b)] = (span, net, (x1 + x2) / 2.0, (y1 + y2) / 2.0,
                              0.0 if abs(y1 - y2) <= 1e-6 else 90.0)
    for (a, b), (_, net, mx, my, angle) in sorted(longest.items()):
        out.append(_label_block(project, net, _snap(mx), _snap(my), angle,
                               f"label:wire:{a}:{b}"))

    for ref in sorted(sheet.placed):
        placed = sheet.placed[ref]
        part = design.parts[ref]
        extra: Dict[str, str] = dict(part.fields)
        if part.lcsc:
            extra.setdefault("LCSC", part.lcsc)
        if part.mpn:
            extra.setdefault("MPN", part.mpn)
        out.append(_symbol_block(
            design, ref, placed.symbol.libid, placed.symbol, placed.x, placed.y,
            value=part.value or ref, footprint=part.footprint, extra_fields=extra,
            in_bom=not part.exclude_from_bom, dnp=part.dnp, root_uuid=root_uuid))

    for i, (net, fx, fy) in enumerate(sheet.flags):
        assert flag_symbol is not None
        out.append(_symbol_block(
            design, f"#FLG{i + 1:02d}", _POWER_FLAG, flag_symbol, fx, fy,
            value="PWR_FLAG", footprint="", extra_fields={},
            in_bom=False, dnp=False, root_uuid=root_uuid))

    out += [
        "\t(sheet_instances",
        "\t\t(path \"/\"",
        "\t\t\t(page \"1\")",
        "\t\t)",
        "\t)",
        "\t(embedded_fonts no)",
        ")",
        "",
    ]
    return "\n".join(out)


_STAMP_KEY = "phicad_sha"


def _stamp(text: str) -> str:
    """Record a digest of the generated body in the title block's comment 2."""
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
    marker = f'\t\t(comment 2 "{_STAMP_KEY}={digest}")'
    return text.replace("\t)\n\t(lib_symbols",
                        f"{marker}\n\t)\n\t(lib_symbols", 1)


def _stamp_of(text: str) -> str:
    m = re.search(r'\(comment 2 "' + _STAMP_KEY + r'=([0-9a-f]{16})"\)', text)
    return m.group(1) if m else ""


def _stamp_matches(text: str) -> bool:
    """True if `text` is exactly what the generator wrote, stamp included."""
    stamp = _stamp_of(text)
    if not stamp:
        return False
    marker = f'\t\t(comment 2 "{_STAMP_KEY}={stamp}")\n'
    body = text.replace(marker, "", 1)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()[:16] == stamp


def write(design: Design, out_dir, library: Optional[Library] = None,
          symbols: Optional[SymbolLibrary] = None,
          overwrite_edits: bool = False) -> Path:
    """Solve the sheet and write `<project>.kicad_sch`.  Returns the path."""
    library = library or Library.default()
    symbols = symbols or SymbolLibrary.default()
    footprints = {ref: library.resolve(part.footprint)
                  for ref, part in design.parts.items() if part.footprint}
    sheet = layout(design, footprints, symbols)
    path = Path(out_dir) / f"{design.project}.kicad_sch"
    path.parent.mkdir(parents=True, exist_ok=True)
    text = emit(design, sheet, symbols)
    text = _stamp(text)

    # Refuse to destroy hand edits. The emitter is deterministic, so a file whose stamp
    # does not match its own contents was edited after generation -- and a schematic you
    # can open in Eeschema but lose on the next build is not a schematic you can work in.
    if path.is_file() and not overwrite_edits:
        existing = path.read_text(encoding="utf-8")
        if _stamp_of(existing) and not _stamp_matches(existing):
            raise SchematicError(
                f"{path.name} has been edited since it was generated — refusing to"
                " overwrite it\n"
                "    hint: keep the edits and stop regenerating the schematic, or pass"
                " --overwrite-schematic to discard them")

    path.write_text(text, encoding="utf-8")
    return path


if __name__ == "__main__":
    # Smoke test:  python3 phicad/synth/schematic.py design.json out/
    from ir import errors, format_problems, load          # noqa: PLC0415

    if len(sys.argv) < 3:
        print("usage: schematic.py <design.json> <out-dir>", file=sys.stderr)
        raise SystemExit(2)
    d = load(sys.argv[1])
    found = d.validate(Library.default())
    if errors(found):
        print(format_problems(found), file=sys.stderr)
        raise SystemExit(1)
    print(f"wrote {write(d, sys.argv[2])}")
