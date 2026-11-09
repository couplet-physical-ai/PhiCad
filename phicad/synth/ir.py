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
"""The design IR: what a model writes instead of a board.

Why an IR at all
----------------
Today the only way an agent produces a *new* board in this fork is to emit s-expressions
directly — `demos/teensy-sensor-suite/generate_board.py` is 1667 lines, and its comments
record the cost honestly: "-4.8, after two corrections", "+34.0, not +33.0", "swept rather
than nudged".  Almost none of that is design; it is a model doing constraint solving in its
head, one coordinate at a time, with a 25 s DRC round trip as the only feedback.

The IR moves that boundary.  A model writes ~60 lines of intent — parts, nets, and where
things should sit relative to each other — and deterministic code turns it into a netlist,
a schematic and a placed board.  Coordinates stop being model output, so they stop being a
source of model error.

The contract this file enforces
-------------------------------
1. **Every pad is accounted for.**  Each part's footprint is read from the real library, and
   every pad it has must appear in exactly one net or in `no_connect`.  This is the
   "all connected" gate, and it runs in milliseconds — before any board exists.  The board
   the netlist bridge produced during development warned `No net found for component J1 pad
   4`; that warning is a design hole, and this check is where it gets caught.
2. **Nothing is referenced that does not exist.**  Unknown footprints, unknown pads,
   duplicate refs and pins claimed by two nets are all rejected with the real alternatives
   listed, not "invalid design".
3. **Problems are data.**  `validate()` returns `Problem` records with a severity and a
   fix hint, so a CLI can print them, an MCP tool can return them as JSON, and an agent can
   act on them without parsing prose.

Format
------
JSON is canonical (stdlib everywhere, and it matches `parts.json`, the parts pipeline's
ledger).  YAML is accepted when PyYAML happens to be importable, because it is nicer to
hand-edit; it is never required.
"""

from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

# Sibling import that works however this module was reached: run as a script
# (`python3 phicad/synth/ir.py`), imported from the package, or imported by a tool that
# put only the repo root on the path.
_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from library import Library, LibraryError  # noqa: E402
from symbols import SymbolError, SymbolLibrary  # noqa: E402

# Reference designators: letters then digits, optionally suffixed (R1, U2A, J10).
_REF_RE = re.compile(r"^[A-Za-z_]+[0-9]+[A-Za-z]?$")

# A pin reference in a net: "U1.3", "J1.A2".  The pad part is free-form because pad names
# are strings in KiCad ("1", "A2", "MP" for mounting pads).
_PIN_RE = re.compile(r"^([A-Za-z_]+[0-9]+[A-Za-z]?)\.(.+)$")

# Net names KiCad treats as power/ground for netclass purposes.  Only used to give better
# hints; nothing here depends on the list being complete.
_POWER_HINTS = ("GND", "AGND", "DGND", "VSS", "VCC", "VDD", "3V3", "5V", "VIN", "VBUS")

SEVERITY_ERROR = "error"
SEVERITY_WARNING = "warning"

# Default schematic symbol per footprint LIBRARY.  Keyed on the library and not the
# footprint because that is the only level at which the mapping is honest: an 0603 land
# pattern in Resistor_SMD is a resistor, but a bare package footprint (SOT-23, SOIC-8)
# says nothing about the part on it — ki_fp_filters plus a pad-set match still leaves
# 137 symbol candidates for SOT-23 — so those parts must declare "symbol" themselves.
_DEFAULT_SYMBOLS = {
    "Resistor_SMD": "Device:R",
    "Resistor_THT": "Device:R",
    "Capacitor_SMD": "Device:C",
    "Capacitor_THT": "Device:C",
    "Inductor_SMD": "Device:L",
    "LED_SMD": "Device:LED",
    "LED_THT": "Device:LED",
    "Diode_SMD": "Device:D",
    "MountingHole": "Mechanical:MountingHole",
}


