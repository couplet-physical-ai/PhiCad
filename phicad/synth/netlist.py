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
"""Design IR -> KiCad netlist, the file that becomes a board.

Why the netlist is the right target
-----------------------------------
The fork's IPC API exposes `ImportNetlist` (`pcbnew/api/api_handler_pcb.cpp:1756`), and —
unlike most editor commands — it carries **no `checkForHeadless` guard**, with
`HEADLESS_PCB_CONTEXT::{ReadNetlistFromFile,MakeNetlistUpdater,OnNetlistChanged}` all
implemented (`pcbnew/api/headless_pcb_context.cpp:141,171,177`).  So a netlist file plus an
empty board is enough to make KiCad itself run the real `BOARD_NETLIST_UPDATER`: it loads
each footprint from the libraries, assigns reference and value, attaches every pad to its
net, and reports what it did.

That was verified live during development against a 37-line netlist: three footprints
loaded, four nets created, zero errors — with no s-expression emission on our side at all.

This matters because the alternative is what the Teensy demo does: write the `.kicad_pcb`
by hand, 1667 lines of it, re-deriving footprint bodies, pad nets and UUIDs that KiCad
already knows how to produce.  Every one of those lines is a chance to be subtly wrong in a
way no checker looks for.  Emitting a netlist instead means the board is built by the same
code path a human's "Update PCB from Schematic" uses.

Determinism
-----------
Component UUIDs are `uuid5` of a fixed namespace plus the project and reference, so
regenerating an unchanged design produces a byte-identical netlist and `git diff` shows only
real changes.  This is the same discipline `generate_board.py:61` uses, and it is what makes
the netlist safe to commit as the design's source of truth.

The UUID is also the *identity* the board remembers: `ImportNetlist` with
`match_mode=NMM_UUID` matches board footprints to netlist components by it, so a stable
UUID is what lets a part be renamed without the board losing its placement.
"""

from __future__ import annotations

import datetime
import sys
import uuid as _uuid
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from ir import Design, Part  # noqa: E402

# Fixed namespace for deterministic component UUIDs.  Never change it: it would re-identify
# every component in every existing design, and boards match footprints to components by
# this value.
_NS = _uuid.UUID("7a1d4e02-3c65-4f8b-9a17-5e2c0b6d94f1")

TOOL = "phicad-synth"

# KiCad's netlist format version tag.  "E" is what eeschema writes for 8.x/9.x/10.x files and
# what `pcbnew/netlist_reader/kicad_netlist_reader.cpp` accepts.
FORMAT_VERSION = "E"

# Pin electrical type written for every node unless the design says otherwise.  "passive" is
# the honest default: the board-side reader ignores it entirely, and claiming power_in or
# output for pins we have not been told about would put false information into any schematic
# generated from the same IR.
DEFAULT_PINTYPE = "passive"


def component_uuid(project: str, ref: str) -> str:
    """Stable per-(project, ref) UUID."""
    return str(_uuid.uuid5(_NS, f"{project}/{ref}"))


def _quote(text) -> str:
    """A netlist string literal.  KiCad's lexer wants backslash and quote escaped."""
    s = "" if text is None else str(text)
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def unconnected_net_name(ref: str, pad: str) -> str:
    """The net name given to a deliberately unconnected pad.

    A no-connect pad still gets a net of its own, matching what eeschema emits for a pin
    with a no-connect flag.  Two reasons, both learned from the updater's own output:

      * left out of the netlist entirely, the updater reports
        `No net found for component J1 pad 4 (no pin 4 in symbol)` as a WARNING — which is
        indistinguishable from the design actually having forgotten the pin, so a real
        omission would hide in a pile of intentional ones;
      * a single-pad net creates no ratsnest, so it cannot show up as an unconnected item
        in DRC.  Intent is recorded without costing a violation.
    """
    return f"unconnected-({ref}-Pad{pad})"


