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
"""Autoroute tool surface for the phicad MCP server: Specctra loop, cleanup, stitching.

WHAT THIS EXPOSES (five plain functions; the server decorates them with @mcp.tool)
    autoroute_freerouting  board -> .dsn -> freerouting.jar -> .ses -> routed board
    autoroute_cleanup      delete the debris an autorouter leaves (dangling vias, necks)
    autoroute_stitch       drop unconnected plane-net BGA balls onto their planes
    pcb_export_dsn         one leg of the loop: `kicad-cli pcb export dsn`
    pcb_import_ses         the other leg:      `kicad-cli pcb import-ses --ses`

They are NOT decorated here on purpose. The server owns the FastMCP instance, the process
lifetime and the tool namespace; this module owns only the argument marshalling and the
result shape, so it can be imported (and unit-driven) without a server at all.

DEPENDENCY INJECTION
    `configure(ctx)` is called once by the server with an object *or* dict carrying
    KICAD_CLI, VENV_PY, REPO_ROOT, run, cli_env, need, out_dir, out_path, preflight,
    read_json.  Nothing here reads the environment for layout, so a server pointed at
    another worktree (PHICAD_REPO/PHICAD_BUILD) drags these tools along with it.

WHICH INTERPRETER RUNS THE SCRIPTS
    All three scripts under `phicad/autoroute/` are stdlib-only: cleanup.py and stitch.py
    import argparse/json/math/os/re/subprocess/tempfile/uuid plus the local `cli_path`,
    freeroute.py adds the in-repo `phicad/pylib/sexp.py`.  No kiutils, no kipy, no venv
    package is touched -- freeroute.py's own README says so ("Python: stdlib only").  So
    any interpreter works; `build/venv/bin/python` (VENV_PY) is preferred simply because it
    is a known-modern one, with the system `python3` as the fallback when the venv is
    absent.  This is the difference from the parts/swarm tools, which genuinely need the
    venv's third-party packages.

NOTHING IS WRITTEN IN PLACE
    Every board-modifying tool defaults to a new file under the server's out dir
    (`<out>/autoroute/<stem>.routed.kicad_pcb`).  The input board is only touched when the
    caller explicitly asks for it (`out_board == board_path`).  cleanup.py and stitch.py
    can only edit a board in place, so `apply=True` copies the board -- with its
    `.kicad_pro` / `.kicad_dru` sidecars, without which DRC silently falls back to stock
    rules and the report is fiction -- and edits the copy.  The path acted on is always
    reported as `output_board`.

WHAT IS EVIDENCE AND WHAT IS NOT
    Freerouting exits 0 on a partial route, so no tool here returns "routed successfully".
    `ok` means the pipeline ran and produced the file it promised.  The routing quality is
    in the numbers: `unconnected_before`/`unconnected_after`, `violations_before/after`,
    `new_violation_types`, and the router's own `unrouted`/`violations` score.  Read them.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
from pathlib import Path
from typing import Any, Callable

# --------------------------------------------------------------------------------------
# Injected context (configure() fills these in; every tool refuses politely until then)
# --------------------------------------------------------------------------------------

KICAD_CLI: Path | None = None
VENV_PY: Path | None = None
REPO_ROOT: Path | None = None

_run: Callable[..., dict[str, Any]] | None = None
_cli_env: Callable[[], dict[str, str]] | None = None
_need: Callable[..., tuple[Path | None, dict[str, Any] | None]] | None = None
_out_dir: Callable[[str], Path] | None = None
_out_path: Callable[[str, str, str, str | None], Path] | None = None
_preflight: Callable[[], dict[str, Any] | None] | None = None
_read_json: Callable[[Path], Any] | None = None

#: Where freeroute.py looks for the router when nothing else says otherwise.
DEFAULT_JAR = Path.home() / ".kicad-mcp" / "freerouting.jar"

#: Sidecars that carry the rules a board was routed under; matched to a board by stem.
SIDECARS = (".kicad_pro", ".kicad_dru")

#: Slack added to a caller's router budget so DRC before/after and the SES import fit too.
FREEROUTE_SLACK_S = 600
#: cleanup.py re-runs DRC up to 8 times (each round asks the CLI what is still loose).
CLEANUP_TIMEOUT_S = 1800
#: stitch.py runs DRC once, or twice with --apply.
STITCH_TIMEOUT_S = 900
#: The DSN/SES legs are single kicad-cli invocations.
CLI_TIMEOUT_S = 900

_REQUIRED = ("KICAD_CLI", "VENV_PY", "REPO_ROOT", "run", "cli_env", "need",
             "out_dir", "out_path", "preflight", "read_json")


def _pick(ctx: Any, name: str) -> Any:
    """Read one dependency out of a dict-ish or attribute-ish context."""
    if isinstance(ctx, dict):
        return ctx.get(name)
    return getattr(ctx, name, None)


def configure(ctx: Any) -> dict[str, Any]:
    """Bind the server's helpers into this module. Called once, before any tool runs.

    Returns a small dict describing what was bound, so the server (or a driver script)
    can assert the wiring instead of discovering a missing helper inside a tool call.
    """
    global KICAD_CLI, VENV_PY, REPO_ROOT
    global _run, _cli_env, _need, _out_dir, _out_path, _preflight, _read_json

    missing = [n for n in _REQUIRED if _pick(ctx, n) is None]
    if missing:
        return {"ok": False, "error": "incomplete context", "missing": missing}

    KICAD_CLI = Path(_pick(ctx, "KICAD_CLI"))
    VENV_PY = Path(_pick(ctx, "VENV_PY"))
    REPO_ROOT = Path(_pick(ctx, "REPO_ROOT"))
    _run = _pick(ctx, "run")
    _cli_env = _pick(ctx, "cli_env")
    _need = _pick(ctx, "need")
    _out_dir = _pick(ctx, "out_dir")
    _out_path = _pick(ctx, "out_path")
    _preflight = _pick(ctx, "preflight")
    _read_json = _pick(ctx, "read_json")

    return {
        "ok": True,
        "kicad_cli": str(KICAD_CLI),
        "kicad_cli_exists": KICAD_CLI.exists(),
        "autoroute_dir": str(_autoroute_dir()),
        "script_python": _script_python(),
        "jar": _jar_path(),
        "java": shutil.which("java"),
        "tools": ["autoroute_freerouting", "autoroute_cleanup", "autoroute_stitch",
                  "pcb_export_dsn", "pcb_import_ses"],
    }


# --------------------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------------------


def _unconfigured() -> dict[str, Any] | None:
    if _run is None or REPO_ROOT is None:
        return {"ok": False,
                "error": "phicad.mcp.tools_autoroute is not configured",
                "hint": "the server must call tools_autoroute.configure(ctx) at import time"}
    return None


def _autoroute_dir() -> Path:
    root = REPO_ROOT if REPO_ROOT is not None else Path(__file__).resolve().parent.parent.parent
    return root / "phicad" / "autoroute"


def _script(name: str) -> tuple[Path | None, dict[str, Any] | None]:
    p = _autoroute_dir() / name
    if not p.exists():
        return None, {"ok": False, "error": f"script not found: {p}",
                      "hint": "phicad/autoroute/ is missing from this worktree"}
    return p, None


def _script_python() -> str:
    """Interpreter for the autoroute scripts (stdlib-only; see the module docstring)."""
    if VENV_PY is not None and Path(VENV_PY).exists():
        return str(VENV_PY)
    return shutil.which("python3") or sys.executable or "python3"


def _jar_path() -> str | None:
    """$FREEROUTING_JAR if set, else the default jar -- but only if the file is there."""
    env = os.environ.get("FREEROUTING_JAR", "").strip()
    if env and Path(env).expanduser().exists():
        return str(Path(env).expanduser())
    if DEFAULT_JAR.exists():
        return str(DEFAULT_JAR)
    return None


def _script_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    """Env for a script child: fork kicad-cli, jar passthrough, no proxy, build-dir flag."""
    env = _cli_env()                       # KICAD_RUN_FROM_BUILD_DIR=1, proxies stripped
    env["KICAD_CLI"] = str(KICAD_CLI)      # cli_path.find_cli() honours this first
    jar = _jar_path()
    if jar:
        env["FREEROUTING_JAR"] = jar       # explicit, so the child cannot disagree with us
    if extra:
        env.update(extra)
    return env


def _default_board_out(board: Path, requested: str | None, tag: str = "autoroute") -> Path:
    """<out>/autoroute/<stem>.routed.kicad_pcb unless the caller named something else."""
    return _out_path(tag, board.stem, ".routed.kicad_pcb", requested)


def _carry_sidecars(source: Path, out: Path) -> list[str]:
    """Copy `.kicad_pro` / `.kicad_dru` next to a board written elsewhere.

    A board finds its net classes in the sibling .kicad_pro and its custom rules in the
    sibling .kicad_dru, matched by file stem -- neither path is stored in the board.  A
    routed copy without them is DRC'd against stock defaults, which reports hundreds of
    violations against perfectly good copper (freeroute.py's carry_project() says the same
    thing at more length).  Never overwrites an existing sidecar.
    """
    if out.resolve() == source.resolve():
        return []
    carried = []
    for ext in SIDECARS:
        src, dst = source.with_suffix(ext), out.with_suffix(ext)
        if src.exists() and not dst.exists():
            try:
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy(src, dst)
                carried.append(str(dst))
            except OSError:
                pass
    return carried


def _working_copy(board: Path, requested: str | None,
                  suffix: str) -> tuple[Path, list[str], bool]:
    """A board to edit in place without editing the caller's board.

    cleanup.py and stitch.py have no -o: they rewrite the file they are given.  So unless
    the caller explicitly named the input board as the output, work on a copy under the
    server's out dir and hand back its path.  Returns (path, carried_sidecars, is_input).
    """
    if requested:
        target = _out_path("autoroute", board.stem, suffix, requested)
    else:
        target = _out_dir("autoroute") / f"{board.stem}{suffix}"
    if target.resolve() == board.resolve():
        return board, [], True
    shutil.copy(board, target)
    return target, _carry_sidecars(board, target), False


def _tail(text: str | None, lines: int = 40) -> list[str]:
    return [ln for ln in (text or "").splitlines() if ln.strip()][-lines:]


def _int(pattern: str, text: str) -> int | None:
    m = re.search(pattern, text)
    return int(m.group(1)) if m else None


def _float(pattern: str, text: str) -> float | None:
    m = re.search(pattern, text)
    return float(m.group(1)) if m else None


def _section(text: str, head: str) -> str:
    """The balanced `(head ...)` block, or "". A DSN carries `(net ...)` in two places.

    Counting `(net` over the whole file counts every wire in `(wiring ...)` too -- on a
    173-net demo that reads as 989 nets, which is not a small error, it is a wrong answer.
    """
    i = text.find(f"({head}")
    if i < 0:
        return ""
    depth = 0
    for j in range(i, len(text)):
        if text[j] == "(":
            depth += 1
        elif text[j] == ")":
            depth -= 1
            if depth == 0:
                return text[i:j + 1]
    return text[i:]


def _drc_delta(before: dict[str, Any] | None, after: dict[str, Any] | None,
               res: dict[str, Any]) -> None:
    """Lift freeroute.py's own drc_before/drc_after blocks to top-level counters."""
    if not (isinstance(before, dict) and isinstance(after, dict)):
        return
    res["unconnected_before"] = before.get("unconnected")
    res["unconnected_after"] = after.get("unconnected")
    res["violations_before"] = before.get("violations")
    res["violations_after"] = after.get("violations")
    b_types = before.get("by_type") or {}
    a_types = after.get("by_type") or {}
    res["new_violation_types"] = {
        k: v - b_types.get(k, 0) for k, v in a_types.items() if v - b_types.get(k, 0) > 0
    }