def default_symbol(footprint: str, pad_count: int) -> str:
    """The symbol a footprint implies, or "" when nothing safe can be implied."""
    lib = footprint.split(":")[0] if ":" in footprint else ""
    if lib in _DEFAULT_SYMBOLS:
        return _DEFAULT_SYMBOLS[lib]
    # Pin headers are generic by construction; the symbol is sized from the pad count.
    if lib.startswith("Connector_PinHeader") or lib.startswith("Connector_PinSocket"):
        return f"Connector_Generic:Conn_01x{pad_count:02d}"
    return ""


class IRError(RuntimeError):
    """The design file could not be read or is structurally unusable."""


@dataclass
class Problem:
    """One validation finding, addressed to whoever (or whatever) wrote the design."""

    severity: str
    where: str                      # "parts.U1", "nets.3V3", "placement.C1"
    message: str
    hint: str = ""

    def __str__(self) -> str:
        line = f"{self.severity}: {self.where}: {self.message}"
        return f"{line}\n    hint: {self.hint}" if self.hint else line

    def as_dict(self) -> Dict[str, str]:
        return {"severity": self.severity, "where": self.where,
                "message": self.message, "hint": self.hint}


@dataclass
class Part:
    """One placeable component."""

    ref: str
    footprint: str                  # "Lib:Name"
    # The schematic symbol.  Optional in the file because passives and headers have safe
    # defaults (default_symbol); required in practice for anything package-generic,
    # because footprint -> symbol cannot be inferred — an 0603 could be R, C or LED, and
    # a SOT-23 could be one of 137 things.
    symbol: str = ""
    value: str = ""
    lcsc: str = ""
    mpn: str = ""
    datasheet: str = ""
    description: str = ""
    dnp: bool = False
    exclude_from_bom: bool = False
    # Free-form extra fields copied through to the netlist and the board's footprint
    # properties, so the parts ledger's provenance survives into the fab outputs.
    fields: Dict[str, str] = field(default_factory=dict)

    @property
    def uuid_seed(self) -> str:
        return f"part:{self.ref}"


@dataclass
class Placement:
    """Where a part goes — either stated outright or constrained relative to others.

    `at` wins when given.  Otherwise the placer resolves `near`/`edge`/`group` into
    coordinates; see place.py.  Keeping intent (not coordinates) in the IR is the whole
    point: moving a connector should move its decoupling with it.
    """

    ref: str
    at: Optional[Tuple[float, float]] = None
    rot: float = 0.0
    side: str = "front"             # front | back
    near: str = ""                  # "U1" or a pin "U1.1"
    within: float = 3.0             # mm, honoured with `near`
    edge: str = ""                  # north | south | east | west
    group: str = ""                 # parts sharing a group are kept together
    lock: bool = False


@dataclass
class Board:
    """Board-level intent."""

    layers: int = 2
    thickness: float = 1.6
    outline: str = "auto"           # "auto" | "rect"
    width: Optional[float] = None   # required when outline == "rect"
    height: Optional[float] = None
    margin: float = 2.0             # copper-to-edge margin used when outline == "auto"
    # Gap left between courtyards.  None means "work it out from the rules": a channel
    # wide enough for one track with clearance on both sides.  Packing tighter than that
    # produces a board that is courtyard-legal and unroutable, which is the worst of both
    # — it passes the placer's own gate and then strands pads with nowhere to escape.
    part_gap: Optional[float] = None
    chamfer: float = 0.0
    origin: Tuple[float, float] = (100.0, 100.0)
    mounting_holes: int = 0
    mounting_hole_footprint: str = "MountingHole:MountingHole_3.2mm_M3"
    ground_net: str = "GND"
    pour_ground: bool = True


# The generated project enforces this between drilled hole edges.  The router uses the same
# number when deciding whether two same-net vias must become one; keeping it here prevents the
# post-route repair and KiCad's DRC from silently drifting apart.
MIN_HOLE_SPACING = 0.25


