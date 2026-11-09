#!/usr/bin/env python3
# This program source code file is part of KiCad, a free EDA CAD application.
#
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
"""Partition a board's nets into N disjoint groups for swarm routing.

Each group is a set of nets one router agent owns exclusively; the merge tool
(merge_routes.py) later takes each group's copper from that agent's candidate
board. Groups are spatially coherent (nets sorted by connection centroid along
the board's long axis, then chunked) and balanced by pad count, which is the
best cheap proxy for routing effort.

Power/ground nets (name match or pad-count outliers) are put in a dedicated
"zones" bucket by default — they are poured/stitched, not point-routed, and
they span the whole board so no single region-agent should own them. Route
them last, single-agent, or via zones (--include-power folds them into normal
groups instead).

Handles BOTH stable-KiCad and fork (version 20260831) board formats via
phicad/pylib/sexp.py — stdlib only, runs under any python3.

Usage:
    partition_nets.py board.kicad_pcb --groups 3 [-o plan.json]
                      [--include-power] [--min-pads 2]

Output (plan.json): {"board", "groups": [{"id", "nets", "pads", "bbox_mm"}…],
"zones_bucket": [names…]}. Nets with fewer than --min-pads connections are
skipped (nothing to route).
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "pylib"))
import sexp  # noqa: E402

POWER_RE = re.compile(
    r"^(gnd\w*|\w*gnd|vcc\w*|vdd\w*|vss\w*|vbat\w*|vbus|[+-]?\d+v\d*|"
    r"[+-]\d+\.?\d*v?|vin|vout|pwr\w*|power|earth|agnd|dgnd|pgnd)$",
    re.IGNORECASE,
)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("board", type=Path)
    ap.add_argument("--groups", type=int, default=3, help="number of router agents")
    ap.add_argument("--min-pads", type=int, default=2,
                    help="skip nets with fewer connections (default 2)")
    ap.add_argument("--include-power", action="store_true",
                    help="treat power nets as routable instead of zone-bucketing them")
    ap.add_argument("-o", "--out", type=Path, help="write plan JSON here (default stdout)")
    args = ap.parse_args()

    doc = sexp.parse_file(args.board)
    if sexp.head(doc) != "kicad_pcb":
        sys.exit(f"error: {args.board} is not a kicad_pcb document")
    net_names = sexp.net_table(doc)   # stable format; empty on fork files

    # net -> pad positions. Footprint (at x y [rot]) + pad (at x y [rot]),
    # pad offset rotated by the footprint angle (KiCad rotates CCW).
    pads_by_net: dict[str, list[tuple[float, float]]] = defaultdict(list)
    for fp in sexp.children(doc, "footprint"):
        at = sexp.floats(fp, "at") or [0.0, 0.0]
        fx, fy = at[0], at[1]
        rot = math.radians(at[2] if len(at) > 2 else 0.0)
        cos_r, sin_r = math.cos(rot), math.sin(rot)
        for pad in sexp.children(fp, "pad"):
            ref = sexp.net_of(pad)
            if ref is None:
                continue
            name = str(ref) if ref.quoted else net_names.get(str(ref))
            if not name:
                continue
            pat = sexp.floats(pad, "at") or [0.0, 0.0]
            px, py = pat[0], pat[1]
            # board Y grows downward; KiCad footprint rotation is CCW on screen
            pads_by_net[name].append(
                (fx + px * cos_r + py * sin_r, fy - px * sin_r + py * cos_r))

    total_pads = sum(len(v) for v in pads_by_net.values())
    mean_pads = total_pads / max(len(pads_by_net), 1)

    zones_bucket, routable = [], []
    for name, pads in pads_by_net.items():
        if len(pads) < args.min_pads:
            continue
        # Power nets: name pattern, or extreme fanout (> 6x mean and >= 8 pads)
        is_power = bool(POWER_RE.match(name.split("/")[-1])) or \
            (len(pads) >= 8 and len(pads) > 6 * mean_pads)
        if is_power and not args.include_power:
            zones_bucket.append(name)
        else:
            cx = sum(p[0] for p in pads) / len(pads)
            cy = sum(p[1] for p in pads) / len(pads)
            routable.append({"name": name, "pads": len(pads), "cx": cx, "cy": cy})

    if not routable:
        sys.exit("error: no routable nets found (all power/too-small?)")

    # Long axis of the connection cloud decides the sort direction.
    xs = [n["cx"] for n in routable]
    ys = [n["cy"] for n in routable]
    axis = "cx" if (max(xs) - min(xs)) >= (max(ys) - min(ys)) else "cy"
    routable.sort(key=lambda n: (n[axis], n["name"]))

    # Chunk into --groups contiguous slices balanced by pad count.
    n_groups = max(1, min(args.groups, len(routable)))
    target = sum(n["pads"] for n in routable) / n_groups
    groups: list[list[dict]] = [[]]
    acc = 0.0
    for net in routable:
        if acc >= target and len(groups) < n_groups:
            groups.append([])
            acc = 0.0
        groups[-1].append(net)
        acc += net["pads"]

    def bbox(nets: list[dict]) -> list[float]:
        return [round(min(n["cx"] for n in nets), 2), round(min(n["cy"] for n in nets), 2),
                round(max(n["cx"] for n in nets), 2), round(max(n["cy"] for n in nets), 2)]

    plan = {
        "board": str(args.board),
        "axis": axis,
        "groups": [
            {"id": f"g{i}", "nets": [n["name"] for n in g],
             "pads": sum(n["pads"] for n in g), "bbox_mm": bbox(g)}
            for i, g in enumerate(groups)
        ],
        "zones_bucket": sorted(zones_bucket),
    }
    text = json.dumps(plan, indent=2) + "\n"
    if args.out:
        args.out.write_text(text)
    else:
        sys.stdout.write(text)
    print(f"partition: {len(routable)} nets -> {len(groups)} groups "
          f"(pads: {[g['pads'] for g in plan['groups']]}), "
          f"{len(zones_bucket)} power nets zone-bucketed", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
