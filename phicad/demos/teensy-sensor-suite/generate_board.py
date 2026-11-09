#!/usr/bin/env python3
#
# This program source code file is part of KiCad, a free EDA CAD application.
#
# Copyright (C) 2026 Exowatt, Inc.
# Copyright The KiCad Developers, see AUTHORS.txt for contributors.
#
# This program is free software; you can redistribute it and/or
# modify it under the terms of the GNU General Public License
# as published by the Free Software Foundation; either version 3
# of the License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, you may find one here:
# http://www.gnu.org/licenses/gpl-3.0.html
#
"""Generate the Teensy 4.0 prototyping sensor-suite demo board.

The board is emitted directly as PhiCad-flavour s-expressions (board file version
20260831).  Writing the file rather than driving the GUI keeps the demo
deterministic, diffable and runnable in CI.

Three fork-specific syntax facts this generator relies on (all verified against a
board written by this fork, not assumed from upstream 10.0):

1. A footprint's placement is ``(transform (translate X Y) (rotate A) (scale 1 1))``,
   *not* upstream's ``(at x y rot)``.
2. A pad's ``(at ...)`` is its LOCAL, UNROTATED offset from the footprint origin; the
   footprint's ``(rotate ...)`` is applied on top of it.  Pad coordinates therefore do
   not change when a part is rotated.
3. Nets are referenced by NAME only -- this fork writes no numeric ``(net N "name")``
   table, so a net exists purely by being named on a pad, segment or zone.

Usage::

    build/venv/bin/python phicad/demos/teensy-sensor-suite/generate_board.py
    build/venv/bin/python .../generate_board.py --output /tmp/scratch
"""

from __future__ import annotations

import argparse
import math
import os
import uuid as _uuid
from pathlib import Path

BOARD_VERSION = "20260831"
GENERATOR = "phicad-teensy-demo"

# Deterministic UUIDs: same input -> same file, so `git diff` is meaningful.
_NS = _uuid.UUID("5f3d1c7a-9b2e-4a61-8f0d-2c6e8a4b7d31")
_seen: set[str] = set()


def uid(tag: str) -> str:
    """Stable UUID for a logical element, with a collision guard."""
    if tag in _seen:
        raise ValueError(f"duplicate uuid tag {tag!r} -- tags must be unique")
    _seen.add(tag)
    return str(_uuid.uuid5(_NS, tag))


# --------------------------------------------------------------------------- geometry
#
# Board: 50 x 35 mm.  Origin at the top-left corner of the outline.
#
BOARD_W = 58.0            # wide enough for 3 F.Cu trunks + a sensor column + breakouts
BOARD_H = 68.0            # header run + a 6-lane B.Cu band + a vertical J5 below it + M3 holes
ORIGIN_X = 100.0
ORIGIN_Y = 75.0
CHAMFER = 3.0            # corner chamfer, mm
ZONE_INSET = 0.5         # pour inset from the outline (>= rules.min_copper_edge_clearance)
HOLE_INSET = 3.5         # M3 centre inset from each corner
HOLE_PAD = 3.2           # M3 NPTH pad diameter (= drill; stock MountingHole_3.2mm)
MASK_EXPANSION = 0.05    # board's pad_to_mask_clearance: the mask opening is this wider
SILK_CLEAR_MM = 0.2      # keep silkscreen this far off a mask aperture

# Teensy 4.0: two 1x14 headers on a 0.6 in (15.24 mm) row pitch, 2.54 mm pin pitch.
TEENSY_ROW_PITCH = 15.24
# ---------------------------------------------------------------------------------------
# Corridor plan.  Every F.Cu trunk gets one reserved x, and every B.Cu crossing gets one
# reserved y, declared here and nowhere else.  Hand-picking a coordinate per net is how the
# earlier revisions produced 20+ crossing errors: two nets would silently pick corridors
# 0.1 mm apart.  Declaring the plan in one table makes the spacing auditable at a glance
# (and check_routing.py proves it).
#
# 2.0 mm pitch >> 0.2 mm clearance + 0.4 mm widest trunk, so trunks cannot interact.
TRUNK_PITCH = 2.0
TRUNK_X0 = 28.0           # first trunk, right of the header block and left of the sensors

# 3V3 goes on the FAR side of the sensor column, not next to the I2C pair.  With all three
# trunks west of the sensors, a part's 3V3 tap had to cross both signal taps to get home;
# putting power east and signals west means each tap leaves in the direction of its own
# trunk and nothing crosses.  This is the ordinary "power one side, signals the other"
# discipline, and it removed the last cluster of unavoidable crossings.
CORRIDOR: dict[str, float] = {}
for _i, _net in enumerate(["SDA", "SCL"]):
    CORRIDOR[_net] = ORIGIN_X + TRUNK_X0 + _i * TRUNK_PITCH

CORRIDOR["3V3"] = ORIGIN_X + TRUNK_X0 + 12.0

# B.Cu horizontal lanes, same discipline: one reserved y per crossing net.  The band sits
# below the header columns (which end at TEENSY_Y + 33.02) and above the bottom mounting
# holes, and every lane here is 1.4 mm from its neighbours.
# The band must clear the LAST header pin row (pin 14 at TEENSY_Y + 33.02, i.e. y offset
# 38.52) by more than min_hole_clearance + pad radius, or a lane grazes a through-hole --
# 0.875 mm for a 0.25 mm track past a 1.0 mm drill.  40.4 gives ~1.9 mm.
LANE_PITCH = 1.6
LANE_Y0 = 40.4

# Every B.Cu crossing gets BOTH a unique lane y and a unique drop column x, allocated from
# the same ordered list.  Sharing either one is what produced the crossing errors; deriving
# both from one index makes that impossible by construction.
DROP_PITCH = 1.5
# Relative to TEENSY_X, i.e. left of the J1 column.  Pulled in from -3.0 so that five drop
# columns at DROP_PITCH still clear the west board edge -- see the assertion below.
DROP_X0 = -1.5

# Nets leaving the J1 column westward; they drop left of J1 and run east.
_CROSSINGS = ["SPI_SCK", "SPI_MOSI", "SPI_MISO", "SPI_CS", "3V3"]

LANE: dict[str, float] = {}
DROP: dict[str, float] = {}
for _i, _net in enumerate(_CROSSINGS):
    LANE[_net] = ORIGIN_Y + LANE_Y0 + _i * LANE_PITCH
    DROP[_net] = ORIGIN_X + 9.0 + DROP_X0 - _i * DROP_PITCH

# The drop columns march WEST, so with enough crossings the last one walks off the board: the
# 5th (3V3) landed at exactly x = ORIGIN_X, i.e. copper on the board edge.  Assert it instead
# of leaving it to be noticed in a render -- an off-board track is the kind of fault that still
# opens, still exports, and is only wrong at the fab.
_DROP_MIN = ORIGIN_X + ZONE_INSET + 0.5
if min(DROP.values()) < _DROP_MIN:
    raise SystemExit(
        f"drop column x={min(DROP.values()):.2f} is inside the board edge margin "
        f"(min {_DROP_MIN:.2f}). Widen the board west, reduce DROP_PITCH, or move a "
        f"crossing to the east side.")

# LED_A leaves the J2 column eastward, so its drop column is east of the header field.  Its
# lane must be the FIRST of the band, not the last: a drop column has to cross every lane
# between the pad and its own, so the net whose column is inside the band's x-range needs
# the topmost lane.  Giving it the bottom lane (the obvious "next free" choice) made its
# column cross all five lanes above it -- the subtle version of the same bug.
#
# It sits a full 2 x LANE_PITCH above the band so it also clears the last header pin row,
# whose pads it would otherwise graze on the way east.
LANE["LED_A"] = ORIGIN_Y + LANE_Y0 - 2 * LANE_PITCH
DROP["LED_A"] = ORIGIN_X + 22.0



# Pads that share a y with another net's tap, and how far to jog the tap off that row before
# heading for the trunk.  The LGA sensors put pads 1/3 (3V3/SDA) and 5/7 (SCL/GND) on single
# rows, and the 4-pin breakouts tap three trunks from three adjacent pins.  1.0 mm is well
# clear of the 0.2 mm clearance plus two 0.25 mm half-widths.
# Jog distances are chosen so that after escaping, every tap of a given part sits at its own
# y -- and the escapes go OUTWARD from the part body (top-row pads up, bottom-row pads down)
# so a track never runs under the package.
JOG: dict[str, float] = {
    # LGA-8 top row is pads 1-4 (local y -1.025), bottom row 5-8 (+1.025), and the two parts
    # do NOT share a pinout: U2 (LSM6DSO) has SCL/SDA/3V3 on pads 1/3/4, U3 (BME280) has
    # 3V3/SDA on 1/3 and SCL on 5.  Each tap therefore needs its own entry -- assuming a
    # shared pinout is how an earlier pass put two nets back on one row.
    # U2: all three taps are on the top row, so all escape upward at distinct offsets.
    # SCL, spread further than SDA so the two runs west stay apart.  -5.2 rather than -5.8:
    # -5.8 landed 0.395 mm from J3's SDA tap and -6.4 overshot to 0.205 mm on the far side, so
    # this is chosen for a ~1 mm gap to BOTH neighbouring SDA rows rather than nudged blindly.
    "U2.1": -5.2,
    "U2.3": -2.2,   # SDA
    "U2.4": -1.2,   # 3V3
    # U3: 3V3 and SDA on the top row escape up, SCL on the bottom row escapes down.
    # U3 at rot 180: pads 1-4 are now the BOTTOM row and 5-8 the top, and the VDD pads face
    # east.  SDA (3) and SCL (5) escape down and up respectively, 3V3 (1) needs no jog since
    # nothing else leaves eastward on its row.
    "U3.3": 3.4,    # SDA, down and west
    "U3.5": -3.1,   # SCL, up and west, clear of SDA (-3.1 not -2.4: at -2.4 it
                    # arrived 0.205 mm from J4's SDA tap -- caught by assert_tap_rows)
    # 4-pin breakouts: pins are on a 2.54 mm pitch but the three trunks are 2.0 mm apart, so
    # the taps are spread to keep each arrival distinct.
    # 4-pin breakouts: pins run down +y on a 2.54 mm pitch, and pad 2/3/4 tap 3V3/SDA/SCL.
    # 3V3's trunk is east of the signal trunks, so its tap heads the other way -- the jogs
    # only need to stop the two signal taps sharing a row with the power tap's escape.
    "J3.3": -1.4,
    "J4.3": -1.4,
    "J3.4": 2.6,
    "J4.4": 2.6,
    # J2 is the Teensy socket: SDA on pin 6, SCL on pin 7, one 2.54 mm pitch apart.  Their
    # taps run west to trunks only 2.0 mm apart, so without a spread the two horizontal runs
    # converge and end up 0.06 mm apart.
    "J2.6": -1.2,
    # +3.8, and the search for it is instructive.  +1.2 arrived 0.16 mm from J4's SDA tap
    # (two DIFFERENT parts, each jog tuned in isolation -- the flaw that assert_tap_rows now
    # catches).  +2.4 then drove the escape into J2 pin 8, 2.54 mm along the header.  On a
    # 2.54 mm pin pitch only offsets that clear a pin AND every other net's arrival row work,
    # which leaves +3.8.
    "J2.7": 3.8,

}

