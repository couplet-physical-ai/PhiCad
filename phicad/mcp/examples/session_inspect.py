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
"""Inspect a board headlessly and print a structured inventory. THE SCREENSHOT REPLACEMENT.

WHY
    When an agent needs to know what is on a board, the wrong move is to open the GUI and
    screenshot it: that hijacks the user's screen, and a picture of a PCB is a terrible
    data structure. This reads the real model over IPC and prints text you can grep, diff,
    or parse (--json). No window is ever created.

WHAT IT REPORTS
    * document identity + fork version
    * footprints: reference, value, library id, position (mm), rotation, layer
    * tracks/arcs: count, per-layer breakdown, width histogram, total routed length
    * vias: count, diameter/drill histogram
    * nets: count, plus the most-connected nets by item count
    * stackup: ordered layers with type, thickness (mm), material
    * board extents in mm

READ-ONLY, WITH ONE ASTERISK
    This script mutates nothing itself. But merely OPENING a board and letting the server
    save it rewrites the file to this fork's version (20260831), which stable KiCad 10.0
    then refuses to open. Nothing here calls save(), so a board opened in place is left
    alone on disk — yet the default is still a temp copy (--in-place to override), because
    an inspection recipe should be impossible to hurt yourself with.

USAGE
    build/venv/bin/python phicad/mcp/examples/session_inspect.py \
        --board demos/pic_programmer/pic_programmer.kicad_pcb

    --json                    machine-readable output (for MCP tools / agents)
    --max-footprints N        cap the footprint listing (default 25; 0 = all)
    --attach ipc://...        inspect a board already open in a running server
    --in-place                skip the temp copy

EXIT CODES
    0 ok, 1 session/board failure, 2 unexpected error, 130 interrupted.

Verified against build/mac-dev, PhiCad 10.99.0, on demos/pic_programmer (63 footprints,
370 tracks) and demos/microwave (4 footprints, 0 tracks - the empty-histogram case).
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))

from session_lifecycle import (  # noqa: E402
    REPO_ROOT,
    HeadlessSession,
    SessionError,
    stage_fixture,
)

NM_PER_MM = 1e6


def nm(value: int) -> float:
    """Nanometres -> millimetres, rounded for display."""
    return round(value / NM_PER_MM, 4)


def collect(board) -> dict[str, Any]:
    """Pull a full inventory off an open board into plain Python data."""
    from kipy.board_types import ArcTrack, BoardLayer

    footprints = board.get_footprints()
    tracks = board.get_tracks()   # includes ArcTrack instances
    vias = board.get_vias()
    nets = board.get_nets()
    pads = board.get_pads()
    zones = board.get_zones()

    fp_rows = []
    for fp in footprints:
        fp_rows.append(
            {
                "reference": fp.reference_field.text.value,
                "value": fp.value_field.text.value,
                "library_id": f"{fp.definition.id.library}:{fp.definition.id.name}",
                "x_mm": nm(fp.position.x),
                "y_mm": nm(fp.position.y),
                "rotation_deg": fp.orientation.degrees,
                "layer": BoardLayer.Name(fp.layer),
                "locked": bool(fp.locked),
            }
        )
    fp_rows.sort(key=lambda r: r["reference"])

    straight = [t for t in tracks if not isinstance(t, ArcTrack)]
    arcs = [t for t in tracks if isinstance(t, ArcTrack)]

    track_layers = Counter(BoardLayer.Name(t.layer) for t in tracks)
    track_widths = Counter(nm(t.width) for t in tracks)
    # Track.length is a METHOD on this kipy version, not a property - calling the
    # bound method by mistake silently yields a method object, so be explicit.
    total_len_nm = 0
    for t in tracks:
        try:
            total_len_nm += t.length()
        except Exception:  # noqa: BLE001 - arcs may not implement it on every build
            pass

    via_sizes = Counter((nm(v.diameter), nm(v.drill_diameter)) for v in vias)

    items_per_net: Counter[str] = Counter()
    for item in list(tracks) + list(vias) + list(pads):
        try:
            items_per_net[item.net.name] += 1
        except Exception:  # noqa: BLE001 - unconnected items have no net
            items_per_net["<none>"] += 1

    # Stackup layer .type is the BoardStackupLayerType enum from api/proto/board/board.proto.
    # Dielectric cores carry no BoardLayer, so their .layer renders as BL_UNDEFINED - the
    # type name is what actually identifies them.
    from kipy.proto.board.board_pb2 import BoardStackupLayerType

    stackup_rows = []
    try:
        for layer in board.get_stackup().layers:
            stackup_rows.append(
                {
                    "layer": BoardLayer.Name(layer.layer),
                    "user_name": layer.user_name,
                    "type": BoardStackupLayerType.Name(layer.type).replace("BSLT_", ""),
                    "thickness_mm": nm(layer.thickness),
                    "material": layer.material_name,
                    "enabled": bool(layer.enabled),
                }
            )
    except Exception as exc:  # noqa: BLE001
        stackup_rows = [{"error": f"{type(exc).__name__}: {exc}"}]

    extents: Optional[dict[str, float]] = None
    xs = [r["x_mm"] for r in fp_rows]
    ys = [r["y_mm"] for r in fp_rows]
    if xs and ys:
        extents = {
            "min_x_mm": min(xs), "max_x_mm": max(xs),
            "min_y_mm": min(ys), "max_y_mm": max(ys),
            "width_mm": round(max(xs) - min(xs), 4),
            "height_mm": round(max(ys) - min(ys), 4),
        }

    return {
        "document": board.name,
        "counts": {
            "footprints": len(footprints),
            "tracks_total": len(tracks),
            "tracks_straight": len(straight),
            "tracks_arc": len(arcs),
            "vias": len(vias),
            "nets": len(nets),
            "pads": len(pads),
            "zones": len(zones),
            "copper_layers": board.get_copper_layer_count(),
        },
        "footprints": fp_rows,
        "tracks": {
            "by_layer": dict(track_layers.most_common()),
            "width_histogram_mm": {str(k): v for k, v in sorted(track_widths.items())},
            "total_routed_length_mm": round(total_len_nm / NM_PER_MM, 3),
        },
        "vias": {
            "size_histogram_mm": {f"d{d}/h{h}": n for (d, h), n in via_sizes.most_common()},
        },
        "nets": {
            "count": len(nets),
            "busiest": [{"net": n or "<unnamed>", "items": c}
                        for n, c in items_per_net.most_common(10)],
        },
        "stackup": stackup_rows,
        "footprint_extents": extents,
    }


def print_report(data: dict[str, Any], max_footprints: int) -> None:
    c = data["counts"]
    print(f"\n{'=' * 78}")
    print(f"  BOARD INVENTORY: {data['document']}")
    print(f"{'=' * 78}")

    print("\n-- counts")
    print(f"   footprints {c['footprints']:<6} pads {c['pads']:<6} "
          f"nets {c['nets']:<6} zones {c['zones']}")
    print(f"   tracks     {c['tracks_total']:<6} (straight {c['tracks_straight']}, "
          f"arc {c['tracks_arc']})   vias {c['vias']}")
    print(f"   copper layers {c['copper_layers']}")

    fps = data["footprints"]
    shown = fps if max_footprints == 0 else fps[:max_footprints]
    print(f"\n-- footprints ({len(shown)} of {len(fps)})")
    print(f"   {'REF':<8} {'VALUE':<16} {'X mm':>9} {'Y mm':>9} {'ROT':>7}  {'LAYER':<9} LIBRARY")
    for r in shown:
        print(f"   {r['reference']:<8} {r['value'][:16]:<16} {r['x_mm']:>9.3f} "
              f"{r['y_mm']:>9.3f} {r['rotation_deg']:>7.1f}  "
              f"{r['layer'].replace('BL_', ''):<9} {r['library_id'][:38]}")
    if max_footprints and len(fps) > max_footprints:
        print(f"   ... {len(fps) - max_footprints} more (use --max-footprints 0 for all)")

    ext = data["footprint_extents"]
    if ext:
        print(f"\n-- footprint extents")
        print(f"   x {ext['min_x_mm']:.3f} .. {ext['max_x_mm']:.3f} mm  "
              f"(w {ext['width_mm']:.3f})")
        print(f"   y {ext['min_y_mm']:.3f} .. {ext['max_y_mm']:.3f} mm  "
              f"(h {ext['height_mm']:.3f})")

    t = data["tracks"]
    print(f"\n-- tracks")
    print(f"   total routed length {t['total_routed_length_mm']:.3f} mm")
    print(f"   by layer            {t['by_layer']}")
    print(f"   width histogram mm  {t['width_histogram_mm']}")

    if data["counts"]["vias"]:
        print(f"\n-- vias")
        print(f"   size histogram (diameter/hole, mm)  {data['vias']['size_histogram_mm']}")

    print(f"\n-- busiest nets")
    for row in data["nets"]["busiest"]:
        print(f"   {row['items']:>5} items  {row['net']}")

    print(f"\n-- stackup ({len(data['stackup'])} layers)")
    for layer in data["stackup"]:
        if "error" in layer:
            print(f"   unavailable: {layer['error']}")
            continue
        print(f"   {layer['layer'].replace('BL_', ''):<12} "
              f"{layer['type']:<11} "
              f"{layer['user_name'][:20]:<20} "
              f"{layer['thickness_mm']:>8.4f} mm  {layer['material'] or '-'}")
    print()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Print a structured inventory of a board, headlessly (no GUI).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--board",
        default="demos/pic_programmer/pic_programmer.kicad_pcb",
        help="board to inspect; relative paths resolve against the repo root "
             "(default: %(default)s)",
    )
    parser.add_argument("--json", action="store_true",
                        help="emit JSON instead of the human report")
    parser.add_argument("--max-footprints", type=int, default=25,
                        help="cap the footprint listing; 0 = all (default: %(default)s)")
    parser.add_argument("--attach", metavar="IPC_URL",
                        help="inspect the board open in an already-running server")
    parser.add_argument("--in-place", action="store_true",
                        help="open the board where it lies (this script never saves, "
                             "so the file is not rewritten)")
    parser.add_argument("--timeout", type=float, default=60.0,
                        help="server readiness timeout (default: %(default)s)")
    args = parser.parse_args()

    work_dir: Optional[Path] = None
    try:
        board_path = Path(args.board)
        if not board_path.is_absolute():
            board_path = (REPO_ROOT / board_path).resolve()

        if not args.in_place and not args.attach:
            board_path = stage_fixture(board_path)
            work_dir = board_path.parent

        with HeadlessSession(attach=args.attach, ready_timeout=args.timeout) as sess:
            if args.attach:
                # Someone else owns the document; just read whatever is open.
                board = sess.kicad.get_board()
            else:
                board = sess.open_board(board_path)

            data = collect(board)
            # get_version() returns a KiCadVersion object; str() it so --json stays valid.
            data["fork_version"] = str(sess.kicad.get_version())
            data["source_path"] = str(board_path)

        if args.json:
            print(json.dumps(data, indent=2, sort_keys=False))
        else:
            print_report(data, args.max_footprints)
            print(f"source  {data['source_path']}")
            print(f"version {data['fork_version']}")
        return 0

    except SessionError as exc:
        print(f"\nFAIL: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nFAIL: interrupted", file=sys.stderr)
        return 130
    except Exception as exc:  # noqa: BLE001
        print(f"\nFAIL: unexpected {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    finally:
        if work_dir:
            shutil.rmtree(work_dir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