def _component_block(design: Design, part: Part, indent: str = "    ") -> str:
    """One `(comp …)` node."""
    lines: List[str] = [f"{indent}(comp (ref {_quote(part.ref)})"]
    body = indent + "  "

    lines.append(f"{body}(value {_quote(part.value or part.ref)})")
    lines.append(f"{body}(footprint {_quote(part.footprint)})")

    if part.datasheet:
        lines.append(f"{body}(datasheet {_quote(part.datasheet)})")

    # libsource is what a schematic would have supplied.  The board reader does not need it,
    # but `sch export bom` and any downstream tool that reads the netlist do, and writing the
    # footprint's own library keeps the row traceable to something real rather than inventing
    # a symbol library that does not exist.
    lib = part.footprint.split(":")[0] if ":" in part.footprint else "Device"
    symbol = part.footprint.split(":")[-1] if ":" in part.footprint else part.ref
    lines.append(f"{body}(libsource (lib {_quote(lib)}) (part {_quote(symbol)})"
                 f" (description {_quote(part.description)}))")

    # Properties the reader understands as flags (kicad_netlist_reader.cpp:750-760).
    if part.dnp:
        lines.append(f'{body}(property (name "dnp") (value "1"))')
    if part.exclude_from_bom:
        lines.append(f'{body}(property (name "exclude_from_bom") (value "1"))')

    # Free-form fields land on the board footprint as properties, because the import runs
    # with SetUpdateFields(true).  This is how the parts ledger's LCSC/MPN provenance reaches
    # the fab outputs without a second pass over the board.
    extra: Dict[str, str] = dict(part.fields)
    if part.lcsc:
        extra.setdefault("LCSC", part.lcsc)
    if part.mpn:
        extra.setdefault("MPN", part.mpn)
    # Datasheet and Description as FIELDS as well, not only as the `(datasheet …)` and
    # `(libsource … (description …))` nodes above.  Those two nodes are consumed by the
    # netlist reader but never reach the board: a footprint ends up with empty Datasheet
    # and Description properties, so anything reading the board back -- a BOM, a parts
    # audit, the pane's own part list -- sees nothing, even though the design said it.
    # Only the `fields` block survives the trip, because the import runs with
    # SetUpdateFields(true).
    if part.datasheet:
        extra.setdefault("Datasheet", part.datasheet)
    if part.description:
        extra.setdefault("Description", part.description)
    if extra:
        lines.append(f"{body}(fields")
        for name in sorted(extra):
            lines.append(f"{body}  (field (name {_quote(name)}) {_quote(extra[name])})")
        lines.append(f"{body})")

    lines.append(f'{body}(sheetpath (names "/") (tstamps "/"))')
    lines.append(f"{body}(tstamps {_quote(component_uuid(design.project, part.ref))}))")
    return "\n".join(lines)


def _split_pin(pin: str) -> Tuple[str, str]:
    ref, _, pad = pin.partition(".")
    return ref, pad


def net_blocks(design: Design) -> List[Tuple[str, List[str]]]:
    """Every net that will exist on the board: declared nets, then no-connect singletons.

    Returned in a stable order (declared nets alphabetically, then no-connects) so the
    emitted file is deterministic.
    """
    blocks: List[Tuple[str, List[str]]] = []
    for name in sorted(design.nets):
        pins = sorted(design.nets[name])
        if pins:
            blocks.append((name, pins))
    for pin in sorted(design.no_connect):
        ref, pad = _split_pin(pin)
        blocks.append((unconnected_net_name(ref, pad), [pin]))
    return blocks