# Reference-designator silkscreen offsets, in footprint-local mm (x, y, rotation).  Chosen so
# each label clears its own pads AND its neighbours: the 0805 parts sit in dense columns, so
# their labels go out to the side rather than above, and the tall headers label at the end of
# the pin run instead of over pin 1.
#
# NOTE the offsets are FOOTPRINT-LOCAL, so they rotate with the part.  C1/C2/C3/D1/R3 are
# placed at rot=90, which lays their two pads along the local y axis -- so "beside the part"
# is a local -y offset for them and a local -x offset for the parts at rot=0.  Getting this
# backwards puts the label straight through the pads it was meant to avoid.
REF_POS: dict[str, tuple[float, float, int]] = {
    "J1": (-2.6, -2.0, 0),
    "J2": (2.6, -2.0, 0),
    "U2": (0.0, -2.6, 0),
    "U3": (0.0, 2.6, 0),
    "R1": (0.0, -1.5, 0),
    "R2": (0.0, -1.5, 0),
    # rot=90 parts: offset along local -y to clear the pad column, label upright on board.
    "R3": (0.0, -2.1, 90),
    "C1": (0.0, -2.1, 90),
    "C2": (0.0, -2.1, 90),
    "C3": (0.0, -2.1, 90),
    "D1": (0.0, -2.1, 90),
    # J3/J4 label EAST of the body, not over pin 1, and this is forced rather than cosmetic:
    # these two stack 11.4 mm apart while the footprint body runs 9 mm down from pin 1, so a
    # label 2.2 mm above pin 1 lands 0.26 mm inside the silk of the header ABOVE it.  Nudging
    # it vertically has only a ~0.11 mm feasible window (J3's silk bottom above, J4's own pin-1
    # pad below), which is not a margin worth shipping, and moving J4 down instead means
    # re-sweeping the header spacing that the routing depends on.  Labelling outward also
    # matches J1/J2 and is what the note at the top of this table asks for.
    "J3": (2.6, 5.135, 0),
    "J4": (2.6, 5.135, 0),
    "J5": (0.0, -2.4, 0),
    "H1": (-2.6, 0.0, 0),
    "H2": (2.6, 0.0, 0),
    "H3": (-2.6, 0.0, 0),
    "H4": (2.6, 0.0, 0),
}

TEENSY_PIN_PITCH = 2.54
TEENSY_X = ORIGIN_X + 9.0                      # left header column
TEENSY_Y = ORIGIN_Y + 5.5                      # pin 1 row
TEENSY_LEN = TEENSY_PIN_PITCH * 13             # pin1 -> pin14 span = 33.02

STOCK = Path(
    os.path.expanduser("~/Library/Application Support/kicad/stock/footprints")
)

FOOTPRINTS = {
    "hdr14":  ("Connector_PinHeader_2.54mm", "PinHeader_1x14_P2.54mm_Vertical"),
    "hdr4":   ("Connector_PinHeader_2.54mm", "PinHeader_1x04_P2.54mm_Vertical"),
    "hdr5":   ("Connector_PinHeader_2.54mm", "PinHeader_1x05_P2.54mm_Vertical"),
    "r0805":  ("Resistor_SMD", "R_0805_2012Metric"),
    "c0805":  ("Capacitor_SMD", "C_0805_2012Metric"),
    "led0805": ("LED_SMD", "LED_0805_2012Metric"),
    "lga8":   ("Package_LGA", "Bosch_LGA-8_2.5x2.5mm_P0.65mm_ClockwisePinNumbering"),
    "mount":  ("MountingHole", "MountingHole_3.2mm_M3"),
}


# Populated by footprint(): logical pad name -> absolute (x, y) in mm.  Routing reads this
# instead of guessing coordinates, so a track always lands on the pad centre and the design
# stays correct when a part moves.
PADS: dict[str, tuple[float, float]] = {}

# Same keys as PADS, holding each pad's largest dimension in mm.  Escapes are sized from this
# rather than from a constant, because pad sizes on this board span 0.35 mm (LGA-8) to 1.7 mm
# (0.1 inch header) and no single escape distance clears both.
PAD_W: dict[str, float] = {}

# Same keys again, holding each pad's net, so collision checks can ignore same-net contact.
PAD_NET: dict[str, str] = {}

# Per-pad (x_extent, y_extent) in BOARD axes, i.e. with the part's rotation already applied.
# PAD_W above deliberately stores max(w, h) because escape distance only cares about the
# worst case; bonding a multi-pad net needs the two axes apart, because "how far outside the
# row" is a y question and "how close is the neighbour" is an x question.
PAD_WH: dict[str, tuple[float, float]] = {}


def pad_xy(ref: str, pad: str | int) -> tuple[float, float]:
    key = f"{ref}.{pad}"

    if key not in PADS:
        raise KeyError(f"unknown pad {key} -- known: {sorted(PADS)[:12]}...")

    return PADS[key]


def rotate(x: float, y: float, angle: float) -> tuple[float, float]:
    """Rotate a footprint-local offset into board space.

    Sense verified against a board this fork wrote: R10 at (57.26, 43.686) rot 90 with pad 1
    local (-1.651, 0) has its track land at (57.26, 45.337), i.e. local -x maps to +y.
    """
    import math

    r = math.radians(angle)
    c, s = math.cos(r), math.sin(r)
    return (x * c + y * s, -x * s + y * c)


def pad_offsets(kind: str) -> dict[str, tuple[float, float]]:
    """Local pad centres of a stock footprint, straight from the library file."""
    import re

    lib, name = FOOTPRINTS[kind]
    text = (STOCK / f"{lib}.pretty" / f"{name}.kicad_mod").read_text()
    out: dict[str, tuple[float, float]] = {}

    for m in re.finditer(r'\(pad "([^"]+)"[^\n]*\n\s*\(at ([-\d.]+) ([-\d.]+)', text):
        out[m.group(1)] = (float(m.group(2)), float(m.group(3)))

    return out


def pad_dims(kind: str) -> dict[str, tuple[float, float]]:
    """Local pad (width, height) of a stock footprint, unrotated."""
    import re

    lib, name = FOOTPRINTS[kind]
    text = (STOCK / f"{lib}.pretty" / f"{name}.kicad_mod").read_text()
    out: dict[str, tuple[float, float]] = {}

    for m in re.finditer(r'\(pad "([^"]+)".*?\(size ([\d.]+) ([\d.]+)\)', text, re.S):
        out.setdefault(m.group(1), (float(m.group(2)), float(m.group(3))))

    return out


def pad_sizes(kind: str) -> dict[str, float]:
    """Local pad WIDTHS of a stock footprint, straight from the library file.

    Escape geometry has to clear the pad it is leaving, and pad sizes differ by an order of
    magnitude across this board: an LGA-8 pad is 0.35 mm wide, a 0.1 inch header pad is 1.7 mm.
    A single tuned escape distance therefore cannot be right for both -- 0.45 mm cleared the
    LGA fine and ran 0.1 mm INSIDE a header pin's copper.  So read the real size.
    """
    import re

    lib, name = FOOTPRINTS[kind]
    text = (STOCK / f"{lib}.pretty" / f"{name}.kicad_mod").read_text()
    out: dict[str, float] = {}

    for m in re.finditer(
        r'\(pad "([^"]+)".*?\(size ([\d.]+) ([\d.]+)\)', text, re.S
    ):
        out.setdefault(m.group(1), max(float(m.group(2)), float(m.group(3))))

    return out


def load_mod(kind: str) -> tuple[str, str]:
    """Return (library:name, inner body) for a stock footprint."""
    lib, name = FOOTPRINTS[kind]
    path = STOCK / f"{lib}.pretty" / f"{name}.kicad_mod"

    if not path.exists():
        raise SystemExit(f"missing stock footprint: {path}")

    text = path.read_text()
    body = text[text.index("\n") + 1:].rstrip()

    if body.endswith(")"):
        body = body[:-1].rstrip()

    # Drop the footprint-file-only header lines, and drop the library's own Reference /
    # Value / Datasheet / Description properties: we emit our own, and keeping both
    # leaves a stray "REF**" on the silkscreen (which DRC then reports against).
    keep: list[str] = []
    skip_depth: int | None = None
    depth = 0

    for line in body.splitlines():
        s = line.strip()

        if skip_depth is None and s.startswith('(property "') and any(
            f'(property "{p}"' in s for p in
            ("Reference", "Value", "Datasheet", "Description", "Footprint")
        ):
            skip_depth = depth

        opens = line.count("(")
        closes = line.count(")")

        if skip_depth is None:
            if not s.startswith(("(version ", "(generator ", "(generator_version ")):
                keep.append(line)

        depth += opens - closes

        if skip_depth is not None and depth <= skip_depth:
            skip_depth = None

    return f"{lib}:{name}", "\n".join(keep)


# --------------------------------------------------------------------------- emitters


# Per-part silkscreen outset, in footprint-local mm.  Each F.SilkS line coordinate is moved
# AWAY from the footprint origin by this much.
#
# Why this is needed at all: several stock 0805 footprints draw their body outline exactly
# 0.20 mm from the pad copper, and KiCad's silk_over_copper test requires MORE than 0.20 mm,
# so the library part sits precisely on the failing side of the comparison.  The consequence
# on a real board is not cosmetic -- the solder-mask aperture clips the silkscreen, so the
# outline is partly missing on the finished part.
#
# Keep these small.  Outsetting also moves the outline toward the NEIGHBOUR above/below, so
# the value is bounded on both sides and the pair (outset, neighbour spacing) has to be
# chosen together -- see the R3 spacing note at its placement.
SILK_OUTSET: dict[str, float] = {
    "D1": 0.10,
}


def _outset_silk(body: str, d: float) -> str:
    """Push every F.SilkS line in `body` away from the footprint origin by `d` mm."""
    if not d:
        return body

    import re      # module-local, matching load_mod() / pad_sizes() above

    def bump(v: str) -> str:
        f = float(v)
        return f"{f + (d if f > 0 else -d):.4f}"

    def one(m: re.Match) -> str:
        s = m.group(0)
        if '(layer "F.SilkS")' not in s:
            return s
        s = re.sub(r"\(start ([-\d.]+) ([-\d.]+)\)",
                   lambda mm: f"(start {bump(mm.group(1))} {bump(mm.group(2))})", s)
        s = re.sub(r"\(end ([-\d.]+) ([-\d.]+)\)",
                   lambda mm: f"(end {bump(mm.group(1))} {bump(mm.group(2))})", s)
        return s

    out = re.sub(r"\(fp_line(?:[^()]|\([^()]*\)|\((?:[^()]|\([^()]*\))*\))*\)", one, body)
    # A silent no-op here would look exactly like "the fix worked", so refuse instead.
    if out == body:
        raise SystemExit("SILK_OUTSET matched no F.SilkS fp_line -- footprint format changed?")
    return out


