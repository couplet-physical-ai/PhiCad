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
"""Board in, routed board out: the whole Specctra loop through Freerouting.

    board.kicad_pcb -> .dsn -> freerouting.jar -> .ses -> board.kicad_pcb

Why this exists
---------------
The fork's own routers each hit a wall on a dense multi-layer board.  PNS
(`pcbnew/router/`) is interactive-only and refuses headless.  `phicad/synth/route.py`
is a grid A* that deliberately refuses rather than guess, and it is 2-layer shaped.
The remaining option was an LLM emitting one track at a time, which is how r0.2 was
routed: ~180 one-off scripts over 70 DRC iterations.  That does not scale to a
196-ball BGA on four layers.

Freerouting is a real autorouter and speaks Specctra, and this fork can now export
DSN and import SES headlessly (`kicad-cli pcb export dsn`, `kicad-cli pcb import-ses`,
added alongside this file).  This driver is the glue and nothing more: it holds no
routing policy, it does not edit boards, and every artifact it produces is a file you
can inspect and re-run by hand.

Freerouting's actual interface (v2.3.0, established by running it, not from docs)
--------------------------------------------------------------------------------
    -de <design.dsn>     input design            -do <out.ses>   output session
    -mp <passes>         autoroute pass ceiling  -mt <threads>   optimizer threads
    -us/-is/-hr          optimizer strategy knobs

It exits 0 on a *partial* route, so the exit code is not the result: the result is in
its log ("... final score: N (X unrouted and Y violations)"), which this driver parses
and re-reports.  It is a Swing application that starts a GUI unless told otherwise;
`-Djava.awt.headless=true` is what makes it run as a batch job, and is passed always.

What this driver will not do
----------------------------
Judge the routing.  `--drc` runs the fork's DRC before and after and prints both, which
is evidence, not a verdict: an autorouter that leaves nets unrouted or lays tracks
thinner than a net class demands will still exit 0 here.  Read the numbers.

Usage
-----
    freeroute.py board.kicad_pcb [-o routed.kicad_pcb] [--passes N] [--drc]
    freeroute.py board.kicad_pcb --in-place --keep work/    # keep .dsn/.ses/logs

    FREEROUTING_JAR=<path to freerouting.jar>   (default ~/.kicad-mcp/freerouting.jar)
    KICAD_CLI=<path to fork kicad-cli>          (else resolved from the repo build tree)
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import selectors
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "pylib"))
import sexp  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
from cli_path import find_cli  # noqa: E402

DEFAULT_JAR = Path.home() / ".kicad-mcp" / "freerouting.jar"

# Freerouting's own end-of-run summary, e.g.
#   "Auto-routing stage completed: ... final score: 767.69 (60 unrouted and 2 violations)"
SCORE_RE = re.compile(r"final score: ([\d.]+) \((\d+) unrouted and (\d+) violations\)")


def find_kicad_cli() -> str:
    """Kept as a name; the logic now lives in cli_path so all three tools agree."""
    return find_cli()


def find_jar(arg: str | None) -> Path:
    for cand in (arg, os.environ.get("FREEROUTING_JAR"), DEFAULT_JAR):
        if cand and Path(cand).exists():
            return Path(cand)
    sys.exit(f"error: freerouting.jar not found — set FREEROUTING_JAR or put it at {DEFAULT_JAR}\n"
             "       (releases: https://github.com/freerouting/freerouting/releases)")


def run_cli(cli: str, args: list[str], what: str) -> str:
    """Run kicad-cli from the build tree; die with its own diagnostics on failure."""
    env = dict(os.environ, KICAD_RUN_FROM_BUILD_DIR="1")
    proc = subprocess.run([cli, *args], capture_output=True, text=True, env=env, timeout=1800)
    if proc.returncode != 0:
        sys.exit(f"error: {what} failed (exit {proc.returncode}):\n{proc.stdout}{proc.stderr}")
    return proc.stdout


def drc_counts(cli: str, board: Path) -> dict:
    """Violation and unconnected counts by type, from the CLI's own JSON report."""
    report = Path(tempfile.mkstemp(suffix=".drc.json")[1])
    try:
        run_cli(cli, ["pcb", "drc", "--format", "json", "--severity-all",
                      "-o", str(report), str(board)], "pcb drc")
        drc = json.loads(report.read_text())
    finally:
        report.unlink(missing_ok=True)
    by_type: dict[str, int] = {}
    for v in drc["violations"]:
        by_type[v["type"]] = by_type.get(v["type"], 0) + 1
    return {"violations": len(drc["violations"]),
            "unconnected": len(drc["unconnected_items"]),
            "by_type": by_type}


