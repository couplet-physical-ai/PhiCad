#!/usr/bin/env python3
"""phicad MCP server — fork-native chat-tool surface for PhiCad (a KiCad fork).

Every tool shells out to the fork's own binaries from `build/mac-dev` (never imports kipy
in-process: kicad-python pins protobuf<6, which conflicts with the MCP stack — that is why
this server lives in its own venv `build/mcp-venv` and talks to `build/venv` by subprocess).

Channels used (see phicad/ai-interfacing.md):
  * kicad-cli   — DRC/ERC, exports, diffs, renders, jobsets            (read-only, fork-native)
  * kicad_tools — s-expression board summary, via build/venv/bin/python (subprocess)
  * IPC API     — one managed `kicad-cli api-server` child + kipy scripts

HEADLESS SESSION: `api_server_start()` takes no file, and `session_open()` then loads documents
by path over IPC (OpenDocument). `session_board_query()` returns real geometry/connectivity from
the live session, so an agent inspects a board in the background instead of driving the user's
screen. Read HEADLESS_LIMITS before writing anything back; in particular note the kipy value-
semantics trap documented there, which makes a client-side mistake look like a server failure.

Run:  build/mcp-venv/bin/python phicad/mcp/server.py
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shlex
import signal
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from fastmcp import FastMCP

# --------------------------------------------------------------------------------------
# Layout — everything derived from this file's location, no cwd dependence.
# --------------------------------------------------------------------------------------

def _path_env(name: str, fallback: Path) -> Path:
    """An overridable path, so the server can be pointed at another worktree.

    Deriving everything from `__file__` assumes the checkout that holds this server also
    holds the build tree and the venv.  That is the exception here, not the rule: source
    and build routinely live in different worktrees, and a server started from one of them
    reported "automation venv missing" for every tool that needs it — which is all the
    interesting ones.  `phicad/bin/phicad` and `plugins/ai-view/design_commands.py` already
    read these same variables; this makes the third surface agree with the other two.
    """
    raw = os.environ.get(name, "").strip()
    return Path(raw).expanduser() if raw else fallback


MCP_DIR = Path(__file__).resolve().parent            # <repo>/phicad/mcp
REPO_ROOT = _path_env("PHICAD_REPO", MCP_DIR.parent.parent)
# build/<mac|linux>-dev and the bundle-vs-flat binary layout come from ONE place, shared with
# register.py / doctor.py / synth (phicad/pylib/build_layout.py). Taken from this file's own
# checkout, not PHICAD_REPO, so the helper always matches the server code it serves.
_PYLIB = str(MCP_DIR.parent / "pylib")
if _PYLIB not in sys.path:
    sys.path.append(_PYLIB)
import build_layout  # noqa: E402

BUILD_DIR = _path_env("PHICAD_BUILD", build_layout.default_build_dir(REPO_ROOT))
KICAD_CLI = _path_env("PHICAD_KICAD_CLI", build_layout.kicad_cli_in(BUILD_DIR))
VENV_PY = _path_env("PHICAD_VENV", REPO_ROOT / "build" / "venv") / "bin" / "python"
PHICAD_BIN = REPO_ROOT / "phicad" / "bin" / "phicad"
AI_VIEW_DIR = REPO_ROOT / "phicad" / "plugins" / "ai-view"

DEFAULT_TIMEOUT = 120
# Parsing an 80 MB board takes ~110 s, which raced DEFAULT_TIMEOUT and produced a flaky
# mix of "timeout" and "could not parse" for the same file.  Whole-file readers get this.
HEAVY_TIMEOUT = 600
# Cap on the stdout a *display* consumer gets.  Children that return JSON must not be
# truncated at all: a clipped document fails to parse and the error blames the worker.
STDOUT_DISPLAY_LIMIT = 20000
OUT_ROOT = Path(os.environ.get("PHICAD_MCP_OUT", tempfile.gettempdir())) / "phicad-mcp-out"


def _load_telemetry():
    """Optional, and guarded: this server must import even when the repo is partial.

    The path is appended, not inserted at 0, so a module in phicad/pylib can never
    shadow a same-named module the server or FastMCP already depends on.
    """
    try:
        pylib = str(REPO_ROOT / "phicad" / "pylib")
        if pylib not in sys.path:
            sys.path.append(pylib)
        import telemetry as _t
        return _t
    except Exception:
        return None


_telemetry = _load_telemetry()

# The DRC classifier is stdlib-only and shared with synth/verify.  Keep it on the same path
# regardless of whether optional telemetry imported successfully.
_PYLIB_DIR = REPO_ROOT / "phicad" / "pylib"
if str(_PYLIB_DIR) not in sys.path:
    sys.path.append(str(_PYLIB_DIR))
from drc_opens import classify_unconnected_report  # noqa: E402
from drc_parity import (  # noqa: E402
    classify_schematic_parity,
    has_no_connect_candidate,
)

# --------------------------------------------------------------------------------------
# Server instructions — the entry point an agent gets from `initialize`
#
# A chat client shows a model the tool NAMES and SCHEMAS and nothing else, so every rule that
# lives only in a docstring (or only in phicad/ai-interfacing.md) is invisible at the moment it
# matters.  This is that missing page: the five workflows, in order, plus the global contracts
# an agent cannot infer from a signature.  Keep it short and verifiable -- it is sent on every
# connection, and anything unverifiable here is worse than nothing.
# --------------------------------------------------------------------------------------

INSTRUCTIONS = """\
PhiCad (a KiCad 10.99 fork) as tools: DRC/ERC gates, exports, board synthesis, a live
headless editing session over the IPC API, and per-project git history. Call `phicad_status()`
first if anything looks unwired (it reports the built CLI, both venvs and every extension
module). No tool count is quoted here on purpose -- your tool list is the authority.

GLOBAL RULES (they apply to every tool)
* ABSOLUTE PATHS ONLY. A relative path is resolved against the SERVER's repo root, not your
  cwd -- so it can silently read a different file that happens to exist there. Always pass
  /full/path/to/board.kicad_pcb.
* Every tool returns a dict and never raises. `ok: true` is the only success signal; a failure
  carries `error` and usually `hint` naming the next step. Non-zero `exit_code` with
  `ok: true` happens only where a non-zero code is documented (see diffs below).
* UNITS ARE MILLIMETRES at every tool boundary, degrees for angles, canonical layer names
  ("F.Cu", "In1.Cu", "Edge.Cuts").
* `pcb_diff` / `sch_diff` / `gerber_diff`: exit code 5 means "the documents differ". That is a
  RESULT, not a failure -- read `differs`, not `exit_code`.
* JOURNALING IS OPT-IN. Mutating tools only record history inside a project that
  `project_init(path=...)` has initialised. Without it a result carries top-level
  `journaled: false` and nothing is versioned -- do not report changes as versioned then.
* Session tools mutate memory, not disk: `saved: false` until `session_save()`.
* HPGL plotting was REMOVED in KiCad/PhiCad 10.0 -- there is no pcb HPGL tool. Use
  pcb_export_ps, pcb_export_dxf or pcb_export_svg.

WORKFLOWS
1. DESIGN A BOARD FROM A DESCRIPTION
   synth_check(design_json)      validate the design IR first -- milliseconds, writes nothing
   synth_build(design_json, out_dir)   -> placed, routed KiCad project
   pcb_drc(board_path)           gate it (severity_all=True; schematic_parity=True if a .kicad_sch
                                 sits next to the board); read gate_status and the explicit
                                 real_open_count / zone_island_count / ambiguous_count fields
   pcb_render(board_path)        a PNG for the human
   synth_verify(board_path)      re-checks the built board against the IR's own gates
2. GATE AND REVIEW AN EXISTING BOARD
   board_summary -> pcb_drc -> sch_erc -> pcb_render / pcb_export_svg. Report the violation
   counts by severity and do not call a result clean unless pcb_drc.clean is true. An
   `uncertain` gate_status means the raw item is preserved but its object type was not proven.
3. FABRICATION PACK
   pcb_export_gerbers + pcb_export_drill + pcb_export_pos + sch_export_bom (+ pcb_export_step,
   pcb_export_ipc2581 or pcb_export_odb if asked). jobset_run(jobset_path, project_path) does a
   whole predefined pack in one call.