def footprint(kind, ref, value, x, y, rot=0, layer="F.Cu", pads=None, tag=None,
              hide_value=True) -> str:
    """One placed footprint.  `pads` maps pad number -> net name."""
    libname, body = load_mod(kind)
    body = _outset_silk(body, SILK_OUTSET.get(ref, 0.0))
    tag = tag or ref

    # Pad numbers in a .kicad_mod are strings ("1", "A2"), so normalise the caller's keys:
    # integer keys silently matched nothing and left every header pin netless.
    pads = {str(k): v for k, v in (pads or {}).items()}

    # Record absolute pad centres so the router can reference them by name.
    for pnum, (px, py) in pad_offsets(kind).items():
        dx, dy = rotate(px, py, rot) if rot else (px, py)
        PADS[f"{ref}.{pnum}"] = (x + dx, y + dy)

    # ...and their widths, so escape geometry can clear the pad it is actually leaving.
    for pnum, pw in pad_sizes(kind).items():
        PAD_W[f"{ref}.{pnum}"] = pw

    # ...and their per-axis extents in board axes, so bonding geometry can ask "how far
    # outside this row" separately from "how close is the pad next door".  A 90/270 rotation
    # swaps the two; getting that wrong silently picks the other axis.
    for pnum, (pw, ph) in pad_dims(kind).items():
        swap = rot % 180 == 90
        PAD_WH[f"{ref}.{pnum}"] = (ph, pw) if swap else (pw, ph)

    # ...and their nets, so a collision check can tell a real clash from same-net contact.
    for pnum, pnet in pads.items():
        PAD_NET[f"{ref}.{pnum}"] = pnet

    out: list[str] = [
        f'\t(footprint "{libname}"',
        f'\t\t(layer "{layer}")',
        f'\t\t(uuid "{uid("fp-" + tag)}")',
        "\t\t(transform",
        f"\t\t\t(translate {x:.4f} {y:.4f})",
    ]
    if rot:
        out.append(f"\t\t\t(rotate {rot})")
    out += ["\t\t\t(scale 1 1)", "\t\t)"]

    silk = "F.SilkS" if layer == "F.Cu" else "B.SilkS"
    fab = "F.Fab" if layer == "F.Cu" else "B.Fab"

    # Reference-designator placement is per-part, not a fixed offset.  A constant (0 -2.2)
    # put every refdes 2.2 mm above its own origin, which on this density lands on a
    # neighbour's pads or on another refdes -- that was the whole silk_over_copper /
    # silk_overlap warning population.  REF_POS gives each part an offset that clears its own
    # courtyard and its neighbours; the fallback stays (0 -2.2) for anything unlisted.
    rx, ry, rrot = REF_POS.get(ref, (0.0, -2.2, 0))

    out += [
        f'\t\t(property "Reference" "{ref}"',
        f"\t\t\t(at {rx} {ry} {rrot})",
        f'\t\t\t(layer "{silk}")',
        f'\t\t\t(uuid "{uid("ref-" + tag)}")',
        "\t\t\t(effects\n\t\t\t\t(font\n\t\t\t\t\t(size 0.8 0.8)"
        "\n\t\t\t\t\t(thickness 0.12)\n\t\t\t\t)\n\t\t\t)",
        "\t\t)",
        f'\t\t(property "Value" "{value}"',
        "\t\t\t(at 0 2.2 0)",
        f'\t\t\t(layer "{fab}")',
    ]
    if hide_value:
        out.append("\t\t\t(hide yes)")
    out += [
        f'\t\t\t(uuid "{uid("val-" + tag)}")',
        "\t\t\t(effects\n\t\t\t\t(font\n\t\t\t\t\t(size 0.8 0.8)"
        "\n\t\t\t\t\t(thickness 0.12)\n\t\t\t\t)\n\t\t\t)",
        "\t\t)",
    ]

    # Re-emit the library body, attaching nets to the pads we know about and dropping the
    # library uuids so the board has no duplicates.
    #
    # The net is appended just before the pad's closing paren rather than after its
    # (layers ...) line: stock pads often carry trailing keys such as
    # (remove_unused_layers no), and anchoring on (layers ...) attached the net to only
    # the first pad, silently leaving every other pad netless.
    pad_no = None
    pad_depth = 0
    depth = 0
    n = 0

    for line in body.splitlines():
        s = line.strip()

        if s.startswith("(uuid "):
            continue

        if pad_no is None and s.startswith("(pad "):
            try:
                pad_no = s.split('"')[1]
            except IndexError:
                pad_no = None
            pad_depth = depth

        depth_before = depth
        depth += line.count("(") - line.count(")")

        # The pad's own closing paren brings us back to where it started.
        if pad_no is not None and depth <= pad_depth and depth_before > pad_depth:
            indent = line[: len(line) - len(line.lstrip())]

            if pad_no in pads:
                out.append(f'\t{indent}\t(net "{pads[pad_no]}")')

            n += 1
            out.append(f'\t{indent}\t(uuid "{uid(f"pad-{tag}-{pad_no}-{n}")}")')
            out.append("\t" + line)
            pad_no = None
            continue

        out.append("\t" + line)

    out.append("\t)")
    return "\n".join(out)


# Track widths by role.  Named and passed EXPLICITLY at every call site: seg() used to default
# to 0.3, so any call that omitted the width silently produced a wider track than its own taps
# -- the SDA and SCL trunks ended up 0.3 mm while every tap into them was 0.25 mm.  A mixed
# width on one net is not a DRC error, so nothing complained; it just quietly stopped being
# the design that was intended.  W_SIGNAL is comfortably above rules.min_track_width (0.15).
# How far a tap leaves a pad PERPENDICULARLY before it may move sideways.  Must exceed half
# the pad's own extent plus clearance, or the sideways leg clips the pad it just left; on the
# LGA-8 here the pads are 0.5 mm tall on a 0.65 mm pitch, so 0.45 mm clears both.
PAD_ESCAPE = 0.45

W_SIGNAL = 0.25          # logic: I2C, SPI, LED
W_GND = 0.3              # ground stubs to stitching vias
W_POWER = 0.4            # 3V3 distribution


def seg(x1, y1, x2, y2, net, layer="F.Cu", width=W_SIGNAL, tag="") -> str:
    ROUTED.append((x1, y1, x2, y2, net, width))
    return (
        "\t(segment\n"
        f"\t\t(start {x1:.4f} {y1:.4f})\n"
        f"\t\t(end {x2:.4f} {y2:.4f})\n"
        f"\t\t(width {width})\n"
        f'\t\t(layer "{layer}")\n'
        f'\t\t(net "{net}")\n'
        f'\t\t(uuid "{uid("seg-" + tag)}")\n'
        "\t)"
    )


def bcu_cross(src: str, dst: str, net: str, drop_x: float, lane_y: float,
              width: float = 0.25, rise_x: float | None = None,
              stub_first: bool = True) -> list[str]:
    """Carry `net` from pad `src` to pad `dst` across the board on B.Cu.

    F.Cu is fully occupied by the three vertical trunks and the header fields, so anything
    that has to travel horizontally goes underneath: stub -> via -> lane -> via -> stub.
    `drop_x` and `lane_y` must come from the CORRIDOR / LANE plans, never be picked by hand.
    """
    sx, sy = pad_xy(*src.split("."))
    dx, dy = pad_xy(*dst.split("."))
    up_x = dx if rise_x is None else rise_x
    out: list[str] = []

    if stub_first and abs(sx - drop_x) > 1e-6:
        out.append(seg(sx, sy, drop_x, sy, net, width=width, tag=f"x_{net}_stub"))

    out += [
        via(drop_x, sy, net, f"x_{net}_a"),
        seg(drop_x, sy, drop_x, lane_y, net, layer="B.Cu", width=width,
            tag=f"x_{net}_down"),
        seg(drop_x, lane_y, up_x, lane_y, net, layer="B.Cu", width=width,
            tag=f"x_{net}_across"),
        via(up_x, lane_y, net, f"x_{net}_b"),
    ]

    # Come back up ORTHOGONALLY (vertical then horizontal), never as one diagonal: a diagonal
    # arrival cuts across whatever sits between the rise column and the target pad, which is
    # how LED_A ended up crossing the LED_K hop between D1 and R3.
    if abs(up_x - dx) > 1e-6 or abs(lane_y - dy) > 1e-6:
        if abs(up_x - dx) > 1e-6 and abs(lane_y - dy) > 1e-6:
            out += [
                seg(up_x, lane_y, up_x, dy, net, width=width, tag=f"x_{net}_up"),
                seg(up_x, dy, dx, dy, net, width=width, tag=f"x_{net}_in"),
            ]
        else:
            out.append(seg(up_x, lane_y, dx, dy, net, width=width, tag=f"x_{net}_up"))

    return out


