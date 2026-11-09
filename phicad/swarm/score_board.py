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
"""Score a board candidate for swarm selection. Lower is better.

The orchestrator runs this on every candidate a swarm produces (best-of-N
placement, per-group routing) and keeps the winner. The score is a single
float built from hard evidence:

    unconnected items   x 10000   (an unrouted board is not a candidate)
    DRC errors          x 1000
    DRC warnings        x 10
    via count           x 2
    track length (mm)   x 0.1

DRC comes from the fork's `kicad-cli pcb drc --severity-all` (the only
oracle); track stats from a phicad/pylib/sexp.py parse (handles both stable
and fork board formats). Exit code is always 0 unless the board cannot be
evaluated — scoring measures, gates decide elsewhere.

Design rules live in the PROJECT file, not the board, and KiCad finds it only
as `<board stem>.kicad_pro` beside the board.  A candidate copied to a scratch
name (`cp placed.kicad_pcb cand-g0.kicad_pcb`) therefore gets scored against
KiCad's DEFAULT rules, which silently invents or hides violations: one real
board here scored 0 violations inside its project and 13 copied out, a 13000
point swing in a score whose whole job is ranking candidates.  So the project
is resolved explicitly, staged beside the board when the stems disagree, and
`rules_source` in the report always says which rules produced the numbers.

Zones are refilled for the check (`--refill-zones`, in memory, board untouched)
because any copper change staleness the pour: without it the same board scored
18 phantom clearance/hole violations.

Usage:
    score_board.py candidate.kicad_pcb [--project p.kicad_pro] [--json] [--keep-report]
    PHICAD_KICAD_CLI / KICAD_CLI=<path to fork kicad-cli>  (else the repo build tree)
"""

from __future__ import annotations

import argparse
import contextlib
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "pylib"))
import sexp  # noqa: E402

WEIGHTS = {"unconnected": 10000.0, "error": 1000.0, "warning": 10.0,
           "via": 2.0, "track_mm": 0.1}


def find_kicad_cli() -> str:
    # Both spellings, PHICAD_KICAD_CLI first: the MCP server, synth and the GUI
    # plugin set that one, while autoroute/qa use KICAD_CLI.  Honoring only one
    # meant a single exported environment scored boards in some tools and failed
    # with "kicad-cli not found" in this one.
    for name in ("PHICAD_KICAD_CLI", "KICAD_CLI"):
        env = os.environ.get(name, "").strip()
        if env and Path(env).exists():
            return env
    here = Path(__file__).resolve()
    for parent in here.parents:
        for app in ("PhiCad", "KiCad"):
            cand = (parent / "build" / "mac-dev" / "kicad" / f"{app}.app"
                    / "Contents" / "MacOS" / "kicad-cli")
            if cand.exists():
                return str(cand)
    sys.exit("error: kicad-cli not found — set KICAD_CLI or build the fork")


def resolve_project(board: Path, explicit: Path | None) -> tuple[Path | None, str]:
    """(project file whose rules will apply, how it was found).

    'kicad-defaults' is a real answer, not an error: scoring an orphan board is
    allowed, it just has to be labelled so nobody compares those numbers with
    numbers from a board that had its project.
    """
    if explicit is not None:
        if not explicit.is_file():
            sys.exit(f"error: --project {explicit} does not exist")
        return explicit, "explicit"
    sibling = board.with_suffix(".kicad_pro")
    if sibling.is_file():
        return sibling, "sibling"
    return None, "kicad-defaults"


@contextlib.contextmanager
def staged_board(board: Path, project: Path | None):
    """Yield a board path that KiCad will pair with `project`.

    KiCad matches a project by filename stem, so a candidate named
    cand-g0.kicad_pcb never sees placed.kicad_pro however close it sits.  When
    the stems already agree nothing is copied (the common in-project case);
    otherwise board and project are staged into a temp dir under the PROJECT's
    stem, which is what makes a scratch-named candidate score against the real
    design rules.  The original files are never modified.
    """
    if project is None or project.with_suffix(".kicad_pcb") == board:
        yield board
        return
    tmp = Path(tempfile.mkdtemp(prefix="score-board-"))
    try:
        target = tmp / (project.stem + ".kicad_pcb")
        shutil.copy2(board, target)
        shutil.copy2(project, tmp / project.name)
        yield target
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("board", type=Path)
    ap.add_argument("--project", type=Path,
                    help="project (.kicad_pro) whose design rules apply; default: the "
                         "board's sibling <stem>.kicad_pro if it exists")
    ap.add_argument("--json", action="store_true", help="JSON only on stdout")
    ap.add_argument("--keep-report", action="store_true",
                    help="leave the DRC json next to the board")
    args = ap.parse_args()

    cli = find_kicad_cli()
    report = (args.board.with_suffix(".drc.json") if args.keep_report
              else Path(tempfile.mkstemp(suffix=".drc.json")[1]))
    env = dict(os.environ, KICAD_RUN_FROM_BUILD_DIR="1")

    project, rules_source = resolve_project(args.board, args.project)
    with staged_board(args.board, project) as drc_target:
        proc = subprocess.run(
            [cli, "pcb", "drc", "--format", "json", "--severity-all",
             "--refill-zones", "-o", str(report), str(drc_target)],
            capture_output=True, text=True, env=env, timeout=600)
    if proc.returncode != 0 or not report.exists():
        sys.exit(f"error: drc failed: {proc.stdout}{proc.stderr}")
    drc = json.loads(report.read_text())
    if not args.keep_report:
        report.unlink(missing_ok=True)

    errors = sum(1 for v in drc["violations"] if v["severity"] == "error")
    warnings = sum(1 for v in drc["violations"] if v["severity"] == "warning")
    unconnected = len(drc["unconnected_items"])

    doc = sexp.parse_file(args.board)
    track_mm = 0.0
    vias = segs = 0
    for item in doc:
        h = sexp.head(item)
        if h in ("segment", "arc"):
            start = sexp.floats(item, "start")
            end = sexp.floats(item, "end")
            if start and end:
                segs += 1
                track_mm += math.dist(start[:2], end[:2])  # arc: chord lower bound
        elif h == "via":
            vias += 1

    score = (WEIGHTS["unconnected"] * unconnected + WEIGHTS["error"] * errors
             + WEIGHTS["warning"] * warnings + WEIGHTS["via"] * vias
             + WEIGHTS["track_mm"] * track_mm)

    out = {"board": str(args.board), "score": round(score, 1),
           "unconnected": unconnected, "drc_errors": errors,
           "drc_warnings": warnings, "vias": vias, "segments": segs,
           "track_mm": round(track_mm, 1), "weights": WEIGHTS,
           "rules_source": rules_source,
           "project": str(project) if project else None,
           "kicad_version": drc.get("kicad_version")}
    print(json.dumps(out, indent=2))
    if rules_source == "kicad-defaults":
        # Loud, because every number above is then measured against rules the
        # project never chose, and scores from differently-situated candidates
        # are not comparable.
        print(f"warning: no project file for {args.board.name} — DRC ran against "
              f"KiCad's DEFAULT design rules, not this design's. Pass "
              f"--project <p.kicad_pro> (or keep <stem>.kicad_pro beside the board) "
              f"to score against the real rules.", file=sys.stderr)
    if not args.json:
        print(f"score {out['score']}: unconnected={unconnected} err={errors} "
              f"warn={warnings} vias={vias} track={out['track_mm']}mm "
              f"[rules: {rules_source}]", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