@dataclass
class Rules:
    """Design rules the generated project carries.  Defaults are JLCPCB 2-layer safe."""

    clearance: float = 0.2
    track_width: float = 0.25
    via_diameter: float = 0.8
    via_drill: float = 0.4
    # The small via kept for fine-pitch fanout.  A 0.8 mm via does not fit between the
    # pads of a 0.65 mm pitch part, so a board with one standard via size cannot bond the
    # middle pads of an LGA to a ground pour at all.
    #
    # 0.6/0.3 rather than the fab's outright minimum: the annular ring is
    # (0.6 - 0.3)/2 = 0.15 mm, which clears the 0.13 mm min_via_annular_width the project
    # file sets, and the hole matches min_through_hole_diameter.  A 0.45/0.3 via looks
    # smaller and better and is rejected by the board's own rules -- DRC reported it as
    # annular_width, which reads like a library problem and is really an arithmetic one.
    via_diameter_min: float = 0.6
    via_drill_min: float = 0.3
    min_track_width: float = 0.15
    min_hole: float = 0.3
    edge_clearance: float = 0.5
    track_widths: Dict[str, float] = field(default_factory=dict)   # net -> width


@dataclass
class Design:
    """A whole design, validated against real footprint libraries."""

    project: str
    title: str = ""
    rev: str = "A"
    company: str = ""
    board: Board = field(default_factory=Board)
    rules: Rules = field(default_factory=Rules)
    parts: Dict[str, Part] = field(default_factory=dict)
    nets: Dict[str, List[str]] = field(default_factory=dict)       # net -> ["U1.1", …]
    no_connect: List[str] = field(default_factory=list)
    placement: Dict[str, Placement] = field(default_factory=dict)
    source_path: Optional[Path] = None

    # ------------------------------------------------------------------ derived views

    def pin_owner(self) -> Dict[str, str]:
        """`"U1.1" -> "3V3"` for every pin the design nets."""
        owner: Dict[str, str] = {}
        for net, pins in self.nets.items():
            for pin in pins:
                owner[pin] = net
        return owner

    def net_of(self, pin: str) -> Optional[str]:
        return self.pin_owner().get(pin)

    def refs(self) -> List[str]:
        return sorted(self.parts)

    def power_nets(self) -> List[str]:
        return [n for n in self.nets if n.upper() in _POWER_HINTS]

    # ------------------------------------------------------------------- validation

    def validate(self, library: Library,
                 symbols: Optional[SymbolLibrary] = None) -> List[Problem]:
        """Every structural check, against the real footprint and symbol libraries.

        Returns problems rather than raising: a caller usually wants the whole list, and an
        agent fixing its own design wants all of them in one pass rather than one per run.
        """
        problems: List[Problem] = []
        problems += self._check_identity()
        footprints = self._check_footprints(library, problems)
        problems += self._check_pins(footprints)
        problems += self._check_nets(footprints)
        problems += self._check_placement()
        problems += self._check_symbols(footprints, symbols or SymbolLibrary.default())
        return problems

    def symbol_id(self, ref: str, pad_count: int) -> str:
        """The symbol a part will be drawn with: declared, or the footprint's default."""
        part = self.parts[ref]
        return part.symbol or default_symbol(part.footprint, pad_count)

    def resolve_symbols(self, footprints: Dict[str, Any],
                        symbols: SymbolLibrary) -> Dict[str, Any]:
        """`ref -> Symbol` for every part whose symbol resolves; the rest are absent.

        Shared by validate() and the schematic emitter so "which symbol is R1" has
        exactly one implementation — a validator that resolves differently from the
        emitter would pass designs the emitter then mis-draws.
        """
        resolved: Dict[str, Any] = {}
        for ref in sorted(self.parts):
            fp = footprints.get(ref)
            if fp is None:
                continue
            plated = [n for n, p in fp.pads.items() if p.plated]
            libid = self.symbol_id(ref, len(plated))
            if not libid:
                continue
            try:
                resolved[ref] = symbols.resolve(libid)
            except SymbolError:
                pass
        return resolved

    def _check_symbols(self, footprints: Dict[str, Any],
                       symbols: SymbolLibrary) -> List[Problem]:
        """The schematic side of the contract: a resolvable symbol whose pins match.

        The pin-number/pad-number comparison is the check that makes "U1.5" mean the
        same thing in both worlds.  A symbol whose pin set differs from the footprint's
        pad set is a real design error — the board and the schematic would describe
        different circuits — and nothing downstream (ERC, DRC) reports it directly.
        """
        out: List[Problem] = []
        for ref, part in sorted(self.parts.items()):
            fp = footprints.get(ref)
            if fp is None:
                continue                          # already reported as a footprint error
            plated = sorted(n for n, p in fp.pads.items() if p.plated)
            libid = self.symbol_id(ref, len(plated))
            if not libid:
                out.append(Problem(
                    SEVERITY_ERROR, f"parts.{ref}",
                    f"no symbol, and none can be inferred from {part.footprint!r}",
                    'add "symbol": "Library:Name" — a package footprint does not '
                    "determine the part on it, so the schematic symbol must be declared"))
                continue
            try:
                symbol = symbols.resolve(libid)
            except SymbolError as exc:
                stem = libid.split(":")[-1]
                near = symbols.suggest(stem) if stem else []
                out.append(Problem(
                    SEVERITY_ERROR, f"parts.{ref}",
                    f"symbol {libid!r} is not in any library",
                    ("did you mean: " + ", ".join(near[:5])) if near
                    else str(exc).replace("\n", " ")))
                continue

            pins = sorted(symbol.pins)
            missing = [n for n in plated if n not in symbol.pins]
            extra = [n for n in pins if n not in set(plated)]
            if missing or extra:
                detail = []
                if missing:
                    detail.append("footprint pads with no symbol pin: "
                                  + ", ".join(missing))
                if extra:
                    detail.append("symbol pins with no footprint pad: "
                                  + ", ".join(extra))
                out.append(Problem(
                    SEVERITY_ERROR, f"parts.{ref}",
                    f"symbol {libid} and footprint {part.footprint} disagree — "
                    + "; ".join(detail),
                    f"symbol pins: {', '.join(pins) or '(none)'}; footprint pads: "
                    f"{', '.join(plated) or '(none)'} — pick a symbol variant whose "
                    "pins match the package, or a footprint that matches the part"))
        return out

    def _check_identity(self) -> List[Problem]:
        out: List[Problem] = []
        if not self.project:
            out.append(Problem(SEVERITY_ERROR, "project", "project name is empty",
                               "give the design a name; it becomes the file stem"))
        elif not re.match(r"^[A-Za-z0-9][A-Za-z0-9._-]*$", self.project):
            out.append(Problem(SEVERITY_ERROR, "project",
                               f"{self.project!r} is not usable as a file name",
                               "letters, digits, dot, dash and underscore only"))
        # A design with no parts is structurally "clean" — every per-part and per-net check
        # has nothing to reject — but it cannot be built: `place` has nothing to place and
        # the netlist import would produce an empty board.  Reporting it here matters most
        # to the automatic repair loop, which treats `ok: true` as permission to ask a human
        # to approve a build; a truncated or placeholder model reply must not qualify.
        if not self.parts:
            out.append(Problem(SEVERITY_ERROR, "parts", "the design has no parts",
                               "add at least one part keyed by reference designator, e.g. "
                               '"parts": {"R1": "Resistor_SMD:R_0603_1608Metric"}'))
        elif not self.nets:
            out.append(Problem(SEVERITY_ERROR, "nets", "the design has no nets",
                               "connect the parts: \"nets\": {\"GND\": [\"J1.1\", \"R1.2\"]}; "
                               "a board with no nets has nothing to route"))
        for ref in self.parts:
            if not _REF_RE.match(ref):
                out.append(Problem(
                    SEVERITY_ERROR, f"parts.{ref}",
                    f"{ref!r} is not a reference designator",
                    "use letters then digits, e.g. R1, U2, J10 — the netlist import "
                    "matches board footprints to schematic symbols by this string"))
        if self.board.layers not in (1, 2, 4, 6, 8):
            out.append(Problem(SEVERITY_ERROR, "board.layers",
                               f"{self.board.layers} is not a supported layer count",
                               "use 1, 2, 4, 6 or 8"))
        if self.board.outline == "rect" and not (self.board.width and self.board.height):
            out.append(Problem(SEVERITY_ERROR, "board.outline",
                               "outline 'rect' needs width and height",
                               "set board.width/board.height, or use outline 'auto' to let "
                               "the placer shrink the board to the parts"))
        return out

    def _check_footprints(self, library: Library,
                          problems: List[Problem]) -> Dict[str, Any]:
        """Resolve every part's footprint; record problems for the ones that fail."""
        resolved: Dict[str, Any] = {}
        for ref, part in sorted(self.parts.items()):
            if not part.footprint:
                problems.append(Problem(
                    SEVERITY_ERROR, f"parts.{ref}", "no footprint",
                    "every part needs 'footprint': 'Library:Name' — the netlist import "
                    "loads the board footprint from this field and cannot invent one"))
                continue
            try:
                resolved[ref] = library.resolve(part.footprint)
            except LibraryError as exc:
                stem = part.footprint.split(":")[-1]
                near = library.suggest(stem) if stem else []
                problems.append(Problem(
                    SEVERITY_ERROR, f"parts.{ref}",
                    f"footprint {part.footprint!r} is not in any library",
                    ("did you mean: " + ", ".join(near[:5])) if near
                    else str(exc).replace("\n", " ")))
        return resolved

    def _check_pins(self, footprints: Dict[str, Any]) -> List[Problem]:
        """The connectivity contract: every real pad is netted or explicitly no-connect.

        This is the check that makes "all connected" a property of the design rather than
        something discovered later by DRC's unconnected-items count.
        """
        out: List[Problem] = []
        owner = self.pin_owner()
        nc = set(self.no_connect)

        for ref, fp in sorted(footprints.items()):
            for number, pad in sorted(fp.pads.items()):
                # An unplated mounting hole carries no net by construction.
                if not pad.plated:
                    continue
                pin = f"{ref}.{number}"
                if pin in owner or pin in nc:
                    continue
                out.append(Problem(
                    SEVERITY_ERROR, f"parts.{ref}",
                    f"pad {number} is in no net and is not declared no-connect",
                    f'add "{pin}" to a net, or list it in "no_connect" if it is '
                    "deliberately unconnected (a mechanical pin, a spare header position)"))

        # A pin that is netted twice is a short the netlist would encode faithfully.
        seen: Dict[str, str] = {}
        for net, pins in sorted(self.nets.items()):
            for pin in pins:
                if pin in seen and seen[pin] != net:
                    out.append(Problem(
                        SEVERITY_ERROR, f"nets.{net}",
                        f"{pin} is already on net {seen[pin]!r}",
                        "a pad belongs to exactly one net; remove it from one of them"))
                seen[pin] = net

        for pin in sorted(nc):
            if pin in owner:
                out.append(Problem(
                    SEVERITY_ERROR, "no_connect",
                    f"{pin} is declared no-connect but is on net {owner[pin]!r}",
                    "decide which one is true"))
        return out

    def _check_nets(self, footprints: Dict[str, Any]) -> List[Problem]:
        out: List[Problem] = []
        for net, pins in sorted(self.nets.items()):
            if not net:
                out.append(Problem(SEVERITY_ERROR, "nets", "a net has an empty name",
                                   "name every net; the fork references nets by name only"))
                continue
            if len(pins) == 1:
                out.append(Problem(
                    SEVERITY_WARNING, f"nets.{net}",
                    "only one pin on this net",
                    "a one-pin net connects nothing — either add the other end or move "
                    "the pin to 'no_connect'"))
            for pin in pins:
                match = _PIN_RE.match(pin)
                if not match:
                    out.append(Problem(
                        SEVERITY_ERROR, f"nets.{net}", f"{pin!r} is not a pin reference",
                        'write pins as "REF.PAD", e.g. "U1.3" or "J1.A2"'))
                    continue
                ref, pad = match.groups()
                if ref not in self.parts:
                    out.append(Problem(
                        SEVERITY_ERROR, f"nets.{net}", f"no part {ref!r}",
                        f"known parts: {', '.join(self.refs()) or '(none)'}"))
                    continue
                fp = footprints.get(ref)
                if fp is not None and pad not in fp.pads:
                    known = ", ".join(sorted(fp.pads))
                    out.append(Problem(
                        SEVERITY_ERROR, f"nets.{net}",
                        f"{ref} ({fp.libid}) has no pad {pad!r}",
                        f"its pads are: {known}"))
        return out

    def _check_placement(self) -> List[Problem]:
        out: List[Problem] = []
        for ref, spec in sorted(self.placement.items()):
            if ref not in self.parts:
                out.append(Problem(SEVERITY_ERROR, f"placement.{ref}",
                                   f"no part {ref!r}",
                                   f"known parts: {', '.join(self.refs()) or '(none)'}"))
                continue
            if spec.side not in ("front", "back"):
                out.append(Problem(SEVERITY_ERROR, f"placement.{ref}",
                                   f"side {spec.side!r} is not 'front' or 'back'", ""))
            if spec.edge and spec.edge not in ("north", "south", "east", "west"):
                out.append(Problem(SEVERITY_ERROR, f"placement.{ref}",
                                   f"edge {spec.edge!r} is not a compass direction",
                                   "use north, south, east or west"))
            if spec.near:
                target = spec.near.split(".")[0]
                if target not in self.parts:
                    out.append(Problem(SEVERITY_ERROR, f"placement.{ref}",
                                       f"near {spec.near!r}: no part {target!r}", ""))
                elif target == ref:
                    out.append(Problem(SEVERITY_ERROR, f"placement.{ref}",
                                       "a part cannot be placed near itself", ""))
        return out