def tap(ref_pad: str, net: str, width: float = 0.25, tag: str = "",
        jog: float = 0.0) -> list[str]:
    """Connect one pad to its net's F.Cu trunk, hopping under any trunk in the way.

    This is the piece that makes a multi-drop bus route itself.  A stub that runs from a pad
    to its trunk must not cross a *different* net's trunk, and on a 2-layer board the only
    way past one is to dive under it.  So:

      * no trunk between the pad and its own trunk  -> plain F.Cu stub
      * one or more in the way                      -> via, B.Cu hop, via, short F.Cu stub

    Earlier revisions picked stub geometry by hand and produced 20-30 crossing/short errors,
    because whether a stub crossed a trunk depended on component placement that kept moving.
    Deriving it from CORRIDOR means placement can change freely and the routing stays legal.
    """
    px, py = pad_xy(*ref_pad.split("."))
    trunk = CORRIDOR[net]

    # Several pads of one part can share a y (an LGA row, a 2-row header).  Taps from those
    # pads to different trunks would then run along the SAME line and short together, so the
    # caller can jog this tap off the pad row first.  Without it, U3 pads 1/3/5 -- all at one
    # y -- put 3V3, SDA and SCL on top of each other.
    # The escape is VERTICAL first: leaving along the pad row (horizontal) keeps this tap on
    # the same line as its neighbours, which is exactly what the jog exists to avoid.  Going
    # perpendicular to the row clears the row immediately, which is also how real LGA/QFN
    # fanout works.
    # The escape is VERTICAL first, then the run heads for the trunk.  The vertical part is
    # offset a little TOWARD the trunk as it goes, so it cannot graze another pad's
    # horizontal tap that leaves at the same x (U2's SCL escape passed 0.1 mm from U2's own
    # SDA tap when the escape was perfectly vertical).
    # The escape leaves the pad row PERPENDICULARLY (straight along y) and only then turns
    # toward the trunk.  Two subtleties, both learned from real collisions:
    #  * a purely vertical escape can graze a sibling pad's horizontal tap leaving at the
    #    same x, so the vertical part is placed half a pad-pitch to the trunk side first;
    #  * a diagonal escape crosses whatever it is trying to avoid, so this is L-shaped, not
    #    a straight lean.
    # The sideways step is proportional to the jog, so two pads of one part escaping by
    # different amounts also use different columns.  A constant step put U3's SDA and 3V3
    # escapes in the same column, where they shorted.
    out_pre: list[str] = []
    if abs(jog) > 1e-6:
        # PERPENDICULAR FIRST, then sideways.  This order is the whole point: on an LGA-8 the
        # pads sit on a 0.65 mm pitch, so a tap that steps sideways while still ON the pad row
        # runs straight over the neighbouring pad -- an outright short.  Real DRC caught four of
        # these (SDA over U2.2/U3.4 GND) that this checker missed for lack of a track-vs-pad
        # test.  Leaving along y clears the row before any sideways motion happens.
        #
        # `side` still exists so two pads escaping by different jogs use different columns (a
        # constant step put U3's SDA and 3V3 escapes in one column), but it is now applied
        # AFTER the row has been cleared.
        # ...but "perpendicular" is only along y for a part whose pads form ROWS.  A vertical
        # 1xN header's pads form a COLUMN, so leaving along y runs straight down the pin line
        # into the next pin -- J2's SCL tap drove into pin 8, 2.54 mm away.  Detect the layout
        # from the part's own pads instead of assuming, then escape across the pads, not along
        # them.
        ref = ref_pad.split(".")[0]
        siblings = [xy for key, xy in PADS.items()
                    if key.split(".")[0] == ref and key != ref_pad]
        in_column = sum(1 for sx, _ in siblings if abs(sx - px) < 0.05) > 1

        side = (-0.4 if trunk < px else 0.4) * (1.0 + 0.28 * abs(jog))

        if in_column:
            # Sideways off the pin column first, then along y in the cleared column.  The step
            # must clear the widest pad in that column, not a fixed distance: a 0.1 inch header
            # has 1.7 mm pads, so PAD_ESCAPE (0.45, sized for an LGA's 0.5 mm pads) left the
            # run 0.1 mm INSIDE pin 8's copper.  Derive it from the column instead.
            col_w = max([w for key, w in PAD_W.items()
                         if key.split(".")[0] == ref] or [0.0])
            step = math.copysign(col_w / 2 + 0.2 + width / 2 + 0.05, side)
            out_pre = [
                seg(px, py, px + step, py, net, width=width, tag=f"jog_{ref_pad}_a"),
                seg(px + step, py, px + step, py + jog, net, width=width,
                    tag=f"jog_{ref_pad}_b"),
            ]
            px, py = px + step, py + jog
        else:
            # Same reasoning as the column case: the perpendicular run must clear the pads it
            # passes, so size it from the row's real pads.  A flat PAD_ESCAPE of 0.45 left the
            # sideways leg 0.200 mm from U2.2/U3.4's GND pads where 0.325 was needed -- close
            # enough to pass a geometric eyeball and still be a DRC clearance error.
            row_w = max([w for key, w in PAD_W.items()
                         if key.split(".")[0] == ref] or [0.0])
            clear = math.copysign(max(PAD_ESCAPE, row_w / 2 + 0.2 + width / 2 + 0.05), jog)
            out_pre = [
                seg(px, py, px, py + clear, net, width=width, tag=f"jog_{ref_pad}_a"),
                seg(px, py + clear, px + side, py + clear, net, width=width,
                    tag=f"jog_{ref_pad}_b"),
                seg(px + side, py + clear, px + side, py + jog, net, width=width,
                    tag=f"jog_{ref_pad}_c"),
            ]
            px, py = px + side, py + jog

    lo, hi = min(px, trunk), max(px, trunk)

    blockers = [x for n, x in CORRIDOR.items()
                if n != net and lo + 1e-6 < x < hi - 1e-6]

    if abs(px - trunk) < 1e-6:
        return out_pre                             # pad already sits on the trunk

    label = tag or ref_pad.replace(".", "")

    if not blockers:
        return out_pre + [seg(px, py, trunk, py, net, width=width, tag=f"tap_{label}")]

    # Dive under the blocking trunk(s): leave the pad on F.Cu just far enough to clear its
    # own body, drop to B.Cu, cross, and come back up right next to the target trunk.
    # The dive-under via was placed at a flat 1.2 mm from the pad, which is blind to whatever
    # else already occupies that column: SDA's via landed 0.436 mm from SCL's escape riser where
    # 0.725 mm is needed.  Try the nominal step first, then progressively larger ones, and keep
    # the first that clears every track routed so far.
    sign = 1.0 if trunk > px else -1.0
    step = sign * 1.2

    # Order matters, and it is counter-intuitive: SMALLER steps are tried before larger ones
    # after the nominal.  Growing the step walks the via toward the neighbouring net's escape
    # riser (SDA's via crossed SCL's column entirely at 2.7 mm), whereas shrinking it keeps the
    # via tucked beside its own pad where there is nothing to hit.  0.9 mm is what clears here;
    # trying 1.7 first found nothing and made the board worse.
    for trial in (1.2, 0.9, 0.75, 1.7, 2.2):
        cand = px + sign * trial
        if _via_column_clear(cand, py, net):
            step = sign * trial
            break

    a_x = px + step
    b_x = trunk - step

    return out_pre + [
        seg(px, py, a_x, py, net, width=width, tag=f"tap_{label}_out"),
        via(a_x, py, net, f"tap_{label}_a"),
        seg(a_x, py, b_x, py, net, layer="B.Cu", width=width,
            tag=f"tap_{label}_under"),
        via(b_x, py, net, f"tap_{label}_b"),
        seg(b_x, py, trunk, py, net, width=width, tag=f"tap_{label}_in"),
    ]


def route(src: str, dst: str, net: str, via_x: float | None = None,
          via_y: float | None = None, layer: str = "F.Cu", width: float = 0.25,
          tag: str = "") -> list[str]:
    """Orthogonal route between two pads, given as "REF.PAD" names.

    Exactly one of `via_x` / `via_y` chooses the corridor: `via_x` goes
    horizontal -> vertical -> horizontal through x = via_x, `via_y` does the
    transpose.  Pad centres come from PADS, so a track always lands on copper.
    Zero-length runs are dropped -- DRC flags them as dangling.
    """
    x1, y1 = pad_xy(*src.split("."))
    x2, y2 = pad_xy(*dst.split("."))
    eps = 1e-6
    pts: list[tuple[float, float]] = [(x1, y1)]

    if via_x is not None:
        pts += [(via_x, y1), (via_x, y2)]
    elif via_y is not None:
        pts += [(x1, via_y), (x2, via_y)]

    pts.append((x2, y2))

    out: list[str] = []
    n = 0
    for (ax, ay), (bx, by) in zip(pts, pts[1:]):
        if abs(ax - bx) < eps and abs(ay - by) < eps:
            continue
        n += 1
        out.append(seg(ax, ay, bx, by, net, layer=layer, width=width,
                       tag=f"{tag or src + dst}-{n}"))

    return out


def via(x, y, net, tag, size=0.8, drill=0.4) -> str:
    return (
        "\t(via\n"
        f"\t\t(at {x:.4f} {y:.4f})\n"
        f"\t\t(size {size})\n"
        f"\t\t(drill {drill})\n"
        '\t\t(layers "F.Cu" "B.Cu")\n'
        f'\t\t(net "{net}")\n'
        f'\t\t(uuid "{uid("via-" + tag)}")\n'
        "\t)"
    )


def gr_line(x1, y1, x2, y2, layer="Edge.Cuts", width=0.1, tag="") -> str:
    return (
        "\t(gr_line\n"
        f"\t\t(start {x1:.4f} {y1:.4f})\n"
        f"\t\t(end {x2:.4f} {y2:.4f})\n"
        f"\t\t(stroke\n\t\t\t(width {width})\n\t\t\t(type solid)\n\t\t)\n"
        f'\t\t(layer "{layer}")\n'
        f'\t\t(uuid "{uid("gr-" + tag)}")\n'
        "\t)"
    )


def gr_text(text, x, y, layer="F.SilkS", size=1.0, tag="") -> str:
    return (
        f'\t(gr_text "{text}"\n'
        f"\t\t(at {x:.4f} {y:.4f})\n"
        f'\t\t(layer "{layer}")\n'
        f'\t\t(uuid "{uid("txt-" + tag)}")\n'
        "\t\t(effects\n\t\t\t(font\n"
        f"\t\t\t\t(size {size} {size})\n"
        f"\t\t\t\t(thickness {size * 0.15:.3f})\n"
        "\t\t\t)\n\t\t\t(justify left)\n\t\t)\n"
        "\t)"
    )


TAP_ROW_MIN = 0.45       # mm, min y separation between two different nets' tap arrival rows
VIA_SIZE = 0.8           # mm, stitching via pad diameter (must match via())

# Every track emitted so far, as (x1, y1, x2, y2, net, width).  Needed because GND stub
# placement has to avoid tracks, not just pads, and tracks are otherwise only ever emitted as
# formatted strings.
ROUTED: list[tuple[float, float, float, float, str, float]] = []


def _escape_columns() -> list[tuple[float, str]]:
    """The x of every jogged tap's escape riser, as (x, net).

    Recomputed from JOG rather than observed from ROUTED, because taps are emitted one net at a
    time: when SDA's dive-under via is placed, SCL's risers do not exist yet.  Letting emission
    order decide whether a board is legal is how SDA's via ended up 0.436 mm from SCL's riser.
    """
    out: list[tuple[float, str]] = []
    for key, jog in JOG.items():
        if key not in PADS or abs(jog) < 1e-6:
            continue
        net = PAD_NET.get(key, "")
        px, _ = PADS[key]
        trunk = CORRIDOR.get(net)
        if trunk is None:
            continue
        out.append((px + (-0.4 if trunk < px else 0.4) * (1.0 + 0.28 * abs(jog)), net))
    return out


def _clear_of_escape_columns(vx: float, net: str) -> bool:
    """True if a via at x=vx keeps clear of every other net's escape riser column."""
    for cx, cnet in _escape_columns():
        if cnet == net:
            continue
        if abs(vx - cx) < VIA_SIZE / 2 + W_SIGNAL / 2 + 0.2:
            return False
    return True


def _via_column_clear(vx: float, vy: float, net: str) -> bool:
    """True if a via of `net` at (vx, vy) clears every track of a different net routed so far."""
    for (ax, ay, bx, by, tnet, tw) in ROUTED:
        if tnet == net:
            continue
        dx, dy = bx - ax, by - ay
        L2 = dx * dx + dy * dy
        t = 0.0 if L2 == 0 else max(0.0, min(1.0, ((vx - ax) * dx + (vy - ay) * dy) / L2))
        if math.dist((vx, vy), (ax + t * dx, ay + t * dy)) < VIA_SIZE / 2 + tw / 2 + 0.2:
            return False
    return True