# --------------------------------------------------------------------------------------
# The Specctra legs — plain kicad-cli, flags verified against
# qa/tests/cli/test_pcb_specctra.py and `kicad-cli pcb {export dsn,import-ses} --help`.
# --------------------------------------------------------------------------------------


def pcb_export_dsn(board_path: str, out_dsn: str | None = None) -> dict[str, Any]:
    """Export a board as a Specctra DSN design for an external autorouter.

    `kicad-cli pcb export dsn -o <out.dsn> <board>`. The DSN is what a router consumes:
    structure/network/placement sections over the board's real copper layers and nets.
    Read-only with respect to the board. Returns the .dsn path plus a section/count probe
    (nets, pin references, placements) so a caller can see the design actually carried.
    """
    if (err := _unconfigured()):
        return err
    if (err := _preflight()):
        return err
    board, err = _need(board_path, ".kicad_pcb")
    if err:
        return err

    dsn = _out_path("autoroute", board.stem, ".dsn", out_dsn)
    res = _run([str(KICAD_CLI), "pcb", "export", "dsn", "-o", str(dsn), str(board)],
               timeout=CLI_TIMEOUT_S, env=_cli_env())
    res["board"] = str(board)
    res["dsn_path"] = str(dsn) if dsn.exists() else None
    res["dsn_bytes"] = dsn.stat().st_size if dsn.exists() else 0
    res["ok"] = bool(res.get("ok")) and dsn.exists() and res["dsn_bytes"] > 0

    if dsn.exists():
        text = dsn.read_text(errors="replace")
        network = _section(text, "network")
        structure = _section(text, "structure")
        res["report"] = {
            "nets": len(re.findall(r"\(net\s+\S", network)),
            "pin_refs": sum(len(m.split()) for m in re.findall(r"\(pins ([^)]*)\)", network)),
            "net_classes": len(re.findall(r"\(class\s+\S", network)),
            "placements": len(re.findall(r"\(place\s+\S", _section(text, "placement"))),
            "copper_layers": re.findall(r"\(layer (\S+)", structure),
            "has_structure": "(structure" in text,
            "has_network": "(network" in text,
            "has_placement": "(placement" in text,
        }
    res.pop("stdout", None)
    return res