# ---------------------------------------------------------------------------------------
# loading
# ---------------------------------------------------------------------------------------

def _as_xy(value: Any, where: str) -> Tuple[float, float]:
    if (isinstance(value, (list, tuple)) and len(value) == 2
            and all(isinstance(v, (int, float)) for v in value)):
        return (float(value[0]), float(value[1]))
    raise IRError(f"{where}: expected [x, y] in mm, got {value!r}")


def _as_bool(value: Any, where: str) -> bool:
    """Require an actual JSON/YAML boolean instead of applying Python truthiness."""
    if isinstance(value, bool):
        return value
    raise IRError(f"{where}: expected true or false, got {value!r}")


# Keys the `placement` block understands. Inside a part they are a mistake, not a field.
_PLACEMENT_KEYS = frozenset({"near", "within", "edge", "group", "rot", "side", "lock",
                             "at", "x", "y"})


def _part_from(ref: str, raw: Any) -> Part:
    # A bare string is the footprint — the common case for passives, and it keeps a
    # 30-part design readable.
    if isinstance(raw, str):
        return Part(ref=ref, footprint=raw)
    if not isinstance(raw, dict):
        raise IRError(f"parts.{ref}: expected an object or a footprint string, "
                      f"got {type(raw).__name__}")

    known = {"footprint", "symbol", "value", "lcsc", "mpn", "datasheet", "description",
             "dnp", "exclude_from_bom", "fields"}

    # Placement constraints belong in the top-level `placement` block. Written inside a part
    # they used to fall through to `extra` and become a *schematic field* named "near" or
    # "edge": the placer never saw them, `_check_placement` never validated them, and a
    # design with `"edge": "left"` (not even a compass direction) reported "no problems".
    # An unknown key here is far more likely to be that mistake than a deliberate field, and
    # `fields` exists for deliberate ones.
    misplaced = sorted(k for k in raw if k in _PLACEMENT_KEYS)
    if misplaced:
        raise IRError(
            f"parts.{ref}: {', '.join(repr(k) for k in misplaced)} "
            f"{'belong' if len(misplaced) > 1 else 'belongs'} in the top-level "
            f"\"placement\" block, not inside a part\n"
            f'    hint: "placement": {{ "{ref}": {{ '
            + ", ".join(f'"{k}": {json.dumps(raw[k])}' for k in misplaced)
            + " } }")

    extra = {k: str(v) for k, v in raw.items() if k not in known}

    fields = dict(raw.get("fields") or {})
    fields.update(extra)

    return Part(
        ref=ref,
        footprint=str(raw.get("footprint", "")),
        symbol=str(raw.get("symbol", "")),
        value=str(raw.get("value", "")),
        lcsc=str(raw.get("lcsc", "")),
        mpn=str(raw.get("mpn", "")),
        datasheet=str(raw.get("datasheet", "")),
        description=str(raw.get("description", "")),
        dnp=_as_bool(raw.get("dnp", False), f"parts.{ref}.dnp"),
        exclude_from_bom=_as_bool(raw.get("exclude_from_bom", False),
                                  f"parts.{ref}.exclude_from_bom"),
        fields={k: str(v) for k, v in fields.items()},
    )


