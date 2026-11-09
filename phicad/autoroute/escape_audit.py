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
"""Did every ball of a fine-pitch part actually get out?  Measured off the copper.

`bga_escape.py` (a private board project) answers the question *before* a routing run, from
pitch and channel arithmetic.  This answers it *after* one, from the board file: for
every netted ball, is there same-net copper touching the pad, and what kind.

Why the distinction matters
---------------------------
"Unconnected items" is the number everyone quotes, and it cannot answer this
question.  A net with 4 pads and one open link reports one unconnected item no
matter *which* pad is stranded, and a ball that escaped perfectly well still
appears in that item when the far end of its net is what is missing.  Escape and
net completion are different properties, and a router that escapes the ball field
but cannot finish a differential pair looks identical, in the unconnected count, to
one that never got out of the package.  Conflating them is how "the router cannot
escape a fine-pitch BGA" survived past the point where it was true.

So this reports both, separately:

  * **escaped**   -- same-net copper (a track, or a fan-out via) touches the pad.
                     This is the escape claim, and it is purely geometric.
  * **net_open**  -- DRC lists this pad in an unconnected item (needs --drc-report).
                     This is the routing-completion claim.

A ball can be escaped and net_open at once; that combination is a routing
remainder somewhere else on the net, NOT an escape failure, and it is the case
worth naming because it is the one that gets misread.

Ring depth is reported with each ball because it is what distinguishes an escape
problem from a capacity problem: channel exhaustion strands the *innermost* rings
first, so open balls spread across rings 0-3 while rings 4+ are clean is not
channel exhaustion.

Usage
-----
    python3 escape_audit.py board.kicad_pcb --ref U1
    python3 escape_audit.py board.kicad_pcb --ref U1 --drc-report drc.json --json

    # gate a board against a committed measurement (exit 1 on drift)
    python3 escape_audit.py board.kicad_pcb --ref U1 --drc-report drc.json \\
        --expect measurements/bga-escape-r1-2026-09-10.json

The DRC report is the JSON from `kicad-cli pcb drc --format json --severity-all
--refill-zones`.  **The refill is not optional**: a via whose only job is to drop a
ball onto an inner plane is connected *by the fill*, so against unfilled zones every
plane stitch reads as dangling and the unconnected count is fiction (measured on
r1: 258 unfilled against 216 filled, on the same board).

Stdlib only -- no venv, no kicad-cli, no IPC session.  Reads the board directly, so
it works on a checkout with no build tree.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "pylib"))
import sexp  # noqa: E402

#: Nets whose balls reach their copper straight down through a via rather than
#: laterally.  Matched by prefix, so `GND` covers `GND`, and `VDD_` covers every
#: `VDD_*` rail without listing them.  Overridable with --power-prefix.
DEFAULT_POWER_PREFIXES = ("GND", "3V3", "VDD_", "NVCC_", "DCDC_IN", "USB_5V", "VUSB")

#: `Pad <name> [<net>] of <refdes>` -- how KiCad's JSON report names a pad.
DRC_PAD_RE = re.compile(r"Pad (\S+) \[([^\]]*)\] of (\S+)")

#: Shapes closer than this are treated as touching.  Board coordinates carry more
#: precision than a router's own rounding, so an exact-contact test would call a
#: track that visibly lands on a pad "not touching".
TOUCH_TOL = 0.002


def load_board(path: Path):
    return sexp.parse_file(path)


def footprint_origin(fp) -> tuple[float, float, float]:
    """(x, y, degrees) for a footprint, in both spellings of the same thing.

    A board written by this fork places a footprint with
    `(transform (translate X Y) (rotate A))`; one written by pcbnew uses
    `(at X Y A)`.  A board that has been through kicad-cli comes back in the first
    form even if it went in as the second, so anything reading a routed board has
    to accept both.
    """
    transform = sexp.child(fp, "transform")
    if transform is not None:
        translate = sexp.floats(transform, "translate") or [0.0, 0.0]
        rotate = sexp.floats(transform, "rotate") or [0.0]
        return translate[0], translate[1], rotate[0]
    at = sexp.floats(fp, "at") or [0.0, 0.0]
    return at[0], at[1], (at[2] if len(at) > 2 else 0.0)


def find_footprint(doc, ref: str):
    for fp in sexp.children(doc, "footprint"):
        if sexp.fp_property(fp, "Reference") == ref:
            return fp
    raise SystemExit(f"no footprint with reference {ref}")


def read_pads(fp) -> list[dict]:
    """Absolute pad centres, sizes and nets for one footprint.

    Each pad is read from its OWN node.  A no-connect ball carries no `(net ...)`
    at all -- on a 196-ball part there can be more of those than netted ones -- and
    any scheme that searches forward from the pad name instead finds the *next*
    pad's net, which reads as an ordinary GND ball and gets counted as one.
    """
    ox, oy, deg = footprint_origin(fp)
    rot = math.radians(deg)
    pads = []
    for pad in sexp.children(fp, "pad"):
        names = sexp.atoms(pad)
        if not names:
            continue
        at = sexp.floats(pad, "at")
        size = sexp.floats(pad, "size") or [0.0, 0.0]
        if not at or len(at) < 2:
            continue
        px, py = at[0], at[1]
        # A pad's (at ...) is local and unrotated; the footprint's rotation applies
        # on top of it.  KiCad's y axis points down, so a positive rotation turns
        # clockwise on screen and the sign here follows that.
        pads.append({
            "pad": str(names[0]),
            "net": str(sexp.net_of(pad)) if sexp.net_of(pad) is not None else None,
            "x": ox + px * math.cos(rot) - py * math.sin(rot),
            "y": oy + px * math.sin(rot) + py * math.cos(rot),
            "half": max(size[0], size[1]) / 2.0 if size else 0.0,
        })
    return pads


def axis_index(values: list[float], value: float, tol: float) -> int:
    for i, v in enumerate(values):
        if abs(v - value) <= tol:
            return i
    raise KeyError(value)


def cluster(values: list[float], tol: float) -> list[float]:
    """Distinct coordinates, merging anything within `tol` into one row/column."""
    out: list[float] = []
    for v in sorted(values):
        if not out or abs(v - out[-1]) > tol:
            out.append(v)
    return out


def grid_of(pads: list[dict]) -> dict:
    """Row/column axes and the pitch of a grid-array part."""
    tol = 0.05
    cols = cluster([p["x"] for p in pads], tol)
    rows = cluster([p["y"] for p in pads], tol)
    gaps = [round(b - a, 3) for a, b in zip(cols, cols[1:])] + \
           [round(b - a, 3) for a, b in zip(rows, rows[1:])]
    pitch = min(gaps) if gaps else 0.0
    return {"cols": cols, "rows": rows, "pitch": pitch}


def ring_of(pad: dict, grid: dict) -> int:
    """0 for the outermost row/column of the array, growing inwards.

    Escape channels are consumed from the inside out -- a ball in ring r crosses
    every ring outside it -- so the ring number is what tells a capacity failure
    (deepest rings strand first) from something else.
    """
    ci = axis_index(grid["cols"], pad["x"], 0.05)
    ri = axis_index(grid["rows"], pad["y"], 0.05)
    return min(ci, len(grid["cols"]) - 1 - ci, ri, len(grid["rows"]) - 1 - ri)


def read_copper(doc) -> tuple[list[dict], list[dict]]:
    """Track segments/arcs and vias, with their nets, from a board document."""
    tracks, vias = [], []
    for item in doc:
        head = sexp.head(item)
        if head in ("segment", "arc"):
            start, end = sexp.floats(item, "start"), sexp.floats(item, "end")
            width = sexp.floats(item, "width") or [0.0]
            net = sexp.net_of(item)
            if not start or not end or net is None:
                continue
            tracks.append({"x1": start[0], "y1": start[1], "x2": end[0], "y2": end[1],
                           "net": str(net), "width": width[0],
                           "layer": sexp.scalar(item, "layer")})
        elif head == "via":
            at = sexp.floats(item, "at")
            size = sexp.floats(item, "size") or [0.0]
            net = sexp.net_of(item)
            if not at or net is None:
                continue
            vias.append({"x": at[0], "y": at[1], "net": str(net), "size": size[0]})
    return tracks, vias


def point_to_segment(px: float, py: float, track: dict) -> float:
    """Distance from a point to a track's finite centreline."""
    dx, dy = track["x2"] - track["x1"], track["y2"] - track["y1"]
    length_sq = dx * dx + dy * dy
    if length_sq < 1.0e-18:
        return math.hypot(px - track["x1"], py - track["y1"])
    t = ((px - track["x1"]) * dx + (py - track["y1"]) * dy) / length_sq
    t = min(1.0, max(0.0, t))
    return math.hypot(px - (track["x1"] + t * dx), py - (track["y1"] + t * dy))


