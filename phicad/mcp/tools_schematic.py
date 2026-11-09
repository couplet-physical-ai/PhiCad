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
"""Schematic + symbol-library tool surface for the phicad MCP server: read AND write.

WHY THIS MODULE EXISTS
    The IPC API's schematic surface is thin (kipy 0.8.0 can open a .kicad_sch and little
    else) and eeschema's reader lives inside a kiface DSO no Python can import.  So the
    schematic side of "total control" is text: parse the s-expression, edit spans, write it
    back, and let the fork's own kicad-cli tell you the file still loads.

    Reads and writes run in `phicad/mcp/sch_worker.py` under the automation venv (VENV_PY),
    one process per call, one JSON object per call.  The worker is stdlib-only (it imports
    phicad/pylib/sexp_edit.py and nothing else), so no fastmcp process ever imports kipy.

HOW FAITHFUL IS THE ROUND TRIP  (measured on demos/pic_programmer/pic_programmer.kicad_sch)
    kiutils 1.4.8    DROPS tokens it does not model — do_not_autoplace 641->0, hide 391->0,
                     exclude_from_sim 135->0, in_pos_files 126->0, body_style 105->0,
                     embedded_fonts 22->0, generator_version 1->0; 229174 -> 178091 bytes.
                     Rejected: an edit through it silently deletes data.
    kicad-skip 0.2.5 keeps every token but reflows the file (16830 diff lines).  Rejected:
                     one changed field would be indistinguishable from a rewrite.
    sexp_edit.py     (this fork, new) parses to character spans and splices.  A no-op load+
                     save is BYTE-IDENTICAL, and setting one field produces a one-line diff.
                     Chosen.  Everything below writes through it.

WHAT A WRITE COSTS YOU
    Every writer defaults to a NEW file — <out>/schematic/<stem>.edited.kicad_sch — and only
    touches the input when out_path equals it (then `mutated` names it).  After the write the
    tool runs `kicad-cli sch erc` on the result purely as a LOAD CHECK: exit 0 or 5 both mean
    the fork parsed the file (5 is "violations exist"), anything else means it did not, and
    that is reported as `loads: false` with the CLI's own stderr.  When the CLI is absent
    `loads` is None and `load_check` says why.

WHAT `ok` MEANS
    The operation ran and produced what it promised.  It does NOT mean the schematic is
    electrically sensible: a placed symbol is wired to nothing, a label in empty space names
    no net, and ERC violation counts are data, not verdicts.  Read the numbers.
"""

from __future__ import annotations

import json
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
_read_json: Callable[[Path], Any | None] | None = None

_REQUIRED = ("KICAD_CLI", "VENV_PY", "REPO_ROOT", "run", "cli_env", "need",
             "out_dir", "out_path", "preflight", "read_json")

#: Tag for this module's output directory: <out>/schematic/.
TAG = "schematic"

#: Worker calls are cheap (a 230 kB schematic parses in ~40 ms) but a deep hierarchy walks
#: every file, so allow a generous ceiling rather than a tight one.
WORKER_TIMEOUT_S = 180
#: `sch erc` on the pic_programmer demo takes a couple of seconds; a big hierarchy more.
ERC_TIMEOUT_S = 600
#: `sch export netlist` is the only CLI call sch_list_nets makes.
NETLIST_TIMEOUT_S = 600

#: `kicad-cli sch erc` exits 5 when violations exist — the FILE STILL LOADED (exit_codes.h
#: ERR_RC_VIOLATIONS).  Anything else after a successful parse means the parse failed.
ERC_LOADED_CODES = (0, 5)


def _pick(ctx: Any, name: str) -> Any:
    """Read one dependency out of a dict-ish or attribute-ish context."""
    if isinstance(ctx, dict):
        return ctx.get(name)
    return getattr(ctx, name, None)