def _gnd_stub_clear(px: float, py: float, vx: float, vy: float,
                    ref: str, pad: str) -> bool:
    """True if a GND stub from (px,py) to a via at (vx,vy) clears all non-GND copper.

    Checks the via and the stub against every recorded pad AND against every routed track so
    far.  Pads alone were not enough -- R3's stub cleared every pad and landed 0.31 mm from the
    LED_A B.Cu lane, because a lane is a track, not a pad.  ROUTED is appended to by seg().
    """
    for (ax, ay, bx, by, tnet, tw) in ROUTED:
        if tnet == "GND":
            continue

        for (qx, qy, rad) in ((vx, vy, VIA_SIZE / 2), (px, py, W_GND / 2)):
            dx, dy = bx - ax, by - ay
            L2 = dx * dx + dy * dy
            t = 0.0 if L2 == 0 else max(0.0, min(1.0, ((qx - ax) * dx + (qy - ay) * dy) / L2))
            if math.dist((qx, qy), (ax + t * dx, ay + t * dy)) < rad + tw / 2 + 0.2:
                return False

    for key, (qx, qy) in PADS.items():
        if key == f"{ref}.{pad}":
            continue
        if PAD_NET.get(key) == "GND":
            continue                    # same net, touching is harmless

        half = PAD_W.get(key, 1.7) / 2

        # via barrel vs pad
        if math.dist((vx, vy), (qx, qy)) < half + VIA_SIZE / 2 + 0.2:
            return False

        # stub centreline vs pad, as a point-to-segment distance
        dx, dy = vx - px, vy - py
        L2 = dx * dx + dy * dy
        t = 0.0 if L2 == 0 else max(0.0, min(1.0, ((qx - px) * dx + (qy - py) * dy) / L2))
        if math.dist((qx, qy), (px + t * dx, py + t * dy)) < half + W_GND / 2 + 0.2:
            return False

    return True


def assert_tap_rows(rows: list[tuple[str, str, float]]) -> None:
    """Fail if two different nets' taps arrive on rows closer than TAP_ROW_MIN.

    Every tap's arrival y comes from a per-pad JOG entry, and those were each tuned by looking
    at one part in isolation.  That works until two taps from DIFFERENT parts happen to land on
    the same row: J2's SCL tap arrived 0.16 mm from J4's SDA tap, and neither part's jog looked
    wrong on its own.  Checking every arrival against every other is the only way to catch that
    class, so the generator now refuses to emit such a board instead of leaving it to DRC.
    """
    bad: list[str] = []
    for i, (net_a, tap_a, y_a) in enumerate(rows):
        for net_b, tap_b, y_b in rows[i + 1:]:
            if net_a == net_b:
                continue
            if abs(y_a - y_b) < TAP_ROW_MIN - 1e-9:
                bad.append(f"  {net_a} tap {tap_a} at y={y_a:.2f} vs "
                           f"{net_b} tap {tap_b} at y={y_b:.2f} "
                           f"(gap {abs(y_a - y_b):.3f} mm, need {TAP_ROW_MIN})")
    if bad:
        raise SystemExit("tap rows of different nets are too close:\n" + "\n".join(bad)
                         + "\nAdjust the JOG entry for one of them.")


# ---------------------------------------------------------------------------------------
# Multi-pad power/ground bonding for the LGA-8 sensors.
#
# Both sensors expose several VDD and several GND pads, and only one of each was ever
# routed -- the other nine sat floating, which is what the 10 "unconnected items" were.
# A floating VDD pad on a real part is a genuine defect: the die bonds to all of them, so
# leaving one open puts the supply through a single bond wire and defeats the decoupling.
#
# The geometry dictates the method.  Pads are 0.35 x 0.5 mm on a 0.65 mm pitch, so the gap
# between two adjacent pads in a row is 0.30 mm -- less than twice the 0.2 mm clearance, so
# NOTHING can be routed between them, at any width.  In-row linking is therefore impossible
# and the only free space is elsewhere:
#
#   * the BAND BETWEEN THE TWO ROWS is 1.55 mm of clear copper (rows sit +/-1.025 mm from
#     the centre, pads are 0.5 mm tall).  GND uses it: every GND pad stubs inward to a spine
#     on the package centreline, and ONE via drops that spine to the B.Cu ground pour.
#     Crossings between GND stubs are same-net, so they are free.
#   * OUTSIDE a row, on the side away from the package, is clear.  The spare VDD pads stub
#     outward to a rail there and run east into the 3V3 trunk, which already spans that y.
#
# GND goes to the pour rather than to a trunk because the pour is on B.Cu and is the lowest
# impedance path available; VDD cannot, because there is no 3V3 plane.
def bond_power_pads(ref: str, v3_side: str, v3_trunk: float,
                    clear: float = 0.30) -> tuple[list[str], list[float]]:
    """Bond every floating GND/VDD pad of an LGA-8 to its net.

    `v3_side` is "n" or "s": which row the spare VDD pads sit on, and therefore which way
    they escape.  `clear` is the gap from a pad edge to a parallel rail.

    Returns (board items, y values the 3V3 trunk must span to receive the new rails).
    """
    out: list[str] = []
    rails: list[float] = []
    pads = {n: xy for n, xy in PADS.items() if n.startswith(ref + ".")}
    if not pads:
        raise SystemExit(f"bond_power_pads: no pads recorded for {ref}")

    nets: dict[str, list[tuple[float, float]]] = {}
    for name, (px, py) in pads.items():
        nets.setdefault(PAD_NET.get(name, ""), []).append((px, py))

    ys = sorted({round(py, 3) for _, py in pads.values()})
    if len(ys) != 2:
        raise SystemExit(f"bond_power_pads: expected 2 pad rows for {ref}, got {ys}")
    y_top, y_bot = ys
    mid = (y_top + y_bot) / 2.0
    half_h = max(PAD_WH[n][1] for n in pads) / 2.0

    # --- GND: stub every pad to the centreline spine, then one via to the B.Cu pour -------
    gnd = sorted(nets.get("GND", []))
    if gnd:
        # Tags are indexed, not derived from the coordinate: three of these pads round to the
        # same integer x, and the uuid guard rejects duplicate tags.
        for i, (gx, gy) in enumerate(gnd):
            out.append(seg(gx, gy, gx, mid, "GND", width=W_GND, tag=f"{ref}gndstub{i}"))
        # The spine only needs to exist if the stubs do not already share an x.
        if len({round(gx, 3) for gx, _ in gnd}) > 1:
            out.append(seg(min(gx for gx, _ in gnd), mid, max(gx for gx, _ in gnd), mid,
                           "GND", width=W_GND, tag=f"{ref}gndspine"))
        vx = sum(gx for gx, _ in gnd) / len(gnd)
        out.append(via(vx, mid, "GND", f"{ref}gndvia"))

    # --- spare VDD pads: escape to the far side of their row, then east into the trunk ----
    v3 = nets.get("3V3", [])
    if v3:
        row_y = y_top if v3_side == "n" else y_bot
        spare = [(vx, vy) for vx, vy in v3 if abs(vy - row_y) < 1e-6]
        if spare:
            sign = -1.0 if v3_side == "n" else 1.0
            rail = row_y + sign * (half_h + clear + W_POWER / 2.0)
            for i, (vx, vy) in enumerate(spare):
                out.append(seg(vx, vy, vx, rail, "3V3", width=W_POWER,
                               tag=f"{ref}v3stub{i}"))
            out.append(seg(min(vx for vx, _ in spare), rail, v3_trunk, rail, "3V3",
                           width=W_POWER, tag=f"{ref}v3rail"))
            rails.append(rail)
    return out, rails


def zone_gnd(tag="gnd") -> str:
    """B.Cu ground pour, inset `ZONE_INSET` from the outline and FOLLOWING the chamfers.

    The pour is a normal net zone, so the filler keeps its own clearance from the B.Cu SPI
    lanes automatically -- they are on a different net.  The inset keeps copper away from the
    routed edge (min_copper_edge_clearance).

    The outline is a chamfered rectangle, so the pour is too.  A plain rectangle here (what
    this used to emit, while the docstring claimed otherwise) overhangs the four chamfers by
    up to the full chamfer depth: KiCad's filler clips it back so the fabbed board is fine,
    but the zone outline then crosses the board edge, which is a DRC copper-edge-clearance
    complaint and makes the plot look like copper is hanging off the corners.  Insetting a
    chamfered outline is not a scalar offset of the corner points -- the chamfer edge is at
    45 degrees, so its own offset is ZONE_INSET * sqrt(2) along each axis.
    """
    i = ZONE_INSET
    x0, y0 = ORIGIN_X + i, ORIGIN_Y + i
    x1, y1 = ORIGIN_X + BOARD_W - i, ORIGIN_Y + BOARD_H - i
    c = CHAMFER - i * (math.sqrt(2) - 1)
    pts = [
        (x0 + c, y0), (x1 - c, y0),          # top edge
        (x1, y0 + c), (x1, y1 - c),          # right edge
        (x1 - c, y1), (x0 + c, y1),          # bottom edge
        (x0, y1 - c), (x0, y0 + c),          # left edge
    ]
    xy = "\n".join(f"\t\t\t\t(xy {x:.4f} {y:.4f})" for x, y in pts)
    return (
        "\t(zone\n"
        '\t\t(net "GND")\n'
        '\t\t(layers "B.Cu")\n'
        f'\t\t(uuid "{uid("zone-" + tag)}")\n'
        '\t\t(name "GND plane")\n'
        "\t\t(hatch edge 0.5)\n"
        "\t\t(connect_pads\n\t\t\t(clearance 0.25)\n\t\t)\n"
        "\t\t(min_thickness 0.25)\n"
        "\t\t(filled_areas_thickness no)\n"
        "\t\t(fill yes\n"
        "\t\t\t(thermal_gap 0.35)\n"
        "\t\t\t(thermal_bridge_width 0.5)\n"
        "\t\t)\n"
        "\t\t(polygon\n\t\t\t(pts\n" + xy + "\n\t\t\t)\n\t\t)\n"
        "\t)"
    )


# --------------------------------------------------------------------------- the design