def escape_of(pad: dict, tracks: list[dict], vias: list[dict]) -> tuple[bool, str]:
    """Is there same-net copper touching this pad, and of what kind.

    A same-net track that merely crosses the pad connects it just as much as one
    that terminates on it -- KiCad's connectivity is by overlap, not by endpoint --
    so the test is shape contact against the centreline, not endpoint proximity.
    A via is contact between two discs.
    """
    for via in vias:
        if via["net"] != pad["net"]:
            continue
        if math.hypot(via["x"] - pad["x"], via["y"] - pad["y"]) <= \
                pad["half"] + via["size"] / 2.0 + TOUCH_TOL:
            return True, "via"
    for track in tracks:
        if track["net"] != pad["net"]:
            continue
        if point_to_segment(pad["x"], pad["y"], track) <= \
                pad["half"] + track["width"] / 2.0 + TOUCH_TOL:
            return True, "track"
    return False, "none"


def open_pads(drc: dict, ref: str) -> set[str]:
    """Pads of `ref` that DRC lists inside an unconnected item."""
    found = set()
    for item in drc.get("unconnected_items", []):
        for sub in item.get("items", []):
            m = DRC_PAD_RE.match(sub.get("description", ""))
            if m and m.group(3) == ref:
                found.add(m.group(1))
    return found