def pcb_import_ses(board_path: str, ses_path: str,
                   out_board: str | None = None) -> dict[str, Any]:
    """Merge a Specctra session (.ses) back into a board: the return leg of the loop.

    `kicad-cli pcb import-ses --ses <s.ses> [-o <out>] <board>`. A session is the COMPLETE
    routing, not an addition: it replaces every unlocked track on the board. Writes a new
    board under the server's out dir unless out_board names the input board explicitly, and
    carries the project sidecars across so the result can be DRC'd against its own rules.
    """
    if (err := _unconfigured()):
        return err
    if (err := _preflight()):
        return err
    board, err = _need(board_path, ".kicad_pcb")
    if err:
        return err
    ses, err = _need(ses_path, ".ses")
    if err:
        return err

    out = _default_board_out(board, out_board)
    in_place = out.resolve() == board.resolve()

    argv = [str(KICAD_CLI), "pcb", "import-ses", "--ses", str(ses)]
    if not in_place:
        argv += ["-o", str(out)]
    argv.append(str(board))

    res = _run(argv, timeout=CLI_TIMEOUT_S, env=_cli_env())
    res["board"] = str(board)
    res["ses"] = str(ses)
    res["in_place"] = in_place
    res["output_board"] = str(out) if out.exists() else None
    res["ok"] = bool(res.get("ok")) and out.exists()
    if out.exists():
        res["carried_project_files"] = _carry_sidecars(board, out)
        text = out.read_text(errors="replace")
        res["report"] = {"segments": text.count("(segment"),
                         "arcs": text.count("(arc "),
                         "vias": len(re.findall(r"\(via\b", text))}
    return res