def build() -> str:
    items: list[str] = []

    # ---- board outline: rectangle with chamfered corners -------------------------
    L, T = ORIGIN_X, ORIGIN_Y
    R, B = ORIGIN_X + BOARD_W, ORIGIN_Y + BOARD_H
    c = CHAMFER
    edge = [
        ((L + c, T), (R - c, T)), ((R - c, T), (R, T + c)),
        ((R, T + c), (R, B - c)), ((R, B - c), (R - c, B)),
        ((R - c, B), (L + c, B)), ((L + c, B), (L, B - c)),
        ((L, B - c), (L, T + c)), ((L, T + c), (L + c, T)),
    ]
    for i, (a, b) in enumerate(edge):
        items.append(gr_line(a[0], a[1], b[0], b[1], tag=f"edge{i}"))

    # ---- Teensy 4.0 headers ------------------------------------------------------
    # Teensy 4.0 pinout, pin 1 at the top of each row.
    # Left row (J1): pins 0..13 ; right row (J2): GND,14..23,3V3,VIN... simplified to
    # the signals this carrier actually uses.
    left = {
        1: "T_D0", 2: "T_D1", 3: "T_D2", 4: "T_D3", 5: "T_D4", 6: "T_D5",
        7: "T_D6", 8: "T_D7", 9: "T_D8", 10: "SPI_CS", 11: "SPI_MOSI",
        12: "SPI_MISO", 13: "SPI_SCK", 14: "3V3",
    }
    right = {
        1: "GND", 2: "T_D14", 3: "T_D15", 4: "T_D16", 5: "T_D17",
        6: "SDA", 7: "SCL", 8: "T_D20", 9: "T_D21", 10: "LED_A",
        11: "T_D23", 12: "3V3", 13: "GND", 14: "VIN",
    }
    # A 1x14 Vertical header already runs pin1 -> pin14 along +y at rot 0, so these are
    # placed unrotated.  (Rotating by 270 would lay them along -x, overlapping.)
    items.append(footprint("hdr14", "J1", "Teensy4.0-L", TEENSY_X, TEENSY_Y,
                           pads=left, tag="J1"))
    items.append(footprint("hdr14", "J2", "Teensy4.0-R",
                           TEENSY_X + TEENSY_ROW_PITCH, TEENSY_Y,
                           pads=right, tag="J2"))

    # ---- I2C pullups to 3V3 ------------------------------------------------------
    # Pull-ups sit between the I2C trunks and the 3V3 trunk, rotated so pad 1 (signal) faces
    # left toward the bus and pad 2 (3V3) faces right toward the power trunk.
    # A pull-up bridges its signal trunk and the 3V3 trunk, so it is centred between them
    # and left unrotated: pad 1 (signal) faces left, pad 2 (3V3) faces right.  0805 pads sit
    # +/-0.9125 mm from centre, and the trunks are 2 mm apart, so the stubs are short.
    # A pull-up bridges its signal trunk (west) and the 3V3 trunk (east), so it is centred
    # between them with pad 1 (signal) facing left and pad 2 (3V3) facing right.  They sit
    # above the sensor block so their taps do not share a row with anything.
    pull_x = (CORRIDOR["SCL"] + CORRIDOR["3V3"]) / 2
    items.append(footprint("r0805", "R1", "4k7", pull_x, ORIGIN_Y + 6.0,
                           pads={"1": "SDA", "2": "3V3"}, tag="R1",
                           hide_value=False))
    items.append(footprint("r0805", "R2", "4k7", pull_x, ORIGIN_Y + 9.2,
                           pads={"1": "SCL", "2": "3V3"}, tag="R2",
                           hide_value=False))

    # ---- sensors -----------------------------------------------------------------
    # U2 IMU and U3 environmental sensor, both on the I2C bus, each with a 100nF.
    # Sensors sit to the right of the I2C trunks; their pad 1/3/5 side faces the bus.
    # Sensors sit right of the 3V3 trunk.  Their pads 1/3 (SCL/SDA side) reach back to the
    # bus trunks, pad 4/1 to 3V3, so the LGA is clear of all three corridors.
    # Sensors sit BETWEEN the I2C trunks (west) and the 3V3 trunk (east).
    imu_x = (CORRIDOR["SCL"] + CORRIDOR["3V3"]) / 2
    imu_y = ORIGIN_Y + 17.0
    env_x, env_y = imu_x, ORIGIN_Y + 25.0

    items.append(footprint("lga8", "U2", "LSM6DSO", imu_x, imu_y,
                           pads={"1": "SCL", "2": "GND", "3": "SDA", "4": "3V3",
                                 "5": "GND", "6": "3V3", "7": "GND", "8": "3V3"},
                           tag="U2", hide_value=False))
    # U3 is rotated 180 so its VDD pads end up on the EAST side (toward the power trunk) and
    # its SDA/SCL pads on the WEST (toward the bus trunks).  At rot 0 the BME280's pad 1
    # (VDD) sat 1.3 mm west of pad 3 (SDA) while needing to reach a trunk 6 mm east, so the
    # power and signal escapes had to cross in the gap between two pads -- no jog can fix
    # that, only turning the part around.  This is the layout-level version of the same
    # "power one side, signals the other" rule that fixed the trunk order.
    items.append(footprint("lga8", "U3", "BME280", env_x, env_y, rot=180,
                           pads={"1": "3V3", "2": "GND", "3": "SDA", "4": "GND",
                                 "5": "SCL", "6": "GND", "7": "3V3", "8": "GND"},
                           tag="U3", hide_value=False))

    # Decoupling sits directly ON the 3V3 trunk column, rotated 90 so pad 1 (3V3) points up
    # into the trunk and pad 2 (GND) points down to its own stitching via -- the shortest
    # possible loop, which is the point of a decoupling cap.
    # Rotated 90 (pads along y) and offset 1.6 mm EAST of the 3V3 trunk: sitting exactly on
    # the trunk means the GND pad is on it too, so the GND stub to its stitching via starts
    # on the power net.  The offset keeps the 3V3 tap short while giving GND a clean exit.
    cap_x = CORRIDOR["3V3"] + 1.6
    # -2.4, not -3.4: J3's SDA tap arrives at y=87.22 after the header moved to clear
    # H2's courtyard, which is 0.43 mm from C1's GND pad.  All three decoupling caps had to
    # move for the same reason -- their y comes from the SENSOR rows while the tap rows come
    # from the HEADERS, so any header shift walks the taps through the caps.
    items.append(footprint("c0805", "C1", "100nF", cap_x, imu_y - 2.4,
                           rot=90, pads={"1": "3V3", "2": "GND"}, tag="C1"))
    # -4.8, after two corrections.  -3.4 put C2's GND pad at the same y as J4's 3V3 tap
    # (0.01 mm); -4.1 moved it only to 0.69 mm where 0.9 mm is needed for a 1.0 mm pad.  Both
    # the cap position and the tap row derive from part rows, so they collide by construction
    # unless the offset is checked against the actual arrival row rather than eyeballed.
    items.append(footprint("c0805", "C2", "100nF", cap_x, env_y - 4.8,
                           rot=90, pads={"1": "3V3", "2": "GND"}, tag="C2"))
    # +34.0, not +33.0: once J4 moved down (to clear H2's courtyard via J3), its SCL tap
    # arrived at y=106.76, only 0.29 mm from C3's GND pad where ~0.9 mm is needed.  The GND
    # stub selector then correctly refused to place a stub at all -- the fault was C3's
    # position, not the stub direction, and the refusal is what surfaced it.
    items.append(footprint("c0805", "C3", "10uF", cap_x, ORIGIN_Y + 34.0,
                           rot=90, pads={"1": "3V3", "2": "GND"}, tag="C3",
                           hide_value=False))

    # ---- status LED --------------------------------------------------------------
    # LED and its series resistor stack vertically at the right edge, in line so LED_K is a
    # single short hop and only LED_A needs to cross the board.
    # D1 and R3 are stacked in one column with a 4 mm gap: LED_A arrives at D1's top pad,
    # LED_K is the short hop between them, and R3's bottom pad goes to a GND via.  The gap
    # has to exceed the two 0805 half-lengths plus clearance, or the LED_A arrival stub and
    # the LED_K hop overlap (they did at 3.2 mm).
    led_x = ORIGIN_X + BOARD_W - 6.5
    # +31.6: at +30.0 D1's LED_A pad left only a 0.062 mm solder-mask web to J4 pin 4,
    # which would bridge in reflow.  Swept rather than nudged (+31.0/+31.6/+32.2 all clear,
    # +29.0 and below get worse), and +31.6 sits mid-window rather than on its edge.
    items.append(footprint("led0805", "D1", "STATUS", led_x, ORIGIN_Y + 31.6, rot=90,
                           pads={"1": "LED_K", "2": "LED_A"}, tag="D1",
                           hide_value=False))
    # +35.8, not +35.0: this spacing is what makes D1's SILK_OUTSET viable.  D1's stock
    # outline sits exactly 0.20 mm from its own pad copper, and silk_over_copper wants MORE
    # than 0.20, so the outline must be outset -- but outsetting also walks it toward R3's
    # upper pad.  Swept both variables together: with R3 at +35.0 the feasible outset window
    # is 0.02-0.03 mm wide, which is not a margin worth shipping; at +35.8 it is 0.02-0.45+.
    # 0.8 mm of extra part spacing buys a 40x wider window and more assembly room.
    items.append(footprint("r0805", "R3", "1k", led_x, ORIGIN_Y + 35.8,
                           rot=90, pads={"1": "GND", "2": "LED_K"}, tag="R3",
                           hide_value=False))

    # ---- breakout headers --------------------------------------------------------
    # Two 4-pin I2C sensor headers (GND/3V3/SDA/SCL) and one 5-pin SPI header.
    # Vertical (rot 0): pins run down +y, so each of GND/3V3/SDA/SCL taps its trunk at a
    # DIFFERENT y.  Laying these headers horizontally put all four pads on one row, where
    # three taps to three trunks necessarily overlapped -- worth stating, since it is the
    # non-obvious half of the crossing bugs this board went through.
    # Pin 1 must clear the top-right M3 hole's COURTYARD, not just its pad.  Two corrections
    # got this wrong before: ORIGIN_Y + 5.0 put pin 1 0.1 mm inside H2's 3.2 mm pad, and
    # HOLE_INSET + pad radius + 1.0 still left the two courtyards overlapping, because the
    # relevant radius is H2's F.CrtYd circle (3.45 mm), not its pad (1.6 mm).
    #
    # H2's courtyard is a CIRCLE and J3's is a RECT, so the constraint is the circle-to-rect
    # distance, not a simple row offset: with the rect's near corner dx from the hole centre,
    # the rect's top edge must sit sqrt(r^2 - dx^2) below it.  (My earlier "0 courtyard
    # violations" measurement missed this entirely -- it only parsed fp_line courtyards, and
    # neither of these two footprints uses one.)
    _h2_r = 3.45                        # H2 F.CrtYd circle radius, from the stock footprint
    _j3_half_w = 1.77                   # J3 F.CrtYd rect half-width and top inset
    _dx = abs((ORIGIN_X + BOARD_W - HOLE_INSET) - (ORIGIN_X + BOARD_W - 6.5 + _j3_half_w))
    _hdr_top = (HOLE_INSET + math.sqrt(max(0.0, _h2_r ** 2 - _dx ** 2)) + _j3_half_w + 0.05)
    for i, (hx, hy, tag) in enumerate(
        [(ORIGIN_X + BOARD_W - 6.5, ORIGIN_Y + _hdr_top, "J3"),
         # 11.4 mm below J3, found by sweeping the gap rather than nudging it.  The
         # constraint is not monotonic -- 9.6, 10.2, 11.4, 13.6 and 14.8 all work
         # while 9.0, 10.8, 12.0, 12.6 trip assert_tap_rows and 14.2 boxes in C3's
         # GND stub -- because moving the header walks its four tap rows past the
         # sensors' tap rows and the caps simultaneously.  Nudging by 0.6 mm at a
         # time just lands in the next bad window.
         (ORIGIN_X + BOARD_W - 6.5, ORIGIN_Y + _hdr_top + 11.4, "J4")]
    ):
        items.append(footprint(
            "hdr4", tag, "I2C", hx, hy,
            pads={"1": "GND", "2": "3V3", "3": "SDA", "4": "SCL"}, tag=tag))

    # rot 90 lays the 5 pins along +x instead of +y (see the rotation note above).
    # Kept clear of the J2 header column, which ends at TEENSY_Y + TEENSY_LEN.
    items.append(footprint(
        # A 1xN Vertical header runs pin1 -> pinN along +y at rotation 0 -- the SAME trap as
        # J1/J2 above.  rot=90 does NOT lay the pins along +x here; it rotates the whole strip
        # about pin 1, which walked J5's pads off the bottom edge of the board and left the
        # SPI taps landing on empty copper.  Place it at rot=0 and let the pins run down.
        # +3.6, not +2.6: at +2.6 pin 1 sat 1.00 mm from the 3V3 B.Cu lane at y=121.80, and a
        # 1.7 mm header pad needs ~1.15 mm of centre-to-centre distance to clear a track.  The
        # fault was never the GND stub direction (which is where I first looked) -- it was the
        # 3V3 lane and the SPI_SCK riser being routed past a pad neither of them knew about.
        "hdr5", "J5", "SPI", ORIGIN_X + 22.0, LANE["SPI_CS"] + 3.6,
        pads={"1": "GND", "2": "SPI_SCK", "3": "SPI_MOSI", "4": "SPI_MISO",
              "5": "SPI_CS"}, tag="J5"))

    # ---- mounting holes ----------------------------------------------------------
    for i, (mx, my) in enumerate([
        (L + HOLE_INSET, T + HOLE_INSET), (R - HOLE_INSET, T + HOLE_INSET),
        (L + HOLE_INSET, B - HOLE_INSET), (R - HOLE_INSET, B - HOLE_INSET),
    ]):
        items.append(footprint("mount", f"H{i + 1}", "M3", mx, my, tag=f"H{i + 1}"))

    # ---- routing -----------------------------------------------------------------
    # I2C bus: Teensy pins 18/19 (J2 pads 6/7) out to the pullups and both headers.
    # Pad centres of a vertical 1x14 rotated 270 deg: x fixed, y = pin1 + (n-1)*pitch.
    j2x = TEENSY_X + TEENSY_ROW_PITCH
    sda_y = TEENSY_Y + 5 * TEENSY_PIN_PITCH
    scl_y = TEENSY_Y + 6 * TEENSY_PIN_PITCH

    # Routing is expressed pad-to-pad via route(), which looks up real pad centres in PADS
    # and emits an orthogonal (Manhattan) path.  Nothing here is a guessed coordinate, so a
    # track always terminates exactly on a pad and the design survives moving a part.

    # --- I2C bus: one dedicated vertical trunk per signal ------------------------------
    # A multi-drop bus routed as a chain of independent hops makes each hop choose its own
    # corridor, and the lanes then cross.  Instead each signal gets ONE trunk x, every stub
    # leaves its pad horizontally and joins that trunk, and the two trunks are 1.4 mm apart.
    sda_trunk = CORRIDOR["SDA"]
    scl_trunk = CORRIDOR["SCL"]

    tap_rows: list[tuple[str, str, float]] = []

    for net, trunk, taps in [
        # SCL FIRST, deliberately.  Dive-under vias avoid tracks already in ROUTED, so whichever
        # net routes first cannot be avoided by the other.  SCL's risers are the longer ones
        # (its trunk is further from the parts), so laying them down first gives SDA's shorter
        # dive vias something concrete to route around -- the reverse order left SDA's via
        # 0.436 mm from an SCL riser that did not exist yet when the via was placed.
        ("SCL", scl_trunk, ["J2.7", "R2.1", "U2.1", "U3.5", "J3.4", "J4.4"]),
        ("SDA", sda_trunk, ["J2.6", "R1.1", "U2.3", "U3.3", "J3.3", "J4.3"]),
    ]:
        ys = []
        for t in taps:
            # Jog off any pad row shared with another net's tap.  JOG maps "REF.PAD" -> the
            # y offset that moves this tap clear of its neighbours.
            j = JOG.get(t, 0.0)
            ys.append(pad_xy(*t.split("."))[1] + j)
            tap_rows.append((net, t, pad_xy(*t.split("."))[1] + j))
            items += tap(t, net, jog=j)

        items.append(seg(trunk, min(ys), trunk, max(ys), net, width=W_SIGNAL,
                         tag=f"{net}_trunk"))

    assert_tap_rows(tap_rows)

    # --- 3V3: J1 pin 14 along the top edge, then down to each consumer ------------------
    # 3V3 gets its own trunk to the RIGHT of the I2C pair, wider (Power netclass), fed from
    # J1 pin 14 across the top edge where nothing else runs.
    # The 3V3 feed comes off J1 pin 14 at the BOTTOM of the left header and has to reach the
    # top-right trunk.  Routing it across the top edge on F.Cu clipped the two upper M3
    # holes, so it crosses on B.Cu in its own lane below the bottom holes, then rises up the
    # right-hand side clear of everything.
    v3_trunk = CORRIDOR["3V3"]
    j1x, j1y = pad_xy("J1", "14")
    v3_lane = LANE["3V3"]

    # The Teensy exposes 3V3 on BOTH headers (J1.14 and J2.12).  Feeding only one leaves the
    # other pad floating, which is electrically harmless but shows up as an unrouted net and
    # wastes the second pin's current capacity, so tie J2.12 into the trunk as well.  It sits
    # at y=108.4 with a clear run east, below every sensor tap, so a plain straight segment
    # does it -- no lane needed.
    j2_v3 = pad_xy("J2", "12")
    items.append(seg(j2_v3[0], j2_v3[1], v3_trunk, j2_v3[1], "3V3", width=0.4,
                     tag="j2_3v3_tie"))

    # 3V3 uses its allocated drop column and lane like every other crossing, rising into the
    # 3V3 trunk rather than into a pad.
    items += bcu_cross("J1.14", "R1.2", "3V3", DROP["3V3"], v3_lane, width=0.4,
                       rise_x=v3_trunk)

    # U3's 3V3 comes off pad 1.  Pads 1 and 7 are both VDD on a BME280, and the choice is a
    # real layout decision: whichever is used, its tap heads EAST to the power trunk, so it
    # must not share a row with a signal tap heading WEST.  Pad 7 shares the bottom row with
    # SCL (pad 5) and pad 1 shares the top row with SDA (pad 3) -- so the row conflict is
    # unavoidable by pad choice alone and is resolved by the escape offsets in JOG instead.
    v3_taps = ["R1.2", "R2.2", "C1.1", "U2.4", "C2.1", "U3.1", "J3.2", "J4.2", "C3.1"]
    # Seeded with the LANE y only.  It used to also include `v3_top` (ORIGIN_Y + 3.2), a
    # leftover from the abandoned "route 3V3 across the top edge" plan described above: with
    # the feed moved to a B.Cu lane, nothing 3V3 exists that far north, so the trunk ran
    # 2.8 mm past its topmost tap and DRC flagged the overshoot as track_dangling. The trunk
    # must span exactly its taps and nothing more.
    ys = [v3_lane]
    for t in v3_taps:
        j = JOG.get(t, 0.0)
        ys.append(pad_xy(*t.split("."))[1] + j)
        items += tap(t, "3V3", width=0.4, jog=j)

    # Bond the sensors' spare VDD/GND pads BEFORE the trunk span is computed, so the VDD
    # rails they add are included in min/max rather than hanging off the end of it.
    #   U2's spare VDD pads are on its SOUTH row, U3's on its NORTH row (U3 is rotated 180),
    #   so each escapes away from the other part rather than into the gap between them.
    for _ref, _side in (("U2", "s"), ("U3", "n")):
        _bonded, _rails = bond_power_pads(_ref, v3_side=_side, v3_trunk=v3_trunk)
        items += _bonded
        ys += _rails

    items.append(seg(v3_trunk, min(ys), v3_trunk, max(ys), "3V3", width=0.4,
                     tag="v3_trunk"))

    # --- status LED: spare GPIO -> LED -> series R -> GND ------------------------------
    # LED_A leaves J2 pin 10 and crosses to the right edge on B.Cu, below the sensor block,
    # so it does not fight the I2C/3V3 trunks on F.Cu.
    # LED_A starts on the RIGHT side of the J2 column, so its drop column is on that side
    # (the shared DROP plan is for nets leaving J1 westward) but it still uses its allocated
    # lane, which is what keeps it off the four SPI lanes.
    # Rise one column EAST of the D1/R3 stack: rising in the stack's own column made the
    # arrival stub run straight through D1 and over the LED_K hop.
    items += bcu_cross("J2.10", "D1.2", "LED_A", DROP["LED_A"], LANE["LED_A"],
                       rise_x=pad_xy("D1", "2")[0] + 2.4)

    # D1 and R3 are stacked in the same column, so LED_K is one straight F.Cu hop.
    items += route("D1.1", "R3.2", "LED_K", via_x=pad_xy("D1", "1")[0], tag="led_k")

    # --- SPI: Teensy -> header, routed on B.Cu ----------------------------------------
    # Four parallel nets cannot share the crowded F.Cu channel between the header rows, so
    # the SPI bundle drops to the back layer through vias and runs in its own lane set.
    # Each net gets an explicitly distinct corridor (1.4 mm apart -- comfortably above the
    # 0.2 mm clearance) rather than a computed offset, which is easy to get subtly wrong.
    # Lanes sit between the bottom of the header columns and the SPI header, and each net
    # drops at its OWN x so the four vertical runs never share a column.  The lane band is
    # kept above the bottom mounting holes (B - HOLE_INSET) -- a B.Cu lane run through an
    # NPTH pad is a hole_clearance error, which is how the first attempt failed.
    # Each net rises in its OWN column WEST of the J5 pin line and then hops sideways into its
    # pad.  Rising in the pad's own column would run the track straight down through J5's other
    # four pins -- a vertical 1x5 header is a wall of holes, so the last approach must be
    # horizontal.  The columns are 1.1 mm apart, ordered so the pin nearest the band gets the
    # column nearest the header and the runs never cross.
    j5_x = pad_xy("J5", "1")[0]
    for i, (src, dst, net) in enumerate([
            ("J1.13", "J5.2", "SPI_SCK"), ("J1.11", "J5.3", "SPI_MOSI"),
            ("J1.12", "J5.4", "SPI_MISO"), ("J1.10", "J5.5", "SPI_CS")]):
        items += bcu_cross(src, dst, net, DROP[net], LANE[net],
                           # 1.3 mm per column, not 1.1: the first riser at 1.1 mm sat 0.25 mm
                           # inside J5 pin 1's 1.7 mm pad.  A 0.1 inch header pad needs
                           # 0.85 + 0.2 + 0.125 = 1.175 mm from its centre, so 1.1 was never
                           # enough for the FIRST column -- the others were only safe by luck.
                           rise_x=j5_x - 1.3 * (i + 1))

    # --- GND: header pins and the R3 return stitched down to the B.Cu pour -------------
    # Each GND pad gets a short stub to its own stitching via.  The stub direction is chosen
    # per pad: stubbing blindly to +x ran C3's return into the 3V3 trunk and R3's into the
    # LED column.
    for i, (ref, pad, dx, dy) in enumerate([
        ("J2", "1", 1.6, 0.0),
        ("J2", "13", 1.6, 0.0),
        # NOTE: there is deliberately no J1 entry.  The Teensy 4.0 has no ground pin on its
        # left row at all (J1 is T_D0..T_D8, SPI, 3V3) -- ground comes out on J2 pins 1 and 13,
        # both stubbed above.  An earlier revision stubbed "J1 pin 1" as if it were ground,
        # which put a GND track on top of the T_D0 signal pin.  The geometric check reported
        # it only as "a track sits on a hole of another net"; the real fault was electrical.
        ("R3", "1", 0.0, 1.6),      # downward, away from the LED_K hop above
        # EAST, and this took two tries.  +y ran the stub straight at pin 2 (2.54 mm away),
        # putting the stitching via 0.94 mm from that pin: a short plus hole-clearance plus
        # hole-to-hole from one wrong sign.  West is worse -- that is exactly where the four
        # SPI risers climb (x = 117.6 .. 120.9), so a westward stub crosses all of them.
        # East of the pin line is the only open copper, so that is where it goes.
        ("J5", "1", 1.6, 0.0),
        ("C3", "2", 1.6, 0.0),      # sideways: C3 sits ON the 3V3 trunk, so a downward
                                    # stub ran along it for 1.6 mm
        ("C1", "2", 1.6, 0.0),
        ("C2", "2", 1.6, 0.0),
    ]):
        px, py = pad_xy(ref, pad)

        # The (dx, dy) above is a PREFERENCE, not a decision.  Every one of these directions
        # was hand-picked, and three of them turned out to collide with something placed later
        # (J5 into pin 2, C3 along the 3V3 trunk, R3 into the LED column) -- hand-picking is
        # what this file keeps getting punished for.  So try the preferred direction, then the
        # three others, and keep the first whose stub AND via clear every existing pad and via.
        # Four axes are not always enough: once J3/J4 moved to clear H2's courtyard, C3 was
        # boxed in on all four sides and the generator (correctly) refused.  Longer reaches and
        # diagonals give it room without hand-placing anything.
        cands = [(dx, dy)] + [d for d in (
            (1.6, 0.0), (-1.6, 0.0), (0.0, 1.6), (0.0, -1.6),
            (2.2, 0.0), (-2.2, 0.0), (0.0, 2.2), (0.0, -2.2),
            (1.4, 1.4), (-1.4, 1.4), (1.4, -1.4), (-1.4, -1.4),
        ) if d != (dx, dy)]
        chosen = None
        for cdx, cdy in cands:
            vx, vy = px + cdx, py + cdy
            if _gnd_stub_clear(px, py, vx, vy, ref, pad):
                chosen = (cdx, cdy)
                break

        if chosen is None:
            raise SystemExit(f"no clear GND stub direction for {ref}.{pad}: all four "
                             f"of +x/-x/+y/-y collide with existing copper")

        cdx, cdy = chosen
        items += [
            seg(px, py, px + cdx, py + cdy, "GND", width=W_GND, tag=f"gnd_stub{i}"),
            via(px + cdx, py + cdy, "GND", f"gnd{i}"),
        ]

    # ---- silkscreen --------------------------------------------------------------
    # The title starts EAST of H1's mask aperture, not at a round L + 3.0.  L + 3.0 put the
    # first glyphs straight over H1's NPTH pad (a 3.2 mm hole has a 1.6 mm pad, and the mask
    # opening is 0.05 mm wider still), which DRC reports as silk_over_copper: the mask
    # aperture clips the silkscreen, so the text is physically missing on the finished board.
    # It cannot move north instead -- the board edge is 1.6 mm above the text baseline.
    _title_x = ORIGIN_X + HOLE_INSET + (HOLE_PAD / 2 + MASK_EXPANSION) + SILK_CLEAR_MM
    items += [
        gr_text("PhiCad Teensy 4.0 Sensor Suite", _title_x, T + 1.6, size=1.2,
                tag="title"),
        gr_text("I2C", ORIGIN_X + 41.0, ORIGIN_Y + 3.0, size=0.9, tag="li2c1"),
        gr_text("SPI", ORIGIN_X + 26.0, ORIGIN_Y + 40.4, size=0.9, tag="lspi"),
        gr_text("rev A", R - 9.0, B - 1.4, size=0.9, tag="lrev"),
    ]

    items.append(zone_gnd())

    # ---- assemble ----------------------------------------------------------------
    head = f"""(kicad_pcb
\t(version {BOARD_VERSION})
\t(generator "{GENERATOR}")
\t(generator_version "10.99")
\t(general
\t\t(thickness 1.6)
\t\t(legacy_teardrops no)
\t)
\t(paper "A4")
\t(title_block
\t\t(title "Teensy 4.0 Prototyping Sensor Suite")
\t\t(rev "A")
\t\t(company "Example Co")
\t\t(comment 1 "PhiCad demo board -- generated by generate_board.py")
\t)
\t(layers
\t\t(0 "F.Cu" signal)
\t\t(2 "B.Cu" signal)
\t\t(9 "F.Adhes" user)
\t\t(11 "B.Adhes" user)
\t\t(13 "F.Paste" user)
\t\t(15 "B.Paste" user)
\t\t(5 "F.SilkS" user)
\t\t(7 "B.SilkS" user)
\t\t(1 "F.Mask" user)
\t\t(3 "B.Mask" user)
\t\t(17 "Dwgs.User" user)
\t\t(19 "Cmts.User" user)
\t\t(21 "Eco1.User" user)
\t\t(23 "Eco2.User" user)
\t\t(25 "Edge.Cuts" user)
\t\t(27 "Margin" user)
\t\t(31 "F.CrtYd" user)
\t\t(29 "B.CrtYd" user)
\t\t(35 "F.Fab" user)
\t\t(33 "B.Fab" user)
\t)
\t(setup
\t\t(pad_to_mask_clearance 0.05)
\t\t(allow_soldermask_bridges_in_footprints no)
\t)"""

    return head + "\n" + "\n".join(items) + "\n)\n"