def audit(board: Path, ref: str, drc: dict | None,
          power_prefixes: tuple[str, ...]) -> dict:
    doc = load_board(board)
    fp = find_footprint(doc, ref)
    pads = read_pads(fp)
    if not pads:
        raise SystemExit(f"{ref} has no pads")
    grid = grid_of(pads)
    tracks, vias = read_copper(doc)
    opened = open_pads(drc, ref) if drc else set()

    balls = []
    for pad in sorted(pads, key=lambda p: p["pad"]):
        escaped, kind = (False, "no-connect")
        if pad["net"]:
            escaped, kind = escape_of(pad, tracks, vias)
        balls.append({
            "pad": pad["pad"],
            "net": pad["net"],
            "ring": ring_of(pad, grid),
            "role": None if not pad["net"] else
                    ("power" if pad["net"].startswith(power_prefixes) else "signal"),
            "escaped": escaped if pad["net"] else None,
            "escape": kind,
            "net_open": (pad["pad"] in opened) if drc else None,
        })

    netted = [b for b in balls if b["net"]]
    signal = [b for b in netted if b["role"] == "signal"]
    power = [b for b in netted if b["role"] == "power"]

    def split(group):
        done = [b for b in group if b["escaped"]]
        return {"total": len(group), "escaped": len(done),
                "stranded": sorted(b["pad"] for b in group if not b["escaped"])}

    result = {
        "ref": ref,
        "package": {
            "balls": len(balls),
            "grid": f"{len(grid['cols'])}x{len(grid['rows'])}",
            "pitch_mm": grid["pitch"],
            "netted": len(netted),
            "no_connect": len(balls) - len(netted),
        },
        "escape": {
            "all": split(netted),
            "signal": split(signal),
            "power": split(power),
        },
        "stranded_detail": [
            {k: b[k] for k in ("pad", "net", "ring", "role")}
            for b in netted if not b["escaped"]
        ],
        "copper": {"segments": len(tracks), "vias": len(vias)},
    }
    if drc is not None:
        escaped_but_open = [b for b in netted if b["escaped"] and b["net_open"]]
        result["net_completion"] = {
            "unconnected_items_board_wide": len(drc.get("unconnected_items", [])),
            "violations_board_wide": len(drc.get("violations", [])),
            "errors_board_wide": len([v for v in drc.get("violations", [])
                                      if v.get("severity") == "error"]),
            "pads_in_open_items": sorted(open_pads(drc, ref)),
            "escaped_but_net_open": [
                {k: b[k] for k in ("pad", "net", "ring", "role")}
                for b in escaped_but_open
            ],
        }
    result["balls"] = balls
    return result