def _placement_from(ref: str, raw: Any) -> Placement:
    if not isinstance(raw, dict):
        raise IRError(f"placement.{ref}: expected an object, got {type(raw).__name__}")
    at = raw.get("at")
    return Placement(
        ref=ref,
        at=_as_xy(at, f"placement.{ref}.at") if at is not None else None,
        rot=float(raw.get("rot", 0.0)),
        side=str(raw.get("side", "front")),
        near=str(raw.get("near", "")),
        within=float(raw.get("within", 3.0)),
        edge=str(raw.get("edge", "")),
        group=str(raw.get("group", "")),
        lock=_as_bool(raw.get("lock", False), f"placement.{ref}.lock"),
    )


def from_dict(raw: Any, source: Optional[Path] = None) -> Design:
    """Build a Design from parsed JSON/YAML.  Raises IRError on structural problems.

    Structural problems (wrong types, missing keys) raise; *design* problems (an unnetted
    pad, an unknown footprint) come back from validate().  The split matters: the first
    kind means the file cannot be understood at all, the second means it was understood
    and is wrong, and only the second is something an agent can iterate on.
    """
    if not isinstance(raw, dict):
        raise IRError(f"a design must be an object, got {type(raw).__name__}")

    board_raw = raw.get("board") or {}
    if not isinstance(board_raw, dict):
        raise IRError("board: expected an object")
    corner = board_raw.get("corner") or {}
    board = Board(
        layers=int(board_raw.get("layers", 2)),
        thickness=float(board_raw.get("thickness", 1.6)),
        outline=str(board_raw.get("outline", "auto")),
        width=(float(board_raw["width"]) if board_raw.get("width") is not None else None),
        height=(float(board_raw["height"]) if board_raw.get("height") is not None else None),
        margin=float(board_raw.get("margin", 2.0)),
        part_gap=(float(board_raw["part_gap"])
                  if board_raw.get("part_gap") is not None else None),
        chamfer=float(corner.get("chamfer", board_raw.get("chamfer", 0.0))),
        origin=_as_xy(board_raw.get("origin", [100.0, 100.0]), "board.origin"),
        mounting_holes=int(board_raw.get("mounting_holes", 0)),
        mounting_hole_footprint=str(board_raw.get(
            "mounting_hole_footprint", Board.mounting_hole_footprint)),
        ground_net=str(board_raw.get("ground_net", "GND")),
        pour_ground=_as_bool(board_raw.get("pour_ground", True), "board.pour_ground"),
    )

    rules_raw = raw.get("rules") or {}
    if not isinstance(rules_raw, dict):
        raise IRError("rules: expected an object")
    defaults = Rules()
    rules = Rules(
        clearance=float(rules_raw.get("clearance", defaults.clearance)),
        track_width=float(rules_raw.get("track_width", defaults.track_width)),
        via_diameter=float(rules_raw.get("via_diameter", defaults.via_diameter)),
        via_drill=float(rules_raw.get("via_drill", defaults.via_drill)),
        via_diameter_min=float(rules_raw.get("via_diameter_min",
                                             defaults.via_diameter_min)),
        via_drill_min=float(rules_raw.get("via_drill_min", defaults.via_drill_min)),
        min_track_width=float(rules_raw.get("min_track_width", defaults.min_track_width)),
        min_hole=float(rules_raw.get("min_hole", defaults.min_hole)),
        edge_clearance=float(rules_raw.get("edge_clearance", defaults.edge_clearance)),
        track_widths={str(k): float(v)
                      for k, v in (rules_raw.get("track_widths") or {}).items()},
    )

    parts_raw = raw.get("parts") or {}
    if not isinstance(parts_raw, dict):
        raise IRError("parts: expected an object keyed by reference designator")
    parts = {str(ref): _part_from(str(ref), spec) for ref, spec in parts_raw.items()}

    nets_raw = raw.get("nets") or {}
    if not isinstance(nets_raw, dict):
        raise IRError("nets: expected an object keyed by net name")
    nets: Dict[str, List[str]] = {}
    for name, pins in nets_raw.items():
        if isinstance(pins, str):
            pins = [pins]
        if not isinstance(pins, list):
            raise IRError(f"nets.{name}: expected a list of \"REF.PAD\" strings")
        nets[str(name)] = [str(p) for p in pins]

    nc_raw = raw.get("no_connect") or []
    if isinstance(nc_raw, str):
        nc_raw = [nc_raw]
    if not isinstance(nc_raw, list):
        raise IRError("no_connect: expected a list of \"REF.PAD\" strings")

    placement_raw = raw.get("placement") or {}
    if not isinstance(placement_raw, dict):
        raise IRError("placement: expected an object keyed by reference designator")

    return Design(
        project=str(raw.get("project", "")),
        title=str(raw.get("title", "")),
        rev=str(raw.get("rev", "A")),
        company=str(raw.get("company", "")),
        board=board,
        rules=rules,
        parts=parts,
        nets=nets,
        no_connect=[str(p) for p in nc_raw],
        placement={str(ref): _placement_from(str(ref), spec)
                   for ref, spec in placement_raw.items()},
        source_path=source,
    )