# --------------------------------------------------------------------------------------
# The whole loop — phicad/autoroute/freeroute.py, which speaks --json
# --------------------------------------------------------------------------------------


def autoroute_freerouting(board_path: str, out_board: str | None = None, passes: int = 100,
                          threads: int | None = None, timeout_s: int = 3600,
                          drc: bool = True, keep_dir: str | None = None,
                          refill_zones: bool = True, cleanup: bool = False) -> dict[str, Any]:
    """Route a placed board with Freerouting, headless: DSN out, router, SES back in.

    Wraps phicad/autoroute/freeroute.py --json, which does the whole loop (export dsn ->
    freerouting.jar -Djava.awt.headless=true -> import-ses) and carries the project
    sidecars to the routed board. Writes <out>/autoroute/<stem>.routed.kicad_pcb unless
    out_board says otherwise; pass out_board == board_path to route in place.

    THE EXIT CODE IS NOT THE RESULT. Freerouting exits 0 on a partial route, so judge it by
    the numbers this returns: `unconnected_before`/`unconnected_after`,
    `violations_before`/`violations_after` and `new_violation_types` (drc=True, the fork's
    own DRC before and after), and `router` -- Freerouting's own final score with its
    unrouted/violations count. keep_dir keeps the .dsn, .ses and the router's full log so
    any step can be re-run by hand.

    Two post-import steps decide whether the board you get back passes DRC at all:
    `refill_zones` (default true) refills the pours, because new copper leaves every zone
    filled against the OLD copper -- worth 23 violations to 0 on a measured example board;
    `cleanup` (default false, DESTRUCTIVE) then deletes the router's dangling vias/tracks
    and widens sub-minimum necks, which is the last violation class standing after a refill.
    With both, this returns a board the fork's own DRC calls clean.

    Needs a JRE on PATH and freerouting.jar ($FREEROUTING_JAR, else ~/.kicad-mcp/).
    """
    if (err := _unconfigured()):
        return err
    if (err := _preflight()):
        return err
    board, err = _need(board_path, ".kicad_pcb")
    if err:
        return err
    script, err = _script("freeroute.py")
    if err:
        return err

    java = shutil.which("java")
    if not java:
        return {"ok": False, "error": "no java on PATH",
                "hint": "Freerouting is a JVM application: install a JRE (brew install openjdk)"}
    jar = _jar_path()
    if not jar:
        return {"ok": False, "error": f"freerouting.jar not found at {DEFAULT_JAR}",
                "hint": "set FREEROUTING_JAR, or download the jar from "
                        "https://github.com/freerouting/freerouting/releases"}
    if passes < 1:
        return {"ok": False, "error": "passes must be >= 1"}

    out = _default_board_out(board, out_board)
    in_place = out.resolve() == board.resolve()

    argv = [_script_python(), str(script), str(board), "--json",
            "--passes", str(passes), "--timeout", str(timeout_s), "--jar", jar]
    if in_place:
        argv.append("--in-place")
    else:
        argv += ["-o", str(out)]
    if threads:
        argv += ["--threads", str(threads)]
    if drc:
        argv.append("--drc")
    if not refill_zones:
        argv.append("--no-refill")
    if cleanup:
        argv.append("--cleanup")
    keep: Path | None = None
    if keep_dir:
        keep = Path(keep_dir).expanduser()
        if not keep.is_absolute():
            keep = _out_dir("autoroute") / keep
        keep.mkdir(parents=True, exist_ok=True)
        argv += ["--keep", str(keep)]

    # The script kills the router at --timeout; give it room for DRC before/after + import.
    res = _run(argv, timeout=timeout_s + FREEROUTE_SLACK_S, env=_script_env())
    stdout = res.pop("stdout", "") or ""
    report = None
    try:
        report = json.loads(stdout)
    except (json.JSONDecodeError, ValueError):
        # freeroute.py prints progress on stderr and JSON on stdout; if it died early the
        # JSON never came, and its own diagnostics (on stderr) are the whole answer.
        if stdout.strip():
            res["stdout_tail"] = _tail(stdout)

    res["board"] = str(board)
    res["in_place"] = in_place
    res["output_board"] = str(out) if out.exists() else None
    res["passes"] = passes
    res["jar"] = jar
    res["java"] = java
    res["keep_dir"] = str(keep) if keep else None
    res["drc_ran"] = drc

    if report:
        res["report"] = report
        res["dsn_path"] = report.get("dsn")
        res["ses_path"] = report.get("ses")
        res["router"] = report.get("freerouting")
        res["copper_before"] = report.get("copper_before")
        res["copper_after"] = report.get("copper_after")
        _drc_delta(report.get("drc_before"), report.get("drc_after"), res)
        if keep:
            res["kept_files"] = sorted(str(p) for p in keep.iterdir() if p.is_file())
    res["ok"] = bool(res.get("ok")) and out.exists() and report is not None
    if not res["ok"] and "error" not in res:
        res["error"] = "freeroute.py did not produce a routed board (read stderr)"
    return res