4. LIVE EDIT (headless, no GUI, no screen control)
   api_server_start()            once; takes no file. socket path is reported.
   session_open(path=<abs .kicad_pcb>)   copy_first=True unless the user consented to a rewrite
   session_board_query / session_list_items / session_get_item     read real geometry
   session_update_footprint(ref=..., x_mm=..., y_mm=...)   ABSOLUTE by default; relative=True
                                 treats x_mm/y_mm as a delta. session_batch_move for many.
   session_add_track / _via / _zone / _text / _graphic, session_delete_*, session_set_net
   session_refill_zones          zones created through the API are UNFILLED until you do
   session_save()                the only thing that touches the file (rewrites it in the
                                 fork's format 20260831, which stock KiCad 10.0.x will refuse)
   api_server_stop()             when done
   Every session tool fails with a hint if the server is not up or nothing is open.
5. VERSION A PROJECT (do this BEFORE editing, or nothing is journaled)
   project_init(path=<project dir or file>)   first, always
   then project_status / project_log / project_journal / project_diff / project_tag, and
   project_restore / project_undo_last to go back. Mutating tools auto-snapshot into that repo.

When a tool's flags are unclear, `kicad_cli_help("pcb export gerbers")` returns the fork
binary's own --help. `kicad_cli(...)` is the escape hatch for anything unexposed.
"""

mcp = FastMCP("phicad", instructions=INSTRUCTIONS)

# These modules are also copied into the installed AI View plugin, so they cannot rely on
# `phicad` being a Python package.  Import from their real directory on both surfaces.
sys.path.insert(0, str(AI_VIEW_DIR))
import project_history  # noqa: E402

# --------------------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------------------


def _cli_env() -> dict[str, str]:
    """Env for running the build-tree kicad-cli (it refuses to run without the flag)."""
    env = dict(os.environ)
    env["KICAD_RUN_FROM_BUILD_DIR"] = "1"
    # Never let an outbound HTTP proxy leak into CLI/venv children.
    for var in ("HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy", "HTTP_PROXY", "http_proxy"):
        env.pop(var, None)
    return env


def _run(
    argv: list[str],
    *,
    timeout: int = DEFAULT_TIMEOUT,
    env: dict[str, str] | None = None,
    cwd: Path | None = None,
    ok_codes: tuple[int, ...] = (0,),
    stdout_limit: int | None = STDOUT_DISPLAY_LIMIT,
) -> dict[str, Any]:
    """Run a subprocess, never raise: always return a structured result dict.

    `stdout_limit` trims the captured stdout for readability.  Pass ``None`` whenever the
    caller parses stdout as JSON -- trimming a JSON document silently corrupts it, which is
    how `session_list_items` came to fail at its own default limit.
    """
    started = time.monotonic()
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env if env is not None else _cli_env(),
            cwd=str(cwd) if cwd else None,
        )
    except FileNotFoundError as exc:
        return {
            "ok": False,
            "error": f"executable not found: {argv[0]}",
            "detail": str(exc),
            "command": shlex.join(argv),
        }
    except subprocess.TimeoutExpired:
        return {
            "ok": False,
            "error": f"timeout after {timeout}s",
            "command": shlex.join(argv),
            "timed_out": True,
        }
    out = proc.stdout
    clipped = stdout_limit is not None and len(out) > stdout_limit
    res = {
        "ok": proc.returncode in ok_codes,
        "exit_code": proc.returncode,
        "command": shlex.join(argv),
        "stdout": out[-stdout_limit:] if clipped else out,
        "stderr": proc.stderr[-8000:],
        "elapsed_s": round(time.monotonic() - started, 2),
    }
    if clipped:
        # Say so, so a parse failure downstream is never mistaken for a broken child.
        res["stdout_truncated"] = True
        res["stdout_bytes"] = len(out)
    return res


def _need(path: str, *suffixes: str) -> tuple[Path | None, dict[str, Any] | None]:
    """Resolve an input path to an absolute existing file, with a suffix sanity check."""
    if not path:
        return None, {"ok": False, "error": "empty path"}
    p = Path(path).expanduser()
    if not p.is_absolute():
        p = (REPO_ROOT / p).resolve()
    else:
        p = p.resolve()
    if not p.exists():
        return None, {"ok": False, "error": f"file not found: {p}"}
    if suffixes and p.suffix not in suffixes:
        return None, {
            "ok": False,
            "error": f"unexpected file type {p.suffix!r}, expected one of {list(suffixes)}",
            "path": str(p),
        }
    return p, None


def _preflight() -> dict[str, Any] | None:
    if not KICAD_CLI.exists():
        return {
            "ok": False,
            "error": f"kicad-cli not built at {KICAD_CLI}",
            "hint": "phicad/bin/phicad build kicad-cli",
        }
    return None


def _out_dir(tag: str) -> Path:
    d = OUT_ROOT / tag
    d.mkdir(parents=True, exist_ok=True)
    return d


def _out_path(tag: str, stem: str, suffix: str, requested: str | None) -> Path:
    if requested:
        p = Path(requested).expanduser()
        if not p.is_absolute():
            p = (OUT_ROOT / tag / p).resolve()
        p.parent.mkdir(parents=True, exist_ok=True)
        return p
    return _out_dir(tag) / f"{stem}{suffix}"


def _read_json(path: Path) -> Any | None:
    try:
        return json.loads(path.read_text())
    except Exception:
        return None


def _summarize_violations(blocks: list[dict[str, Any]]) -> dict[str, Any]:
    """Collapse a DRC/ERC JSON report's violation lists into severity/type counts."""
    by_severity: dict[str, int] = {}
    by_type: dict[str, int] = {}
    examples: list[dict[str, Any]] = []
    total = 0
    for entry in blocks:
        sev = str(entry.get("severity", "unknown"))
        typ = str(entry.get("type", "unknown"))
        by_severity[sev] = by_severity.get(sev, 0) + 1
        by_type[typ] = by_type.get(typ, 0) + 1
        total += 1
        if len(examples) < 10:
            examples.append(
                {
                    "type": typ,
                    "severity": sev,
                    "description": entry.get("description"),
                    "items": [i.get("description") for i in entry.get("items", [])][:3],
                }
            )
    return {
        "total": total,
        "by_severity": dict(sorted(by_severity.items())),
        "by_type": dict(sorted(by_type.items(), key=lambda kv: -kv[1])),
        "examples": examples,
    }


# Report bucket -> friendly name. `sch erc` nests everything under "sheets".
_BUCKET_NAMES = {"sheets": "violations"}


def _gate_report(report: dict[str, Any], keys: tuple[str, ...]) -> dict[str, Any]:
    """Build the violation summary for a DRC/ERC json report (keys = violation buckets)."""
    out: dict[str, Any] = {}
    grand_sev: dict[str, int] = {}
    grand_total = 0
    for key in keys:
        blocks = report.get(key) or []
        if isinstance(blocks, list):
            flat: list[dict[str, Any]] = []
            for b in blocks:
                # sch erc nests violations per sheet
                if isinstance(b, dict) and "violations" in b:
                    flat.extend(b.get("violations") or [])
                elif isinstance(b, dict):
                    flat.append(b)
            summary = _summarize_violations(flat)
            out[_BUCKET_NAMES.get(key, key)] = summary
            grand_total += summary["total"]
            for sev, n in summary["by_severity"].items():
                grand_sev[sev] = grand_sev.get(sev, 0) + n
    out["total_violations"] = grand_total
    out["by_severity"] = dict(sorted(grand_sev.items()))
    return out


# --------------------------------------------------------------------------------------
# Gates — DRC / ERC
# --------------------------------------------------------------------------------------


def _classify_parity_report(data: dict[str, Any], board: Path) -> dict[str, Any]:
    """Gather authoritative schematic evidence only for the one ignorable candidate shape."""
    export_evidence: dict[str, Any] = {
        "needed": has_no_connect_candidate(data),
        "attempted": False,
        "ok": None,
        "schematic": str(board.with_suffix(".kicad_sch")),
    }
    if not export_evidence["needed"]:
        classification = classify_schematic_parity(data, board, None)
    else:
        schematic = board.with_suffix(".kicad_sch")
        if not schematic.is_file():
            export_evidence.update({"ok": False, "error": "sibling schematic not found"})
            classification = classify_schematic_parity(data, board, None)
        else:
            export_evidence["attempted"] = True
            with tempfile.TemporaryDirectory(prefix="phicad-parity-") as tmp:
                netlist = Path(tmp) / "schematic.net"
                exported = _run([
                    str(KICAD_CLI), "sch", "export", "netlist", "--format", "kicadsexpr",
                    "-o", str(netlist), str(schematic),
                ])
                export_evidence.update({
                    "ok": bool(exported.get("ok") and netlist.is_file()),
                    "exit_code": exported.get("exit_code"),
                    "elapsed_s": exported.get("elapsed_s"),
                })
                if not export_evidence["ok"]:
                    export_evidence["error"] = (exported.get("error")
                                                  or exported.get("stderr")
                                                  or "schematic netlist was not written")
                classification = classify_schematic_parity(
                    data, board, netlist if export_evidence["ok"] else None)
    classification["evidence"]["schematic_netlist_export"] = export_evidence
    return classification


@mcp.tool
def pcb_drc(board_path: str, severity_all: bool = True, schematic_parity: bool = False,
            refill_zones: bool = False, units: str = "mm", all_track_errors: bool = False,
            save_board: bool = False, severity_error: bool = False,
            severity_warning: bool = False, severity_exclusions: bool = False,
            define_var: dict[str, str] | None = None) -> dict[str, Any]:
    """Run design-rule check on a .kicad_pcb with the fork's kicad-cli.

    Returns violation counts by severity and by rule type, plus the JSON report path.

    `schematic_parity=True` additionally answers "does this board still match its schematic?"
    -- the check to run after editing either side. It needs the sibling .kicad_sch, and the
    `schematic_parity` summary key is present ONLY when it was requested, so an absent key is
    never mistaken for "parity is clean". The raw KiCad findings and raw count remain intact.
    A separate effective count ignores only an exact generated schematic no-connect pseudo-net
    paired by UUID/reference/pad with an unnetted board pad; every other `net_conflict`, and any
    candidate lacking authoritative board/netlist evidence, remains an active failure.

    `refill_zones=True` refills zones first, which removes stale-fill false positives
    (`save_board=True` then writes the refilled board back -- the only mutating combination).
    `units` ('mm', 'in', 'mils') sets the report's coordinate units.
    """
    if (err := _preflight()):
        return err
    board, err = _need(board_path, ".kicad_pcb")
    if err:
        return err
    if units not in ("mm", "in", "mils"):
        return {"ok": False, "error": f"units must be mm, in or mils (got {units!r})"}
    if save_board and not refill_zones:
        return {"ok": False, "error": "save_board requires refill_zones=True",
                "hint": "kicad-cli refuses --save-board without --refill-zones"}
    report = _out_path("drc", board.stem, ".drc.json", None)
    argv = [str(KICAD_CLI), "pcb", "drc", "--format", "json", "-o", str(report),
            "--units", units]
    if severity_all:
        argv.append("--severity-all")
    for flag, on in (("--severity-error", severity_error),
                     ("--severity-warning", severity_warning),
                     ("--severity-exclusions", severity_exclusions),
                     ("--all-track-errors", all_track_errors),
                     ("--schematic-parity", schematic_parity),
                     ("--refill-zones", refill_zones),
                     ("--save-board", save_board)):
        if on:
            argv.append(flag)
    for key, value in (define_var or {}).items():
        argv += ["-D", f"{key}={value}"]
    argv.append(str(board))
    res = _run(argv)
    res["board"] = str(board)
    res["report_path"] = str(report) if report.exists() else None
    res["schematic_parity_requested"] = schematic_parity
    if save_board:
        # Refilled zones were written back into the input board: let the journal see it.
        res["mutated"] = [str(board)]
    data = _read_json(report) if report.exists() else None
    if data:
        buckets = ["violations", "unconnected_items"]
        if schematic_parity:
            buckets.append("schematic_parity")
        res["summary"] = _gate_report(data, tuple(buckets))

        # KiCad's raw report remains authoritative and untouched.  The separate classifications
        # make intentional special cases visible without silently turning unknown evidence into a
        # pass.  `clean` is intentionally false for a zone-only report; callers that want the
        # routing gate must use `no_real_opens` and handle `uncertain` explicitly.
        classification = classify_unconnected_report(data, board)
        res["unconnected_classification"] = classification
        res["raw_unconnected_items"] = data.get("unconnected_items", [])
        res["summary"]["unconnected_items"].update({
            "raw_count": classification["unconnected_item_count"],
            "connectivity_finding_count": classification["raw_count"],
            "real_open_count": classification["real_open_count"],
            "zone_island_count": classification["zone_island_count"],
            "ambiguous_count": classification["ambiguous_count"],
            "no_real_opens": classification["no_real_opens"],
            "classification_status": classification["status"],
            "classification_confidence": classification["confidence"],
        })

        ignored_parity = 0
        parity_status = "not_requested"
        if schematic_parity:
            parity = _classify_parity_report(data, board)
            res["schematic_parity_classification"] = parity
            res["raw_schematic_parity"] = parity["raw_items"]
            res["summary"].setdefault("schematic_parity", _summarize_violations([]))
            res["summary"]["schematic_parity"].update({
                "raw_count": parity["raw_count"],
                "ignored_intentional_no_connect_count": parity["ignored_count"],
                "effective_count": parity["effective_count"],
                "unresolved_count": parity["unresolved_count"],
                "classification_status": parity["status"],
                "by_classification": parity["by_classification"],
            })
            ignored_parity = parity["ignored_count"]
            parity_status = parity["status"]

        raw_total = res["summary"]["total_violations"]
        effective_total = raw_total - ignored_parity
        res["summary"]["raw_total_violations"] = raw_total
        res["summary"]["effective_total_violations"] = effective_total
        uncertain = (classification["status"] == "uncertain"
                     or parity_status == "uncertain")
        res["clean"] = bool(effective_total == 0 and classification["clean"]
                             and not uncertain)
        res["gate_status"] = (
            "clean" if res["clean"] else
            "uncertain" if uncertain else
            "zone_island_only" if classification["status"] == "zone_island_only" else
            "violations"
        )
        res["coordinate_units"] = data.get("coordinate_units")
        res["source"] = data.get("source")
    else:
        res["clean"] = False
        res["gate_status"] = "unknown"
    return res


@mcp.tool
def sch_erc(sch_path: str, severity_all: bool = True) -> dict[str, Any]:
    """Run electrical-rule check on a .kicad_sch with the fork's kicad-cli.

    Returns violation counts by severity and by rule type, plus the JSON report path.
    """
    if (err := _preflight()):
        return err
    sch, err = _need(sch_path, ".kicad_sch")
    if err:
        return err
    report = _out_path("erc", sch.stem, ".erc.json", None)
    argv = [str(KICAD_CLI), "sch", "erc", "--format", "json", "-o", str(report)]
    if severity_all:
        argv.append("--severity-all")
    argv.append(str(sch))
    res = _run(argv)
    res["schematic"] = str(sch)
    res["report_path"] = str(report) if report.exists() else None
    data = _read_json(report) if report.exists() else None
    if data:
        res["summary"] = _gate_report(data, ("sheets",))
        res["coordinate_units"] = data.get("coordinate_units")
        res["sheet_count"] = len(data.get("sheets") or [])
    return res


# --------------------------------------------------------------------------------------
# Visual + fabrication exports
# --------------------------------------------------------------------------------------


@mcp.tool
def pcb_render(board_path: str, out_png: str | None = None, side: str = "top",
               quality: str = "basic") -> dict[str, Any]:
    """Raytraced PNG render of a board (works headless). Returns the PNG path and size."""
    if (err := _preflight()):
        return err
    board, err = _need(board_path, ".kicad_pcb")
    if err:
        return err
    png = _out_path("render", board.stem, ".png", out_png)
    argv = [str(KICAD_CLI), "pcb", "render", "-o", str(png),
            "--side", side, "--quality", quality, str(board)]
    res = _run(argv)
    res["board"] = str(board)
    res["png_path"] = str(png) if png.exists() else None
    res["png_bytes"] = png.stat().st_size if png.exists() else 0
    res["ok"] = res.get("ok", False) and png.exists()
    return res


@mcp.tool
def pcb_export_svg(board_path: str, layers: str = "F.Cu,B.Cu,Edge.Cuts",
                   out_dir: str | None = None, mode_multi: bool = True) -> dict[str, Any]:
    """Export board layers to SVG (multi-file by default). `layers` is a comma list."""
    if (err := _preflight()):
        return err
    board, err = _need(board_path, ".kicad_pcb")
    if err:
        return err
    dest = Path(out_dir).expanduser().resolve() if out_dir else _out_dir(f"svg/{board.stem}")
    dest.mkdir(parents=True, exist_ok=True)
    argv = [str(KICAD_CLI), "pcb", "export", "svg", "-l", layers, "-o", str(dest)]
    if mode_multi:
        argv.append("--mode-multi")
    argv.append(str(board))
    res = _run(argv)
    files = sorted(str(p) for p in dest.glob("*.svg"))
    res.update({"board": str(board), "layers": layers, "out_dir": str(dest), "svg_files": files})
    res["ok"] = res.get("ok", False) and bool(files)
    return res


@mcp.tool
def pcb_export_gerbers(board_path: str, out_dir: str | None = None,
                       with_drill: bool = True) -> dict[str, Any]:
    """Export Gerbers (and, by default, Excellon drill files) for fabrication."""
    if (err := _preflight()):
        return err
    board, err = _need(board_path, ".kicad_pcb")
    if err:
        return err
    dest = Path(out_dir).expanduser().resolve() if out_dir else _out_dir(f"gerbers/{board.stem}")
    dest.mkdir(parents=True, exist_ok=True)
    res = _run([str(KICAD_CLI), "pcb", "export", "gerbers", "-o", str(dest) + os.sep, str(board)])
    res["board"] = str(board)
    res["out_dir"] = str(dest)
    if with_drill and res.get("ok"):
        res["drill"] = _run(
            [str(KICAD_CLI), "pcb", "export", "drill", "-o", str(dest) + os.sep, str(board)]
        )
    files = sorted(p.name for p in dest.iterdir() if p.is_file())
    res["files"] = files
    res["file_count"] = len(files)
    res["ok"] = res.get("ok", False) and bool(files)
    return res


@mcp.tool
def pcb_export_step(board_path: str, out_step: str | None = None,
                    subst_models: bool = True) -> dict[str, Any]:
    """Export a 3D STEP model of the board (OpenCascade). Returns the .step path.

    Note: kicad-cli exits 2 when some component 3D models are missing (a stock KiCad library
    install provides them; the build tree alone does not) but still writes a valid STEP of the
    board body. That is reported as ok=True with `missing_models` populated.
    """
    if (err := _preflight()):
        return err
    board, err = _need(board_path, ".kicad_pcb")
    if err:
        return err
    step = _out_path("step", board.stem, ".step", out_step)
    argv = [str(KICAD_CLI), "pcb", "export", "step", "--no-dnp", "-f"]
    if subst_models:
        argv.append("--subst-models")
    argv += ["-o", str(step), str(board)]
    res = _run(argv, ok_codes=(0, 2))
    missing = [ln.split("File not found: ", 1)[1].strip()
               for ln in (res.get("stdout") or "").splitlines()
               if "File not found: " in ln]
    res["board"] = str(board)
    res["step_path"] = str(step) if step.exists() else None
    res["step_bytes"] = step.stat().st_size if step.exists() else 0
    res["missing_models"] = missing[:20]
    res["missing_model_count"] = len(missing)
    res["ok"] = step.exists() and res["step_bytes"] > 0
    res.pop("stdout", None)
    return res


@mcp.tool
def sch_export_bom(sch_path: str, out_csv: str | None = None,
                   fields: str = "Reference,Value,Footprint,${QUANTITY},DNP") -> dict[str, Any]:
    """Export a grouped BOM CSV from a schematic. Returns the CSV path and row count."""
    if (err := _preflight()):
        return err
    sch, err = _need(sch_path, ".kicad_sch")
    if err:
        return err
    csv_path = _out_path("bom", sch.stem, ".bom.csv", out_csv)
    res = _run([str(KICAD_CLI), "sch", "export", "bom", "--fields", fields,
                "-o", str(csv_path), str(sch)])
    res["schematic"] = str(sch)
    res["csv_path"] = str(csv_path) if csv_path.exists() else None
    if csv_path.exists():
        lines = [ln for ln in csv_path.read_text(errors="replace").splitlines() if ln.strip()]
        res["header"] = lines[0] if lines else None
        res["row_count"] = max(len(lines) - 1, 0)
        res["preview"] = lines[1:6]
    res["ok"] = res.get("ok", False) and csv_path.exists()
    return res


@mcp.tool
def pcb_export_stats(board_path: str, subtract_holes: bool = False) -> dict[str, Any]:
    """Board statistics as JSON (`kicad-cli pcb export stats --format json`).

    Returns the parsed report under `stats`: board size/area, component and pad counts per
    side and type, via counts, drill table, copper layers. Cheaper than board_summary when
    only numbers are needed, and it comes from KiCad itself rather than an s-expression parser.
    """
    if (err := _preflight()):
        return err
    board, err = _need(board_path, ".kicad_pcb")
    if err:
        return err
    report = _out_path("stats", board.stem, ".stats.json", None)
    argv = [str(KICAD_CLI), "pcb", "export", "stats", "--format", "json", "--units", "mm",
            "-o", str(report)]
    if subtract_holes:
        argv += ["--subtract-holes-from-board", "--subtract-holes-from-copper"]
    argv.append(str(board))
    res = _run(argv)
    res["board"] = str(board)
    res["report_path"] = str(report) if report.exists() else None
    data = _read_json(report) if report.exists() else None
    if isinstance(data, dict):
        res["stats"] = data
    res["ok"] = res.get("ok", False) and isinstance(data, dict)
    res.pop("stdout", None)
    return res


@mcp.tool
def pcb_export_pos(board_path: str, out_file: str | None = None, fmt: str = "csv",
                   units: str = "mm", side: str = "both", smd_only: bool = False,
                   exclude_dnp: bool = True) -> dict[str, Any]:
    """Pick-and-place (component position) file for assembly.

    `fmt` is ascii | csv | gerber; `side` is front | back | both (gerber supports one side
    only). Returns the file path, row count and a preview of the first rows.
    """
    if (err := _preflight()):
        return err
    board, err = _need(board_path, ".kicad_pcb")
    if err:
        return err
    fmt = (fmt or "csv").lower()
    if fmt not in ("ascii", "csv", "gerber"):
        return {"ok": False, "error": f"unknown fmt {fmt!r}", "supported": ["ascii", "csv", "gerber"]}
    if side not in ("front", "back", "both"):
        return {"ok": False, "error": f"unknown side {side!r}", "supported": ["front", "back", "both"]}
    if fmt == "gerber" and side == "both":
        side = "front"
    suffix = {"ascii": ".pos", "csv": ".pos.csv", "gerber": ".gbr"}[fmt]
    out = _out_path("pos", board.stem, suffix, out_file)
    argv = [str(KICAD_CLI), "pcb", "export", "pos", "--format", fmt, "--side", side,
            "-o", str(out)]
    if fmt != "gerber":
        argv += ["--units", units]
    if smd_only and fmt != "gerber":
        argv.append("--smd-only")
    if exclude_dnp:
        argv.append("--exclude-dnp")
    argv.append(str(board))
    res = _run(argv)
    res["board"] = str(board)
    res["format"] = fmt
    res["pos_path"] = str(out) if out.exists() else None
    if out.exists() and fmt != "gerber":
        lines = [ln for ln in out.read_text(errors="replace").splitlines() if ln.strip()]
        body = [ln for ln in lines if not ln.startswith("#")]
        res["row_count"] = max(len(body) - (1 if fmt == "csv" else 0), 0)
        res["preview"] = lines[:8]
    res["ok"] = res.get("ok", False) and out.exists()
    return res


@mcp.tool
def pcb_export_pdf(board_path: str, layers: str = "F.Cu,B.Cu,Edge.Cuts",
                   out_pdf: str | None = None, include_title_block: bool = True,
                   black_and_white: bool = False) -> dict[str, Any]:
    """Plot board layers into ONE multi-page PDF (one page per layer) for review.

    `layers` is a comma list of untranslated layer names. Returns the PDF path and size.
    """
    if (err := _preflight()):
        return err
    board, err = _need(board_path, ".kicad_pcb")
    if err:
        return err
    pdf = _out_path("pdf", board.stem, ".pdf", out_pdf)
    argv = [str(KICAD_CLI), "pcb", "export", "pdf", "-l", layers, "--mode-multipage",
            "-o", str(pdf)]
    if include_title_block:
        argv.append("--include-border-title")
    if black_and_white:
        argv.append("--black-and-white")
    argv.append(str(board))
    res = _run(argv)
    res["board"] = str(board)
    res["layers"] = layers
    res["pdf_path"] = str(pdf) if pdf.exists() else None
    res["pdf_bytes"] = pdf.stat().st_size if pdf.exists() else 0
    res["ok"] = res.get("ok", False) and res["pdf_bytes"] > 0
    return res


@mcp.tool
def sch_export_netlist(sch_path: str, out_file: str | None = None,
                       fmt: str = "kicadsexpr") -> dict[str, Any]:
    """Export a schematic netlist (`kicad-cli sch export netlist`).

    `fmt` is one of kicadsexpr | kicadxml | cadstar | orcadpcb2 | spice | spicemodel | pads |
    allegro. For kicadsexpr the result also carries `component_count` and `net_count` parsed
    from the file, which is the input the synth pipeline and `ImportNetlist` consume.
    """
    if (err := _preflight()):
        return err
    sch, err = _need(sch_path, ".kicad_sch")
    if err:
        return err
    fmt = (fmt or "kicadsexpr").lower()
    formats = ("kicadsexpr", "kicadxml", "cadstar", "orcadpcb2", "spice", "spicemodel",
               "pads", "allegro")
    if fmt not in formats:
        return {"ok": False, "error": f"unknown fmt {fmt!r}", "supported": list(formats)}
    suffix = {"kicadsexpr": ".net", "kicadxml": ".xml", "spice": ".cir", "spicemodel": ".cir",
              "cadstar": ".frp", "orcadpcb2": ".net", "pads": ".asc", "allegro": ".txt"}[fmt]
    out = _out_path("netlist", sch.stem, suffix, out_file)
    res = _run([str(KICAD_CLI), "sch", "export", "netlist", "--format", fmt,
                "-o", str(out), str(sch)])
    res["schematic"] = str(sch)
    res["format"] = fmt
    res["netlist_path"] = str(out) if out.exists() else None
    res["netlist_bytes"] = out.stat().st_size if out.exists() else 0
    if out.exists() and fmt == "kicadsexpr":
        text = out.read_text(errors="replace")
        res["component_count"] = len(re.findall(r"^\s*\(comp\b", text, re.M))
        res["net_count"] = len(re.findall(r"^\s*\(net\b", text, re.M))
    res["ok"] = res.get("ok", False) and res["netlist_bytes"] > 0
    return res


@mcp.tool
def sch_export_pdf(sch_path: str, out_pdf: str | None = None,
                   black_and_white: bool = False) -> dict[str, Any]:
    """Export the whole schematic hierarchy to a single PDF for review."""
    if (err := _preflight()):
        return err
    sch, err = _need(sch_path, ".kicad_sch")
    if err:
        return err
    pdf = _out_path("schpdf", sch.stem, ".pdf", out_pdf)
    argv = [str(KICAD_CLI), "sch", "export", "pdf", "-o", str(pdf)]
    if black_and_white:
        argv.append("--black-and-white")
    argv.append(str(sch))
    res = _run(argv)
    res["schematic"] = str(sch)
    res["pdf_path"] = str(pdf) if pdf.exists() else None
    res["pdf_bytes"] = pdf.stat().st_size if pdf.exists() else 0
    res["ok"] = res.get("ok", False) and res["pdf_bytes"] > 0
    return res


# --------------------------------------------------------------------------------------
# Semantic diffs — exit code 5 means "documents differ", not an error
# --------------------------------------------------------------------------------------


def _diff(kind: str, a: str, b: str, suffix: str) -> dict[str, Any]:
    if (err := _preflight()):
        return err
    pa, err = _need(a, suffix)
    if err:
        return err
    pb, err = _need(b, suffix)
    if err:
        return err
    report = _out_path("diff", f"{pa.stem}--{pb.stem}", f".{kind}diff.json", None)
    # `diff -o` is a FILE path; without it the JSON goes to stdout. Keep both.
    argv = [str(KICAD_CLI), kind, "diff", "--format", "json", "-o", str(report), str(pa), str(pb)]
    # The JSON may come back on stdout instead of the report file: never clip it.
    res = _run(argv, ok_codes=(0, 5), stdout_limit=None)
    # exit 5 == "documents differ": a normal result, NOT a failure.
    res["a"] = str(pa)
    res["b"] = str(pb)
    res["differs"] = res.get("exit_code") == 5
    res["identical"] = res.get("exit_code") == 0
    payload = _read_json(report) if report.exists() else None
    if payload is None and (res.get("stdout") or "").lstrip().startswith(("{", "[")):
        try:
            payload = json.loads(res["stdout"])
        except Exception:
            payload = None
    if report.exists():
        res["report_path"] = str(report)
    if isinstance(payload, dict):
        changes = payload.get("changes") or []
        kinds: dict[str, int] = {}
        types: dict[str, int] = {}
        items: list[dict[str, Any]] = []
        for ch in changes:
            if not isinstance(ch, dict):
                continue
            k = str(ch.get("kind", "?"))
            t = str(ch.get("typeName", "?"))
            kinds[k] = kinds.get(k, 0) + 1
            types[t] = types.get(t, 0) + 1
            if len(items) < 20:
                items.append(
                    {
                        "kind": k,
                        "type": t,
                        "refdes": ch.get("refdes"),
                        "uuid": ch.get("id"),
                        "properties": [
                            {
                                "name": p.get("name"),
                                "before": (p.get("before") or {}).get("v"),
                                "after": (p.get("after") or {}).get("v"),
                            }
                            for p in (ch.get("properties") or [])
                            if isinstance(p, dict)
                        ][:8],
                    }
                )
        res["doc_type"] = payload.get("docType")
        res["change_count"] = len(changes)
        res["changes_by_kind"] = kinds
        res["changes_by_type"] = types
        res["changes"] = items
    res.pop("stdout", None)
    return res


@mcp.tool
def pcb_diff(a: str, b: str) -> dict[str, Any]:
    """Semantic diff of two .kicad_pcb files. `differs` is True when they are not identical
    (kicad-cli exits 5 in that case — that is a normal result, not a failure)."""
    return _diff("pcb", a, b, ".kicad_pcb")


@mcp.tool
def sch_diff(a: str, b: str) -> dict[str, Any]:
    """Semantic diff of two .kicad_sch files. `differs` is True when they are not identical
    (kicad-cli exit code 5)."""
    return _diff("sch", a, b, ".kicad_sch")


# --------------------------------------------------------------------------------------
# Jobsets — the repeatable agent -> fab handoff
# --------------------------------------------------------------------------------------


@mcp.tool
def jobset_run(jobset_path: str, project_path: str) -> dict[str, Any]:
    """Run a .kicad_jobset against a .kicad_pro project (so ${KIPRJMOD} resolves)."""
    if (err := _preflight()):
        return err
    jobset, err = _need(jobset_path, ".kicad_jobset")
    if err:
        return err
    project, err = _need(project_path, ".kicad_pro")
    if err:
        return err
    res = _run([str(KICAD_CLI), "jobset", "run", "--file", str(jobset), str(project)],
               cwd=project.parent)
    res["jobset"] = str(jobset)
    res["project"] = str(project)
    spec = _read_json(jobset)
    if isinstance(spec, dict):
        res["jobs"] = [j.get("type") for j in spec.get("jobs", []) if isinstance(j, dict)]
        res["job_descriptions"] = [j.get("description") for j in spec.get("jobs", [])
                                   if isinstance(j, dict)]
        res["output_paths"] = [(o.get("settings") or {}).get("output_path")
                               for o in spec.get("outputs", []) if isinstance(o, dict)]
    # kicad-cli prints a tally line like "2 jobs succeeded, 0 job failed" (ANSI-coloured).
    plain = re.sub(r"\x1b\[[0-9;]*m", "", res.get("stdout") or "")
    tally = re.search(r"(\d+) jobs? succeeded, (\d+) jobs? failed", plain)
    if tally:
        res["jobs_succeeded"] = int(tally.group(1))
        res["jobs_failed"] = int(tally.group(2))
        res["ok"] = res["jobs_failed"] == 0
    res["failures"] = [ln.strip() for ln in plain.splitlines()
                       if "Job failed" in ln or "File not found" in ln][:20]
    res["log_tail"] = plain[-3000:]
    res.pop("stdout", None)
    return res


# --------------------------------------------------------------------------------------
# Board summary — s-expression read via the kipy/kicad-tools venv (subprocess only)
# --------------------------------------------------------------------------------------

_SUMMARY_SNIPPET = r"""
import json, sys
from kicad_tools import PCB          # kicad-tools 0.20: PCB.load(path), not from_file()

pcb = PCB.load(sys.argv[1])
out = {"summary": pcb.summary()}     # title/revision/size/area/layers/counts/trace length

try:
    out["routing"] = {k: (list(v) if isinstance(v, set) else v)
                      for k, v in pcb.routing_status().items()}
except Exception as exc:
    out["routing_error"] = str(exc)

fps = []
try:
    for fp in list(pcb.footprints)[:500]:
        pos = getattr(fp, "position", None)
        fps.append({
            "reference": getattr(fp, "reference", None),
            "value": getattr(fp, "value", None),
            "footprint": getattr(fp, "name", None),
            "layer": getattr(fp, "layer", None),
            "position_mm": list(pos)[:2] if isinstance(pos, (tuple, list)) else None,
            "rotation": getattr(fp, "rotation", None),
            "dnp": getattr(fp, "dnp", None),
            "pads": len(getattr(fp, "pads", []) or []),
        })
except Exception as exc:
    out["footprint_error"] = str(exc)
out["footprint_list"] = fps

try:
    out["copper_layers"] = [l.name for l in pcb.layers.values() if l.type == "signal"]
    out["layer_count"] = len(pcb.layers)
except Exception:
    pass
try:
    out["net_names"] = sorted(str(getattr(n, "name", n)) for n in pcb.nets)[:100]
except Exception:
    pass
try:
    out["board_outline_segments"] = len(pcb.get_board_outline_segments() or [])
except Exception:
    pass
print(json.dumps(out, default=str))
"""


@mcp.tool
def board_summary(board_path: str) -> dict[str, Any]:
    """Structural summary of a board (footprints, tracks, vias, zones, nets) parsed with
    kicad-tools in the automation venv — no kicad-cli, no GUI, works on fork-format files."""
    board, err = _need(board_path, ".kicad_pcb")
    if err:
        return err
    if not VENV_PY.exists():
        return {"ok": False, "error": f"automation venv missing at {VENV_PY}",
                "hint": "phicad/bin/phicad venv"}
    # Whole-file parse of a possibly-huge board, and the answer is JSON: no clipping, and
    # a timeout that an 80 MB board can actually meet.
    res = _run([str(VENV_PY), "-c", _SUMMARY_SNIPPET, str(board)],
               timeout=HEAVY_TIMEOUT, stdout_limit=None)
    res["board"] = str(board)
    if res.get("ok") and res.get("stdout"):
        try:
            data = json.loads(res["stdout"])
        except json.JSONDecodeError as exc:
            res["ok"] = False
            res["error"] = f"could not parse summary output: {exc}"
            return res
        fps = data.pop("footprint_list", [])
        res["summary"] = data.pop("summary", {})
        res["details"] = data
        res["footprints"] = fps[:50]
        res["footprint_count"] = res["summary"].get("footprints", len(fps))
        res.pop("stdout", None)
    return res


# --------------------------------------------------------------------------------------
# IPC API — ONE managed api-server child
# --------------------------------------------------------------------------------------

DEFAULT_SOCKET = str(Path(tempfile.gettempdir()) / "phicad-mcp-api.sock")

# `documents` is OUR bookkeeping of what the managed session has open: the server only answers
# GetOpenDocuments while a document is actually open (see session_documents), so a client-side
# ledger is the only thing that survives an empty session.
_api: dict[str, Any] = {"proc": None, "socket": None, "file": None, "log": None,
                        "documents": []}

# Suffix -> the doc_type token understood by the session worker snippet.
_DOCTYPES = {".kicad_pcb": "pcb", ".kicad_sch": "sch", ".kicad_pro": "pro"}

# doc_type aliases accepted from callers.
_DOCTYPE_ALIASES = {
    "pcb": "pcb", "board": "pcb", "kicad_pcb": "pcb", ".kicad_pcb": "pcb",
    "sch": "sch", "schematic": "sch", "kicad_sch": "sch", ".kicad_sch": "sch",
    "pro": "pro", "project": "pro", "kicad_pro": "pro", ".kicad_pro": "pro",
}

# Verified behaviour of the headless (`kicad-cli api-server`) backend, returned with session
# tools so an agent cannot mistake a silent no-op for a successful edit.
HEADLESS_LIMITS = {
    "kipy_value_semantics_trap": (
        "THE #1 CAUSE OF 'my write did not apply'. kipy getters return a COPY, not a reference: "
        "Vector2.__init__ does proto.CopyFrom(). So `fp.position.x += 1000000` mutates a "
        "throwaway wrapper, leaves fp._proto untouched, and sends an UNCHANGED item — the "
        "server then truthfully answers ISC_OK and nothing moves. Assign the whole object back "
        "instead:  v = fp.position; v.x += 1000000; fp.position = v.  Verified both ways on one "
        "board: in-place edit read back 77310000 (no-op), setter assignment read back 81310000 "
        "(applied). An earlier PhiCad note blamed the headless backend for this; that was wrong."
    ),
    "footprint_and_group_updates": (
        "Work headless, contrary to an earlier note in this file. Footprint moves and field "
        "edits apply and persist to disk. pcbnew/api/api_handler_board.cpp (~line 445) does "
        "route PCB_FOOTPRINT_T / PCB_GROUP_T through commit Remove()+Add(), but that exists so "
        "a live editor view does not keep stale cached child geometry — it is not data loss. "
        "Two real consequences: the item is REPLACED, so anything the proto round-trip does not "
        "carry is reconstructed, and an ATTACHED GUI may render stale geometry until refreshed. "
        "Always read back to confirm (see kipy_value_semantics_trap)."
    ),
    "track_updates": (
        "Work headless — verified: track width 203200 -> 253200 survived update_items() and "
        "persisted to disk through session_save()."
    ),
    "stale_read_handles": (
        "get_board() returns a SNAPSHOT. After a successful write, re-reading through the same "
        "handle can still show the old value; call k.get_board() again for a fresh document, or "
        "save and diff the file."
    ),
    "one_project_at_a_time": (
        "The server holds ONE project. Opening any other document (even re-opening the same "
        "path) fails with 'already open' / 'because project X is already open' until the current "
        "one is closed — that is why session_open(close_others=True) is the default."
    ),
    "save_rewrites_file": (
        "session_save() REWRITES the file in the fork's format (verified: version 20200628 -> "
        "20260831) and touches the sibling .kicad_pro. Opening alone does NOT modify anything "
        "(byte-identical md5 after open) — only saving does."
    ),
    "save_as_unsupported": (
        "SaveDocument only accepts the BASENAME of the already-open document; a different name "
        "is rejected with 'the requested document X is not open'. There is no save-as."
    ),
    "schematic_reads": (
        "A .kicad_sch can be opened, but kipy exposes no schematic reader — session_board_query "
        "is PCB-only. Use sch_erc / sch_export_bom / sch_diff for schematic facts."
    ),
}


def _api_alive() -> bool:
    proc = _api.get("proc")
    return proc is not None and proc.poll() is None


def _pidfile_for(sock: Path) -> Path:
    return sock.with_name(sock.name + ".pid")


def _pid_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _api_socket_reachable(raw: str | None) -> bool:
    """Does something actually answer on this api-server socket?

    Used to tell a healthy backgrounded server (its launcher shell exited, so it is orphaned
    but serving) from a genuinely stranded one. A missing socket file means unreachable; a
    present one is probed, because a killed server can leave the file behind.
    """
    if not raw:
        return False
    path = Path(str(raw).replace("ipc://", "")).expanduser()
    if not path.exists():
        return False
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    probe.settimeout(1.5)
    try:
        probe.connect(str(path))
        return True
    except OSError:
        return False
    finally:
        probe.close()


def _foreign_api(sock: Path) -> dict[str, Any] | None:
    """A live api-server on this socket that THIS process did not spawn, or None.

    Every `phicad tool call` is a fresh process, so a server started by an earlier call is
    invisible in `_api`.  Reading the pidfile lets `api_server_status` report it and
    `api_server_stop` reap it instead of leaving an unreachable orphan.
    """
    pf = _pidfile_for(sock)
    try:
        pid = int(pf.read_text().strip())
    except (OSError, ValueError):
        return None
    if not _pid_running(pid):
        return None
    return {"pid": pid, "socket": str(sock), "pidfile": str(pf), "adopted": True}


def _doc_type_for(path: Path, requested: str) -> tuple[str | None, dict[str, Any] | None]:
    """Resolve the doc_type token for a path ('auto' -> infer from the suffix)."""
    req = (requested or "auto").strip().lower()
    if req in ("", "auto"):
        inferred = _DOCTYPES.get(path.suffix)
        if inferred is None:
            return None, {"ok": False,
                          "error": f"cannot infer document type from suffix {path.suffix!r}",
                          "supported_suffixes": sorted(_DOCTYPES)}
        return inferred, None
    token = _DOCTYPE_ALIASES.get(req)
    if token is None:
        return None, {"ok": False, "error": f"unknown doc_type {requested!r}",
                      "supported": sorted(set(_DOCTYPE_ALIASES))}
    return token, None


@mcp.tool
def api_server_start(file_path: str | None = None, socket: str | None = None,
                     wait_s: int = 20) -> dict[str, Any]:
    """Start the single managed `kicad-cli api-server` child — a HEADLESS PhiCad session.

    `file_path` is OPTIONAL: with no file the server comes up empty and documents are opened on
    demand with session_open() (that is the preferred flow — it keeps one long-lived session and
    never touches the user's screen). Passing a file behaves exactly as before, opening it at
    launch (verified non-destructive: the file is byte-identical afterwards).

    Refuses to start a second one. `socket` is a PLAIN filesystem path (the server wraps it
    into ipc:// itself); kipy clients get `ipc://<socket>` via kipy_exec.
    """
    if (err := _preflight()):
        return err
    if _api_alive():
        return {
            "ok": False,
            "error": "api-server already running — call api_server_stop() first",
            "pid": _api["proc"].pid,
            "socket": _api["socket"],
            "file": _api["file"],
        }
    target: Path | None = None
    doc_type: str | None = None
    if file_path:
        target, err = _need(file_path, ".kicad_pcb", ".kicad_sch", ".kicad_pro")
        if err:
            return err
        doc_type = _DOCTYPES.get(target.suffix)
    sock = socket or DEFAULT_SOCKET
    if sock.startswith("ipc://"):        # be forgiving, the server wants a plain path
        sock = sock[len("ipc://"):]
    sock_path = Path(sock).expanduser()
    sock_path.parent.mkdir(parents=True, exist_ok=True)
    # A server from an earlier process (each CLI call is its own process) still owns this
    # socket: say so with its pid instead of starting a second one that cannot bind.
    if (foreign := _foreign_api(sock_path)) is not None:
        return {
            "ok": False,
            "error": "an api-server started by another process is already on this socket",
            "hint": "api_server_stop() reaps it, or pass a different socket=",
            **foreign,
        }
    log_path = _out_dir("api") / "api-server.log"
    log = open(log_path, "w")
    argv = [str(KICAD_CLI), "api-server", "--socket", str(sock_path)]
    if target is not None:
        argv.append(str(target))
    proc = subprocess.Popen(
        argv, stdout=log, stderr=subprocess.STDOUT, env=_cli_env(), start_new_session=True,
    )
    docs: list[dict[str, Any]] = []
    if target is not None:
        docs.append({"path": str(target), "doc_type": doc_type, "basename": target.name,
                     "opened_by": "launch_argument", "opened_at": time.strftime("%FT%T")})
    _api.update({"proc": proc, "socket": str(sock_path), "file": str(target) if target else None,
                 "log": log_path, "documents": docs})
    try:
        _pidfile_for(sock_path).write_text(f"{proc.pid}\n")
    except OSError:
        pass          # advisory only; never fail a start over the pidfile

    # The socket appears before the server is ready — poll it, then poll Ping via kipy.
    deadline = time.monotonic() + wait_s
    appeared = False
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            return {"ok": False, "error": f"api-server exited with {proc.returncode}",
                    "log_path": str(log_path), "log_tail": log_path.read_text()[-2000:]}
        if sock_path.exists():
            appeared = True
            break
        time.sleep(0.2)
    ready = _api_ping(timeout=max(int(deadline - time.monotonic()), 5)) if appeared else None
    return {
        "ok": appeared,
        "pid": proc.pid,
        "socket": str(sock_path),
        "client_socket_url": f"ipc://{sock_path}",
        "file": str(target) if target else None,
        "empty_session": target is None,
        "documents": docs,
        "socket_appeared": appeared,
        "ping": ready,
        "log_path": str(log_path),
        "next": ("session_open('<absolute path>') to load a document" if target is None
                 else "session_board_query() to read the board"),
    }


_PING_SNIPPET = r"""
import json, os, sys, time
sys.path.insert(0, os.getcwd())
from kipy import KiCad
deadline = time.time() + float(sys.argv[1])
last = None
while time.time() < deadline:
    try:
        k = KiCad()
        k.ping()
        print(json.dumps({"ready": True}))
        sys.exit(0)
    except Exception as exc:
        last = f"{type(exc).__name__}: {exc}"
        time.sleep(0.5)
print(json.dumps({"ready": False, "last_error": last}))
"""


def _api_ping(timeout: int = 10) -> dict[str, Any]:
    if not VENV_PY.exists() or not _api.get("socket"):
        return {"ready": None, "reason": "no venv or no socket"}
    env = _cli_env()
    env["KICAD_API_SOCKET"] = f"ipc://{_api['socket']}"
    res = _run([str(VENV_PY), "-c", _PING_SNIPPET, str(timeout)],
               env=env, timeout=timeout + 20)
    try:
        return json.loads((res.get("stdout") or "").strip().splitlines()[-1])
    except Exception:
        return {"ready": False, "raw": res.get("stdout"), "stderr": res.get("stderr")}


# --------------------------------------------------------------------------------------
# Headless SESSION surface — open/inspect/save documents over IPC in the managed session.
#
# All of it runs through ONE snippet executed by build/venv/bin/python (kipy lives there and
# pins protobuf<6; this server must never import kipy in-process). argv: <subcommand> [args...],
# always prints a single JSON object on the last stdout line — kipy emits DeprecationWarnings on
# stderr, so the reader below scans for the last JSON line rather than trusting stdout wholesale.
# --------------------------------------------------------------------------------------

_SESSION_SNIPPET = r"""
import json, sys

NM = 1e6  # nanometres per millimetre


def emit(payload, code=0):
    print(json.dumps(payload, default=str))
    sys.exit(code)


try:
    from kipy import KiCad
    from kipy.board_types import BoardLayer
    from kipy.proto.common.commands.project_commands_pb2 import (
        CloseAllDocuments, CloseDocument, OpenDocument, OpenDocumentResponse, SaveDocument)
    from kipy.proto.common.commands.editor_commands_pb2 import (
        GetOpenDocuments, GetOpenDocumentsResponse)
    from kipy.proto.common.types.base_types_pb2 import (
        DOCTYPE_PCB, DOCTYPE_SCHEMATIC, DOCTYPE_PROJECT)
    from google.protobuf.empty_pb2 import Empty
except Exception as exc:
    emit({"ok": False, "error": "kipy import failed: %s: %s" % (type(exc).__name__, exc)}, 0)

DOCTYPES = {"pcb": DOCTYPE_PCB, "sch": DOCTYPE_SCHEMATIC, "pro": DOCTYPE_PROJECT}
DOCTYPE_NAMES = {DOCTYPE_PCB: "pcb", DOCTYPE_SCHEMATIC: "sch", DOCTYPE_PROJECT: "pro"}


def install_fork_compat():
    # The fork emits kiapi.board.types.ReferencePoint children inside footprints; kipy 0.8
    # has no wrapper for them and get_footprints() raises NotImplementedError on any board
    # that carries one. Same shim as plugins/ai-view/board_context.py (duplicated on purpose:
    # this snippet must stay self-contained).
    try:
        from kipy import board_types as _bt
        from kipy.proto.board import board_types_pb2
        from kipy.util import proto as _kproto
    except Exception:
        return False
    url = "type.googleapis.com/kiapi.board.types.ReferencePoint"
    if url in _kproto._any_urls:
        return False

    class ReferencePoint(_bt.BoardItem):
        def __init__(self, proto=None, proto_ref=None):
            self._proto = (proto_ref if proto_ref is not None
                           else board_types_pb2.ReferencePoint())
            if proto is not None:
                self._proto.CopyFrom(proto)

        @property
        def layer(self):
            return self._proto.layer

        @layer.setter
        def layer(self, value):
            self._proto.layer = value

    _kproto._any_urls[url] = board_types_pb2.ReferencePoint
    _bt._proto_to_object[board_types_pb2.ReferencePoint] = ReferencePoint
    return True


install_fork_compat()

sub = sys.argv[1]

try:
    kicad = KiCad()
    kicad.ping()
except Exception as exc:
    emit({"ok": False, "error": "cannot reach api-server: %s: %s" % (type(exc).__name__, exc)}, 0)


def doc_json(doc):
    return {
        "doc_type": DOCTYPE_NAMES.get(doc.type, int(doc.type)),
        "board_filename": doc.board_filename,
        "project_name": doc.project.name,
        "project_path": doc.project.path,
    }


# "no handler available for ... GetOpenDocuments" is what the server says when NO document is
# open (that handler is registered by the board handler). Translate it instead of leaking it.
NO_DOC = "no document is open in the session"


def get_board():
    try:
        return kicad.get_board(), None
    except Exception as exc:
        msg = str(exc)
        if "GetOpenDocuments" in msg or "at least one board" in msg:
            return None, NO_DOC
        return None, "%s: %s" % (type(exc).__name__, exc)


_layer_cache = {}


def layer_name(board, idx):
    # Canonical name ('F.Cu'), plus the board's own name when it differs.
    if idx in _layer_cache:
        return _layer_cache[idx]
    canon = None
    try:
        canon = BoardLayer.Name(idx)
        if canon.startswith("BL_"):
            canon = canon[3:].replace("_", ".")
    except Exception:
        canon = str(idx)
    try:
        own = board.get_layer_name(idx)
    except Exception:
        own = None
    if not own or own.lower() == canon.lower():
        out = canon
    else:
        out = "%s (%s)" % (canon, own)
    _layer_cache[idx] = out
    return out


if sub == "open":
    path, want, close_others = sys.argv[2], sys.argv[3], sys.argv[4] == "1"
    closed = False
    if close_others:
        try:
            kicad._client.send(CloseAllDocuments(), Empty)
            closed = True
        except Exception:
            pass
    req = OpenDocument()
    req.type = DOCTYPES[want]
    req.path = path
    try:
        resp = kicad._client.send(req, OpenDocumentResponse)
    except Exception as exc:
        emit({"ok": False, "error": "%s: %s" % (type(exc).__name__, exc),
              "closed_others": closed, "path": path}, 0)
    out = {"ok": True, "document": doc_json(resp.document), "closed_others": closed,
           "path": path}
    board, err = get_board()
    if board is None:
        out["board_readable"] = False
        out["board_note"] = err
    else:
        out["board_readable"] = True
        out["counts"] = {"footprints": len(board.get_footprints()),
                         "tracks": len(board.get_tracks()),
                         "nets": len(board.get_nets())}
    emit(out)

if sub == "documents":
    out = {"ok": True}
    live = []
    errors = {}
    for token, dt in DOCTYPES.items():
        try:
            resp = kicad._client.send(GetOpenDocuments(type=dt), GetOpenDocumentsResponse)
            for d in resp.documents:
                live.append(doc_json(d))
        except Exception as exc:
            errors[token] = str(exc)[:160]
    out["live_documents"] = live
    out["live_query_supported"] = bool(live) or not errors
    if errors:
        out["live_query_errors"] = errors
    emit(out)

if sub == "save":
    basename = sys.argv[2] if len(sys.argv) > 2 else ""
    board, err = get_board()
    if board is None:
        emit({"ok": False, "error": err}, 0)
    doc = board.document
    req = SaveDocument()
    req.document.type = doc.type
    req.document.board_filename = basename or doc.board_filename
    try:
        kicad._client.send(req, Empty)
    except Exception as exc:
        emit({"ok": False, "error": "%s: %s" % (type(exc).__name__, exc),
              "requested_basename": req.document.board_filename}, 0)
    emit({"ok": True, "saved": req.document.board_filename, "document": doc_json(doc)})

if sub == "close":
    all_docs = sys.argv[2] == "1"
    if all_docs:
        try:
            kicad._client.send(CloseAllDocuments(), Empty)
        except Exception as exc:
            emit({"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)}, 0)
        emit({"ok": True, "closed": "all"})
    board, err = get_board()
    if board is None:
        emit({"ok": True, "closed": "nothing", "note": err}, 0)
    doc = board.document
    req = CloseDocument()
    req.document.type = doc.type
    req.document.board_filename = doc.board_filename
    try:
        kicad._client.send(req, Empty)
    except Exception as exc:
        emit({"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)}, 0)
    emit({"ok": True, "closed": doc.board_filename, "document": doc_json(doc)})

if sub == "query":
    what = sys.argv[2]
    limit = int(sys.argv[3])
    board, err = get_board()
    if board is None:
        emit({"ok": False, "error": err,
              "hint": "session_open('<abs path>.kicad_pcb') first; a .kicad_pro alone does "
                      "NOT load a board"}, 0)
    out = {"ok": True, "what": what, "document": doc_json(board.document)}
    if what == "all":
        want = {"counts", "footprints", "tracks", "nets", "stackup", "title"}
    elif what in ("counts", "summary"):
        want = {"counts"}
    else:
        want = {what}

    if "counts" in want:
        counts = {}
        for name, fn in (("footprints", board.get_footprints), ("tracks", board.get_tracks),
                         ("vias", board.get_vias), ("zones", board.get_zones),
                         ("pads", board.get_pads), ("nets", board.get_nets),
                         ("shapes", board.get_shapes), ("text", board.get_text),
                         ("groups", board.get_groups), ("dimensions", board.get_dimensions)):
            try:
                counts[name] = len(fn())
            except Exception as exc:
                counts[name] = "error: %s" % type(exc).__name__
        try:
            counts["copper_layers"] = board.get_copper_layer_count()
        except Exception:
            pass
        out["counts"] = counts

    if "footprints" in want:
        items = []
        fps = board.get_footprints()
        for fp in fps[:limit]:
            try:
                items.append({
                    "reference": fp.reference_field.text.value,
                    "value": fp.value_field.text.value,
                    "layer": layer_name(board, fp.layer),
                    "position_mm": [round(fp.position.x / NM, 4), round(fp.position.y / NM, 4)],
                    "orientation_deg": round(fp.orientation.degrees, 3),
                    "locked": bool(fp.locked),
                    "id": fp.id.value,
                })
            except Exception as exc:
                items.append({"error": "%s: %s" % (type(exc).__name__, exc)})
        out["footprints"] = items
        out["footprints_total"] = len(fps)
        out["footprints_truncated"] = len(fps) > limit

    if "tracks" in want:
        items = []
        tracks = board.get_tracks()
        total_len = 0.0
        for t in tracks:
            try:
                total_len += t.length() / NM
            except Exception:
                pass
        for t in tracks[:limit]:
            try:
                items.append({
                    "layer": layer_name(board, t.layer),
                    "net": t.net.name,
                    "width_mm": round(t.width / NM, 4),
                    "start_mm": [round(t.start.x / NM, 4), round(t.start.y / NM, 4)],
                    "end_mm": [round(t.end.x / NM, 4), round(t.end.y / NM, 4)],
                    "length_mm": round(t.length() / NM, 4),
                    "id": t.id.value,
                })
            except Exception as exc:
                items.append({"error": "%s: %s" % (type(exc).__name__, exc)})
        out["tracks"] = items
        out["tracks_total"] = len(tracks)
        out["tracks_truncated"] = len(tracks) > limit
        out["track_length_total_mm"] = round(total_len, 3)

    if "nets" in want:
        try:
            names = sorted(n.name for n in board.get_nets())
            out["nets"] = names[:limit]
            out["nets_total"] = len(names)
            out["nets_truncated"] = len(names) > limit
        except Exception as exc:
            out["nets_error"] = "%s: %s" % (type(exc).__name__, exc)

    if "stackup" in want:
        try:
            su = board.get_stackup()
            layers = []
            for L in su.layers:
                layers.append({
                    "layer": layer_name(board, L.layer),
                    "type": int(L.type),
                    "thickness_mm": round(L.thickness / NM, 5),
                    "material": L.material_name,
                    "user_name": L.user_name,
                    "enabled": bool(L.enabled),
                })
            out["stackup"] = {"layer_count": len(layers), "layers": layers}
        except Exception as exc:
            out["stackup_error"] = "%s: %s" % (type(exc).__name__, exc)

    if "vias" in want:
        items = []
        for v in board.get_vias()[:limit]:
            try:
                items.append({
                    "position_mm": [round(v.position.x / NM, 4), round(v.position.y / NM, 4)],
                    "net": v.net.name,
                    "id": v.id.value,
                })
            except Exception as exc:
                items.append({"error": "%s: %s" % (type(exc).__name__, exc)})
        out["vias"] = items

    if "title" in want:
        try:
            tb = board.get_title_block_info()
            out["title_block"] = {"title": tb.title, "revision": tb.revision, "date": tb.date,
                                  "company": tb.company}
        except Exception as exc:
            out["title_block_error"] = "%s: %s" % (type(exc).__name__, exc)

    emit(out)

if sub == "edit":
    # edit <op> <REF> <args...>  -- every write is read back through a FRESH board handle
    # (get_board() is a snapshot) and reported as applied only when the read-back matches.
    op, ref = sys.argv[2], sys.argv[3]
    board, err = get_board()
    if board is None:
        emit({"ok": False, "error": err,
              "hint": "session_open('<abs path>.kicad_pcb') first"}, 0)

    def ref_of(fp):
        try:
            return fp.reference_field.text.value or ""
        except Exception:
            return ""

    def find(b, want):
        fps = b.get_footprints()
        hits = [fp for fp in fps if ref_of(fp).upper() == want.upper()]
        if not hits:
            known = sorted({ref_of(fp) for fp in fps if ref_of(fp)})
            emit({"ok": False, "error": "no footprint %r on the open board" % want,
                  "known_references": known[:200]}, 0)
        if len(hits) > 1:
            emit({"ok": False,
                  "error": "%d footprints share the reference %r" % (len(hits), want)}, 0)
        return hits[0]

    fp = find(board, ref)
    before = {"position_mm": [fp.position.x / NM, fp.position.y / NM],
              "orientation_deg": float(fp.orientation.degrees),
              "value": fp.value_field.text.value,
              "layer": layer_name(board, fp.layer)}

    if op == "move":
        x, y, absolute = float(sys.argv[4]), float(sys.argv[5]), sys.argv[6] == "1"
        from kipy.geometry import Vector2
        nx, ny = (x, y) if absolute else (before["position_mm"][0] + x,
                                          before["position_mm"][1] + y)
        # Assignment, never fp.position.x = ...: the getter hands back a copy.
        fp.position = Vector2.from_xy_mm(nx, ny)
        board.update_items(fp)
        got = find(kicad.get_board(), ref)
        gx, gy = got.position.x / NM, got.position.y / NM
        applied = abs(gx - nx) < 1e-4 and abs(gy - ny) < 1e-4
        emit({"ok": applied, "op": op, "reference": ref_of(got), "before": before,
              "requested_mm": [nx, ny], "after_mm": [round(gx, 4), round(gy, 4)],
              "applied": applied,
              "error": None if applied else "write did not apply (read-back differs)"})

    if op == "rotate":
        deg, relative = float(sys.argv[4]), sys.argv[5] == "1"
        from kipy.geometry import Angle
        new = (before["orientation_deg"] + deg) if relative else deg
        fp.orientation = Angle.from_degrees(new)
        board.update_items(fp)
        got = find(kicad.get_board(), ref)
        g = float(got.orientation.degrees)
        d = abs(g - new) % 360.0
        applied = min(d, 360.0 - d) < 1e-3
        emit({"ok": applied, "op": op, "reference": ref_of(got), "before": before,
              "requested_deg": new, "after_deg": round(g, 4), "applied": applied,
              "error": None if applied else "write did not apply (read-back differs)"})

    if op == "value":
        text = sys.argv[4]
        fp.value_field.text.value = text
        board.update_items(fp)
        got = find(kicad.get_board(), ref)
        g = got.value_field.text.value
        applied = g == text
        emit({"ok": applied, "op": op, "reference": ref_of(got), "before": before,
              "requested": text, "after": g, "applied": applied,
              "error": None if applied else "write did not apply (read-back differs)"})

    emit({"ok": False, "error": "unknown edit op %r" % op}, 0)

emit({"ok": False, "error": "unknown subcommand %r" % sub}, 0)
"""

_QUERY_WHATS = ("all", "counts", "summary", "footprints", "tracks", "nets", "stackup",
                "vias", "title")


def _session(*args: str, timeout: int = DEFAULT_TIMEOUT) -> dict[str, Any]:
    """Run one session subcommand in the automation venv and parse its JSON line.

    Same shape as _api_ping(): subprocess to VENV_PY with KICAD_API_SOCKET set, never an
    in-process kipy import.
    """
    if not VENV_PY.exists():
        return {"ok": False, "error": f"automation venv missing at {VENV_PY}",
                "hint": "phicad/bin/phicad venv"}
    if not _api_alive():
        return {"ok": False, "error": "no managed api-server running",
                "hint": "api_server_start() — the file argument is optional"}
    env = _cli_env()
    env["KICAD_API_SOCKET"] = f"ipc://{_api['socket']}"
    res = _run([str(VENV_PY), "-c", _SESSION_SNIPPET, *args], env=env, timeout=timeout,
               stdout_limit=None)
    if not res.get("ok") and res.get("timed_out"):
        return res
    # kipy warns on stderr; take the last JSON object printed on stdout.
    for line in reversed((res.get("stdout") or "").strip().splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                continue
    return {"ok": False, "error": "no JSON returned by the session worker",
            "exit_code": res.get("exit_code"), "stdout": (res.get("stdout") or "")[-1500:],
            "stderr": (res.get("stderr") or "")[-1500:]}


@mcp.tool
def session_open(path: str, doc_type: str = "auto", close_others: bool = True,
                 copy_first: bool = False) -> dict[str, Any]:
    """Open a document in the managed HEADLESS session over IPC (no GUI, no screen control).

    `doc_type` is auto-detected from the suffix (.kicad_pcb/.kicad_sch/.kicad_pro); the path is
    always resolved to an absolute path (the server requires one).

    `close_others=True` (default) sends CloseAllDocuments first: the server holds exactly ONE
    project, and without it even re-opening the same path fails with 'already open'. It discards
    unsaved in-session edits.

    `copy_first=True` copies the document and its siblings (.kicad_pro/.kicad_prl/.kicad_dru) to
    a scratch directory and opens THAT, so the user's file can never be rewritten.

    Opening is non-destructive (verified byte-identical), but session_save() rewrites the file in
    the fork's format (20260831) — see `rewrites_file` and `headless_limitations` in the result.
    A .kicad_pro alone does NOT load a board: open the .kicad_pcb for session_board_query().
    """
    target, err = _need(path, ".kicad_pcb", ".kicad_sch", ".kicad_pro")
    if err:
        return err
    token, err = _doc_type_for(target, doc_type)
    if err:
        return err
    original = target
    copied_to = None
    if copy_first:
        import shutil

        dest = _out_dir(f"session/{int(time.time())}-{target.stem}")
        for sibling in sorted(target.parent.glob(f"{target.stem}.*")):
            if sibling.is_file():
                try:
                    shutil.copy2(sibling, dest / sibling.name)
                except Exception:
                    pass
        target = dest / target.name
        if not target.exists():
            return {"ok": False, "error": f"copy_first failed to place {original.name} in {dest}"}
        copied_to = str(target)
    res = _session("open", str(target), token, "1" if close_others else "0")
    res.setdefault("path", str(target))
    res["doc_type"] = token
    res["absolute_path"] = str(target)
    res["requested_path"] = str(original)
    res["copied_first"] = bool(copy_first)
    if copied_to:
        res["copy_path"] = copied_to
        res["rewrites_file"] = False
        res["rewrites_file_warning"] = (
            f"SAFE: the session is on a COPY at {copied_to}; {original} is untouched."
        )
    else:
        res["rewrites_file"] = True
        res["rewrites_file_warning"] = (
            f"session_save() will REWRITE {target} (and its .kicad_pro) in the fork's file "
            f"format 20260831, which older KiCad cannot open. Opening alone changes nothing. "
            f"Use copy_first=True unless the user consented to the rewrite."
        )
    if res.get("ok"):
        entry = {"path": str(target), "doc_type": token, "basename": target.name,
                 "opened_by": "session_open", "opened_at": time.strftime("%FT%T"),
                 "board_readable": res.get("board_readable"),
                 "copy_of": str(original) if copied_to else None}
        docs: list[dict[str, Any]] = [] if close_others else list(_api.get("documents") or [])
        docs = [d for d in docs if d.get("path") != str(target)]
        docs.append(entry)
        _api["documents"] = docs
        _api["file"] = str(target)
        if token == "pro":
            sibling = target.with_suffix(".kicad_pcb")
            res["board_hint"] = (
                f"a project has no board handler — call session_open('{sibling}') to read the "
                f"board" if sibling.exists() else
                "a project document exposes no board; open the .kicad_pcb itself"
            )
    res["headless_limitations"] = HEADLESS_LIMITS
    return res


@mcp.tool
def session_documents() -> dict[str, Any]:
    """List what the managed headless session has open.

    `documents` is this server's own bookkeeping and is the authoritative answer: the server only
    registers the GetOpenDocuments handler while a document is actually open, so a live query on
    an empty session fails with 'no handler available' rather than returning an empty list.
    `live_documents` is the cross-check from the server when that query is answerable.
    """
    tracked = list(_api.get("documents") or [])
    out: dict[str, Any] = {
        "ok": True,
        "running": _api_alive(),
        "documents": tracked,
        "document_count": len(tracked),
        "socket": _api.get("socket"),
        "bookkeeping_note": (
            "`documents` is client-side bookkeeping by this MCP server. GetOpenDocuments is "
            "unavailable in an empty session (the handler ships with the board handler), and "
            "documents opened by a different client would not appear here."
        ),
    }
    if not _api_alive():
        out["documents"] = []
        out["document_count"] = 0
        out["note"] = "no managed api-server running — nothing can be open"
        return out
    live = _session("documents", timeout=60)
    out["live"] = live
    if live.get("ok"):
        out["live_documents"] = live.get("live_documents")
    return out


@mcp.tool
def session_save(basename: str | None = None) -> dict[str, Any]:
    """Save the document open in the managed headless session (SaveDocument over IPC).

    DESTRUCTIVE: this rewrites the file in the fork's format (verified 20200628 -> 20260831) and
    touches the sibling .kicad_pro. Only call it on a file the user consented to have rewritten
    (or one opened with session_open(copy_first=True)).

    `basename` must be the BASENAME of the already-open document — there is no save-as; a
    different name is rejected with 'the requested document X is not open'. Defaults to the open
    document's own name.
    """
    if basename and Path(basename).name != basename:
        return {"ok": False,
                "error": f"basename must be a bare filename, not a path: {basename!r}",
                "hint": f"pass {Path(basename).name!r}"}
    tracked = [d for d in (_api.get("documents") or []) if d.get("doc_type") == "pcb"]
    before = None
    target = Path(tracked[-1]["path"]) if tracked else None
    if target and target.exists():
        before = target.stat().st_mtime
    res = _session("save", basename or "")
    if res.get("ok") and target and target.exists():
        st = target.stat()
        res["file"] = str(target)
        res["file_bytes"] = st.st_size
        res["file_mtime_changed"] = before is None or st.st_mtime != before
        try:
            first = target.read_text(errors="replace")[:200]
            m = re.search(r"version\s+(\d+)", first)
            if m:
                res["file_version"] = m.group(1)
        except Exception:
            pass
    res["rewrites_file"] = True
    res["rewrite_warning"] = (
        "the saved file is now in the fork's format (20260831); stock KiCad 10.0.x will refuse "
        "or downgrade it"
    )
    if res.get("ok"):
        res["footprint_edit_warning"] = HEADLESS_LIMITS["footprint_and_group_updates"]
    return res


@mcp.tool
def session_board_query(what: str = "all", limit: int = 50) -> dict[str, Any]:
    """Read structured facts from the board LIVE in the managed headless session.

    This is the tool that replaces screenshotting the GUI: it returns real geometry and
    connectivity instead of pixels. `what` is one of
    all | counts (= summary) | footprints | tracks | nets | stackup | vias | title.
    `all` = counts + footprints + tracks + nets + stackup + title; via/pad listings stay opt-in
    so a dense board cannot blow up the response. `limit` caps every listing (clamped to 2000).

    Read-only — it never mutates or saves. Coordinates are millimetres; layers are canonical
    names ('F.Cu'), with the board's own name in parentheses when it differs. PCB only
    (kipy has no schematic reader): use sch_erc / sch_export_bom for schematics.
    """
    key = (what or "all").strip().lower()
    if key not in _QUERY_WHATS:
        return {"ok": False, "error": f"unknown `what`: {what!r}", "supported": list(_QUERY_WHATS)}
    limit = max(1, min(int(limit), 2000))
    res = _session("query", key, str(limit))
    res["limit"] = limit
    res["read_only"] = True
    if res.get("ok"):
        res["mutation_note"] = HEADLESS_LIMITS["footprint_and_group_updates"]
    return res


# THREE TOOLS DELIBERATELY REMOVED HERE: session_move_footprint, session_rotate_footprint,
# session_set_value.  Each was a strict subset of tools_session.session_update_footprint, and
# together with it they shipped a real trap: session_move_footprint was RELATIVE by default
# while session_update_footprint is ABSOLUTE, so the same verb meant two different frames of
# reference on one tool list -- an agent asking to "move U1 to 50 mm" had a coin-flip chance of
# moving it BY 50 mm.  session_update_footprint now carries `relative: bool = False`, covers
# rotation and value as well, verifies the same way, and states its frame of reference in its
# first paragraph.  The `edit` op in _SESSION_SNIPPET above is kept as _session()'s debugging
# path; no registered tool reaches it.


@mcp.tool
def session_close(all_documents: bool = True) -> dict[str, Any]:
    """Close the document(s) open in the managed headless session, leaving the server running.

    `all_documents=True` (default) sends CloseAllDocuments, which works whatever is open —
    including a project or schematic, whose specifier this server cannot read back. With False it
    closes just the open board. UNSAVED IN-SESSION EDITS ARE DISCARDED; call session_save() first
    if they matter. Use api_server_stop() to shut the server down too.
    """
    res = _session("close", "1" if all_documents else "0")
    if res.get("ok"):
        if all_documents:
            _api["documents"] = []
            _api["file"] = None
        else:
            closed = res.get("closed")
            _api["documents"] = [d for d in (_api.get("documents") or [])
                                 if d.get("basename") != closed]
            _api["file"] = (_api["documents"][-1]["path"] if _api["documents"] else None)
        res["documents"] = list(_api.get("documents") or [])
        res["unsaved_edits_discarded"] = True
    return res


@mcp.tool
def api_server_stop() -> dict[str, Any]:
    """Stop the managed api-server child (SIGTERM, then SIGKILL) and clean up its socket.

    Also reaps a server recorded in the pidfile beside the socket, so a server started by an
    earlier `phicad tool call` process can be stopped instead of orphaned.
    """
    proc = _api.get("proc")
    if proc is None:
        # Nothing of ours; adopt a pidfile-recorded server on the default socket if alive.
        sock_path = Path(_api.get("socket") or DEFAULT_SOCKET).expanduser()
        foreign = _foreign_api(sock_path)
        if foreign is None:
            return {"ok": True, "state": "no api-server was started by this server"}
        pid = int(foreign["pid"])
        try:
            os.killpg(os.getpgid(pid), signal.SIGTERM)
        except Exception:
            try:
                os.kill(pid, signal.SIGTERM)
            except Exception:
                pass
        for _ in range(50):
            if not _pid_running(pid):
                break
            time.sleep(0.2)
        if _pid_running(pid):
            try:
                os.killpg(os.getpgid(pid), signal.SIGKILL)
            except Exception:
                try:
                    os.kill(pid, signal.SIGKILL)
                except Exception:
                    pass
        for p in (sock_path, _pidfile_for(sock_path)):
            try:
                p.unlink(missing_ok=True)
            except Exception:
                pass
        return {"ok": True, "state": "stopped", "adopted": True, "pid": pid,
                "socket": str(sock_path),
                "note": "reaped an api-server started by another phicad process"}
    if proc.poll() is not None:
        info = {"ok": True, "state": "already exited", "exit_code": proc.returncode}
    else:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except Exception:
            proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except Exception:
                proc.kill()
            proc.wait(timeout=10)
        info = {"ok": True, "state": "stopped", "exit_code": proc.returncode, "pid": proc.pid}
    sock = _api.get("socket")
    if sock:
        info["socket"] = sock
        for p in (Path(sock), _pidfile_for(Path(sock))):
            try:
                p.unlink(missing_ok=True)
            except Exception:
                pass
    info["log_path"] = str(_api.get("log")) if _api.get("log") else None
    info["documents_dropped"] = list(_api.get("documents") or [])
    _api.update({"proc": None, "socket": None, "file": None, "documents": []})
    return info


@mcp.tool
def api_server_reap_orphans(dry_run: bool = True,
                            include_reachable: bool = False) -> dict[str, Any]:
    """Find and kill `kicad-cli api-server` processes that are orphaned AND unreachable.

    `api_server_stop` only reaps a server on ITS socket, so servers left on other sockets --
    by a crashed host, a killed worktree session, or a build that spawned its own -- stay
    stranded holding a board in memory, and `phicad mcp doctor` can only tell you to copy a
    raw `kill`. This is that cleanup as a real operation.

    Being orphaned (ppid 1) is NOT sufficient on its own: `phicad api-server` is normally
    started in the background, its launcher shell exits, and the perfectly healthy server it
    left behind is reparented to init. Killing on ppid alone therefore shoots down the live
    server the web panel and `kipy` are talking to. So a process is only reaped when its
    socket is also unreachable -- gone from disk, or refusing a connection -- which is exactly
    the condition the doctor warns about ("holds its ipc:// socket and the next managed
    session will collide").

    `dry_run=True` (the default) reports what it would kill. `include_reachable=True` also
    reaps orphans that still answer, for deliberately clearing every server on the machine.
    """
    found = subprocess.run(["pgrep", "-fl", "kicad-cli api-server"],
                           capture_output=True, text=True, timeout=30)
    candidates: list[dict[str, Any]] = []
    for line in (found.stdout or "").splitlines():
        parts = line.strip().split(None, 1)
        if not parts or not parts[0].isdigit():
            continue
        pid = int(parts[0])
        if pid == os.getpid():
            continue
        command = parts[1] if len(parts) > 1 else ""
        probe = subprocess.run(["ps", "-o", "ppid=", "-p", str(pid)],
                               capture_output=True, text=True, timeout=30)
        ppid = int(probe.stdout.strip()) if probe.stdout.strip().isdigit() else -1
        # A managed server keeps a real parent; ppid 1 means whoever started it is gone.
        if ppid != 1:
            continue
        socket_path = None
        tokens = command.split()
        if "--socket" in tokens:
            index = tokens.index("--socket") + 1
            if index < len(tokens):
                socket_path = tokens[index]
        reachable = _api_socket_reachable(socket_path)
        candidates.append({"pid": pid, "ppid": ppid, "socket": socket_path,
                           "reachable": reachable, "command": command[:200]})

    reapable = [c for c in candidates
                if include_reachable or not c["reachable"]]
    skipped = [c for c in candidates if c not in reapable]
    if not reapable:
        return {"ok": True, "orphans": candidates, "reapable": [], "killed": [],
                "skipped_reachable": skipped, "dry_run": bool(dry_run),
                "note": ("no unreachable orphans; "
                         f"{len(skipped)} orphaned but still serving (left alone)")
                        if skipped else "no orphaned api-server processes"}
    if dry_run:
        return {"ok": True, "orphans": candidates, "reapable": reapable, "killed": [],
                "skipped_reachable": skipped, "dry_run": True,
                "next": "re-run with dry_run=false to terminate the reapable ones"}

    killed: list[int] = []
    failed: list[dict[str, Any]] = []
    for entry in reapable:
        pid = int(entry["pid"])
        for sig in (signal.SIGTERM, signal.SIGKILL):
            if not _pid_running(pid):
                break
            try:
                os.killpg(os.getpgid(pid), sig)
            except Exception:
                try:
                    os.kill(pid, sig)
                except Exception:
                    pass
            for _ in range(25):
                if not _pid_running(pid):
                    break
                time.sleep(0.2)
        if _pid_running(pid):
            failed.append(entry)
            continue
        killed.append(pid)
        raw = entry.get("socket")
        if raw:
            # The socket may be given as ipc:///path or a plain path.
            sock = Path(str(raw).replace("ipc://", "")).expanduser()
            for path in (sock, _pidfile_for(sock)):
                try:
                    path.unlink(missing_ok=True)
                except Exception:
                    pass
    return {"ok": not failed, "orphans": candidates, "reapable": reapable,
            "killed": killed, "failed": failed, "skipped_reachable": skipped,
            "dry_run": False}


@mcp.tool
def api_server_status() -> dict[str, Any]:
    """Report the managed api-server child: pid, socket, opened file, liveness.

    When this process started no server, a live one recorded in the pidfile beside the socket
    is reported as `adopted` -- otherwise a fresh CLI process claims nothing is running while
    a server holds the socket.
    """
    proc = _api.get("proc")
    if proc is None:
        sock_path = Path(_api.get("socket") or DEFAULT_SOCKET).expanduser()
        if (foreign := _foreign_api(sock_path)) is not None:
            return {
                "ok": True, "running": True, "adopted": True, "pid": foreign["pid"],
                "exit_code": None, "socket": str(sock_path),
                "client_socket_url": f"ipc://{sock_path}", "file": None,
                "documents": [], "document_count": 0, "log_path": None,
                "note": "started by another phicad process; api_server_stop() can reap it",
            }
    return {
        "ok": True,
        "running": _api_alive(),
        "pid": proc.pid if proc else None,
        "exit_code": proc.poll() if proc else None,
        "socket": _api.get("socket"),
        "client_socket_url": f"ipc://{_api['socket']}" if _api.get("socket") else None,
        "file": _api.get("file"),
        "documents": list(_api.get("documents") or []),
        "document_count": len(_api.get("documents") or []),
        "log_path": str(_api["log"]) if _api.get("log") else None,
    }


@mcp.tool
def kipy_exec(script_path: str, args: list[str] | None = None,
              timeout_s: int = DEFAULT_TIMEOUT) -> dict[str, Any]:
    """Run a kipy script in the automation venv against the managed api-server.

    The script is executed by `build/venv/bin/python` with KICAD_API_SOCKET=ipc://<socket>,
    so plain `KiCad()` connects. Requires api_server_start() first. Never call
    `KiCad().check_version()` — kipy 0.8 is generated against 10.0.6 and raises against the
    10.99 fork; ordinary commands work.

    Headless traps (see HEADLESS_LIMITS): kipy getters return COPIES, so `item.position.x += n`
    silently sends an unchanged item and the server answers ISC_OK — assign the whole object
    back (`v = item.position; v.x += n; item.position = v`) and always read back to verify.
    `KiCad().get_board()` raises 'no handler available ... GetOpenDocuments' when no document is
    open, not just when the API is missing.
    """
    if not VENV_PY.exists():
        return {"ok": False, "error": f"automation venv missing at {VENV_PY}",
                "hint": "phicad/bin/phicad venv"}
    script, err = _need(script_path, ".py")
    if err:
        return err
    if not _api_alive():
        return {"ok": False, "error": "no api-server running — call api_server_start(file) first"}
    env = _cli_env()
    env["KICAD_API_SOCKET"] = f"ipc://{_api['socket']}"
    res = _run([str(VENV_PY), str(script), *(args or [])], env=env,
               timeout=timeout_s, cwd=script.parent)
    res["script"] = str(script)
    res["socket"] = env["KICAD_API_SOCKET"]
    res["board_file"] = _api.get("file")
    return res


# --------------------------------------------------------------------------------------
# Parts pipeline & layout swarm (phicad/parts, phicad/swarm)
# --------------------------------------------------------------------------------------

PARTS_DIR = REPO_ROOT / "phicad" / "parts"
SWARM_DIR = REPO_ROOT / "phicad" / "swarm"


def _venv_script(script: Path, argv: list[str], *, timeout: int = DEFAULT_TIMEOUT,
                 ok_codes: tuple[int, ...] = (0,), require_report: bool = True) -> dict[str, Any]:
    """Run a pipeline script and require its complete JSON object report.

    The structured stdout channel is captured without a display limit.  Only malformed-output
    diagnostics are clipped, because retaining a partial JSON document can make a failed child
    look successful to a caller.  ``ok_codes`` remains an input to ``_run`` for compatibility,
    but a pipeline child that exits nonzero is never a successful tool result.  Set
    ``require_report=False`` for scripts whose authoritative result is a file channel.
    """
    if not VENV_PY.exists():
        return {"ok": False, "error": f"automation venv missing at {VENV_PY}",
                "hint": "phicad/bin/phicad venv"}
    if not script.exists():
        return {"ok": False, "error": f"script not found: {script}"}
    env = _cli_env()
    env["KICAD_CLI"] = str(KICAD_CLI)
    res = _run([str(VENV_PY), str(script), *argv], timeout=timeout, env=env,
               ok_codes=ok_codes, stdout_limit=None)
    out = res.pop("stdout", "") or ""

    # File/exec/timeout failures do not have a child exit status or a report to parse.  Keep the
    # more useful error from _run instead of replacing it with a generic JSON diagnostic.
    if "exit_code" not in res:
        return res

    exit_code = res["exit_code"]
    if exit_code != 0:
        res["ok"] = False
        res.setdefault("error", f"pipeline script exited with code {exit_code}")

    if not require_report and not out.strip():
        return res

    try:
        report = json.loads(out)
    except (json.JSONDecodeError, TypeError, ValueError):
        if not require_report:
            if out.strip():
                res["stdout"] = out[-4000:]
            return res
        res["ok"] = False
        res["error"] = "pipeline script returned no valid JSON report"
        if out.strip():
            res["stdout"] = out[-4000:]
            if len(out) > 4000:
                res["stdout_truncated"] = True
                res["stdout_bytes"] = len(out)
        return res

    if not isinstance(report, dict):
        if not require_report:
            res["stdout"] = out[-4000:]
            return res
        res["ok"] = False
        res["error"] = "pipeline script returned JSON that is not an object report"
        return res

    res["report"] = report
    return res


@mcp.tool
def parts_fetch(project_dir: str, lcsc_ids: list[str] | None = None,
                parts_json: str | None = None, lib: str = "project") -> dict[str, Any]:
    """Fetch exact parts (symbol + footprint + 3D model) into a project library.

    Wraps phicad/parts/fetch_part.py: verified LCSC ids -> <project>/lib/ assets with
    ${KIPRJMOD}-relative 3D bindings + lib-table registration. Either pass lcsc_ids
    directly or parts_json (ledger mode: fetches rows with status approved/proposed and
    promotes them to fetched). Needs network (LCSC/EasyEDA API).
    """
    proj = Path(project_dir).expanduser()
    if not proj.is_absolute():
        proj = (REPO_ROOT / proj).resolve()
    if not proj.is_dir():
        return {"ok": False, "error": f"not a directory: {proj}"}
    argv = ["--project", str(proj), "--lib", lib]
    if lcsc_ids:
        argv += ["--lcsc", *lcsc_ids]
    if parts_json:
        pj, err = _need(parts_json, ".json")
        if err:
            return err
        argv += ["--parts", str(pj)]
    if not lcsc_ids and not parts_json:
        return {"ok": False, "error": "need lcsc_ids and/or parts_json"}
    return _venv_script(PARTS_DIR / "fetch_part.py", argv, timeout=600)


@mcp.tool
def parts_audit(board_path: str, parts_json: str | None = None,
                require_id: bool = False) -> dict[str, Any]:
    """Fab-readiness gate: every footprint has a resolvable 3D model + LCSC/MPN id.

    Wraps phicad/parts/audit_parts.py. ok=false means gaps were found — read
    report.failures / report.warnings. require_id escalates missing ids to failures.
    """
    board, err = _need(board_path, ".kicad_pcb")
    if err:
        return err
    argv = [str(board)]
    if parts_json:
        pj, err = _need(parts_json, ".json")
        if err:
            return err
        argv += ["--parts", str(pj)]
    if require_id:
        argv.append("--require-id")
    res = _venv_script(PARTS_DIR / "audit_parts.py", argv, ok_codes=(0, 1))
    report = res.get("report")
    if isinstance(report, dict):
        # A valid audit report carries the domain verdict, but the child status is an
        # independent transport gate: a nonzero child can never be promoted to ok=true.
        res["ok"] = (res.get("exit_code") == 0) and bool(report.get("ok"))
        res["failures"] = report.get("failures", [])[:20]
    return res


@mcp.tool
def swarm_partition(board_path: str, groups: int = 3,
                    include_power: bool = False) -> dict[str, Any]:
    """Partition a board's nets into N disjoint groups for parallel routing agents.

    Wraps phicad/swarm/partition_nets.py. Returns the plan (groups with nets/pads/bbox,
    power nets in zones_bucket) and writes plan.json next to the board for merge use.
    """
    board, err = _need(board_path, ".kicad_pcb")
    if err:
        return err
    plan_path = board.with_suffix(".plan.json")
    argv = [str(board), "--groups", str(groups), "-o", str(plan_path)]
    if include_power:
        argv.append("--include-power")
    res = _venv_script(SWARM_DIR / "partition_nets.py", argv, require_report=False)
    res["plan_path"] = str(plan_path) if plan_path.exists() else None
    if plan_path.exists():
        report = _read_json(plan_path)
        if isinstance(report, dict):
            res["report"] = report
        elif res.get("ok"):
            res["ok"] = False
            res["error"] = "partition script wrote no valid JSON plan"
    elif res.get("ok"):
        res["ok"] = False
        res["error"] = "partition script did not write a plan"
    return res


@mcp.tool
def swarm_score(board_path: str, project_path: str | None = None) -> dict[str, Any]:
    """Score a board candidate (lower = better) for best-of-N selection.

    Wraps phicad/swarm/score_board.py: 10000*unconnected + 1000*drc_err + 10*warn
    + 2*vias + 0.1*track_mm, from the fork's DRC plus a phicad/pylib/sexp.py parse.

    PASS project_path FOR SCRATCH-NAMED CANDIDATES. Design rules live in the
    .kicad_pro and KiCad pairs one with a board only by filename stem, so a
    candidate copied to cand-g0.kicad_pcb is otherwise judged by KiCad's DEFAULT
    rules: one real board scored 0 DRC errors inside its project and 13 copied
    out -- score 36.2 against 13036.2, on the number you are ranking by. The
    reply's `rules_source` says which rules produced the numbers ('explicit',
    'sibling', or 'kicad-defaults'), and only scores with the same
    `rules_source` and project are comparable with each other.
    """
    board, err = _need(board_path, ".kicad_pcb")
    if err:
        return err
    argv = [str(board), "--json"]
    if project_path:
        project, err = _need(project_path, ".kicad_pro")
        if err:
            return err
        argv += ["--project", str(project)]
    return _venv_script(SWARM_DIR / "score_board.py", argv, timeout=600)


@mcp.tool
def swarm_merge(base_board: str, plan_json: str, take: dict[str, str] | None = None,
                strip: list[str] | None = None, out_board: str | None = None,
                run_drc: bool = True) -> dict[str, Any]:
    """Merge per-group copper from swarm candidate boards into the base board.

    Wraps phicad/swarm/merge_routes.py (lossless text splice, replace semantics).
    take maps group id -> candidate board path (e.g. {"g0": "/tmp/sw/cand-g0.kicad_pcb"});
    strip removes groups' copper without replacement. Optionally DRC-gates the result.
    """
    base, err = _need(base_board, ".kicad_pcb")
    if err:
        return err
    plan, err = _need(plan_json, ".json")
    if err:
        return err
    out = Path(out_board).expanduser().resolve() if out_board \
        else _out_dir("swarm") / f"{base.stem}.merged.kicad_pcb"
    argv = [str(base), "--plan", str(plan), "-o", str(out)]
    for gid, cand in (take or {}).items():
        cpath, err = _need(cand, ".kicad_pcb")
        if err:
            return err
        argv += ["--take", f"{gid}={cpath}"]
    for gid in strip or []:
        argv += ["--strip", gid]
    res = _venv_script(SWARM_DIR / "merge_routes.py", argv, require_report=False)
    res["merged_path"] = str(out) if out.exists() else None
    if res.get("ok") and not out.exists():
        res["ok"] = False
        res["error"] = "merge script did not write a merged board"
    if res.get("ok") and run_drc and out.exists():
        res["drc"] = pcb_drc.fn(str(out))  # type: ignore[attr-defined]
        summary = (res["drc"] or {}).get("summary") or {}
        res["drc_total_violations"] = summary.get("total_violations")
    return res


# --------------------------------------------------------------------------------------
# Design synthesis (phicad/synth)
# --------------------------------------------------------------------------------------

SYNTH_CLI = REPO_ROOT / "phicad" / "synth" / "cli.py"


def _synth(argv: list[str], *, timeout: int = 900) -> dict[str, Any]:
    """Run `synth` under the automation venv and return its JSON report.

    The venv rather than any python3: `build` opens an IPC session, which needs kipy.
    `check` would run anywhere, but using one interpreter for both keeps a caller from
    discovering the difference the hard way.
    """
    if not VENV_PY.exists():
        return {"ok": False, "error": f"automation venv missing at {VENV_PY}",
                "hint": "phicad/bin/phicad venv"}
    if not SYNTH_CLI.exists():
        return {"ok": False, "error": f"synth CLI not found at {SYNTH_CLI}"}
    env = _cli_env()
    env["PHICAD_KICAD_CLI"] = str(KICAD_CLI)

    # Recorded with source="mcp" because this is a third way into the same compiler,
    # reached by an external agent rather than by the pane.  Agent-driven work is
    # exactly the activity a telemetry file is worth having for, and it would
    # otherwise be the largest invisible surface in the product.
    started = time.monotonic()
    command = argv[0] if argv else "?"

    # A failing gate is a result, not a crash: exit 1 carries the report that says which
    # gate failed and why, and swallowing it as an error would throw that away.
    res = _run([str(VENV_PY), str(SYNTH_CLI), *argv, "--json"],
               timeout=timeout, env=env, ok_codes=(0, 1), stdout_limit=None)
    out = res.pop("stdout", "") or ""
    if "exit_code" in res:
        try:
            report = json.loads(out)
        except (json.JSONDecodeError, TypeError, ValueError):
            res["ok"] = False
            res["error"] = "synth CLI returned no valid JSON report"
            res["stdout"] = out[-4000:]
            if len(out) > 4000:
                res["stdout_truncated"] = True
                res["stdout_bytes"] = len(out)
        else:
            res["report"] = report
            if not isinstance(report, dict):
                res["ok"] = False
                res["error"] = "synth CLI returned JSON that is not an object report"
            elif not isinstance(report.get("ok"), bool):
                res["ok"] = False
                res["error"] = "synth report is missing a boolean 'ok' verdict"
            else:
                res["ok"] = report["ok"]

    if _telemetry is not None:
        report = res.get("report")
        report_dict = report if isinstance(report, dict) else {}
        stages = report_dict.get("stages") or []
        failed = report_dict.get("failed") or []
        _telemetry.emit(
            "synth.run", command=command, source="mcp", streaming=False,
            timeout_s=float(timeout),
            duration_ms=round((time.monotonic() - started) * 1000),
            outcome=("ok" if report_dict.get("ok") is True else
                     "failed" if report_dict else "no_json"),
            stages_total=len(stages) or None,
            stages_passed=(len(stages) - len(failed)) if stages else None,
            failed_stages=failed or None)
    return res


@mcp.tool
def synth_check(design_json: str) -> dict[str, Any]:
    """Validate a design IR without writing anything. Milliseconds, no board needed.

    Wraps `phicad/synth/cli.py check`. Reports every problem at once with a fix hint each:
    a pad in no net and not declared no-connect, a pad number the footprint does not have,
    a pin claimed by two nets, an unresolvable footprint (with the closest real names).
    Run this after every edit to a design — it is the cheap half of the DRC loop, and it
    catches the class of hole that would otherwise surface as unconnected items twenty-five
    seconds later.
    """
    path, err = _need(design_json, ".json", ".yaml", ".yml")
    if err:
        return err
    return _synth(["check", str(path)], timeout=120)


@mcp.tool
def synth_build(design_json: str, out_dir: str, force: bool = False,
                route: bool = True, drc: bool = True,
                grid_mm: float = 0.1) -> dict[str, Any]:
    """Compile a design IR into a placed, routed, DRC-checked KiCad project.

    Wraps `phicad/synth/cli.py build`. Runs the whole pipeline with a gate after every
    stage — validate, place, scaffold, import (KiCad's own BOARD_NETLIST_UPDATER over IPC),
    position, connectivity, outline, route, silk, drc — and stops at the first failure
    rather than handing the next stage a broken input. The report names every stage, its
    verdict and its evidence, plus the paths of the board, project, netlist and drc.json.

    `route=False` places without routing (fast, leaves the ratsnest); `drc=False` skips the
    slow authoritative gate for an inner loop. `grid_mm` is the router's grid pitch: a
    finer grid finds corridors a coarse one misses, at four times the cost per halving.
    """
    design, err = _need(design_json, ".json", ".yaml", ".yml")
    if err:
        return err
    argv = ["build", str(design), "--output", str(Path(out_dir).expanduser()),
            "--grid", str(grid_mm)]
    if force:
        argv.append("--force")
    if not route:
        argv.append("--no-route")
    if not drc:
        argv.append("--no-drc")
    result = _synth(argv)
    report = result.get("report") or {}
    if report.get("ok"):
        try:
            result["project_history"] = project_history.prepare_generated_project(
                Path(out_dir).expanduser(), design)
        except project_history.HistoryError as exc:
            result["project_history"] = {"ok": False, "error": str(exc)}
    return result


@mcp.tool
def synth_verify(board_path: str) -> dict[str, Any]:
    """Run the synth pipeline's gates (connectivity, outline, DRC) against an EXISTING board.

    Wraps `phicad/synth/cli.py verify`. Use it to re-check a board after manual or agent
    edits with the same verdicts synth_build reports, without rebuilding anything.
    """
    board, err = _need(board_path, ".kicad_pcb")
    if err:
        return err
    return _synth(["verify", str(board)], timeout=600)


# --------------------------------------------------------------------------------------
# Per-project Git history
# --------------------------------------------------------------------------------------


def _project_target(raw: str) -> tuple[Path | None, dict[str, Any] | None]:
    if not raw:
        return None, {"ok": False, "error": "empty project path"}
    path = Path(raw).expanduser()
    path = (REPO_ROOT / path).resolve() if not path.is_absolute() else path.resolve()
    if not path.exists():
        return None, {"ok": False, "error": f"project or board not found: {path}"}
    return path, None


@mcp.tool
def project_git_snapshot(project_path: str,
                         message: str = "phicad: agent checkpoint") -> dict[str, Any]:
    """Commit all current files in one board project's Git history scope.

    New/generated standalone projects are initialized automatically; projects inside an
    existing repository reuse it with a project-relative path scope. AI View autosnapshots
    stable manual or agent saves. Use this tool for an explicit design checkpoint before a
    risky edit, after a verified milestone, or before handoff. An ambiguous checkout is
    refused rather than staged wholesale.
    """
    target, err = _project_target(project_path)
    if err:
        return err
    board_file = target.is_file() or target.suffix in (".kicad_pcb", ".kicad_pro", ".kicad_sch")
    root = target.parent if board_file else target
    located, reason = project_history.locate_project(root)
    if located is None:
        return {"ok": False, "error": reason, "project_dir": str(root),
                "hint": "pass the board or project directory outside the PhiCad source tree"}
    root = located
    history = project_history.ProjectHistory(root)
    try:
        if history.is_repository:
            committed = history.snapshot(message)
        else:
            history.ensure(message=message)
            committed = True
        status = history.status()
    except project_history.HistoryError as exc:
        return {"ok": False, "error": str(exc), "project_dir": str(root)}
    return {"ok": True, "committed": committed, "project_dir": str(root),
            "git": status.display(), "head": status.head, "branch": status.branch}


# --------------------------------------------------------------------------------------
# Autorouting (phicad/autoroute) — Freerouting via Specctra DSN/SES, cleanup, BGA stitching.
# Lives in tools_autoroute.py; it receives this server's helpers rather than importing them so
# the module stays testable from a standalone driver.
# --------------------------------------------------------------------------------------

sys.path.insert(0, str(MCP_DIR))
import tools_autoroute  # noqa: E402

_AUTOROUTE_WIRING = tools_autoroute.configure({
    "KICAD_CLI": KICAD_CLI, "VENV_PY": VENV_PY, "REPO_ROOT": REPO_ROOT,
    "run": _run, "cli_env": _cli_env, "need": _need, "out_dir": _out_dir,
    "out_path": _out_path, "preflight": _preflight, "read_json": _read_json,
})
if not _AUTOROUTE_WIRING.get("ok"):
    raise RuntimeError(f"tools_autoroute wiring incomplete: {_AUTOROUTE_WIRING}")

# --------------------------------------------------------------------------------------
# PhiCad Verify (phicad/core) — tscircuit build + independent KiCad CAD check.
# Registered from tools_verify.py, which shells out to the SAME core CLI a human runs, so an
# agent and a terminal cannot receive different answers. It adds no model runtime and no
# credentials: phi is the only AI harness.
# --------------------------------------------------------------------------------------

import tools_verify  # noqa: E402

tools_verify.register(mcp)

_AUTOROUTE_TOOLS = (tools_autoroute.autoroute_freerouting, tools_autoroute.autoroute_cleanup,
                    tools_autoroute.autoroute_stitch, tools_autoroute.pcb_export_dsn,
                    tools_autoroute.pcb_import_ses)
# Registered below by _register(), together with the other extension modules, so the autoroute
# writers go through the project-git journal hook like every other mutating tool.


# --------------------------------------------------------------------------------------
# Project git journal — every mutating tool is logged to the project's Git history scope.
#
# A standalone KiCad project gets a repo; a project in a monorepo uses a scoped enclosing repo
# (phicad/pylib/project_history.py); this hook
# never touches the PhiCad source repo.  After a mutating tool returns, we work out which files
# on disk it changed (explicit `mutated`/`outputs`/`file` keys, else the in-place input), append
# a journal entry under <project>/.phicad/journal.jsonl and, unless the client opted out with
# PHICAD_TOOL_SNAPSHOT=0, commit a snapshot with a structured message.  Files that are not
# inside a PhiCad project repo (e.g. exports under $TMPDIR) are simply not journaled.
# --------------------------------------------------------------------------------------

# Two different modules are named `project_history`: the AI View plugin copy imported above
# (the Tk/web panels' own small git layer) and the richer stdlib-only one in phicad/pylib
# that owns the journal.  `import project_history` here would just hand back the cached
# ai-view module -- which is exactly what happened, leaving the journal hook calling a
# function that only exists in the pylib and reporting AttributeError on every mutating tool.
# Load the pylib by path under its own module name, the way tools_git.py already does.
try:
    _PH_PATH = REPO_ROOT / "phicad" / "pylib" / "project_history.py"
    _PH_SPEC = importlib.util.spec_from_file_location("phicad_project_history", _PH_PATH)
    if _PH_SPEC is None or _PH_SPEC.loader is None:
        raise ImportError(f"cannot load {_PH_PATH}")
    _journal_history = importlib.util.module_from_spec(_PH_SPEC)
    sys.modules.setdefault("phicad_project_history", _journal_history)
    _PH_SPEC.loader.exec_module(_journal_history)
except Exception as _exc:  # the module lands with the git layer; the server must still start
    _journal_history = None  # type: ignore[assignment]
    _PROJECT_HISTORY_ERROR = f"{type(_exc).__name__}: {_exc}"
else:
    _PROJECT_HISTORY_ERROR = None

_PATH_KEYS = ("mutated", "outputs", "file", "out_board", "board", "saved_to", "out_path",
              "merged_board", "routed_board", "project_dir", "out_dir")


def _paths_in(res: dict[str, Any]) -> list[str]:
    """Absolute FILES a result reports having written.

    Directories are deliberately excluded.  `out_dir`/`project_dir` are frequently the
    project root, and handing a directory to `git add` swept the user's unrelated dirty
    files into a commit attributed to the tool -- an audit trail that lies is worse than
    none.  A tool that genuinely produces a tree must enumerate its files (the CLI export
    tools already do, via their before/after snapshot).

    Symlinks are resolved, so an in-place edit through a link journals the file that really
    changed rather than the link.
    """
    found: list[str] = []
    for key in _PATH_KEYS:
        val = res.get(key)
        vals = val if isinstance(val, list) else [val]
        for v in vals:
            if not isinstance(v, str) or not v.startswith("/"):
                continue
            p = Path(v)
            if p.is_file():
                found.append(str(p.resolve()))
    return sorted(set(found))


def _promote_journaled(res: dict[str, Any]) -> dict[str, Any]:
    """Copy the journal verdict to a TOP-LEVEL `journaled` key on a mutating tool's result.

    Journaling is opt-in (it needs a project that `project_init` has initialised), and the
    verdict used to live only inside the nested `project_history` block.  A client that shows a
    model the first level of a result -- or a model that simply does not dig -- saw twenty
    successful edits and reported "all changes versioned" while twenty nested
    `journaled: false` blocks said otherwise.  The nested block is left exactly as it was for
    compatibility; this adds the one flag that has to be impossible to miss, plus a hint when
    `project_init` is what would fix it.
    """
    block = res.get("project_history")
    if not isinstance(block, dict):
        return res
    res["journaled"] = bool(block.get("journaled"))
    if res["journaled"]:
        res.pop("journal_hint", None)
        return res
    reason = str(block.get("reason") or "")
    res["journal_note"] = (f"NOT versioned: {reason}" if reason
                           else "NOT versioned: the project git journal did not record this")
    if "project_init" in reason or "no project history" in reason:
        target = block.get("project_dir") or ""
        res["journal_hint"] = (f"project_init(path='{target}') to start versioning, then redo "
                               f"this change" if target else
                               "project_init(path=<project dir>) to start versioning")
    return res


def _journal_after(tool_name: str, args: dict[str, Any], res: Any) -> Any:
    """Attach a `project_history` block to a mutating tool's result. Never raises."""
    if not isinstance(res, dict):
        return res
    if _journal_history is None:
        res["project_history"] = {"journaled": False, "reason": _PROJECT_HISTORY_ERROR}
        return _promote_journaled(res)
    try:
        paths = _paths_in(res)
        # An in-place edit reports the input path itself; make sure it is considered.
        for key in ("board_path", "sch_path", "path", "project_path", "base_board"):
            v = args.get(key)
            if isinstance(v, str) and Path(v).expanduser().is_absolute() \
                    and Path(v).expanduser().is_file():
                paths.append(str(Path(v).expanduser().resolve()))
        if not paths:
            res["project_history"] = {"journaled": False, "reason": "no on-disk files reported"}
            return _promote_journaled(res)
        snapshot = os.environ.get("PHICAD_TOOL_SNAPSHOT", "1") != "0"
        summary = {k: res.get(k) for k in ("ok", "applied", "exit_code", "error") if k in res}
        res["project_history"] = _journal_history.record_and_snapshot(
            paths[0], actor="mcp:" + tool_name, action=tool_name, args=args,
            outputs=[p for p in paths if p in (res.get("outputs") or [])],
            mutated=paths, result_summary=summary, autosnapshot=snapshot and bool(res.get("ok")))
    except Exception as exc:  # journaling must never turn a successful edit into a failure
        res["project_history"] = {"journaled": False,
                                  "reason": f"{type(exc).__name__}: {exc}"}
    return _promote_journaled(res)


_MUTATING_PREFIXES = ("session_add", "session_update", "session_delete", "session_set",
                      "session_move", "session_rotate", "session_batch", "session_refill",
                      "session_revert", "session_save", "session_flip", "autoroute_",
                      "pcb_import", "pcb_upgrade", "sch_import", "sch_upgrade", "fp_upgrade",
                      "sym_upgrade", "swarm_merge", "synth_build", "parts_fetch", "sch_set",
                      "sch_add", "sch_delete", "sch_place")


def _is_mutating(fn: Any) -> bool:
    name = fn.__name__
    if name.startswith("project_"):        # the git tools journal themselves
        return False
    return name.startswith(_MUTATING_PREFIXES) or "in place" in (fn.__doc__ or "").lower()


def _journaled(fn: Any) -> Any:
    """Wrap a tool so its result carries the project-git journal/snapshot outcome."""
    import functools
    import inspect

    sig = inspect.signature(fn)

    @functools.wraps(fn)
    def wrapper(*a: Any, **kw: Any) -> Any:
        try:
            bound = sig.bind_partial(*a, **kw)
            bound.apply_defaults()
            args = dict(bound.arguments)
        except Exception:
            args = dict(kw)
        return _journal_after(fn.__name__, args, fn(*a, **kw))

    wrapper.__signature__ = sig  # type: ignore[attr-defined]  # FastMCP builds the schema from it
    return wrapper


def _register(fns: tuple[Any, ...] | list[Any]) -> None:
    for fn in fns:
        mcp.tool(_journaled(fn) if _is_mutating(fn) else fn)


# Retro-fit the journal onto the mutating tools defined above in this file.
for _name in ("session_save", "swarm_merge", "synth_build", "parts_fetch"):
    _tool = mcp._tool_manager._tools.get(_name)  # noqa: SLF001 — no public re-register API
    if _tool is not None:
        _tool.fn = _journaled(_tool.fn)


# --------------------------------------------------------------------------------------
# Extension modules — each exposes configure(ctx) + TOOLS (see tools_autoroute.py).
# Missing modules are reported through phicad_status, never fatal, so a partial checkout
# still serves what it has.
# --------------------------------------------------------------------------------------

_CTX = {
    "KICAD_CLI": KICAD_CLI, "VENV_PY": VENV_PY, "REPO_ROOT": REPO_ROOT, "OUT_ROOT": OUT_ROOT,
    "MCP_DIR": MCP_DIR, "run": _run, "cli_env": _cli_env, "need": _need, "out_dir": _out_dir,
    "out_path": _out_path, "preflight": _preflight, "read_json": _read_json,
    "session": _session, "api_alive": _api_alive, "api": _api,
}
_EXTENSIONS: dict[str, dict[str, Any]] = {}

_register(_AUTOROUTE_TOOLS)

for _mod_name in ("tools_cli", "tools_session", "tools_schematic", "tools_git"):
    try:
        _mod = __import__(_mod_name)
        _wiring = _mod.configure(_CTX)
        if not _wiring.get("ok"):
            raise RuntimeError(f"configure() incomplete: {_wiring}")
        _register(tuple(_mod.TOOLS))
        _EXTENSIONS[_mod_name] = {"ok": True, "tools": [f.__name__ for f in _mod.TOOLS]}
    except Exception as _exc:
        _EXTENSIONS[_mod_name] = {"ok": False, "error": f"{type(_exc).__name__}: {_exc}"}


# --------------------------------------------------------------------------------------
# Precondition stamping for THIS file's session tools
#
# tools_session.configure() stamps the item-level tools it owns before they are registered.
# The board-level lifecycle tools live here and were already registered by @mcp.tool at
# definition time, so both halves need updating: `fn.__doc__` (what registry.py and the
# generated docs read) and `FunctionTool.description` (what an MCP client actually sees).
# The wording comes from tools_session so there is exactly one copy of the sentence.
# --------------------------------------------------------------------------------------

#: tool name -> which precondition applies. Needing an OPEN DOCUMENT is a stronger statement
#: than needing the server, and telling session_open to call session_open would be nonsense.
_SESSION_PRECONDITION_TOOLS = {
    "session_open": "api", "session_documents": "api", "session_save": "session",
    "session_board_query": "session", "session_close": "session", "kipy_exec": "api",
}


def _stamp_session_preconditions() -> list[str]:
    """Append the precondition sentence to this file's session tools. Never raises."""
    import inspect

    try:
        import tools_session as _ts
    except Exception:
        return []
    stamped: list[str] = []
    for name, which in _SESSION_PRECONDITION_TOOLS.items():
        tool = mcp._tool_manager._tools.get(name)  # noqa: SLF001 — no public re-describe API
        if tool is None:
            continue
        sentence = (_ts.SESSION_PRECONDITION if which == "session"
                    else _ts.API_SERVER_PRECONDITION)
        fn = getattr(tool, "fn", None)
        original = inspect.unwrap(fn) if fn is not None else None
        if original is None or not _ts.stamp_precondition([original], sentence):
            continue                                   # already stamped, or no docstring
        tool.description = original.__doc__ or tool.description
        stamped.append(name)
    return stamped


_SESSION_PRECONDITION_STAMPED = _stamp_session_preconditions()


# --------------------------------------------------------------------------------------
# Resources and prompts — the non-tool half of the MCP surface
#
# `initialize` instructions are pushed at every client; these are what a client can PULL when
# it needs the detail: the workflow map on its own (so a host that drops instructions can still
# fetch it), and the live tool index grouped by section (so an agent can orient without 122
# descriptions in context).  The prompts are the three workflows worth starting from a slash
# command; they are strings, not tool calls, so they cannot mutate anything.
# --------------------------------------------------------------------------------------


@mcp.resource("phicad://instructions", mime_type="text/markdown")
def resource_instructions() -> str:
    """The same workflow map and global rules `initialize` returns, fetchable on demand."""
    return INSTRUCTIONS


@mcp.resource("phicad://tools", mime_type="text/markdown")
async def resource_tool_index() -> str:
    """Every registered tool name grouped by section — the cheap map before reading schemas."""
    tools = await mcp.get_tools()
    groups: dict[str, list[str]] = {}
    for name, tool in sorted(tools.items()):
        fn = getattr(tool, "fn", None)
        mod = getattr(fn, "__module__", "") or ""
        key = mod if mod.startswith("tools_") else "server"
        groups.setdefault(key, []).append(name)
    lines = [f"# phicad MCP tools ({len(tools)} registered)", ""]
    for key in sorted(groups):
        lines.append(f"## {key}")
        lines.append(", ".join(groups[key]))
        lines.append("")
    lines.append("Use `phicad-tool describe <name>` or the tool's own schema for parameters.")
    return "\n".join(lines)


@mcp.prompt
def design_a_board(description: str) -> str:
    """Start the synth pipeline from a plain-language circuit description."""
    return (
        f"Design this board with PhiCad: {description}\n\n"
        "Write a design IR JSON to an absolute path, then: synth_check(design_json) until it "
        "passes, synth_build(design_json, out_dir), pcb_drc(board_path, severity_all=True), "
        "pcb_render(board_path). Report every gate's real output — violation counts by "
        "severity, not a summary of intent. Absolute paths only."
    )


@mcp.prompt
def gate_a_board(board_path: str) -> str:
    """Run the review gates on an existing board and report what they actually said."""
    return (
        f"Review {board_path}. Run board_summary, then pcb_drc(severity_all=True) "
        "(schematic_parity=True if a .kicad_sch sits next to it), sch_erc on that schematic, "
        "and pcb_render for a picture. `ok: false` from pcb_drc means violations exist, not "
        "that DRC failed — quote the counts by severity and the worst offenders."
    )


@mcp.prompt
def live_edit_board(board_path: str, change: str) -> str:
    """Edit a board in the headless session, versioned, with read-back proof."""
    return (
        f"Change {board_path}: {change}\n\n"
        "First project_init(path=<the project dir>) or nothing is journaled. Then "
        "api_server_start(), session_open(path=<absolute .kicad_pcb>), inspect with "
        "session_board_query / session_list_items before writing. session_update_footprint is "
        "ABSOLUTE by default (relative=True for a delta). Check `applied` and top-level "
        "`journaled` on every write, session_refill_zones if you touched copper zones, then "
        "session_save() — nothing reaches the file until then — pcb_drc, and api_server_stop()."
    )


# --------------------------------------------------------------------------------------
# Host integration / repo state
# --------------------------------------------------------------------------------------


@mcp.tool
def integration_capabilities() -> dict[str, Any]:
    """Discover PhiCad's host/model protocol without guessing model or UI versions.

    Agent hosts and future model adapters use this as the handshake: MCP is the command plane,
    the schemas describe design/event payloads, and the advertised transport list says
    which workflow event transports are implemented today.
    """
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    from phicad.integration.capabilities import describe  # noqa: PLC0415

    return describe(REPO_ROOT)


@mcp.tool
def phicad_status() -> dict[str, Any]:
    """Parsed `phicad status`: branch, HEAD, build dir, upstream delta, venv, api-server."""
    if not PHICAD_BIN.exists():
        return {"ok": False, "error": f"phicad helper not found at {PHICAD_BIN}"}
    # `phicad status` reads PHICAD_BUILD / PHICAD_VENV itself; hand it the paths this server
    # resolved so both report the same build tree when source and build live apart.
    env = _cli_env()
    env["PHICAD_BUILD"] = str(BUILD_DIR)
    env["PHICAD_VENV"] = str(VENV_PY.parent.parent)
    res = _run(["bash", str(PHICAD_BIN), "status"], timeout=90, cwd=REPO_ROOT, env=env)
    fields: dict[str, str] = {}
    for line in (res.get("stdout") or "").splitlines():
        if ":" in line:
            key, _, val = line.partition(":")
            key = key.strip()
            if key and " " not in key.strip():
                fields[key] = val.strip()
    res["status"] = fields
    res["repo_root"] = str(REPO_ROOT)
    res["kicad_cli"] = str(KICAD_CLI)
    res["kicad_cli_built"] = KICAD_CLI.exists()
    res["automation_venv"] = str(VENV_PY) if VENV_PY.exists() else None
    res["mcp_out_root"] = str(OUT_ROOT)
    res["managed_api_server_running"] = _api_alive()
    res["autoroute"] = {"java": _AUTOROUTE_WIRING.get("java"),
                        "freerouting_jar": _AUTOROUTE_WIRING.get("jar")}
    res["extensions"] = _EXTENSIONS
    res["project_history"] = ("available" if _journal_history is not None
                              else f"unavailable: {_PROJECT_HISTORY_ERROR}")
    res.pop("stdout", None)
    return res


# --------------------------------------------------------------------------------------


def _shutdown(*_args: Any) -> None:
    try:
        if _api_alive():
            api_server_stop.fn()  # type: ignore[attr-defined]
    except Exception:
        pass


def _install_signal_handlers() -> None:
    """Reap the managed api-server on SIGTERM/SIGINT too, not only on a clean exit.

    `atexit` does not run when the host kills us, and an orphaned `kicad-cli api-server`
    holds a board in memory while being unreachable from `api_server_status` -- the only
    recovery was a manual kill.  Chain to any previous handler so we do not change how the
    process ultimately dies.
    """
    import signal

    def _handler(signum: int, frame: Any) -> None:
        _shutdown()
        previous = _prior.get(signum)
        if callable(previous):
            previous(signum, frame)
            return
        if previous == signal.SIG_IGN:
            return
        signal.signal(signum, signal.SIG_DFL)
        os.kill(os.getpid(), signum)

    _prior: dict[int, Any] = {}
    for name in ("SIGTERM", "SIGINT", "SIGHUP"):
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        try:
            _prior[int(sig)] = signal.getsignal(sig)
            signal.signal(sig, _handler)
        except (ValueError, OSError):
            # Not the main thread, or the platform refuses: cleanup still runs via atexit.
            continue


def main() -> None:
    import atexit

    atexit.register(_shutdown)
    _install_signal_handlers()
    if not KICAD_CLI.exists():
        print(f"warning: kicad-cli not found at {KICAD_CLI}", file=sys.stderr)
    mcp.run()


if __name__ == "__main__":
    main()