def load(path) -> Design:
    """Read a design from JSON (always) or YAML (when PyYAML is importable)."""
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise IRError(f"cannot read {path}: {exc}") from None

    if path.suffix.lower() in (".yaml", ".yml"):
        try:
            import yaml                                     # noqa: PLC0415
        except ImportError:
            raise IRError(
                f"{path} is YAML but PyYAML is not installed in this interpreter.\n"
                "Run it with the automation venv (phicad py …), or write the design as "
                "JSON — JSON is the canonical format and needs no dependency.") from None
        raw = yaml.safe_load(text)
    else:
        try:
            raw = json.loads(text)
        except json.JSONDecodeError as exc:
            raise IRError(f"{path}: invalid JSON at line {exc.lineno}: {exc.msg}") from None

    return from_dict(raw, source=path)


def format_problems(problems: Sequence[Problem]) -> str:
    """Problems as a block of text, errors first."""
    if not problems:
        return "no problems"
    order = {SEVERITY_ERROR: 0, SEVERITY_WARNING: 1}
    return "\n".join(str(p) for p in
                     sorted(problems, key=lambda p: (order.get(p.severity, 2), p.where)))


def errors(problems: Iterable[Problem]) -> List[Problem]:
    return [p for p in problems if p.severity == SEVERITY_ERROR]


if __name__ == "__main__":
    # Smoke test:  python3 phicad/synth/ir.py design.json
    if len(sys.argv) < 2:
        print("usage: ir.py <design.json>", file=sys.stderr)
        raise SystemExit(2)
    design = load(sys.argv[1])
    found = design.validate(Library.default())
    print(f"{design.project}: {len(design.parts)} parts, {len(design.nets)} nets")
    print(format_problems(found))
    raise SystemExit(1 if errors(found) else 0)