def emit(design: Design, source: str = "", when: Optional[str] = None) -> str:
    """The whole netlist as text."""
    stamp = when or datetime.date.today().isoformat()
    src = source or f"{design.project}.kicad_sch"

    out: List[str] = [f"(export (version {_quote(FORMAT_VERSION)})"]
    out.append("  (design")
    out.append(f"    (source {_quote(src)})")
    out.append(f"    (date {_quote(stamp)})")
    out.append(f"    (tool {_quote(TOOL)})")
    out.append('    (sheet (number "1") (name "/") (tstamps "/")')
    out.append("      (title_block")
    out.append(f"        (title {_quote(design.title or design.project)})")
    out.append(f"        (company {_quote(design.company)})")
    out.append(f"        (rev {_quote(design.rev)})")
    out.append(f"        (source {_quote(src)}))))")

    out.append("  (components")
    parts = [design.parts[ref] for ref in sorted(design.parts)]
    for i, part in enumerate(parts):
        block = _component_block(design, part)
        out.append(block + (")" if i == len(parts) - 1 else ""))
    if not parts:
        out[-1] = "  (components)"

    out.append("  (nets")
    blocks = net_blocks(design)
    for i, (name, pins) in enumerate(blocks):
        out.append(f'    (net (code "{i + 1}") (name {_quote(name)})')
        for j, pin in enumerate(pins):
            ref, pad = _split_pin(pin)
            tail = "" if (j < len(pins) - 1) else ")"
            out.append(f"      (node (ref {_quote(ref)}) (pin {_quote(pad)})"
                       f" (pintype {_quote(DEFAULT_PINTYPE)})){tail}")
        if i == len(blocks) - 1:
            out[-1] += ")"
    if not blocks:
        out[-1] = "  (nets)"

    # Close the root `(export` node.  Every inner node closes itself above, so without this
    # the document is one paren short: it parses as truncated, and KiCad rejects it.
    out[-1] += ")"
    out.append("")
    return "\n".join(out)


def write(design: Design, path, source: str = "", when: Optional[str] = None) -> Path:
    """Emit the netlist to `path` and return it."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(emit(design, source=source, when=when), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------------------
# reading a netlist back — the cross-check side
# ---------------------------------------------------------------------------------------

def connectivity(design: Design) -> Dict[str, List[str]]:
    """`net -> sorted pins`, including the no-connect singletons.

    This is the design's own statement of what is connected.  `verify.py` compares it
    against what the *board* says, so "the board matches the intent" is a checkable claim
    rather than an assumption inherited from a successful import.
    """
    return {name: sorted(pins) for name, pins in net_blocks(design)}


def compare_connectivity(expected: Dict[str, Sequence[str]],
                         actual: Dict[str, Sequence[str]]) -> List[str]:
    """Differences between two `net -> pins` maps, as human-readable lines.

    Empty list means the board carries exactly the design's connectivity.  Nets are compared
    by name because the fork references nets by name only — there is no numeric net table to
    disagree about.
    """
    problems: List[str] = []
    for name in sorted(set(expected) | set(actual)):
        want = set(expected.get(name, ()))
        got = set(actual.get(name, ()))
        if want == got:
            continue
        if not got:
            problems.append(f"net {name!r} is missing from the board "
                            f"(design has {len(want)} pins on it)")
            continue
        if not want:
            problems.append(f"net {name!r} is on the board but not in the design "
                            f"({len(got)} pins)")
            continue
        missing = sorted(want - got)
        extra = sorted(got - want)
        detail = []
        if missing:
            detail.append("not connected on the board: " + ", ".join(missing))
        if extra:
            detail.append("connected but not in the design: " + ", ".join(extra))
        problems.append(f"net {name!r} differs — " + "; ".join(detail))
    return problems


if __name__ == "__main__":
    # Smoke test:  python3 phicad/synth/netlist.py design.json [out.net]
    from ir import Library, errors, format_problems, load          # noqa: PLC0415

    if len(sys.argv) < 2:
        print("usage: netlist.py <design.json> [out.net]", file=sys.stderr)
        raise SystemExit(2)

    d = load(sys.argv[1])
    found = d.validate(Library.default())
    if errors(found):
        print(format_problems(found), file=sys.stderr)
        raise SystemExit(1)

    text = emit(d)
    if len(sys.argv) > 2:
        Path(sys.argv[2]).write_text(text, encoding="utf-8")
        print(f"wrote {sys.argv[2]} — {len(d.parts)} components, "
              f"{len(net_blocks(d))} nets")
    else:
        print(text)