# A production-intent project file.  Two things matter here beyond the geometry:
#
# * `rules` are a real fab's capability envelope (JLCPCB/PCBWay 2-layer standard process:
#   0.15 mm track/space, 0.3 mm drill, 0.2 mm annular ring), so DRC measures the board
#   against something manufacturable instead of KiCad's permissive defaults.
# * `rule_severities` downgrades the checks that are advisory for THIS board rather than
#   silencing real problems.  Each one is justified in the README; nothing that affects
#   electrical correctness or manufacturability is touched.
#
#   Note what is deliberately NOT in that list: `solder_mask_bridge` keeps KiCad's default
#   error severity.  It was downgraded here while the LGA-8 pads were reported as bridging,
#   which was exactly the wrong response -- a merged mask aperture between two different nets
#   is a reflow-shorting risk, not a cosmetic complaint.  check_routing.py now computes the
#   mask web for every different-net pad pair (pad gap minus twice pad_to_mask_clearance) and
#   the tightest on this board is +0.200 mm against a 0.100 mm limit, so there is nothing left
#   to suppress.  Measure it, then delete the suppression -- do not silence a class of error
#   on the assumption it is cosmetic.
PROJECT = """{
  "board": {
    "design_settings": {
      "defaults": {
        "board_outline_line_width": 0.1,
        "copper_line_width": 0.25,
        "silk_line_width": 0.15,
        "silk_text_size_h": 1.0,
        "silk_text_size_v": 1.0,
        "silk_text_thickness": 0.15,
        "other_line_width": 0.15
      },
      "diff_pair_dimensions": [],
      "drc_exclusions": [],
      "rules": {
        "max_error": 0.005,
        "min_clearance": 0.15,
        "min_connection": 0.15,
        "min_copper_edge_clearance": 0.3,
        "min_hole_clearance": 0.25,
        "min_hole_to_hole": 0.25,
        "min_microvia_diameter": 0.2,
        "min_microvia_drill": 0.1,
        "min_silk_clearance": 0.0,
        "min_text_height": 0.8,
        "min_text_thickness": 0.08,
        "min_through_hole_diameter": 0.3,
        "min_track_width": 0.15,
        "min_via_annular_width": 0.13,
        "min_via_diameter": 0.45,
        "solder_mask_to_copper_clearance": 0.0,
        "use_height_for_length_calcs": true
      },
      "rule_severities": {
        "copper_sliver": "warning",
        "footprint_type_mismatch": "ignore",
        "lib_footprint_issues": "warning",
        "lib_footprint_mismatch": "ignore"
      },
      "track_widths": [ 0.0, 0.25, 0.4, 0.8 ],
      "via_dimensions": [
        { "diameter": 0.0, "drill": 0.0 },
        { "diameter": 0.6, "drill": 0.3 },
        { "diameter": 0.8, "drill": 0.4 }
      ],
      "zones_allow_external_fillets": false
    }
  },
  "meta": {
    "filename": "teensy-sensor-suite.kicad_pro",
    "version": 3
  },
  "net_settings": {
    "classes": [
      {
        "bus_width": 0.25,
        "clearance": 0.2,
        "diff_pair_gap": 0.25,
        "diff_pair_width": 0.2,
        "line_style": 0,
        "microvia_diameter": 0.3,
        "microvia_drill": 0.1,
        "name": "Default",
        "pcb_color": "rgba(0, 0, 0, 0.000)",
        "priority": 2147483647,
        "schematic_color": "rgba(0, 0, 0, 0.000)",
        "track_width": 0.25,
        "via_diameter": 0.8,
        "via_drill": 0.4,
        "wire_width": 6.0
      },
      {
        "clearance": 0.25,
        "name": "Power",
        "priority": 1,
        "track_width": 0.5,
        "via_diameter": 0.8,
        "via_drill": 0.4
      }
    ],
    "meta": { "version": 4 },
    "net_class_patterns": [
      { "netclass": "Power", "pattern": "3V3" },
      { "netclass": "Power", "pattern": "GND" },
      { "netclass": "Power", "pattern": "VIN" }
    ]
  }
}
"""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--output", "-o", default=None,
                    help="directory to write into (default: this script's dir)")
    ap.add_argument("--name", default="teensy-sensor-suite")
    args = ap.parse_args()

    out = Path(args.output) if args.output else Path(__file__).resolve().parent
    out.mkdir(parents=True, exist_ok=True)

    pcb = out / f"{args.name}.kicad_pcb"
    pro = out / f"{args.name}.kicad_pro"

    pcb.write_text(build())
    pro.write_text(PROJECT.replace("teensy-sensor-suite", args.name))

    print(f"wrote {pcb}  ({pcb.stat().st_size} bytes)")
    print(f"wrote {pro}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