# --------------------------------------------------------------------------------------
# Post-router passes — cleanup.py / stitch.py, which print prose (no --json)
# --------------------------------------------------------------------------------------


def autoroute_cleanup(board_path: str, apply: bool = False,
                      min_width_mm: float | None = None) -> dict[str, Any]:
    """Remove the debris an autorouter leaves behind: dangling vias/tracks, sub-min necks.

    Wraps phicad/autoroute/cleanup.py, which drives entirely off KiCad's own DRC report
    (with zone refill -- a via whose job is to drop a pad onto a plane is connected BY the
    fill, and against unfilled zones it reads as dangling). If DRC did not flag it, nothing
    is touched.

    apply=False (default) reports what it would do and writes nothing. apply=True works on
    a COPY of the board -- cleanup.py can only edit in place -- and returns its path as
    `output_board`; the caller's board is never modified. Iterates until the board stops
    changing (removing a via orphans the track that reached it). min_width_mm overrides the
    width necks are grown to; by default it is read out of the DRC message.
    """
    if (err := _unconfigured()):
        return err
    if (err := _preflight()):
        return err
    board, err = _need(board_path, ".kicad_pcb")
    if err:
        return err
    script, err = _script("cleanup.py")
    if err:
        return err

    target, carried, is_input = (board, [], True)
    if apply:
        target, carried, is_input = _working_copy(board, None, ".cleaned.kicad_pcb")

    argv = [_script_python(), str(script), str(target)]
    if apply:
        argv.append("--apply")
    if min_width_mm is not None:
        argv += ["--min-width", str(min_width_mm)]
    argv += ["--cli", str(KICAD_CLI)]

    res = _run(argv, timeout=CLEANUP_TIMEOUT_S, env=_script_env())
    stdout = res.pop("stdout", "") or ""

    report: dict[str, Any] = {
        "violations_before": _int(r"(\d+) violations", stdout),
        "unconnected_before": _int(r"(\d+) unconnected", stdout),
        "min_width_mm": min_width_mm if min_width_mm is not None
        else _float(r"necks to widen to ([\d.]+) mm", stdout),
    }
    if apply:
        report.update({
            "removed_dangling_vias": _int(r"removed (\d+) dangling via", stdout),
            "removed_dangling_tracks": _int(r"dangling via\(s\), (\d+) dangling track", stdout),
            "widened_necks": _int(r"widened (\d+) neck", stdout),
            "violations_after": _int(r"->\s+(\d+) violations", stdout),
            "errors_after": _int(r"violations, (\d+) errors", stdout),
            "unconnected_after": _int(r"errors, (\d+) unconnected", stdout),
            "disconnect_warning": "WARNING" in stdout,
        })
    else:
        report.update({
            "dangling_vias": _int(r"dangling vias (\d+)", stdout),
            "dangling_tracks": _int(r"dangling tracks (\d+)", stdout),
            "necks_to_widen": _int(r"necks to widen to [\d.]+ mm (\d+)", stdout),
            "nothing_to_do": "nothing to do" in stdout,
        })
        if report["nothing_to_do"]:
            # cleanup.py prints no per-kind counts when DRC flagged nothing to fix.
            report.update(dangling_vias=0, dangling_tracks=0, necks_to_widen=0)

    res.update({
        "board": str(board),
        "applied": bool(apply),
        "in_place": bool(apply and is_input),
        "output_board": str(target) if apply and target.exists() else None,
        "carried_project_files": carried,
        "report": report,
        "stdout_tail": _tail(stdout),
    })
    if apply and target.exists() and report.get("disconnect_warning"):
        # cleanup.py must never disconnect anything; if it says it did, that is a failure.
        res["ok"] = False
        res["error"] = "cleanup changed the unconnected count — inspect stdout_tail"
    else:
        res["ok"] = bool(res.get("ok")) and (target.exists() if apply else True)
    return res