def refill_zones(cli: str, board: Path) -> dict | None:
    """Refill the pours and SAVE the board, because new copper stales every zone.

    A zone's filled polygons are cached in the file; the router adds copper
    without touching them, so the pour that comes back still has knockouts for
    the OLD copper and none for the new.  Measured on a routed example board:
    a plain `pcb drc` reported 18 violations (14 clearance, 4 hole_clearance)
    that a refill cleared completely -- i.e. the tool used to hand back a board
    that failed DRC for a reason the router never caused.

    `pcb drc --refill-zones --save-board` is the only headless filler kicad-cli
    offers (synth fills over IPC instead).  Returns None when there is nothing
    to fill, or when the CLI cannot do it -- an enhancement must not take the
    whole route down, so a failure here is reported and the routed board is
    still delivered.
    """
    zones = len(sexp.children(sexp.parse_file(board), "zone"))
    if not zones:
        return None
    report = Path(tempfile.mkstemp(suffix=".refill.json")[1])
    env = dict(os.environ, KICAD_RUN_FROM_BUILD_DIR="1")
    try:
        proc = subprocess.run(
            [cli, "pcb", "drc", "--refill-zones", "--save-board", "--format", "json",
             "--severity-all", "-o", str(report), str(board)],
            capture_output=True, text=True, env=env, timeout=1800)
        if proc.returncode not in (0, 5):      # 5 = violations found, board still saved
            return {"zones": zones, "filled": False,
                    "error": (proc.stderr or proc.stdout or "").strip().splitlines()[-1:]}
        return {"zones": zones, "filled": True}
    except (OSError, subprocess.SubprocessError) as exc:
        return {"zones": zones, "filled": False, "error": str(exc)}
    finally:
        report.unlink(missing_ok=True)


def run_cleanup(board: Path) -> dict:
    """Delete the router's dangling vias/tracks and widen sub-minimum necks.

    Freerouting leaves artifacts a refill cannot fix -- on the example board a
    single dangling via, the last violation standing after the pour was refilled.
    cleanup.py already fixes exactly these, driven off KiCad's own DRC report, so
    this only sequences it.  Kept opt-in because it DELETES copper, and reported
    verbatim so a surprising removal is visible rather than silent.
    """
    script = Path(__file__).resolve().parent / "cleanup.py"
    env = dict(os.environ, KICAD_RUN_FROM_BUILD_DIR="1")
    proc = subprocess.run([sys.executable, str(script), str(board), "--apply"],
                          capture_output=True, text=True, env=env, timeout=1800)
    out = (proc.stdout or "").strip().splitlines()
    return {"ok": proc.returncode == 0,
            "report": out[-2:] if proc.returncode == 0 else
                      (proc.stderr or proc.stdout or "").strip().splitlines()[-2:]}


def carry_project(source: Path, out: Path) -> list[Path]:
    """Give the routed board the project files the input board was routed under.

    A board finds its net classes in the sibling `.kicad_pro` and its custom rules
    in the sibling `.kicad_dru`, matched by file stem -- neither path is recorded
    inside the board.  So routing `b.kicad_pcb` to `b-routed.kicad_pcb` yields a
    board that no longer has any of the rules it was routed under, and both DRC
    and the GUI silently fall back to stock defaults.

    That is not a cosmetic difference.  On a design whose BGA escape needs a
    0.1 mm clearance and 0.45/0.25 vias, the stock minimums rejected almost
    everything the router had just correctly done: 942 violations reported
    against a real count of 27, the bulk of them clearance, track_width and
    via_diameter on perfectly good copper.

    Returns the files written, so the caller can say what it left behind.
    """
    if out == source:
        return []
    written = []
    for ext in (".kicad_pro", ".kicad_dru"):
        src, dst = source.with_suffix(ext), out.with_suffix(ext)
        if src.exists() and not dst.exists():
            shutil.copy(src, dst)
            written.append(dst)
    return written


def copper_stats(board: Path) -> dict:
    """Segments, vias and track length on a board, for the before/after delta."""
    doc = sexp.parse_file(board)
    segs = vias = 0
    track_mm = 0.0
    for item in doc:
        h = sexp.head(item)
        if h in ("segment", "arc"):
            start, end = sexp.floats(item, "start"), sexp.floats(item, "end")
            if start and end:
                segs += 1
                track_mm += math.dist(start[:2], end[:2])  # arc: chord, a lower bound
        elif h == "via":
            vias += 1
    return {"segments": segs, "vias": vias, "track_mm": round(track_mm, 1)}


