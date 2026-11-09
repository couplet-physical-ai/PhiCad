#!/usr/bin/env python3
"""Clear the two things an autorouter reliably leaves behind.

Freerouting finishes with artefacts that are not routing mistakes so much as
debris, and both of them are DRC failures:

  * **Dangling vias.**  When a connection is ripped up and re-tried elsewhere,
    the via it used can stay behind, landing on nothing.  It carries a net, so
    it looks purposeful, and it is invisible in a ratsnest view.
  * **Sub-minimum necks.**  Approaching a pad narrower than the track, the
    router necks down to the pad's own width and leaves a stub below the board
    minimum -- 0.075 mm against a 0.1 mm rule, over a millimetre or so.

Neither needs judgement to fix: the via connects nothing, and the neck only has
to grow back to the minimum it already violates.  What does need care is *which*
items to touch, so this drives entirely off KiCad's own DRC report rather than
re-deriving connectivity.  If DRC did not flag it, this does not touch it.

    python3 cleanup.py board.kicad_pcb            # report only
    python3 cleanup.py board.kicad_pcb --apply

The board keeps its project sidecars, so run it on a board that has them --
without the .kicad_pro the rules are stock defaults and the report is fiction.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from cli_path import find_cli  # noqa: E402

TOL = 0.002  # mm; DRC positions are rounded, board coordinates are not


def run_drc(cli: str, board: Path, refill: bool = True) -> dict:
    """DRC the board, refilling zones first.

    The refill is not optional in practice.  A via whose only job is to drop a
    pad onto a plane is connected BY the zone fill, so against unfilled zones it
    reports as dangling -- and this tool's whole purpose is deleting things that
    report as dangling.  Run once without it and the pass cheerfully removes
    every plane stitch on the board: 35 vias and the 40 tracks that reached
    them, on r1.
    """
    report = Path(tempfile.mkstemp(suffix=".drc.json")[1])
    try:
        args = [cli, "pcb", "drc", "--format", "json", "--severity-all"]
        if refill:
            args.append("--refill-zones")
        args += ["-o", str(report), str(board)]
        proc = subprocess.run(args, capture_output=True, text=True,
                              env=dict(os.environ, KICAD_RUN_FROM_BUILD_DIR="1"))
        if proc.returncode != 0:
            raise SystemExit(f"drc failed: {proc.stdout}{proc.stderr}")
        return json.loads(report.read_text())
    finally:
        report.unlink(missing_ok=True)


def blocks(text: str, head: str):
    """Yield (start, end) of every top-level `(head ...)` s-expression."""
    for m in re.finditer(r"\(%s\b" % re.escape(head), text):
        depth = 0
        for i in range(m.start(), len(text)):
            if text[i] == "(":
                depth += 1
            elif text[i] == ")":
                depth -= 1
                if depth == 0:
                    yield m.start(), i + 1
                    break


def near(a: float, b: float) -> bool:
    return abs(a - b) < TOL


def targets(drc: dict) -> tuple[list[tuple[float, float]],
                                list[tuple[float, float]],
                                list[tuple[float, float]]]:
    """Positions of dangling vias, dangling tracks, and sub-minimum necks."""
    vias, stubs, necks = [], [], []
    for v in drc["violations"]:
        for item in v.get("items", []):
            pos = item.get("pos")
            if not pos:
                continue
            if v["type"] == "via_dangling":
                vias.append((pos["x"], pos["y"]))
            elif v["type"] == "track_dangling":
                stubs.append((pos["x"], pos["y"]))
            elif v["type"] in ("track_width", "connection_width"):
                necks.append((pos["x"], pos["y"]))
    return vias, stubs, necks


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("board", type=Path)
    ap.add_argument("--apply", action="store_true", help="write the board (default: report only)")
    ap.add_argument("--min-width", type=float, default=None,
                    help="width to grow necks to (default: read from the DRC message)")
    ap.add_argument("--cli", default=None)
    args = ap.parse_args()

    cli = find_cli(args.cli)
    before = run_drc(cli, args.board)
    print(f"{args.board.name}: {len(before['violations'])} violations, "
          f"{len(before['unconnected_items'])} unconnected")

    minw = args.min_width
    if minw is None:
        for v in before["violations"]:
            m = re.search(r"min width ([\d.]+) mm", v.get("description", ""))
            if m:
                minw = float(m.group(1))
                break
    if minw is None:
        minw = 0.1

    # Removing a via that was "connected on only one layer" orphans the track that
    # reached it, so one pass trades ten dangling vias for four dangling tracks.
    # Repeat until the board stops changing; each round re-asks DRC what is loose
    # rather than trying to predict it.
    drc, total = before, {"via": 0, "stub": 0, "neck": 0}
    for _ in range(8):
        via_pos, stub_pos, neck_pos = targets(drc)
        text = args.board.read_text()
        edits: list[tuple[int, int, str]] = []
        counts = {"via": 0, "stub": 0, "neck": 0}

        for start, end in blocks(text, "via"):
            at = re.search(r"\(at ([-\d.]+) ([-\d.]+)\)", text[start:end])
            if at and any(near(float(at.group(1)), x) and near(float(at.group(2)), y)
                          for x, y in via_pos):
                edits.append((start, end, ""))
                counts["via"] += 1

        for start, end in blocks(text, "segment"):
            blk = text[start:end]
            ends = [m for m in (re.search(r"\(start ([-\d.]+) ([-\d.]+)\)", blk),
                                re.search(r"\(end ([-\d.]+) ([-\d.]+)\)", blk)) if m]
            touches = lambda pts: any(  # noqa: E731
                near(float(p.group(1)), x) and near(float(p.group(2)), y)
                for p in ends for x, y in pts)
            if touches(stub_pos):
                edits.append((start, end, ""))
                counts["stub"] += 1
                continue
            w = re.search(r"\(width ([\d.]+)\)", blk)
            if w and float(w.group(1)) < minw and touches(neck_pos):
                edits.append((start, end, blk.replace(w.group(0), f"(width {minw:g})", 1)))
                counts["neck"] += 1

        if not edits:
            break
        if not args.apply:
            print(f"  dangling vias {counts['via']}, dangling tracks {counts['stub']}, "
                  f"necks to widen to {minw:g} mm {counts['neck']}")
            print("  (report only; pass --apply to write)")
            return 0

        # Splice from the end so earlier offsets stay valid.
        for start, end, replacement in sorted(edits, key=lambda e: -e[0]):
            if replacement == "":
                # Take the indent in front of the item too, or the file grows blank lines.
                lead = start
                while lead > 0 and text[lead - 1] in "\t ":
                    lead -= 1
                if lead > 0 and text[lead - 1] == "\n":
                    lead -= 1
                text = text[:lead] + text[end:]
            else:
                text = text[:start] + replacement + text[end:]
        args.board.write_text(text)
        for k in total:
            total[k] += counts[k]
        drc = run_drc(cli, args.board)

    if not args.apply:
        print("  nothing to do")
        return 0

    print(f"  removed {total['via']} dangling via(s), {total['stub']} dangling track(s); "
          f"widened {total['neck']} neck(s) to {minw:g} mm")
    errs = [v for v in drc["violations"] if v["severity"] == "error"]
    print(f"  -> {len(drc['violations'])} violations, {len(errs)} errors, "
          f"{len(drc['unconnected_items'])} unconnected")
    if len(drc["unconnected_items"]) != len(before["unconnected_items"]):
        print(f"  WARNING: unconnected changed from {len(before['unconnected_items'])} "
              f"-- this pass should never disconnect anything")
    return 0


if __name__ == "__main__":
    sys.exit(main())