def autoroute_stitch(board_path: str, planes: list[str], apply: bool = False,
                     pitch_mm: float = 0.8, via_mm: float = 0.40, drill_mm: float = 0.20,
                     clearance_mm: float = 0.15,
                     fanout_ref: str | None = None) -> dict[str, Any]:
    """Drop plane-net BGA balls onto their planes: the dogbone fanout the router skipped.

    Wraps phicad/autoroute/stitch.py. `planes` is a list of NET=LAYER strings, e.g.
    ["GND=In1.Cu", "3V3=In2.Cu"] -- one --plane per entry. Each ball gets a through via in
    a free diagonal gap between four balls plus a short trace to it; a candidate site is
    checked against the vias and pads already there and a ball is left alone when every
    diagonal is taken (it never forces one in).

    By default only pads DRC actually reports unconnected are touched. fanout_ref fans out
    EVERY plane-net pad of that footprint instead -- use it BEFORE routing, since fanout
    placed afterwards has to thread between tracks that already exist.

    apply=False reports and writes nothing. apply=True works on a COPY (stitch.py can only
    edit in place) and returns it as `output_board`; the caller's board is untouched.
    """
    if (err := _unconfigured()):
        return err
    if (err := _preflight()):
        return err
    board, err = _need(board_path, ".kicad_pcb")
    if err:
        return err
    script, err = _script("stitch.py")
    if err:
        return err
    if not planes:
        return {"ok": False, "error": "planes is required",
                "hint": 'e.g. planes=["GND=In1.Cu", "3V3=In2.Cu"]'}
    bad = [p for p in planes if "=" not in p or not all(p.split("=", 1))]
    if bad:
        return {"ok": False, "error": f"malformed plane spec(s): {bad}",
                "hint": "each entry is NET=LAYER, e.g. GND=In1.Cu"}

    target, carried, is_input = (board, [], True)
    if apply:
        target, carried, is_input = _working_copy(board, None, ".stitched.kicad_pcb")

    argv = [_script_python(), str(script), str(target)]
    for plane in planes:
        argv += ["--plane", plane]
    argv += ["--pitch", str(pitch_mm), "--via", str(via_mm),
             "--drill", str(drill_mm), "--clearance", str(clearance_mm)]
    if fanout_ref:
        argv += ["--fanout", fanout_ref]
    if apply:
        argv.append("--apply")
    argv += ["--cli", str(KICAD_CLI)]

    res = _run(argv, timeout=STITCH_TIMEOUT_S, env=_script_env())
    stdout = res.pop("stdout", "") or ""

    report: dict[str, Any] = {
        "planes": list(planes),
        "fanout_ref": fanout_ref,
        "unconnected_items_before": _int(r"(\d+) unconnected items", stdout),
        "pads_targeted": _int(r"unconnected: (\d+)", stdout)
        if not fanout_ref else _int(r"pads on .*: (\d+)", stdout),
        "dogbones_to_add": _int(r"dogbones to add\s*:\s*(\d+)", stdout),
        "no_free_diagonal": _int(r"no free diagonal\s*:\s*(\d+)", stdout) or 0,
    }
    if report["dogbones_to_add"] is None and report["pads_targeted"] == 0:
        # stitch.py returns before printing a count when there is nothing to stitch.
        report["dogbones_to_add"] = 0
    if apply:
        report.update({
            "unconnected_after": _int(r"->\s+(\d+) unconnected", stdout),
            "violations_after": _int(r"unconnected, (\d+) violations", stdout),
            "errors_after": _int(r"violations, (\d+) errors", stdout),
        })

    res.update({
        "board": str(board),
        "applied": bool(apply),
        "in_place": bool(apply and is_input),
        "output_board": str(target) if apply and target.exists() else None,
        "carried_project_files": carried,
        "report": report,
        "stdout_tail": _tail(stdout),
    })
    res["ok"] = bool(res.get("ok")) and (target.exists() if apply else True)
    return res


#: What the server registers. Kept as data so registration cannot drift from the module.
TOOLS = (autoroute_freerouting, autoroute_cleanup, autoroute_stitch,
         pcb_export_dsn, pcb_import_ses)