def run_freerouting(jar: Path, dsn: Path, ses: Path, passes: int, threads: int | None,
                    timeout: int, java: str, verbose: bool) -> tuple[str, dict | None]:
    cmd = [java, "-Djava.awt.headless=true", "-jar", str(jar),
           "-de", str(dsn), "-do", str(ses), "-mp", str(passes)]
    if threads:
        cmd += ["-mt", str(threads)]

    print(f"==> freerouting: {' '.join(cmd)}", file=sys.stderr, flush=True)
    lines: list[str] = []
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        assert proc.stdout is not None
        # --timeout has to be enforced while the log is being drained, not after,
        # and it must fire even when the JVM goes SILENT: a blocking
        # `for line in proc.stdout` re-checks the deadline only when a line
        # arrives, so a wedged-but-quiet run used to hang until process exit.
        # select() on the raw pipe bounds every wait, so the deadline fires on
        # schedule whether freerouting is chatty or stuck.  Killing it is still
        # destructive -- the .ses is written only at the end of the final pass,
        # so a killed run yields nothing.  Prefer bounding the work with
        # --passes; --timeout is the backstop.
        deadline = time.monotonic() + timeout

        def overtime() -> None:
            proc.kill()
            sys.exit(
                f"error: freerouting exceeded --timeout {timeout}s and was killed "
                f"before it could write a session file. Re-run with a --passes "
                f"ceiling it can finish inside, since the .ses is only written "
                f"after the last pass.\npartial log:\n" + "\n".join(lines[-15:])
            )

        def take(raw: bytes) -> None:
            line = raw.decode("utf-8", "replace").rstrip()
            lines.append(line)
            # Freerouting is chatty per pass; the stage summaries are the interesting part.
            if verbose or " completed" in line or "finished with state" in line:
                print("    " + line, file=sys.stderr, flush=True)

        sel = selectors.DefaultSelector()
        sel.register(proc.stdout.fileno(), selectors.EVENT_READ)
        pending = b""
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    overtime()
                if not sel.select(timeout=min(remaining, 5.0)):
                    continue  # silent JVM: loop back and re-judge the deadline
                chunk = os.read(proc.stdout.fileno(), 65536)
                if not chunk:
                    break     # EOF: freerouting closed its end
                pending += chunk
                *complete, pending = pending.split(b"\n")
                for raw in complete:
                    take(raw)
        finally:
            sel.close()
        if pending:
            take(pending)
        proc.wait()
    except FileNotFoundError:
        sys.exit(f"error: no java at '{java}' — install a JRE or pass --java")

    log = "\n".join(lines)
    if not ses.exists():
        sys.exit(f"error: freerouting produced no session file (exit {proc.returncode}):\n"
                 + "\n".join(lines[-25:]))

    score = None
    for m in SCORE_RE.finditer(log):     # last one wins: optimization follows autorouting
        score = {"score": float(m.group(1)), "unrouted": int(m.group(2)),
                 "violations": int(m.group(3))}
    return log, score


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("board", type=Path, help="input .kicad_pcb")
    ap.add_argument("-o", "--output", type=Path,
                    help="routed board (default: <board>-routed.kicad_pcb)")
    ap.add_argument("--in-place", action="store_true", help="overwrite the input board")
    ap.add_argument("--passes", type=int, default=100,
                    help="autoroute pass ceiling, freerouting -mp (default: 100)")
    ap.add_argument("--threads", type=int, help="optimizer threads, freerouting -mt")
    ap.add_argument("--jar", help=f"freerouting.jar (default: $FREEROUTING_JAR or {DEFAULT_JAR})")
    ap.add_argument("--java", default="java", help="java binary (default: java)")
    ap.add_argument("--timeout", type=int, default=3600,
                    help="seconds before freerouting is killed (default: 3600)")
    ap.add_argument("--keep", type=Path, metavar="DIR",
                    help="write .dsn/.ses/freerouting.log here instead of a temp dir")
    ap.add_argument("--drc", action="store_true", help="run DRC before and after")
    ap.add_argument("--no-refill", action="store_true",
                    help="do not refill zones after the import (leaves every pour stale "
                         "against the new copper, which DRC reports as clearance errors)")
    ap.add_argument("--cleanup", action="store_true",
                    help="after refilling, run cleanup.py --apply to delete the router's "
                         "dangling vias/tracks and widen sub-minimum necks (DESTRUCTIVE: "
                         "it removes copper, driven only by DRC's own findings)")
    ap.add_argument("--json", action="store_true", help="machine-readable summary on stdout")
    ap.add_argument("-v", "--verbose", action="store_true", help="stream freerouting's full log")
    args = ap.parse_args()

    if not args.board.exists():
        sys.exit(f"error: no such board: {args.board}")
    if args.in_place and args.output:
        sys.exit("error: --in-place and --output are mutually exclusive")

    out = (args.board if args.in_place else
           args.output or args.board.with_name(args.board.stem + "-routed.kicad_pcb"))
    cli = find_kicad_cli()
    jar = find_jar(args.jar)

    tmp = None
    if args.keep:
        args.keep.mkdir(parents=True, exist_ok=True)
        work = args.keep
    else:
        tmp = tempfile.TemporaryDirectory(prefix="phicad-freeroute-")
        work = Path(tmp.name)
    dsn, ses = work / f"{args.board.stem}.dsn", work / f"{args.board.stem}.ses"

    before = drc_counts(cli, args.board) if args.drc else None
    stats_before = copper_stats(args.board)

    print(f"==> export dsn: {dsn}", file=sys.stderr, flush=True)
    run_cli(cli, ["pcb", "export", "dsn", "-o", str(dsn), str(args.board)], "pcb export dsn")

    log, score = run_freerouting(jar, dsn, ses, args.passes, args.threads,
                                 args.timeout, args.java, args.verbose)
    if args.keep:
        (work / "freerouting.log").write_text(log)

    # The import is what makes the board; do it last so a router failure leaves the input alone.
    print(f"==> import ses -> {out}", file=sys.stderr, flush=True)
    import_args = ["pcb", "import-ses", "--ses", str(ses), str(args.board)]
    if out != args.board:
        import_args += ["-o", str(out)]
    run_cli(cli, import_args, "pcb import-ses")
    carried = carry_project(args.board, out)
    # Order matters: the pour must be refilled AFTER the project is carried, or
    # it is filled against KiCad's default rules instead of the design's.
    filled = None if args.no_refill else refill_zones(cli, out)
    # Cleanup last: it drives off DRC, so it must see the refilled pour, or it
    # judges the board against a fill that is already stale.
    cleaned = run_cleanup(out) if args.cleanup else None

    stats_after = copper_stats(out)
    after = drc_counts(cli, out) if args.drc else None

    summary = {"board": str(args.board), "output": str(out), "dsn": str(dsn), "ses": str(ses),
               "freerouting": {"jar": str(jar), "passes": args.passes, **(score or {})},
               "copper_before": stats_before, "copper_after": stats_after,
               "zone_refill": filled, "cleanup": cleaned}
    if args.drc:
        summary["drc_before"], summary["drc_after"] = before, after

    if args.json:
        print(json.dumps(summary, indent=2))
    else:
        print(f"routed: {out}")
        if carried:
            print("  project " + ", ".join(p.name for p in carried))
        print(f"  copper   {stats_before['segments']} seg / {stats_before['vias']} via"
              f"  ->  {stats_after['segments']} seg / {stats_after['vias']} via"
              f"  ({stats_after['track_mm']} mm)")
        if score:
            print(f"  router   score {score['score']}: {score['unrouted']} unrouted, "
                  f"{score['violations']} violations (freerouting's own count)")
        if cleaned:
            for line in cleaned["report"]:
                print("  cleanup  " + line.strip())
        if filled and filled.get("filled"):
            print(f"  zones    {filled['zones']} refilled against the carried rules")
        elif filled:
            print(f"  zones    {filled['zones']} NOT refilled ({filled.get('error')}) — "
                  f"pours are stale against the new copper; refill in the GUI or run "
                  f"kicad-cli pcb drc --refill-zones --save-board")
        if args.drc:
            print(f"  drc      {before['violations']} violations / {before['unconnected']} unconnected"
                  f"  ->  {after['violations']} / {after['unconnected']}")
            new = {k: v - before["by_type"].get(k, 0) for k, v in after["by_type"].items()
                   if v - before["by_type"].get(k, 0) > 0}
            if new:
                print("  new      " + ", ".join(f"{k} +{v}" for k, v in sorted(new.items())))
    if tmp:
        tmp.cleanup()
    return 0


if __name__ == "__main__":
    sys.exit(main())