def compare(measured: dict, expected: dict) -> list[str]:
    """Drift between a fresh audit and a committed measurement.

    Only the claims are compared, not the provenance: a different machine, jar or
    day is expected, a different escape result is not.  `stranded` lists compare as
    sets of pad names, so a reordering is not a failure but a swapped ball is.
    """
    drift = []
    want = expected.get("expect", expected)

    def check(path: str, got, exp):
        if got != exp:
            drift.append(f"{path}: measured {got!r}, expected {exp!r}")

    for key in ("balls", "netted", "no_connect", "grid", "pitch_mm"):
        if key in want.get("package", {}):
            check(f"package.{key}", measured["package"].get(key), want["package"][key])
    for group in ("all", "signal", "power"):
        if group not in want.get("escape", {}):
            continue
        exp, got = want["escape"][group], measured["escape"][group]
        check(f"escape.{group}.total", got["total"], exp["total"])
        check(f"escape.{group}.escaped", got["escaped"], exp["escaped"])
        if "stranded" in exp:
            check(f"escape.{group}.stranded", sorted(got["stranded"]),
                  sorted(exp["stranded"]))
    if "net_completion" in want and "net_completion" in measured:
        for key in ("unconnected_items_board_wide", "errors_board_wide"):
            if key in want["net_completion"]:
                check(f"net_completion.{key}", measured["net_completion"].get(key),
                      want["net_completion"][key])
        if "pads_in_open_items" in want["net_completion"]:
            check("net_completion.pads_in_open_items",
                  sorted(measured["net_completion"]["pads_in_open_items"]),
                  sorted(want["net_completion"]["pads_in_open_items"]))
    return drift


def report(result: dict) -> None:
    pkg, esc = result["package"], result["escape"]
    print(f"{result['ref']}: {pkg['balls']} balls, {pkg['grid']} on {pkg['pitch_mm']} mm "
          f"pitch, {pkg['netted']} netted, {pkg['no_connect']} no-connect")
    print(f"  copper on the board: {result['copper']['segments']} segments, "
          f"{result['copper']['vias']} vias")
    for name in ("all", "signal", "power"):
        group = esc[name]
        if not group["total"]:
            continue
        print(f"  {name:<6} escaped {group['escaped']}/{group['total']}"
              + (f"   stranded: {', '.join(group['stranded'])}" if group["stranded"] else ""))
    if result["stranded_detail"]:
        print("  stranded balls (no same-net copper touching the pad):")
        for ball in result["stranded_detail"]:
            print(f"    {ball['pad']:>4}  {ball['net']:<16} ring {ball['ring']}  {ball['role']}")
    completion = result.get("net_completion")
    if completion:
        print(f"  DRC (board-wide): {completion['unconnected_items_board_wide']} unconnected, "
              f"{completion['violations_board_wide']} violations, "
              f"{completion['errors_board_wide']} errors")
        if completion["escaped_but_net_open"]:
            print("  escaped but their net is still open elsewhere "
                  "(a routing remainder, NOT an escape failure):")
            for ball in completion["escaped_but_net_open"]:
                print(f"    {ball['pad']:>4}  {ball['net']:<16} ring {ball['ring']}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("board", type=Path, help="the board to measure (.kicad_pcb)")
    ap.add_argument("--ref", default="U1", help="reference designator of the part")
    ap.add_argument("--drc-report", type=Path,
                    help="kicad-cli pcb drc --format json --severity-all --refill-zones "
                         "output; adds net-completion attribution")
    ap.add_argument("--expect", type=Path,
                    help="a committed measurement to gate against; exit 1 on drift")
    ap.add_argument("--power-prefix", action="append", metavar="PREFIX",
                    help="net prefix that reaches copper through a via rather than "
                         "laterally (repeatable; replaces the defaults)")
    ap.add_argument("--json", action="store_true", help="machine-readable result")
    args = ap.parse_args(argv)

    if not args.board.exists():
        sys.exit(f"error: no such board: {args.board}")
    drc = None
    if args.drc_report:
        if not args.drc_report.exists():
            sys.exit(f"error: no such DRC report: {args.drc_report}")
        drc = json.loads(args.drc_report.read_text())

    prefixes = tuple(args.power_prefix) if args.power_prefix else DEFAULT_POWER_PREFIXES
    result = audit(args.board, args.ref, drc, prefixes)

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        report(result)

    if args.expect:
        expected = json.loads(args.expect.read_text())
        drift = compare(result, expected)
        if drift:
            print(f"\nDRIFT from {args.expect}:", file=sys.stderr)
            for line in drift:
                print(f"  {line}", file=sys.stderr)
            return 1
        print(f"\nmatches {args.expect.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