def configure(ctx: Any) -> dict[str, Any]:
    """Bind the server's helpers into this module. Called once, before any tool runs.

    Returns what was bound (and whether the worker script and venv are actually there), so
    the server or a driver can assert the wiring instead of discovering a hole inside a call.
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

    worker = _worker()
    return {
        "ok": True,
        "kicad_cli": str(KICAD_CLI),
        "kicad_cli_exists": KICAD_CLI.exists(),
        "worker": str(worker),
        "worker_exists": worker.exists(),
        "sexp_edit": str(_pylib() / "sexp_edit.py"),
        "sexp_edit_exists": (_pylib() / "sexp_edit.py").exists(),
        "worker_python": _worker_python(),
        "tools": [fn.__name__ for fn in TOOLS],
    }


# --------------------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------------------


def _unconfigured() -> dict[str, Any] | None:
    if _run is None or REPO_ROOT is None:
        return {"ok": False,
                "error": "phicad.mcp.tools_schematic is not configured",
                "hint": "the server must call tools_schematic.configure(ctx) at import time"}
    return None


def _mcp_dir() -> Path:
    root = REPO_ROOT if REPO_ROOT is not None else Path(__file__).resolve().parent.parent.parent
    return root / "phicad" / "mcp"


def _pylib() -> Path:
    root = REPO_ROOT if REPO_ROOT is not None else Path(__file__).resolve().parent.parent.parent
    return root / "phicad" / "pylib"


def _worker() -> Path:
    """The worker next to THIS file, falling back to the configured repo root.

    A server pointed at another worktree (PHICAD_REPO) must run that worktree's worker, but a
    module imported from this directory should still work when nothing was configured.
    """
    local = Path(__file__).resolve().parent / "sch_worker.py"
    return local if local.exists() else _mcp_dir() / "sch_worker.py"


def _worker_python() -> str:
    """Interpreter for the worker: the automation venv, else whatever python3 is around.

    The worker is stdlib-only on purpose (kiutils LOSES DATA and kicad-skip reformats — see
    the module docstring), so this never actually needs the venv's packages; the venv is
    simply the known-modern interpreter this repo already guarantees.
    """
    if VENV_PY is not None and Path(VENV_PY).exists():
        return str(VENV_PY)
    return shutil.which("python3") or sys.executable or "python3"


def _worker_exec(argv: list[str], timeout: int) -> dict[str, Any]:
    """Run the worker and return its COMPLETE stdout — the server's `run` truncates.

    `run(...)` keeps only the last 20 000 characters of stdout, which is right for a CLI whose
    output is a log and fatal here: the worker's answer IS its stdout, and one JSON object for
    a 105-symbol schematic is ~34 kB, so a truncated document fails to parse and a perfectly
    good read reports "produced no JSON".  Same contract as `run` otherwise (never raises,
    ok/exit_code/command/stdout/stderr/elapsed_s), so the caller below is unchanged.
    """
    import shlex
    import subprocess
    import time

    started = time.monotonic()
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout,
                              env=_cli_env())
    except FileNotFoundError as exc:
        return {"ok": False, "error": f"executable not found: {argv[0]}", "detail": str(exc),
                "command": shlex.join(argv), "stdout": "", "stderr": ""}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"timeout after {timeout}s", "timed_out": True,
                "command": shlex.join(argv), "stdout": "", "stderr": ""}
    return {"ok": proc.returncode in (0, 1), "exit_code": proc.returncode,
            "command": shlex.join(argv), "stdout": proc.stdout, "stderr": proc.stderr[-8000:],
            "elapsed_s": round(time.monotonic() - started, 2)}


def _worker_call(command: str, payload: dict[str, Any], *,
                 timeout: int = WORKER_TIMEOUT_S) -> dict[str, Any]:
    """Run one worker command and return its JSON result (never raise, never leak stdout).

    The worker exits 1 on a failed result, so a non-zero exit with parsed JSON is a normal
    business failure and the JSON is the answer; a non-zero exit WITHOUT JSON is a crash and
    the stderr is the answer.
    """
    worker = _worker()
    if not worker.exists():
        return {"ok": False, "error": f"worker not found: {worker}",
                "hint": "phicad/mcp/sch_worker.py is missing from this worktree"}
    res = _worker_exec([_worker_python(), str(worker), command, json.dumps(payload)], timeout)
    stdout = res.pop("stdout", "") or ""
    try:
        out = json.loads(stdout)
    except (json.JSONDecodeError, ValueError):
        return {"ok": False,
                "error": f"sch_worker {command} produced no JSON (exit {res.get('exit_code')})",
                "stderr": (res.get("stderr") or "")[-2000:],
                "stdout_head": stdout[:500],
                "command": res.get("command")}
    if not isinstance(out, dict):
        return {"ok": False, "error": "sch_worker returned a non-object result"}
    out["elapsed_s"] = res.get("elapsed_s")
    if not out.get("ok") and res.get("stderr"):
        out["stderr"] = res["stderr"][-2000:]
    return out


def _sch(path: str) -> tuple[Path | None, dict[str, Any] | None]:
    return _need(path, ".kicad_sch")


def _sym(path: str) -> tuple[Path | None, dict[str, Any] | None]:
    return _need(path, ".kicad_sym")


def _default_out(sch: Path, requested: str | None) -> str:
    """<out>/schematic/<stem>.edited.kicad_sch unless the caller named something else.

    Passing the input path writes in place — that is the only way to mutate the caller's
    file, and it is always reflected in the result's `in_place` / `mutated`.
    """
    return str(_out_path(TAG, sch.stem, ".edited.kicad_sch", requested))


def _load_check(target: Path) -> dict[str, Any]:
    """Ask the fork whether it can still parse a written schematic.

    `kicad-cli sch erc` is the cheapest full parse the CLI offers: exit 0 (clean) and exit 5
    (ERR_RC_VIOLATIONS) both mean the document LOADED, which is the only question here.  A
    schematic full of pre-existing ERC warnings must not read as a broken write, so the
    violation count is reported separately from `loads`.
    """
    if KICAD_CLI is None or not KICAD_CLI.exists():
        return {"loads": None,
                "load_check": f"skipped: kicad-cli not built at {KICAD_CLI}",
                "hint": "phicad/bin/phicad build kicad-cli"}
    report = _out_path(TAG, f"{target.stem}.loadcheck", ".erc.json", None)
    res = _run([str(KICAD_CLI), "sch", "erc", "--format", "json", "--severity-all",
                "-o", str(report), str(target)],
               timeout=ERC_TIMEOUT_S, env=_cli_env(), ok_codes=ERC_LOADED_CODES)
    loaded = res.get("exit_code") in ERC_LOADED_CODES
    out: dict[str, Any] = {
        "loads": loaded,
        "load_check": ("kicad-cli sch erc parsed the file (exit %s)" % res.get("exit_code")
                       if loaded else
                       "kicad-cli sch erc could NOT parse the file (exit %s)"
                       % res.get("exit_code")),
        "erc_exit_code": res.get("exit_code"),
    }
    if not loaded:
        out["erc_stderr"] = (res.get("stderr") or "")[-2000:]
    data = _read_json(report) if report.exists() else None
    if isinstance(data, dict):
        violations = 0
        by_severity: dict[str, int] = {}
        for sheet in data.get("sheets") or []:
            for v in (sheet.get("violations") or []):
                violations += 1
                sev = str(v.get("severity", "unknown"))
                by_severity[sev] = by_severity.get(sev, 0) + 1
        out["erc_violations"] = violations
        out["erc_by_severity"] = dict(sorted(by_severity.items()))
        out["erc_report_path"] = str(report)
    return out


def _finish_write(res: dict[str, Any], verify: bool) -> dict[str, Any]:
    """Normalise a worker write result: outputs/mutated + the post-write load check."""
    if not res.get("ok"):
        return res
    res.setdefault("outputs", [])
    res.setdefault("mutated", [])
    out = res.get("out_path")
    if verify and out:
        res.update(_load_check(Path(out)))
    elif not verify:
        res["loads"] = None
        res["load_check"] = "skipped: verify=False"
    return res


# --------------------------------------------------------------------------------------
# READ TOOLS
# --------------------------------------------------------------------------------------


def sch_list_symbols(sch_path: str, sheet: str | None = None) -> dict[str, Any]:
    """List the symbols placed on a schematic: refs, values, footprints, lib_ids, positions.

    Read-only.  Each row carries ref, value, footprint, lib_id, at {x_mm, y_mm, rotation},
    unit, body_style, uuid and the in_bom/on_board/dnp/exclude_from_sim flags.

    `sheet` selects the scope: None = THIS FILE ONLY (a .kicad_sch is one sheet), "*" = the
    whole hierarchy reachable from it, or a Sheetname / sheet filename for one child sheet.
    Refs starting with '#' are power/flag pseudo-symbols (`power_symbol_count`), not parts —
    they carry no footprint and never reach the board.

    A multi-unit part appears once PER UNIT with the same ref, so `symbol_count` is normally
    larger than `unique_refs`.
    """
    if (err := _unconfigured()):
        return err
    sch, err = _sch(sch_path)
    if err:
        return err
    return _worker_call("list_symbols", {"path": str(sch), "sheet": sheet})


def sch_get_symbol(sch_path: str, ref: str, sheet: str | None = None) -> dict[str, Any]:
    """Everything about one placed symbol, by reference designator.

    Read-only.  Searches the whole hierarchy by default (`sheet` narrows it as in
    sch_list_symbols) and returns one entry per unit: all fields (`fields`), the non-stock
    ones separately (`custom_fields`), the pin-number -> pin-uuid map, the `(instances …)`
    blocks that netlisting actually reads, and the cached library definition with its pins
    (number/name/type/position) when the schematic carries one.

    `definition_cached: false` means the schematic references a lib_id it has no cached
    definition for — the symbol would draw as a placeholder in eeschema.
    """
    if (err := _unconfigured()):
        return err
    sch, err = _sch(sch_path)
    if err:
        return err
    if not (ref or "").strip():
        return {"ok": False, "error": "ref is required"}
    return _worker_call("get_symbol", {"path": str(sch), "ref": ref.strip(),
                                       "sheet": sheet if sheet is not None else "*"})


def sch_annotate_check(sch_path: str, sheet: str | None = None) -> dict[str, Any]:
    """Find unannotated (R?, U?) and duplicated reference designators in a hierarchy.

    Read-only; walks the whole hierarchy by default.  Returns `unannotated` (any ref holding
    a '?'), `duplicates` (the SAME ref AND unit used twice — a multi-unit part legitimately
    repeats its ref with different units, and that is not flagged), `missing_reference`
    (a symbol with no Reference at all) and `field_instance_mismatch`.

    That last one is the trap this tool exists for: a refdes is stored twice, in the drawn
    `(property "Reference" …)` and in `(instances … (reference …))`, and only the instance
    reaches the netlist and the board.  A mismatch means the canvas and the netlist disagree.
    `annotated` is true only when unannotated, duplicates and missing are all empty.
    """
    if (err := _unconfigured()):
        return err
    sch, err = _sch(sch_path)
    if err:
        return err
    return _worker_call("annotate_check",
                        {"path": str(sch), "sheet": sheet if sheet is not None else "*"})


def sch_list_sheets(sch_path: str) -> dict[str, Any]:
    """Walk the sheet hierarchy from a root schematic: files, sheet pins, page numbers.

    Read-only, cycle-safe, depth-capped at 16.  `tree` is the nested structure (each sheet
    with its Sheetname, Sheetfile, resolved path, position, size, hierarchical pins and page
    numbers); `files` is the flat list with a per-file symbol count; `missing_sheet_files`
    names children whose .kicad_sch is not on disk (a broken project, and the reason a sheet
    can silently contribute no parts). `is_flat` is true for a single-sheet design.
    """
    if (err := _unconfigured()):
        return err
    sch, err = _sch(sch_path)
    if err:
        return err
    return _worker_call("list_sheets", {"path": str(sch)})


def sch_find(sch_path: str, query: str, regex: bool = False,
             sheet: str | None = None, limit: int = 200) -> dict[str, Any]:
    """Search a schematic hierarchy across refs, lib_ids and every field name and value.

    Read-only, case-insensitive substring match by default; `regex=True` treats `query` as a
    Python regular expression.  Each hit reports `matched_in` (e.g. ["ref"],
    ["field:Value"], ["field_name:MPN"]) so it is obvious WHY it matched, plus the symbol's
    full field dict.  This is the tool for "which parts have no MPN", "who uses this
    footprint", "find every 0603".
    """
    if (err := _unconfigured()):
        return err
    sch, err = _sch(sch_path)
    if err:
        return err
    if not (query or "").strip():
        return {"ok": False, "error": "query is required"}
    return _worker_call("find", {"path": str(sch), "query": query, "regex": bool(regex),
                                 "sheet": sheet if sheet is not None else "*",
                                 "limit": max(1, min(int(limit), 5000))})


def sch_list_nets(sch_path: str, out_netlist: str | None = None) -> dict[str, Any]:
    """List the schematic's nets and the pins on them (the real connectivity, not geometry).

    Runs `kicad-cli sch export netlist --format kicadsexpr` — the fork's own netlister, which
    resolves wires, labels, hierarchical pins and buses — and parses the result: every net
    with its `pins` (ref, pin number, pin function, pin type), the component list, and
    `single_pin_nets`, which is the fastest signal that something is not actually wired.

    Read-only with respect to the schematic; the netlist file itself is written under
    <out>/netlist/ unless out_netlist says otherwise.  Needs kicad-cli.
    """
    if (err := _unconfigured()):
        return err
    if (err := _preflight()):
        return err
    sch, err = _sch(sch_path)
    if err:
        return err
    netlist = _out_path("netlist", sch.stem, ".net", out_netlist)
    res = _run([str(KICAD_CLI), "sch", "export", "netlist", "--format", "kicadsexpr",
                "-o", str(netlist), str(sch)], timeout=NETLIST_TIMEOUT_S, env=_cli_env())
    if not res.get("ok") or not netlist.exists():
        res["error"] = res.get("error") or "kicad-cli sch export netlist failed"
        res["schematic"] = str(sch)
        res.pop("stdout", None)
        return res
    parsed = _worker_call("parse_netlist", {"path": str(netlist)})
    parsed["schematic"] = str(sch)
    parsed["netlist_path"] = str(netlist)
    parsed["netlist_bytes"] = netlist.stat().st_size
    parsed["outputs"] = [str(netlist)]
    parsed["export_elapsed_s"] = res.get("elapsed_s")
    return parsed


def sch_symbol_lib_list(sym_path: str) -> dict[str, Any]:
    """List every symbol in a .kicad_sym library: units, pin counts, prefix, footprint.

    Read-only.  Each row carries name, `extends` (an alias inherits its parent's pins and
    graphics, so its own pin_count is 0), is_power, unit_count, pin_count, the Reference
    prefix, Value, Footprint, Datasheet, Description, ki_keywords and ki_fp_filters.
    Use sch_symbol_lib_get for the pin table of one symbol.
    """
    if (err := _unconfigured()):
        return err
    sym, err = _sym(sym_path)
    if err:
        return err
    return _worker_call("sym_lib_list", {"path": str(sym)})


def sch_symbol_lib_get(sym_path: str, name: str) -> dict[str, Any]:
    """One symbol definition from a .kicad_sym: its pins and its properties.

    Read-only.  `pins` lists every pin with number, name, electrical type (input, output,
    power_in, tri_state, …), graphic style, position {x_mm, y_mm, rotation}, length and unit;
    `units` maps the `<name>_<unit>_<body>` sub-symbols (unit 0 = shared across all units,
    which is where a multi-unit part keeps VCC/GND); `properties` is the raw property dict.
    On a miss, `did_you_mean` lists names containing the query.
    """
    if (err := _unconfigured()):
        return err
    sym, err = _sym(sym_path)
    if err:
        return err
    if not (name or "").strip():
        return {"ok": False, "error": "name is required"}
    return _worker_call("sym_lib_get", {"path": str(sym), "name": name.strip()})


# --------------------------------------------------------------------------------------
# WRITE TOOLS — new file by default, load-checked after every write
# --------------------------------------------------------------------------------------


def sch_set_field(sch_path: str, ref: str, field: str, value: str,
                  out_path: str | None = None, verify: bool = True) -> dict[str, Any]:
    """Set one field on one placed symbol (Value, Footprint, MPN, any custom field).

    An existing field is rewritten in place; a field the symbol does not have is CREATED
    hidden at the symbol's own position (which is what eeschema does for a BOM field). A
    multi-unit part carries a copy of the field on every unit and all of them are updated.
    Setting "Reference" also rewrites the `(instances … (reference …))` blocks, because those
    are what the netlist and the board read — updating only one of the two is how a
    schematic ends up annotated on screen and unannotated in the netlist.

    Writes <out>/schematic/<stem>.edited.kicad_sch unless out_path says otherwise; pass
    out_path == sch_path to edit in place (then the file appears in `mutated`).  Untouched
    text is preserved byte-for-byte, so `changes` and a one-line diff agree.  With
    verify=True the result is parsed back by `kicad-cli sch erc` and reported as
    `loads: true/false`.
    """
    return sch_set_fields_bulk(sch_path, [{"ref": ref, "field": field, "value": value}],
                               out_path=out_path, verify=verify)


def sch_set_fields_bulk(sch_path: str, updates: list[dict[str, str]],
                        out_path: str | None = None, verify: bool = True) -> dict[str, Any]:
    """Set many fields across many symbols in ONE pass over one schematic file.

    `updates` is a list of {"ref": …, "field": …, "value": …}.  All edits are applied to a
    single parse and written once, so the BOM-stamping case (MPN + Manufacturer + LCSC on
    fifty parts) costs one file rewrite instead of fifty, and two updates targeting the same
    (ref, field) are rejected rather than silently resolved.

    Refs are matched in THIS FILE only — a hierarchical child is a separate .kicad_sch (call
    sch_list_sheets, then this tool per file).  Unknown refs fail the whole call with the
    known refs listed; nothing is written.  Same output/verify rules as sch_set_field.
    """
    if (err := _unconfigured()):
        return err
    sch, err = _sch(sch_path)
    if err:
        return err
    if not updates:
        return {"ok": False, "error": "updates is required",
                "hint": '[{"ref": "R1", "field": "MPN", "value": "RC0603FR-0712KL"}]'}
    clean: list[dict[str, str]] = []
    for i, upd in enumerate(updates):
        if not isinstance(upd, dict):
            return {"ok": False, "error": f"updates[{i}] is not an object", "got": str(upd)}
        ref, field = str(upd.get("ref", "")).strip(), str(upd.get("field", "")).strip()
        if not ref or not field:
            return {"ok": False, "error": f"updates[{i}] needs both ref and field",
                    "got": upd}
        if upd.get("value") is None:
            return {"ok": False,
                    "error": f'updates[{i}] has no value (use "" to blank a field)',
                    "got": upd}
        clean.append({"ref": ref, "field": field, "value": str(upd["value"])})
    res = _worker_call("set_fields", {"path": str(sch), "updates": clean,
                                      "out_path": _default_out(sch, out_path)})
    return _finish_write(res, verify)


def sch_place_symbol(sch_path: str, lib_id: str, ref: str, value: str = "",
                     x_mm: float = 0.0, y_mm: float = 0.0, rotation: float = 0.0,
                     unit: int = 1, footprint: str | None = None, from_lib: str | None = None,
                     out_path: str | None = None, verify: bool = True) -> dict[str, Any]:
    """Place a symbol instance on a schematic sheet at (x_mm, y_mm).

    `lib_id` is LIBRARY:SYMBOL.  If the schematic already caches that definition in its
    `(lib_symbols …)` block the placement just references it; otherwise pass `from_lib` = a
    .kicad_sym file and the definition is COPIED into the cache (a placement whose definition
    is missing loads as a broken symbol).  Aliases (`extends`) are refused rather than
    half-flattened.  Refuses a refdes already used in the file, and rotation other than
    0/90/180/270.

    The placement gets a fresh uuid, a uuid per pin, Reference/Value/Footprint/Datasheet/
    Description fields (defaults taken from the definition) and an `(instances …)` block
    copied from a symbol already in the file — which is the only way to get the /root/sheet
    path right in a CHILD sheet, since a child does not know who instantiated it.

    THE SYMBOL IS WIRED TO NOTHING.  Its pins are unconnected until sch_add_wire /
    sch_add_label reaches them, and ERC will say so.  For a multi-unit part only `unit` is
    placed (`multi_unit_note` says how many there are).  Same output/verify rules as
    sch_set_field.
    """
    if (err := _unconfigured()):
        return err
    sch, err = _sch(sch_path)
    if err:
        return err
    if ":" not in (lib_id or ""):
        return {"ok": False, "error": "lib_id must be LIBRARY:SYMBOL", "got": lib_id}
    if not (ref or "").strip():
        return {"ok": False, "error": "ref is required"}
    lib: Path | None = None
    if from_lib:
        lib, err = _sym(from_lib)
        if err:
            return err
    payload: dict[str, Any] = {
        "path": str(sch), "lib_id": lib_id.strip(), "ref": ref.strip(), "value": value,
        "x_mm": x_mm, "y_mm": y_mm, "rotation": rotation, "unit": int(unit),
        "out_path": _default_out(sch, out_path),
    }
    if footprint is not None:
        payload["footprint"] = footprint
    if lib is not None:
        payload["from_lib"] = str(lib)
    return _finish_write(_worker_call("place_symbol", payload), verify)


def sch_add_wire(sch_path: str, x1_mm: float, y1_mm: float, x2_mm: float, y2_mm: float,
                 kind: str = "wire", style: str = "solid", out_path: str | None = None,
                 verify: bool = True) -> dict[str, Any]:
    """Add a wire (or `kind="bus"`) segment between two points, in millimetres.

    A wire connects ONLY where an endpoint lands exactly on a pin, a label anchor or another
    wire's endpoint (or a junction); wires that merely cross are not connected, and KiCad's
    grid is 1.27 mm for a reason — off-grid endpoints look connected and are not.  Verify
    with sch_list_nets, not with the picture.

    `style` is one of default/solid/dash/dash_dot/dash_dot_dot/dot.  Zero-length segments are
    refused.  Returns the new element's uuid.  Same output/verify rules as sch_set_field.
    """
    if (err := _unconfigured()):
        return err
    sch, err = _sch(sch_path)
    if err:
        return err
    return _finish_write(_worker_call("add_wire", {
        "path": str(sch), "x1_mm": x1_mm, "y1_mm": y1_mm, "x2_mm": x2_mm, "y2_mm": y2_mm,
        "kind": kind, "style": style, "out_path": _default_out(sch, out_path)}), verify)


def sch_add_label(sch_path: str, text: str, x_mm: float, y_mm: float, kind: str = "local",
                  rotation: float = 0.0, shape: str | None = None, size_mm: float = 1.27,
                  out_path: str | None = None, verify: bool = True) -> dict[str, Any]:
    """Add a net label: `kind` = local | global | hier.

    A local label names a net inside one sheet; a global label joins nets across the whole
    hierarchy by name; a hierarchical label is the sheet-side end of a sheet pin and must be
    matched by a pin of the same name on the parent's `(sheet …)` block, or the connection
    simply does not exist.  Global and hierarchical labels take a `shape`
    (input/output/bidirectional/tri_state/passive, default input); a local label must not.

    A label names a net only where it TOUCHES a wire or pin — a label in empty space is an
    ERC 'unconnected' item, not a net.  rotation must be 0/90/180/270.  Same output/verify
    rules as sch_set_field.
    """
    if (err := _unconfigured()):
        return err
    sch, err = _sch(sch_path)
    if err:
        return err
    if not (text or "").strip():
        return {"ok": False, "error": "text is required"}
    payload: dict[str, Any] = {"path": str(sch), "text": text, "x_mm": x_mm, "y_mm": y_mm,
                               "kind": kind, "rotation": rotation, "size_mm": size_mm,
                               "out_path": _default_out(sch, out_path)}
    if shape is not None:
        payload["shape"] = shape
    return _finish_write(_worker_call("add_label", payload), verify)


def sch_add_no_connect(sch_path: str, x_mm: float, y_mm: float,
                       out_path: str | None = None, verify: bool = True) -> dict[str, Any]:
    """Mark a pin deliberately unconnected: the X flag that silences ERC for that pin.

    The flag must sit EXACTLY on the pin's connection point (pin position + length along its
    rotation); a millimetre off and ERC reports both the pin and the flag as unconnected.
    Get the pin geometry from sch_get_symbol's `definition.pins` plus the symbol's own `at`.
    Refuses a duplicate at the same coordinates.  Same output/verify rules as sch_set_field.
    """
    if (err := _unconfigured()):
        return err
    sch, err = _sch(sch_path)
    if err:
        return err
    return _finish_write(_worker_call("add_no_connect", {
        "path": str(sch), "x_mm": x_mm, "y_mm": y_mm,
        "out_path": _default_out(sch, out_path)}), verify)


def sch_delete_symbol(sch_path: str, ref: str, out_path: str | None = None,
                      verify: bool = True) -> dict[str, Any]:
    """Delete every unit of one placed symbol from one schematic file.

    Only the `(symbol …)` blocks go.  The wires, junctions, labels and no-connects that
    touched it are geometry, not children of the symbol, and are LEFT IN PLACE — they become
    dangling stubs that ERC will report, which is deliberate: silently deleting a net's
    wiring because a part was removed is how a schematic quietly changes meaning.  The
    `(lib_symbols …)` cache entry is kept too, exactly as eeschema keeps it.

    Refuses a ref this file does not contain (listing what it does contain).  Same
    output/verify rules as sch_set_field.
    """
    if (err := _unconfigured()):
        return err
    sch, err = _sch(sch_path)
    if err:
        return err
    if not (ref or "").strip():
        return {"ok": False, "error": "ref is required"}
    return _finish_write(_worker_call("delete_symbol", {
        "path": str(sch), "ref": ref.strip(),
        "out_path": _default_out(sch, out_path)}), verify)


#: What the server registers. Kept as data so registration cannot drift from the module.
TOOLS = (sch_list_symbols, sch_get_symbol, sch_annotate_check, sch_list_sheets, sch_find,
         sch_list_nets, sch_symbol_lib_list, sch_symbol_lib_get,
         sch_set_field, sch_set_fields_bulk, sch_place_symbol, sch_add_wire, sch_add_label,
         sch_add_no_connect, sch_delete_symbol)
