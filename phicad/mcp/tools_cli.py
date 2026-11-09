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
"""The rest of `kicad-cli`, as MCP tools: every fork subcommand server.py does not already own.

WHY THIS MODULE EXISTS
    server.py wraps the subcommands an agent reaches for first (drc, erc, render, svg,
    gerbers, step, stats, pos, pdf, bom, netlist, sch pdf, pcb/sch diff, jobset run, dsn,
    import-ses).  That is maybe a third of the command tree.  Everything else -- footprint
    and symbol library work, the gerber toolbox, drill/dxf/gencad/ipc2581/ipcd356/odb/png/ps
    /stackup, the non-STEP 3D formats, the foreign-format importers, the format upgraders and
    the generic BOM -- was only reachable by hand.  This module closes that gap with typed
    tools, and adds three self-service helpers:

        pcb_layers        the board's layer table without spawning kicad-cli at all
        kicad_cli_help    the real --help text for any subcommand (flag self-discovery)
        kicad_cli         one escape hatch: any argv, with new-file detection

    Flags were read out of kicad/cli/command_*.cpp (`m_argParser.add_argument(...)` plus the
    ARG_* macros in command.h / command_pcb_export_base.h) and confirmed against the built
    binary's own --help.  A subcommand the fork PARSES but REFUSES gets no tool of its own --
    a tool whose only possible outcome is failure is a trap on an agent's tool list, since the
    only signal it carries is a name that looks callable.  `pcb export hpgl` is the one such
    case (removed in KiCad 10.0); it is listed in REMOVED_SUBCOMMANDS instead, which is what
    kicad_cli_help() reports and what the server's instructions name.  `sch export hpgl` is
    NOT in that class: the schematic side still accepts the format, so sch_export_plot keeps
    it (and reports the removal only if the fork ever refuses it).

DEPENDENCY INJECTION (same contract as tools_autoroute.py)
    `configure(ctx)` is called once by the server with a dict or object carrying KICAD_CLI,
    VENV_PY, REPO_ROOT, run, cli_env, need, out_dir, out_path, preflight, read_json.  Nothing
    here reads the environment for layout, so PHICAD_REPO/PHICAD_BUILD point these tools at
    another worktree along with the rest of the server.  Tools are NOT decorated here; the
    server owns the FastMCP instance and registers `TOOLS`.

WHAT EVERY TOOL RETURNS
    The standard run dict (ok/exit_code/command/stderr/elapsed_s) plus:
        outputs      absolute paths of files/dirs that appeared during this run
        output_bytes {path: size}, directories measured recursively
        report       a cheap parsed summary where one is available for free -- drill hole and
                     tool counts, stackup layer count, gerber info JSON, svg/csv counts
        mutated      only for the in-place variants (upgraders), listing what was rewritten
    `ok` means "the CLI exited 0 (or a documented non-zero, e.g. 5 == 'they differ') AND the
    file it promised exists".  A diff's `differs` flag is a result, not a failure.

NOTHING IS WRITTEN IN PLACE UNLESS ASKED
    Exports land under <out>/<tag>/.  The upgraders (pcb/sch/fp/sym) can only rewrite what
    they are given, so they copy the input to <out>/upgrade/ and rewrite the copy;
    `in_place=True` rewrites the original and reports it in `mutated`.
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

#: phicad/pylib/sexp.py, imported by path on first use (see _load_sexp).
_sexp: Any = None

#: phicad/pylib/ibom_json.py, imported by path on first use (see _load_ibom_json).
_ibom: Any = None

_REQUIRED = ("KICAD_CLI", "VENV_PY", "REPO_ROOT", "run", "cli_env", "need",
             "out_dir", "out_path", "preflight", "read_json")

#: Plain exports (one board/schematic in, files out).
T_EXPORT = 300
#: OpenCascade-backed 3D exports and ODB++ chew through a big board.
T_HEAVY = 1200
#: Library-wide operations (a .pretty dir can hold hundreds of footprints).
T_LIB = 600

#: `<kind> diff` and `gerber diff --exit-code-only` return 5 for "they differ".
EXIT_DIFFER = 5

# Subcommands the fork's argument parser still ACCEPTS and whose handler then refuses.  Each
# one is a tool that could only ever fail, so none is registered; kicad_cli_help() surfaces the
# knowledge instead, keyed on the command words an agent would have to type to reach it.
REMOVED_SUBCOMMANDS: dict[tuple[str, ...], dict[str, Any]] = {
    ("pcb", "export", "hpgl"): {
        "removed_in": "10.0",
        "message": "Plotting to HPGL is no longer supported as of KiCad 10.0.",
        "detail": "the subcommand parses and then exits 1; there is no pcb HPGL tool",
        "use_instead": ["pcb_export_ps", "pcb_export_dxf", "pcb_export_svg"],
    },
}


def removed_notes(parts: list[str]) -> list[dict[str, Any]]:
    """Every REMOVED_SUBCOMMANDS entry at or below the queried command words.

    Prefix matching both ways round: asking about `pcb export` should mention that `hpgl` under
    it is gone, and asking about `pcb export hpgl` should say so outright.
    """
    key = tuple(parts)
    out: list[dict[str, Any]] = []
    for cmd, info in REMOVED_SUBCOMMANDS.items():
        if cmd[:len(key)] == key or key[:len(cmd)] == cmd:
            out.append({"command": "kicad-cli " + " ".join(cmd), **info})
    return out


#: `pcb export <name>` 3D subcommands, and the extension each writes.
FMT_3D = {
    "step": ".step", "stpz": ".stpz", "brep": ".brep", "xao": ".xao", "glb": ".glb",
    "ply": ".ply", "stl": ".stl", "u3d": ".u3d", "3dpdf": ".pdf", "vrml": ".wrl",
}
#: 3D formats that accept the OCC geometry-selection flags (VRML has its own, smaller set).
FMT_3D_OCC = tuple(k for k in FMT_3D if k != "vrml")

#: `sch export <name>` plot formats (pdf is server.py's sch_export_pdf).
FMT_SCH_PLOT = {"svg": ".svg", "dxf": ".dxf", "ps": ".ps", "png": ".png", "hpgl": ".plt"}

#: Foreign board formats `pcb import` understands (kicad/cli/command_pcb_import.cpp).
PCB_IMPORT_FORMATS = ("auto", "pads", "altium", "eagle", "cadstar", "fabmaster", "pcad",
                      "solidworks")
#: Foreign schematic formats `sch import` understands (command_sch_import.cpp).
SCH_IMPORT_FORMATS = ("auto", "altium", "eagle", "cadstar", "easyeda", "easyedapro",
                      "ltspice", "pads", "diptrace", "pcad", "orcad")


def _pick(ctx: Any, name: str) -> Any:
    """Read one dependency out of a dict-ish or attribute-ish context."""
    if isinstance(ctx, dict):
        return ctx.get(name)
    return getattr(ctx, name, None)


def configure(ctx: Any) -> dict[str, Any]:
    """Bind the server's helpers into this module. Called once, before any tool runs."""
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
        "missing": [],
        "kicad_cli": str(KICAD_CLI),
        "kicad_cli_exists": KICAD_CLI.exists(),
        "tools": [fn.__name__ for fn in TOOLS],
    }


def _unconfigured() -> dict[str, Any] | None:
    if _run is None or REPO_ROOT is None:
        return {"ok": False,
                "error": "phicad.mcp.tools_cli is not configured",
                "hint": "the server must call tools_cli.configure(ctx) at import time"}
    return None


def _ready() -> dict[str, Any] | None:
    """Both gates every tool opens with: wiring present, and kicad-cli actually built."""
    return _unconfigured() or _preflight()


# --------------------------------------------------------------------------------------
# Output bookkeeping — what did the CLI actually write?
#
# kicad-cli decides most output names itself (per-layer plot files, PTH/NPTH drill pairs, an
# ODB++ tree, a library's worth of SVGs).  Rather than guess, snapshot the directories a
# command may touch and diff them afterwards.  Serves the typed tools and kicad_cli() alike.
# --------------------------------------------------------------------------------------


def _snapshot(dirs: list[Path]) -> set[str]:
    """Identity of everything under `dirs`, as path + mtime_ns + size.

    The mtime/size are part of the key on purpose: exporting twice into the same directory
    OVERWRITES files rather than creating them, and a plain set-of-paths diff would then report
    zero outputs and (through _finish) a bogus ok=False. Comparing identity catches a rewrite.
    """
    seen: set[str] = set()
    for d in dirs:
        if not d.is_dir():
            continue
        for p in d.rglob("*"):
            try:
                st = p.stat()
                seen.add(f"{p}\x00{st.st_mtime_ns}\x00{st.st_size}")
            except OSError:
                seen.add(f"{p}\x00?\x00?")
    return seen


def _appeared(before: set[str], dirs: list[Path]) -> list[str]:
    """Files (and top-level dirs) created OR rewritten during the run, sorted, deduped."""
    new = sorted({e.split("\x00", 1)[0] for e in (_snapshot(dirs) - before)})
    roots: list[str] = []
    for p in new:
        # Drop children of new directories: "odb/" beats 400 leaf files.
        if not any(p.startswith(r.rstrip("/") + "/") for r in roots):
            roots.append(p)
    return roots


def _size(path: Path) -> int:
    try:
        if path.is_dir():
            return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())
        return path.stat().st_size
    except OSError:
        return 0


_CREATED_RE = re.compile(
    r"(?:Created file|Plotted to|Successfully created|written to)[: ]+'?([^'\n]+)'?")


def _mentioned_paths(text: str) -> list[str]:
    """Paths kicad-cli says it wrote; a safety net when snapshotting misses a location."""
    out: list[str] = []
    for m in _CREATED_RE.finditer(text or ""):
        cand = m.group(1).strip().strip("'\"")
        if cand.startswith("/") and Path(cand).exists():
            out.append(str(Path(cand)))
    return sorted(set(out))


def _finish(res: dict[str, Any], *, watch: list[Path], before: set[str],
            require: Path | None = None, keep_stdout: bool = False) -> dict[str, Any]:
    """Attach outputs/sizes to a run result; decide `ok` on evidence, not exit code alone."""
    outs = _appeared(before, watch)
    for extra in _mentioned_paths(res.get("stdout", "")):
        if extra not in outs:
            outs.append(extra)
    if require is not None and require.exists() and str(require) not in outs:
        outs.append(str(require))
    outs = sorted(set(outs))

    res["outputs"] = outs
    res["output_bytes"] = {p: _size(Path(p)) for p in outs}
    if require is not None:
        res["ok"] = bool(res.get("ok")) and require.exists() and _size(require) > 0
    else:
        res["ok"] = bool(res.get("ok")) and bool(outs)
    if not keep_stdout:
        res["stdout_tail"] = "\n".join((res.get("stdout") or "").splitlines()[-12:])
        res.pop("stdout", None)
    return res


def _dir_arg(path: Path) -> str:
    """A -o value the CLI reads as a directory (several commands insist on the separator)."""
    path.mkdir(parents=True, exist_ok=True)
    return str(path) + "/"


def _tag_dir(tag: str, stem: str) -> Path:
    d = _out_dir(tag) / stem
    d.mkdir(parents=True, exist_ok=True)
    return d


def _flag(argv: list[str], name: str, on: bool) -> None:
    if on:
        argv.append(name)


def _opt(argv: list[str], name: str, value: Any) -> None:
    if value is None:
        return
    if isinstance(value, str) and not value.strip():
        return
    argv += [name, str(value)]


def _copy_for_edit(src: Path, dest_dir: Path) -> tuple[Path, list[str]]:
    """Copy an input plus its project sidecars, so an in-place-only command edits a copy."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / src.name
    if src.is_dir():
        if dest.exists():
            shutil.rmtree(dest)
        shutil.copytree(src, dest)
        return dest, []
    shutil.copy2(src, dest)
    carried: list[str] = []
    for suffix in (".kicad_pro", ".kicad_dru", ".kicad_prl"):
        side = src.with_suffix(suffix)
        if side.exists() and side != src:
            shutil.copy2(side, dest_dir / side.name)
            carried.append(str(dest_dir / side.name))
    return dest, carried


def _need_dir(path: str, *, suffix: str | None = None) -> tuple[Path | None,
                                                               dict[str, Any] | None]:
    """`need()` for a directory input (a .pretty library), since need() wants a file."""
    if not path:
        return None, {"ok": False, "error": "empty path"}
    p = Path(path).expanduser()
    p = (REPO_ROOT / p).resolve() if not p.is_absolute() else p.resolve()
    if not p.is_dir():
        return None, {"ok": False, "error": f"directory not found: {p}"}
    if suffix and p.suffix != suffix:
        return None, {"ok": False,
                      "error": f"expected a {suffix} directory, got {p.suffix or 'none'}",
                      "path": str(p)}
    return p, None


def _bad(field: str, value: Any, allowed: Any) -> dict[str, Any]:
    return {"ok": False, "error": f"invalid {field}: {value!r}", "allowed": list(allowed)}


# --------------------------------------------------------------------------------------
# Cheap parsed summaries — free numbers out of the file that was just written.
# --------------------------------------------------------------------------------------


def _drill_report(outputs: list[str]) -> dict[str, Any]:
    """Hole and tool counts straight out of the Excellon/Gerber drill files."""
    rep: dict[str, Any] = {"files": {}, "holes": 0, "tools": 0}
    for p in outputs:
        path = Path(p)
        if path.suffix.lower() not in (".drl", ".gbr", ".nc", ".xnc", ".txt"):
            continue
        try:
            text = path.read_text(errors="replace")
        except OSError:
            continue
        if path.suffix.lower() == ".gbr":
            holes = len(re.findall(r"D0?3\*", text))
            tools = len(re.findall(r"^%ADD\d+", text, re.M))
        else:
            holes = len(re.findall(r"^X[-\d.]+", text, re.M))
            tools = len(re.findall(r"^T\d+C", text, re.M))
        rep["files"][str(path)] = {"holes": holes, "tools": tools}
        rep["holes"] += holes
        rep["tools"] += tools
    return rep


def _stackup_json_report(data: Any) -> dict[str, Any]:
    """Summarize the protobuf-JSON shape emitted by ``pcb export stackup --format json``.

    The CLI serializes ``kiapi.board.BoardStackup``.  Its layer display name is ``userName``
    (not ``name``), and its type is the enum value ``BSLT_COPPER``.  Protobuf JSON omits unset
    optional/details fields, so only the fields used for this summary are validated here.
    """
    if not isinstance(data, dict):
        return {"parse_error": "stackup JSON root is not an object"}

    layers = data.get("layers")
    if not isinstance(layers, list):
        return {"parse_error": "stackup JSON layers is not an array"}

    names: list[str | None] = []
    copper_layers = 0
    for index, layer in enumerate(layers):
        if not isinstance(layer, dict):
            return {"parse_error": f"stackup JSON layer {index} is not an object"}

        name = layer.get("userName")
        if name is not None and (not isinstance(name, str) or not name):
            return {"parse_error": f"stackup JSON layer {index} has an invalid userName"}

        layer_type = layer.get("type")
        if not isinstance(layer_type, str) or not layer_type:
            return {"parse_error": f"stackup JSON layer {index} has an invalid type"}

        names.append(name)
        if layer_type == "BSLT_COPPER":
            copper_layers += 1

    return {
        "layer_count": len(layers),
        "layers": names[:40],
        "copper_layers": copper_layers,
    }


def _stackup_report(path: Path, fmt: str) -> dict[str, Any]:
    """Layer count (and names, when the format gives them) from a stackup report."""
    try:
        text = path.read_text(errors="replace")
    except OSError:
        return {}
    if fmt == "json":
        try:
            data = json.loads(text)
        except ValueError:
            return {"parse_error": "not valid json"}
        return _stackup_json_report(data)
    rows = [ln for ln in text.splitlines() if ln.strip()]
    return {"layer_count": max(len(rows) - 1, 0), "header": rows[0] if rows else None,
            "preview": rows[1:6]}


def _svg_report(outputs: list[str]) -> dict[str, Any]:
    svgs = [p for p in outputs if p.lower().endswith(".svg")]
    return {"svg_count": len(svgs), "svgs": [Path(p).name for p in svgs[:40]]}


def _csv_report(path: Path) -> dict[str, Any]:
    try:
        rows = [ln for ln in path.read_text(errors="replace").splitlines() if ln.strip()]
    except OSError:
        return {}
    return {"header": rows[0] if rows else None, "row_count": max(len(rows) - 1, 0),
            "preview": rows[1:6]}


# ======================================================================================
# Footprint libraries — `kicad-cli fp {export svg, upgrade, diff}`
# ======================================================================================


def fp_export_svg(lib_dir: str, out_dir: str | None = None, footprint: str | None = None,
                  layers: str | None = None, theme: str | None = None,
                  black_and_white: bool = False, sketch_pads_on_fab: bool = False,
                  sketch_pad_numbers: bool = False, hide_dnp_on_fab: bool = False,
                  sketch_dnp_on_fab: bool = False, crossout_dnp_on_fab: bool = False,
                  define_vars: list[str] | None = None) -> dict[str, Any]:
    """Render a .pretty footprint library (or one footprint in it) to SVG.

    `kicad-cli fp export svg -o <dir> [--fp NAME] [-l LAYERS] [-t THEME] … <lib.pretty>`.
    lib_dir must be a .pretty DIRECTORY, not a .kicad_mod file — the CLI enforces that. One
    SVG per footprint lands in the output directory (default <out>/fp-svg/<lib>/), so
    `report.svg_count` is the number of footprints rendered. Read-only on the library.
    `layers` is a comma list of untranslated names (F.Cu,F.SilkS,F.Fab,…); omit it for the
    footprint editor's default set. `define_vars` entries are KEY=VALUE text-variable
    overrides passed as -D.
    """
    if (err := _ready()):
        return err
    lib, err = _need_dir(lib_dir, suffix=".pretty")
    if err:
        return err

    dest = Path(out_dir).expanduser() if out_dir else _tag_dir("fp-svg", lib.stem)
    dest.mkdir(parents=True, exist_ok=True)
    before = _snapshot([dest])

    argv = [str(KICAD_CLI), "fp", "export", "svg", "-o", _dir_arg(dest)]
    _opt(argv, "--footprint", footprint)
    _opt(argv, "--layers", layers)
    _opt(argv, "--theme", theme)
    _flag(argv, "--black-and-white", black_and_white)
    _flag(argv, "--sketch-pads-on-fab-layers", sketch_pads_on_fab)
    _flag(argv, "--sketch-pad-numbers", sketch_pad_numbers)
    _flag(argv, "--hide-DNP-footprints-on-fab-layers", hide_dnp_on_fab)
    _flag(argv, "--sketch-DNP-footprints-on-fab-layers", sketch_dnp_on_fab)
    _flag(argv, "--crossout-DNP-footprints-on-fab-layers", crossout_dnp_on_fab)
    for var in define_vars or []:
        argv += ["-D", var]
    argv.append(str(lib))

    res = _run(argv, timeout=T_LIB, env=_cli_env())
    res["library"] = str(lib)
    res["out_dir"] = str(dest)
    res = _finish(res, watch=[dest], before=before)
    res["report"] = _svg_report(res["outputs"])
    return res


def fp_upgrade(lib_dir: str, out_dir: str | None = None, force: bool = False,
               in_place: bool = False) -> dict[str, Any]:
    """Rewrite a .pretty footprint library in the current file format.

    `kicad-cli fp upgrade -o <dir> [--force] <lib.pretty>`. Without --force only footprints
    older than the current format are resaved, so an already-current library legitimately
    reports nothing resaved (ok=True). By default a fresh copy of the library is written under
    <out>/upgrade/<lib>.pretty and the input is untouched; in_place=True rewrites the given
    library and lists every file it wrote under `mutated`. `report.resaved` names the files
    whose mtime moved.
    """
    if (err := _ready()):
        return err
    lib, err = _need_dir(lib_dir, suffix=".pretty")
    if err:
        return err

    if in_place:
        dest = lib
    elif out_dir:
        dest = Path(out_dir).expanduser()
    else:
        dest = _out_dir("upgrade") / lib.name
    dest.parent.mkdir(parents=True, exist_ok=True)
    before_mods = {p.name: p.stat().st_mtime for p in lib.glob("*.kicad_mod")}
    before = _snapshot([dest.parent])

    argv = [str(KICAD_CLI), "fp", "upgrade", "-o", _dir_arg(dest)]
    _flag(argv, "--force", force)
    argv.append(str(lib))
    res = _run(argv, timeout=T_LIB, env=_cli_env())

    res["library"] = str(lib)
    res["output_library"] = str(dest)
    res["in_place"] = bool(in_place)
    written = sorted(str(p) for p in dest.glob("*.kicad_mod")) if dest.is_dir() else []
    res = _finish(res, watch=[dest.parent], before=before)
    res["outputs"] = [] if in_place else sorted(set(res["outputs"]) | set(written))
    res["output_bytes"] = {p: _size(Path(p)) for p in res["outputs"]}
    res["ok"] = res.get("exit_code") == 0
    res["mutated"] = written if in_place else []
    res["report"] = {
        "footprints_in": len(before_mods),
        "footprints_out": len(written),
        "resaved": [Path(p).name for p in written
                    if Path(p).stat().st_mtime > before_mods.get(Path(p).name, 0)][:40],
    }
    return res


def fp_diff(library_a: str, library_b: str, fmt: str = "json",
            out_file: str | None = None) -> dict[str, Any]:
    """Semantic diff of two .pretty footprint library directories.

    `kicad-cli fp diff --format {json,text,png,svg} -o <path> <dirA> <dirB>`. Exit 5 means
    "they differ" — a result, not a failure: read `differs`. json/text also print to stdout
    and come back parsed (`report`) or raw (`text`); png/svg require a file, so one is created
    under <out>/diff/ automatically.
    """
    return _library_diff("fp", library_a, library_b, fmt, out_file, is_dir=True)


# ======================================================================================
# Symbol libraries — `kicad-cli sym {export svg, upgrade, diff}`
# ======================================================================================


def sym_export_svg(lib_path: str, out_dir: str | None = None, symbol: str | None = None,
                   theme: str | None = None, black_and_white: bool = False,
                   include_hidden_pins: bool = False,
                   include_hidden_fields: bool = False) -> dict[str, Any]:
    """Render a .kicad_sym symbol library (or one symbol in it) to SVG.

    `kicad-cli sym export svg -o <dir> [-s SYMBOL] [-t THEME] … <lib.kicad_sym>`. One SVG per
    symbol unit lands in the output directory (default <out>/sym-svg/<lib>/), so
    `report.svg_count` counts what was rendered — a multi-unit symbol yields several files.
    Read-only on the library.
    """
    if (err := _ready()):
        return err
    lib, err = _need(lib_path, ".kicad_sym")
    if err:
        return err

    dest = Path(out_dir).expanduser() if out_dir else _tag_dir("sym-svg", lib.stem)
    dest.mkdir(parents=True, exist_ok=True)
    before = _snapshot([dest])

    argv = [str(KICAD_CLI), "sym", "export", "svg", "-o", _dir_arg(dest)]
    _opt(argv, "--symbol", symbol)
    _opt(argv, "--theme", theme)
    _flag(argv, "--black-and-white", black_and_white)
    _flag(argv, "--include-hidden-pins", include_hidden_pins)
    _flag(argv, "--include-hidden-fields", include_hidden_fields)
    argv.append(str(lib))

    res = _run(argv, timeout=T_LIB, env=_cli_env())
    res["library"] = str(lib)
    res["out_dir"] = str(dest)
    res = _finish(res, watch=[dest], before=before)
    res["report"] = _svg_report(res["outputs"])
    return res


def sym_upgrade(lib_path: str, out_path: str | None = None, force: bool = False,
                in_place: bool = False) -> dict[str, Any]:
    """Rewrite a .kicad_sym symbol library in the current file format.

    `kicad-cli sym upgrade -o <path> [--force] <lib.kicad_sym>`. Without --force an already
    current library is left alone (ok=True, nothing changed). Defaults to writing a copy under
    <out>/upgrade/; in_place=True rewrites the input and reports it in `mutated`. `report`
    carries the byte size before/after and the symbol count in the result.
    """
    if (err := _ready()):
        return err
    lib, err = _need(lib_path, ".kicad_sym")
    if err:
        return err

    if in_place:
        dest = lib
    elif out_path:
        dest = Path(out_path).expanduser()
        if not dest.is_absolute():
            dest = _out_dir("upgrade") / dest
    else:
        dest = _out_dir("upgrade") / lib.name
    dest.parent.mkdir(parents=True, exist_ok=True)
    before_bytes = lib.stat().st_size
    before = _snapshot([dest.parent])

    argv = [str(KICAD_CLI), "sym", "upgrade", "-o", str(dest)]
    _flag(argv, "--force", force)
    argv.append(str(lib))
    res = _run(argv, timeout=T_LIB, env=_cli_env())

    res["library"] = str(lib)
    res["output_library"] = str(dest) if dest.exists() else None
    res["in_place"] = bool(in_place)
    res = _finish(res, watch=[dest.parent], before=before,
                  require=None if in_place else dest)
    if in_place:
        res["ok"] = res.get("exit_code") == 0
        res["mutated"] = [str(lib)]
        res["outputs"] = []
    else:
        res["mutated"] = []
    res["report"] = {
        "bytes_before": before_bytes,
        "bytes_after": _size(dest) if dest.exists() else 0,
        "symbols_after": (dest.read_text(errors="replace").count('(symbol "')
                          if dest.is_file() else None),
    }
    return res


def sym_diff(file_a: str, file_b: str, fmt: str = "json",
             out_file: str | None = None) -> dict[str, Any]:
    """Semantic diff of two .kicad_sym symbol libraries.

    `kicad-cli sym diff --format {json,text,png,svg} -o <path> <a> <b>`. Exit 5 means "they
    differ" — read `differs`, not `ok`. json/text come back inline; png/svg are written under
    <out>/diff/ because the CLI requires an output path for them.
    """
    return _library_diff("sym", file_a, file_b, fmt, out_file, suffix=".kicad_sym")


def _library_diff(kind: str, a: str, b: str, fmt: str, out_file: str | None,
                  *, is_dir: bool = False, suffix: str | None = None) -> dict[str, Any]:
    """Shared body for fp/sym diff — kicad/cli/command_diff_base.h gives both the same flags."""
    if (err := _ready()):
        return err
    fmt = (fmt or "json").lower()
    if fmt not in ("json", "text", "png", "svg"):
        return _bad("fmt", fmt, ("json", "text", "png", "svg"))

    if is_dir:
        pa, err = _need_dir(a)
        if err:
            return err
        pb, err = _need_dir(b)
        if err:
            return err
    else:
        pa, err = _need(a, suffix) if suffix else _need(a)
        if err:
            return err
        pb, err = _need(b, suffix) if suffix else _need(b)
        if err:
            return err

    ext = {"json": ".json", "text": ".txt", "png": ".png", "svg": ".svg"}[fmt]
    report = _out_path("diff", f"{pa.stem}--{pb.stem}", f".{kind}diff{ext}", out_file)
    before = _snapshot([report.parent])

    argv = [str(KICAD_CLI), kind, "diff", "--format", fmt, "-o", str(report),
            str(pa), str(pb)]
    # A JSON diff can be reported on stdout instead of the report file: never clip it.
    res = _run(argv, timeout=T_LIB, env=_cli_env(), ok_codes=(0, EXIT_DIFFER),
               stdout_limit=None)
    res["a"] = str(pa)
    res["b"] = str(pb)
    res["format"] = fmt
    res["differs"] = res.get("exit_code") == EXIT_DIFFER
    res["identical"] = res.get("exit_code") == 0

    payload = None
    if fmt == "json":
        payload = _read_json(report) if report.exists() else None
        if payload is None and (res.get("stdout") or "").lstrip().startswith(("{", "[")):
            try:
                payload = json.loads(res["stdout"])
            except ValueError:
                payload = None
    elif fmt == "text":
        res["text"] = (report.read_text(errors="replace")[-4000:] if report.exists()
                       else (res.get("stdout") or "")[-4000:])

    res = _finish(res, watch=[report.parent], before=before,
                  require=report if fmt in ("png", "svg") else None)
    if fmt in ("json", "text"):
        # These print to stdout as well as -o; a documented exit code is enough evidence.
        res["ok"] = res.get("exit_code") in (0, EXIT_DIFFER)
    if isinstance(payload, dict):
        changes = [c for c in (payload.get("changes") or []) if isinstance(c, dict)]
        kinds: dict[str, int] = {}
        for c in changes:
            k = str(c.get("kind", "?"))
            kinds[k] = kinds.get(k, 0) + 1
        res["report"] = {
            "doc_type": payload.get("docType"),
            "change_count": len(changes),
            "changes_by_kind": kinds,
            "changes": [{"kind": c.get("kind"), "type": c.get("typeName"),
                         "name": c.get("refdes") or c.get("name")} for c in changes[:20]],
        }
    return res


# ======================================================================================
# Gerber / Excellon toolbox — `kicad-cli gerber {info, diff, convert png}`
#
# These run in the GerbView kiface, so they read fab output back the way a fab house does:
# no board file involved, which makes them the honest check on an export.
# ======================================================================================


def gerber_info(file_path: str, units: str = "mm", area: bool = True, strict: bool = False,
                fmt: str = "json") -> dict[str, Any]:
    """Inspect one Gerber or Excellon file: type, item/aperture counts, bbox, copper area.

    `kicad-cli gerber info --format {json,text} --units {mm,inch,mils} [--area] [--strict]`.
    Writes nothing — the report goes to stdout, so the parsed JSON comes back under `report`
    and the human rendering under `text`. `--area` computes real copper area (worth the extra
    work; on by default here). `--strict` turns parse warnings into a non-zero exit, which is
    how you assert an export is clean rather than merely readable.
    """
    if (err := _ready()):
        return err
    fmt = (fmt or "json").lower()
    if fmt not in ("json", "text"):
        return _bad("fmt", fmt, ("json", "text"))
    units = (units or "mm").lower()
    if units not in ("mm", "inch", "mils"):
        return _bad("units", units, ("mm", "inch", "mils"))
    src, err = _need(file_path)
    if err:
        return err

    argv = [str(KICAD_CLI), "gerber", "info", "--format", fmt, "--units", units]
    _flag(argv, "--area", area)
    _flag(argv, "--strict", strict)
    argv.append(str(src))

    res = _run(argv, timeout=T_EXPORT, env=_cli_env())
    res["file"] = str(src)
    res["units"] = units
    res["outputs"] = []
    text = res.get("stdout") or ""
    if fmt == "json":
        try:
            res["report"] = json.loads(text)
        except ValueError:
            res["report"] = None
            res["text"] = text[-4000:]
            res["ok"] = False
        else:
            res.pop("stdout", None)
    else:
        res["text"] = text[-8000:]
        res.pop("stdout", None)
    return res


def gerber_diff(file_a: str, file_b: str, fmt: str = "json", out_file: str | None = None,
                dpi: int = 300, tolerance_nm: int = 0, no_align: bool = False,
                transparent: bool = False, no_antialias: bool = False,
                strict: bool = False, exit_code_only: bool = False) -> dict[str, Any]:
    """Geometric diff of two Gerber/Excellon files: added/removed copper area.

    `kicad-cli gerber diff --format {png,text,json} [-o PATH] [--dpi N] [--tolerance NM]
    [--no-align] [--transparent] [--no-antialias] [--strict] [--exit-code-only] <a> <b>`.
    This is an AREA comparison, not a text diff: the two files are rasterised to polygon sets
    and booleaned. Exit 5 means "they differ" (`differs`); exit 0 means the difference is below
    1 µm². By default the two files' bounding boxes are origin-aligned first — pass
    no_align=True when you are testing absolute placement, which alignment would hide.
    png output is written under <out>/gerber-diff/; json/text also print to stdout.
    """
    if (err := _ready()):
        return err
    fmt = (fmt or "json").lower()
    if fmt not in ("png", "text", "json"):
        return _bad("fmt", fmt, ("png", "text", "json"))
    pa, err = _need(file_a)
    if err:
        return err
    pb, err = _need(file_b)
    if err:
        return err

    ext = {"png": ".png", "text": ".txt", "json": ".json"}[fmt]
    report = _out_path("gerber-diff", f"{pa.stem}--{pb.stem}", f".diff{ext}", out_file)
    before = _snapshot([report.parent])

    argv = [str(KICAD_CLI), "gerber", "diff", "--format", fmt]
    if not exit_code_only:
        argv += ["-o", str(report)]
    _opt(argv, "--dpi", dpi if fmt == "png" else None)
    _opt(argv, "--tolerance", tolerance_nm if tolerance_nm else None)
    _flag(argv, "--no-align", no_align)
    _flag(argv, "--transparent", transparent)
    _flag(argv, "--no-antialias", no_antialias)
    _flag(argv, "--strict", strict)
    _flag(argv, "--exit-code-only", exit_code_only)
    argv += [str(pa), str(pb)]

    res = _run(argv, timeout=T_EXPORT, env=_cli_env(), ok_codes=(0, EXIT_DIFFER),
               stdout_limit=None)
    res["a"] = str(pa)
    res["b"] = str(pb)
    res["format"] = fmt
    res["differs"] = res.get("exit_code") == EXIT_DIFFER
    res["identical"] = res.get("exit_code") == 0
    stdout = res.get("stdout") or ""

    if fmt == "json" and not exit_code_only:
        payload = _read_json(report) if report.exists() else None
        if payload is None and stdout.lstrip().startswith("{"):
            try:
                payload = json.loads(stdout)
            except ValueError:
                payload = None
        res["report"] = payload
    elif fmt == "text" and not exit_code_only:
        res["text"] = (report.read_text(errors="replace")[-4000:] if report.exists()
                       else stdout[-4000:])

    res = _finish(res, watch=[report.parent], before=before,
                  require=report if fmt == "png" and not exit_code_only else None)
    if exit_code_only or fmt in ("json", "text"):
        res["ok"] = res.get("exit_code") in (0, EXIT_DIFFER)
    return res


def gerber_convert_png(file_path: str, out_png: str | None = None, dpi: int = 300,
                       width_px: int = 0, height_px: int = 0, units: str = "mm",
                       origin_x: float = 0.0, origin_y: float = 0.0,
                       window_width: float = 0.0, window_height: float = 0.0,
                       foreground: str | None = None, background: str | None = None,
                       transparent: bool = False, no_antialias: bool = False,
                       strict: bool = False) -> dict[str, Any]:
    """Rasterise one Gerber/Excellon file to PNG — a visual check on fab output.

    `kicad-cli gerber convert png -o <png> [--dpi N | --width PX --height PX]
    [--units mm|inch|mils --origin-x/--origin-y --window-width/--window-height]
    [--foreground #RRGGBB --background #RRGGBB] [--transparent] [--no-antialias] [--strict]`.
    width_px/height_px override DPI when non-zero. The viewport flags crop to a region:
    window_width AND window_height must be given together (the CLI rejects one alone). Writes
    <out>/gerber-png/<name>.png by default; `report.bytes` is the PNG size.
    """
    if (err := _ready()):
        return err
    units = (units or "mm").lower()
    if units not in ("mm", "inch", "mils"):
        return _bad("units", units, ("mm", "inch", "mils"))
    if (window_width > 0) != (window_height > 0):
        return {"ok": False,
                "error": "window_width and window_height must both be set (or both left 0)"}
    src, err = _need(file_path)
    if err:
        return err

    png = _out_path("gerber-png", src.name.replace(".", "_"), ".png", out_png)
    before = _snapshot([png.parent])

    argv = [str(KICAD_CLI), "gerber", "convert", "png", "-o", str(png), "--units", units]
    _opt(argv, "--dpi", dpi)
    _opt(argv, "--width", width_px if width_px else None)
    _opt(argv, "--height", height_px if height_px else None)
    _opt(argv, "--origin-x", origin_x if origin_x else None)
    _opt(argv, "--origin-y", origin_y if origin_y else None)
    _opt(argv, "--window-width", window_width if window_width else None)
    _opt(argv, "--window-height", window_height if window_height else None)
    _opt(argv, "--foreground", foreground)
    _opt(argv, "--background", background)
    _flag(argv, "--transparent", transparent)
    _flag(argv, "--no-antialias", no_antialias)
    _flag(argv, "--strict", strict)
    argv.append(str(src))

    res = _run(argv, timeout=T_EXPORT, env=_cli_env())
    res["file"] = str(src)
    res["png_path"] = str(png) if png.exists() else None
    res = _finish(res, watch=[png.parent], before=before, require=png)
    res["report"] = {"bytes": _size(png), "dpi": dpi,
                     "width_px": width_px or None, "height_px": height_px or None}
    return res


# ======================================================================================
# Board exports — the fabrication and documentation formats server.py does not cover.
# ======================================================================================


def pcb_export_drill(board_path: str, out_dir: str | None = None, fmt: str = "excellon",
                     units: str = "mm", drill_origin: str = "absolute",
                     zeros_format: str = "decimal", oval_format: str = "alternate",
                     map_format: str = "pdf", separate_th: bool = False,
                     generate_map: bool = False, generate_report: bool = False,
                     generate_tenting: bool = False, mirror_y: bool = False,
                     minimal_header: bool = False,
                     gerber_precision: int = 6) -> dict[str, Any]:
    """Drill files for fabrication (Excellon by default, Gerber X2 optionally).

    `kicad-cli pcb export drill -o <dir>/ --format {excellon,gerber} -u {mm,in}
    --drill-origin {absolute,plot} --excellon-zeros-format {decimal,suppressleading,
    suppresstrailing,keep} --excellon-oval-format {route,alternate} --map-format
    {pdf,gerberx2,ps,dxf,svg} [--excellon-separate-th] [--generate-map] [--generate-report]
    [--generate-tenting] [--excellon-mirror-y] [--excellon-min-header]
    [--gerber-precision 5|6]`.

    The output is a DIRECTORY (default <out>/drill/<board>/): one .drl for combined
    PTH+NPTH, or -PTH/-NPTH pairs with separate_th=True, plus the map/report when asked.
    `report.holes` and `report.tools` are counted out of the written files, per file and in
    total — that is the number to compare against a board's via/pad-hole count.
    """
    if (err := _ready()):
        return err
    fmt = (fmt or "excellon").lower()
    if fmt not in ("excellon", "gerber"):
        return _bad("fmt", fmt, ("excellon", "gerber"))
    units = (units or "mm").lower()
    if units not in ("mm", "in"):
        return _bad("units", units, ("mm", "in"))
    if drill_origin not in ("absolute", "plot"):
        return _bad("drill_origin", drill_origin, ("absolute", "plot"))
    if zeros_format not in ("decimal", "suppressleading", "suppresstrailing", "keep"):
        return _bad("zeros_format", zeros_format,
                    ("decimal", "suppressleading", "suppresstrailing", "keep"))
    if oval_format not in ("route", "alternate"):
        return _bad("oval_format", oval_format, ("route", "alternate"))
    if map_format not in ("pdf", "gerberx2", "ps", "dxf", "svg"):
        return _bad("map_format", map_format, ("pdf", "gerberx2", "ps", "dxf", "svg"))
    if gerber_precision not in (5, 6):
        return _bad("gerber_precision", gerber_precision, (5, 6))
    board, err = _need(board_path, ".kicad_pcb")
    if err:
        return err

    dest = Path(out_dir).expanduser() if out_dir else _tag_dir("drill", board.stem)
    before = _snapshot([dest])

    argv = [str(KICAD_CLI), "pcb", "export", "drill", "-o", _dir_arg(dest),
            "--format", fmt, "-u", units, "--drill-origin", drill_origin,
            "--excellon-zeros-format", zeros_format, "--excellon-oval-format", oval_format,
            "--map-format", map_format, "--gerber-precision", str(gerber_precision)]
    _flag(argv, "--excellon-separate-th", separate_th)
    _flag(argv, "--generate-map", generate_map)
    _flag(argv, "--generate-report", generate_report)
    _flag(argv, "--generate-tenting", generate_tenting)
    _flag(argv, "--excellon-mirror-y", mirror_y)
    _flag(argv, "--excellon-min-header", minimal_header)
    argv.append(str(board))

    res = _run(argv, timeout=T_EXPORT, env=_cli_env())
    res["board"] = str(board)
    res["out_dir"] = str(dest)
    res = _finish(res, watch=[dest], before=before)
    res["report"] = _drill_report(res["outputs"])
    res["report"]["format"] = fmt
    return res


# --------------------------------------------------------------------------------------
# InteractiveHtmlBom (MIT) via its generic JSON input
#
# ibom normally reads a board through `import pcbnew`, which this fork does not ship, and its
# IPC-API backend is still an open upstream PR.  Its documented generic-JSON input is the
# third door, and phicad/pylib/ibom_json.py builds that document out of three kicad-cli
# exports.  ibom itself is a SUBPROCESS of a clone/pip install under build/ -- never vendored,
# because it is MIT and the fork's licence inventory (phicad/README.md) stays honest only if
# third-party trees are not copied into it.
# --------------------------------------------------------------------------------------

#: Where an ibom checkout is looked for, in order, after `ibom_dir` and $PHICAD_IBOM.
_IBOM_DIR_NAMES = ("InteractiveHtmlBom", "ibom", "interactive-html-bom")

_IBOM_CLONE_HINT = ("clone it next to the build tree: git clone --depth 1 "
                    "https://github.com/openscopeproject/InteractiveHtmlBom.git "
                    "<build>/InteractiveHtmlBom  (or set PHICAD_IBOM to an existing checkout)")


def _ibom_script(ibom_dir: str | None) -> tuple[Path | None, list[str]]:
    """Locate generate_interactive_bom.py. Returns (script, directories that were tried)."""
    tried: list[str] = []
    roots: list[Path] = []
    for raw in (ibom_dir, os.environ.get("PHICAD_IBOM")):
        if raw:
            roots.append(Path(raw).expanduser())
    build_roots: list[Path] = []
    if REPO_ROOT is not None:
        build_roots.append(REPO_ROOT / "build")
    if KICAD_CLI is not None and len(KICAD_CLI.parents) >= 3:
        # <build>/<platform>-dev/kicad/kicad-cli -> <build>
        build_roots.append(KICAD_CLI.parents[2])
    for base in build_roots:
        roots += [base / name for name in _IBOM_DIR_NAMES]

    for root in roots:
        if root.is_file() and root.name == "generate_interactive_bom.py":
            return root, tried
        tried.append(str(root))
        for cand in (root / "InteractiveHtmlBom" / "generate_interactive_bom.py",
                     root / "generate_interactive_bom.py"):
            if cand.is_file():
                return cand, tried
    return None, tried


def _ibom_python() -> Path:
    """An interpreter that can import jsonschema (ibom validates its own input with it).

    The automation venv is preferred over the interpreter running the server so that a stray
    dependency stays out of the MCP venv; either is acceptable, and the check is one -c run.
    """
    for py in (VENV_PY, Path(sys.executable)):
        if py is None or not Path(py).exists():
            continue
        probe = _run([str(py), "-c", "import jsonschema"], timeout=60, env=_cli_env())
        if probe.get("ok"):
            return Path(py)
    return Path(sys.executable)


def pcb_export_ibom(board_path: str, out_path: str | None = None, ibom_dir: str | None = None,
                    exclude_dnp: bool = True, dark_mode: bool = False,
                    highlight_pin1: bool = False, keep_json: bool = True,
                    extra_args: list[str] | None = None) -> dict[str, Any]:
    """Interactive assembly BOM (InteractiveHtmlBom) for a board — one self-contained HTML file.

    No `import pcbnew` anywhere: three kicad-cli exports are translated into ibom's documented
    *generic JSON* input by phicad/pylib/ibom_json.py, and `generate_interactive_bom.py` runs on
    that JSON as a subprocess.

        pcb export pos --format csv --side both --units mm   placements -> components
        pcb export ipcd356                                   pads, nets (no schematic needed)
        pcb export svg -l Edge.Cuts --exclude-drawing-sheet   board outline

    ibom is NOT vendored. It is looked for in `ibom_dir`, then $PHICAD_IBOM, then
    <repo>/build/ and the build tree's parent under InteractiveHtmlBom/ibom/
    interactive-html-bom; when none exists the tool fails with the clone command in `hint`.

    The HTML lands at `out_path` (default <out>/ibom/<board>.html) and the generic JSON next to
    it as <board>.ibom.json when keep_json is true — that file is the thing to inspect when the
    render looks wrong. `report` carries the component count, per-side counts, pad and net
    counts, and `warnings`: what this path CANNOT know (silkscreen and fabrication drawings,
    tracks and zones, pad shapes finer than rect/circle) is listed there, not hidden.
    """
    if (err := _ready()):
        return err
    board, err = _need(board_path, ".kicad_pcb")
    if err:
        return err
    builder = _load_ibom_json()
    if builder is None:
        return {"ok": False, "error": "phicad/pylib/ibom_json.py is missing or failed to import",
                "hint": "the builder ships with the repo; check REPO_ROOT"}
    script, tried = _ibom_script(ibom_dir)
    if script is None:
        return {"ok": False, "error": "InteractiveHtmlBom not found", "searched": tried,
                "hint": _IBOM_CLONE_HINT}

    parser = _load_sexp()
    if parser is None:
        return {"ok": False, "error": "board parser unavailable; cannot normalize IPC-D-356 origin"}
    try:
        setup = parser.child(parser.parse(board.read_text(encoding="utf-8")), "setup")
        origin_node = parser.child(setup, "aux_axis_origin") if setup else None
        aux_origin = (float(origin_node[1]), float(origin_node[2])) if origin_node else (0.0, 0.0)
    except (ValueError, IndexError, OSError) as exc:
        return {"ok": False, "error": f"cannot read board auxiliary origin: {exc}"}

    out = _out_path("ibom", board.stem, ".html", out_path)
    work = out.parent
    work.mkdir(parents=True, exist_ok=True)
    before = _snapshot([work])

    # --- the three kicad-cli exports ---------------------------------------------------
    stage = _tag_dir("ibom", f"{board.stem}-inputs")
    pos = stage / f"{board.stem}.pos.csv"
    argv = [str(KICAD_CLI), "pcb", "export", "pos", "--format", "csv", "--side", "both",
            "--units", "mm", "-o", str(pos)]
    _flag(argv, "--exclude-dnp", exclude_dnp)
    argv.append(str(board))
    res = _run(argv, timeout=T_EXPORT, env=_cli_env())
    res["board"] = str(board)
    if not res.get("ok") or not pos.exists():
        res["ok"] = False
        res["error"] = "pcb export pos failed; without placements there is no BOM"
        return _finish(res, watch=[stage], before=before)

    d356 = stage / f"{board.stem}.d356"
    net_res = _run([str(KICAD_CLI), "pcb", "export", "ipcd356", "-o", str(d356), str(board)],
                   timeout=T_EXPORT, env=_cli_env())
    edge_dir = stage / "edges"
    edge_dir.mkdir(parents=True, exist_ok=True)
    edge_svg = edge_dir / f"{board.stem}-Edge_Cuts.svg"
    svg_res = _run([str(KICAD_CLI), "pcb", "export", "svg", "--mode-single",
                    "-l", "Edge.Cuts", "--exclude-drawing-sheet", "--page-size-mode", "1",
                    "--black-and-white", "--drill-shape-opt", "0", "-o", str(edge_svg), str(board)],
                   timeout=T_EXPORT, env=_cli_env())

    # --- generic JSON ------------------------------------------------------------------
    def text_of(path: Path, ran: dict[str, Any]) -> str | None:
        if ran.get("ok") and path.exists() and path.stat().st_size > 0:
            return path.read_text(errors="replace")
        return None

    try:
        doc = builder.build_from_text(pos.read_text(errors="replace"),
                                      text_of(d356, net_res), text_of(edge_svg, svg_res),
                                      {"title": board.stem, "revision": "", "company": "",
                                       "date": ""}, aux_origin=aux_origin)
    except ValueError as exc:
        res["ok"] = False
        res["error"] = f"could not build ibom generic JSON: {exc}"
        return _finish(res, watch=[stage], before=before)

    problems = builder.validate_document(doc)
    if problems:
        res["ok"] = False
        res["error"] = "generic JSON failed the structural check"
        res["problems"] = problems[:20]
        return _finish(res, watch=[stage], before=before)

    json_path = (work / f"{board.stem}.ibom.json") if keep_json else (stage / "ibom.json")
    json_bytes = builder.write_document(doc, json_path)
    report = builder.summary(doc)

    # --- ibom itself, as a subprocess --------------------------------------------------
    env = _cli_env()
    env["INTERACTIVE_HTML_BOM_NO_DISPLAY"] = "1"   # ibom demands wxPython otherwise
    ibom_argv = [str(_ibom_python()), str(script), "--no-browser",
                 "--dest-dir", str(work), "--name-format", out.stem]
    _flag(ibom_argv, "--dark-mode", dark_mode)
    _flag(ibom_argv, "--highlight-pin1", highlight_pin1)
    ibom_argv += list(extra_args or [])
    ibom_argv.append(str(json_path))

    run = _run(ibom_argv, timeout=T_EXPORT, env=env)
    run["board"] = str(board)
    run = _finish(run, watch=[work], before=before, require=out)
    run["json_path"] = str(json_path) if json_path.exists() else None
    run["inputs"] = {"pos": str(pos),
                     "ipcd356": str(d356) if d356.exists() else None,
                     "edge_svg": str(edge_svg) if edge_svg.exists() else None}
    run["ibom"] = {"script": str(script), "python": ibom_argv[0]}
    report.update({
        "json_bytes": json_bytes,
        "html_bytes": _size(out),
        "netlist": bool(net_res.get("ok") and d356.exists()),
        "outline_svg": bool(svg_res.get("ok") and edge_svg.exists()),
    })
    if not net_res.get("ok"):
        report.setdefault("warnings", []).append(
            "pcb export ipcd356 failed: no pads and no nets in the render")
    run["report"] = report
    return run


def pcb_export_dxf(board_path: str, out_path: str | None = None,
                   layers: str = "F.Cu,B.Cu,Edge.Cuts", common_layers: str | None = None,
                   mode: str = "multi", output_units: str = "mm",
                   use_contours: bool = False, include_border_title: bool = False,
                   exclude_refdes: bool = False, exclude_value: bool = False,
                   subtract_soldermask: bool = False, use_drill_origin: bool = False,
                   drill_shape: int = 2, scale: float = 1.0, check_zones: bool = False,
                   sketch_pads_on_fab: bool = False, sketch_pad_numbers: bool = False,
                   hide_dnp_on_fab: bool = False, sketch_dnp_on_fab: bool = False,
                   crossout_dnp_on_fab: bool = False, drawing_sheet: str | None = None,
                   variant: str | None = None,
                   define_vars: list[str] | None = None) -> dict[str, Any]:
    """DXF plot of a layer list — the format mechanical CAD actually reads.

    `kicad-cli pcb export dxf -o <path> -l LAYERS [--cl COMMON] --mode-multi|--mode-single
    --ou {mm,in} [--uc] [--ibt] [--erd] [--ev] [--subtract-soldermask] [--udo]
    [--drill-shape-opt 0|1|2] [--scale S] [--check-zones] [--sp/--spn/--hdnp/--sdnp/--cdnp]
    [--drawing-sheet F] [--variant V] [-D K=V]`.

    mode="multi" (the default here, and KiCad's future default) writes one DXF per layer into
    a directory; mode="single" writes ONE file containing every layer in `layers`, and then
    `common_layers` is ignored. Passing neither prints a deprecation warning, so this tool
    always passes one. Output defaults to <out>/dxf/<board>/. `--uc` (use_contours) plots
    graphics as outlines, which is what a mechanical importer usually wants.
    """
    if (err := _ready()):
        return err
    if mode not in ("multi", "single"):
        return _bad("mode", mode, ("multi", "single"))
    output_units = (output_units or "mm").lower()
    if output_units not in ("mm", "in"):
        return _bad("output_units", output_units, ("mm", "in"))
    if drill_shape not in (0, 1, 2):
        return _bad("drill_shape", drill_shape, (0, 1, 2))
    board, err = _need(board_path, ".kicad_pcb")
    if err:
        return err

    single = mode == "single"
    if single:
        target = _out_path("dxf", board.stem, ".dxf", out_path)
        watch_dir = target.parent
        out_arg = str(target)
    else:
        target = None
        watch_dir = Path(out_path).expanduser() if out_path else _tag_dir("dxf", board.stem)
        out_arg = _dir_arg(watch_dir)
    before = _snapshot([watch_dir])

    argv = [str(KICAD_CLI), "pcb", "export", "dxf", "-o", out_arg,
            "--mode-single" if single else "--mode-multi",
            "--output-units", output_units, "--drill-shape-opt", str(drill_shape),
            "--scale", str(scale)]
    _opt(argv, "--layers", layers)
    if not single:
        _opt(argv, "--common-layers", common_layers)
    _flag(argv, "--use-contours", use_contours)
    _flag(argv, "--include-border-title", include_border_title)
    _flag(argv, "--exclude-refdes", exclude_refdes)
    _flag(argv, "--exclude-value", exclude_value)
    _flag(argv, "--subtract-soldermask", subtract_soldermask)
    _flag(argv, "--use-drill-origin", use_drill_origin)
    _flag(argv, "--check-zones", check_zones)
    _flag(argv, "--sketch-pads-on-fab-layers", sketch_pads_on_fab)
    _flag(argv, "--sketch-pad-numbers", sketch_pad_numbers)
    _flag(argv, "--hide-DNP-footprints-on-fab-layers", hide_dnp_on_fab)
    _flag(argv, "--sketch-DNP-footprints-on-fab-layers", sketch_dnp_on_fab)
    _flag(argv, "--crossout-DNP-footprints-on-fab-layers", crossout_dnp_on_fab)
    _opt(argv, "--drawing-sheet", drawing_sheet)
    _opt(argv, "--variant", variant)
    for var in define_vars or []:
        argv += ["-D", var]
    argv.append(str(board))

    res = _run(argv, timeout=T_EXPORT, env=_cli_env())
    res["board"] = str(board)
    res["mode"] = mode
    res = _finish(res, watch=[watch_dir], before=before, require=target)
    dxfs = [p for p in res["outputs"] if p.lower().endswith(".dxf")]
    res["report"] = {"dxf_count": len(dxfs), "dxf_files": [Path(p).name for p in dxfs[:40]],
                     "layers_requested": layers}
    return res


def pcb_export_gencad(board_path: str, out_path: str | None = None,
                      flip_bottom_pads: bool = False, unique_pins: bool = False,
                      unique_footprints: bool = False, use_drill_origin: bool = False,
                      store_origin_coord: bool = False,
                      define_vars: list[str] | None = None) -> dict[str, Any]:
    """GenCAD 1.4 export — the format many older test/assembly houses ingest.

    `kicad-cli pcb export gencad -o <file.cad> [-f|--flip-bottom-pads] [--unique-pins]
    [--unique-footprints] [--use-drill-origin] [--store-origin-coord] [-D K=V]`. One text
    file (default <out>/gencad/<board>.cad); `report` counts its $-delimited sections plus
    the number of PADS/SHAPES/COMPONENTS/ROUTES entries, so you can see the board arrived.
    """
    if (err := _ready()):
        return err
    board, err = _need(board_path, ".kicad_pcb")
    if err:
        return err

    out = _out_path("gencad", board.stem, ".cad", out_path)
    before = _snapshot([out.parent])

    argv = [str(KICAD_CLI), "pcb", "export", "gencad", "-o", str(out)]
    _flag(argv, "--flip-bottom-pads", flip_bottom_pads)
    _flag(argv, "--unique-pins", unique_pins)
    _flag(argv, "--unique-footprints", unique_footprints)
    _flag(argv, "--use-drill-origin", use_drill_origin)
    _flag(argv, "--store-origin-coord", store_origin_coord)
    for var in define_vars or []:
        argv += ["-D", var]
    argv.append(str(board))

    res = _run(argv, timeout=T_EXPORT, env=_cli_env())
    res["board"] = str(board)
    res = _finish(res, watch=[out.parent], before=before, require=out)
    if out.exists():
        text = out.read_text(errors="replace")
        res["report"] = {
            "sections": re.findall(r"^\$([A-Z]+)$", text, re.M)[:30],
            "pads": len(re.findall(r"^PAD ", text, re.M)),
            "shapes": len(re.findall(r"^SHAPE ", text, re.M)),
            "components": len(re.findall(r"^COMPONENT ", text, re.M)),
            "signals": len(re.findall(r"^SIGNAL ", text, re.M)),
        }
    return res


def pcb_export_ipcd356(board_path: str, out_path: str | None = None) -> dict[str, Any]:
    """IPC-D-356 netlist — the electrical-test (bed-of-nails / flying probe) handoff.

    `kicad-cli pcb export ipcd356 -o <file.d356> <board>`. No options exist; the format is
    fixed. `report.test_points` counts the 317/327 record lines and `report.nets` the distinct
    net names in them, which is the number a test house cares about.
    """
    if (err := _ready()):
        return err
    board, err = _need(board_path, ".kicad_pcb")
    if err:
        return err

    out = _out_path("ipcd356", board.stem, ".d356", out_path)
    before = _snapshot([out.parent])
    res = _run([str(KICAD_CLI), "pcb", "export", "ipcd356", "-o", str(out), str(board)],
               timeout=T_EXPORT, env=_cli_env())
    res["board"] = str(board)
    res = _finish(res, watch=[out.parent], before=before, require=out)
    if out.exists():
        lines = out.read_text(errors="replace").splitlines()
        records = [ln for ln in lines if ln.startswith(("317", "327"))]
        nets = {ln[3:17].strip() for ln in records if len(ln) > 17}
        res["report"] = {"lines": len(lines), "test_points": len(records),
                         "nets": len(nets), "sample_nets": sorted(nets)[:10]}
    return res


def pcb_export_ipc2581(board_path: str, out_path: str | None = None, precision: int = 6,
                       units: str = "mm", version: str = "C", compress: bool = False,
                       mode: str | None = None, sections: str | None = None,
                       net_names: str = "include", ref_des: str = "include",
                       bom_rev: str | None = None, bom_col_int_id: str | None = None,
                       bom_col_mfg: str | None = None, bom_col_mfg_pn: str | None = None,
                       bom_col_dist: str | None = None, bom_col_dist_pn: str | None = None,
                       drawing_sheet: str | None = None, variant: str | None = None,
                       define_vars: list[str] | None = None) -> dict[str, Any]:
    """IPC-2581 export — the single-file modern fab/assembly handoff (board + stackup + BOM).

    `kicad-cli pcb export ipc2581 -o <file.xml|.zip> [--precision N] [--units mm|in]
    [--version B|C] [--compress] [--mode DATA_SET] [--sections KEYS]
    [--net-names include|anonymize] [--ref-des include|omit] [--bom-rev R]
    [--bom-col-int-id F --bom-col-mfg F --bom-col-mfg-pn F --bom-col-dist F
    --bom-col-dist-pn F] [--drawing-sheet F] [--variant V] [-D K=V]`.

    `mode` picks the IPC-2581 function mode data set (userdef, bom, stackup, fabrication,
    assembly, test, stencil); leaving it unset keeps KiCad's pre-function-mode content.
    net_names="anonymize" ships connectivity without leaking net names to a vendor.
    compress=True writes a .zip instead of .xml. `report.bytes` plus the top-level XML element
    counts show what landed.
    """
    if (err := _ready()):
        return err
    units = (units or "mm").lower()
    if units not in ("mm", "in"):
        return _bad("units", units, ("mm", "in"))
    if version.upper() not in ("B", "C"):
        return _bad("version", version, ("B", "C"))
    if net_names not in ("include", "anonymize"):
        return _bad("net_names", net_names, ("include", "anonymize"))
    if ref_des not in ("include", "omit"):
        return _bad("ref_des", ref_des, ("include", "omit"))
    if mode is not None and mode not in ("userdef", "bom", "stackup", "fabrication",
                                         "assembly", "test", "stencil"):
        return _bad("mode", mode, ("userdef", "bom", "stackup", "fabrication", "assembly",
                                   "test", "stencil"))
    board, err = _need(board_path, ".kicad_pcb")
    if err:
        return err

    out = _out_path("ipc2581", board.stem, ".zip" if compress else ".xml", out_path)
    before = _snapshot([out.parent])

    argv = [str(KICAD_CLI), "pcb", "export", "ipc2581", "-o", str(out),
            "--precision", str(precision), "--units", units, "--version", version.upper(),
            "--net-names", net_names, "--ref-des", ref_des]
    _flag(argv, "--compress", compress)
    _opt(argv, "--mode", mode)
    _opt(argv, "--sections", sections)
    _opt(argv, "--bom-rev", bom_rev)
    _opt(argv, "--bom-col-int-id", bom_col_int_id)
    _opt(argv, "--bom-col-mfg", bom_col_mfg)
    _opt(argv, "--bom-col-mfg-pn", bom_col_mfg_pn)
    _opt(argv, "--bom-col-dist", bom_col_dist)
    _opt(argv, "--bom-col-dist-pn", bom_col_dist_pn)
    _opt(argv, "--drawing-sheet", drawing_sheet)
    _opt(argv, "--variant", variant)
    for var in define_vars or []:
        argv += ["-D", var]
    argv.append(str(board))

    res = _run(argv, timeout=T_HEAVY, env=_cli_env())
    res["board"] = str(board)
    res = _finish(res, watch=[out.parent], before=before, require=out)
    rep: dict[str, Any] = {"bytes": _size(out), "compressed": bool(compress),
                           "units": units, "version": version.upper()}
    if out.exists() and not compress:
        text = out.read_text(errors="replace")
        rep.update({
            "components": text.count("<Component "),
            "packages": text.count("<Package "),
            "layers": text.count("<Layer "),
            "logical_nets": text.count("<LogicalNet "),
        })
    res["report"] = rep
    return res


def pcb_export_odb(board_path: str, out_path: str | None = None, precision: int = 2,
                   units: str = "mm", compression: str = "zip", check_zones: bool = False,
                   drawing_sheet: str | None = None, variant: str | None = None,
                   define_vars: list[str] | None = None) -> dict[str, Any]:
    """ODB++ export — the fab format most Asian board houses prefer.

    `kicad-cli pcb export odb -o <path> [--precision N] [--units mm|in]
    [--compression none|zip|tgz] [--check-zones] [--drawing-sheet F] [--variant V] [-D K=V]`.
    compression="none" writes a DIRECTORY tree (default <out>/odb/<board>-odb/), "zip"/"tgz"
    a single archive. `report.bytes` measures either (a directory recursively) and
    `report.entries` counts the tree's files, which is the cheap "did the whole set come out"
    check. check_zones=True refills zones first — slower, but it stops a stale-fill export.
    """
    if (err := _ready()):
        return err
    units = (units or "mm").lower()
    if units not in ("mm", "in"):
        return _bad("units", units, ("mm", "in"))
    compression = (compression or "zip").lower()
    if compression not in ("none", "zip", "tgz"):
        return _bad("compression", compression, ("none", "zip", "tgz"))
    board, err = _need(board_path, ".kicad_pcb")
    if err:
        return err

    tag_root = _out_dir("odb")
    if out_path:
        out = Path(out_path).expanduser()
        if not out.is_absolute():
            out = tag_root / out
    elif compression == "none":
        out = tag_root / f"{board.stem}-odb"
    else:
        out = tag_root / f"{board.stem}-odb.{'zip' if compression == 'zip' else 'tgz'}"
    out.parent.mkdir(parents=True, exist_ok=True)
    before = _snapshot([out.parent])

    argv = [str(KICAD_CLI), "pcb", "export", "odb", "-o", str(out),
            "--precision", str(precision), "--units", units,
            "--compression", compression]
    _flag(argv, "--check-zones", check_zones)
    _opt(argv, "--drawing-sheet", drawing_sheet)
    _opt(argv, "--variant", variant)
    for var in define_vars or []:
        argv += ["-D", var]
    argv.append(str(board))

    res = _run(argv, timeout=T_HEAVY, env=_cli_env())
    res["board"] = str(board)
    res["output"] = str(out) if out.exists() else None
    res = _finish(res, watch=[out.parent], before=before, require=out)
    res["report"] = {
        "bytes": _size(out),
        "compression": compression,
        "is_directory": out.is_dir(),
        "entries": (sum(1 for _ in out.rglob("*")) if out.is_dir() else 1),
    }
    return res


def pcb_export_png(board_path: str, out_path: str | None = None,
                   layers: str = "F.Cu,B.Cu,Edge.Cuts", common_layers: str | None = None,
                   dpi: int = 300, mirror: bool = False, negative: bool = False,
                   black_and_white: bool = False, theme: str | None = None,
                   include_border_title: bool = False, subtract_soldermask: bool = False,
                   drill_shape: int = 2, scale: float = 1.0, no_antialias: bool = False,
                   check_zones: bool = False, sketch_pads_on_fab: bool = False,
                   sketch_pad_numbers: bool = False, hide_dnp_on_fab: bool = False,
                   sketch_dnp_on_fab: bool = False, crossout_dnp_on_fab: bool = False,
                   drawing_sheet: str | None = None, variant: str | None = None,
                   define_vars: list[str] | None = None) -> dict[str, Any]:
    """Flat 2D PNG plot of a layer list (NOT the 3D raytraced view — that is pcb_render).

    `kicad-cli pcb export png -o <dir> -l LAYERS [--cl COMMON] [--dpi N] [-m] [-n]
    [--black-and-white] [-t THEME] [--ibt] [--subtract-soldermask] [--drill-shape-opt 0|1|2]
    [--scale S] [--no-antialias] [--check-zones] [--sp/--spn/--hdnp/--sdnp/--cdnp]
    [--drawing-sheet F] [--variant V] [-D K=V]`.

    Output is a DIRECTORY (default <out>/png/<board>/): one PNG per layer in `layers`, with
    every layer in `common_layers` drawn into each. This is the artwork-review render — use
    mirror=True to look at bottom layers the right way round. `report.png_count` and
    `report.bytes_total` say what came out.
    """
    if (err := _ready()):
        return err
    if drill_shape not in (0, 1, 2):
        return _bad("drill_shape", drill_shape, (0, 1, 2))
    board, err = _need(board_path, ".kicad_pcb")
    if err:
        return err

    dest = Path(out_path).expanduser() if out_path else _tag_dir("png", board.stem)
    before = _snapshot([dest])

    argv = [str(KICAD_CLI), "pcb", "export", "png", "-o", _dir_arg(dest),
            "--dpi", str(dpi), "--drill-shape-opt", str(drill_shape), "--scale", str(scale)]
    _opt(argv, "--layers", layers)
    _opt(argv, "--common-layers", common_layers)
    _opt(argv, "--theme", theme)
    _flag(argv, "--mirror", mirror)
    _flag(argv, "--negative", negative)
    _flag(argv, "--black-and-white", black_and_white)
    _flag(argv, "--include-border-title", include_border_title)
    _flag(argv, "--subtract-soldermask", subtract_soldermask)
    _flag(argv, "--no-antialias", no_antialias)
    _flag(argv, "--check-zones", check_zones)
    _flag(argv, "--sketch-pads-on-fab-layers", sketch_pads_on_fab)
    _flag(argv, "--sketch-pad-numbers", sketch_pad_numbers)
    _flag(argv, "--hide-DNP-footprints-on-fab-layers", hide_dnp_on_fab)
    _flag(argv, "--sketch-DNP-footprints-on-fab-layers", sketch_dnp_on_fab)
    _flag(argv, "--crossout-DNP-footprints-on-fab-layers", crossout_dnp_on_fab)
    _opt(argv, "--drawing-sheet", drawing_sheet)
    _opt(argv, "--variant", variant)
    for var in define_vars or []:
        argv += ["-D", var]
    argv.append(str(board))

    res = _run(argv, timeout=T_EXPORT, env=_cli_env())
    res["board"] = str(board)
    res["out_dir"] = str(dest)
    res = _finish(res, watch=[dest], before=before)
    pngs = [p for p in res["outputs"] if p.lower().endswith(".png")]
    res["report"] = {"png_count": len(pngs), "pngs": [Path(p).name for p in pngs[:40]],
                     "bytes_total": sum(_size(Path(p)) for p in pngs), "dpi": dpi}
    return res


def pcb_export_ps(board_path: str, out_path: str | None = None,
                  layers: str = "F.Cu,B.Cu,Edge.Cuts", common_layers: str | None = None,
                  mode: str = "multi", mirror: bool = False, negative: bool = False,
                  black_and_white: bool = False, theme: str | None = None,
                  include_border_title: bool = False, exclude_refdes: bool = False,
                  exclude_value: bool = False, subtract_soldermask: bool = False,
                  drill_shape: int = 2, scale: float = 1.0, force_a4: bool = False,
                  track_width_correction: float = 0.0, x_scale_factor: float = 1.0,
                  y_scale_factor: float = 1.0, check_zones: bool = False,
                  sketch_pads_on_fab: bool = False, sketch_pad_numbers: bool = False,
                  hide_dnp_on_fab: bool = False, sketch_dnp_on_fab: bool = False,
                  crossout_dnp_on_fab: bool = False, drawing_sheet: str | None = None,
                  variant: str | None = None,
                  define_vars: list[str] | None = None) -> dict[str, Any]:
    """PostScript plot of a layer list — the classic 1:1 etch/photoplot output.

    `kicad-cli pcb export ps -o <path> -l LAYERS [--cl COMMON] --mode-multi|--mode-single
    [-m] [-n] [--black-and-white] [-t THEME] [--ibt] [--erd] [--ev]
    [--subtract-soldermask] [--drill-shape-opt 0|1|2] [--scale S] [-A|--force-a4]
    [-C TRACK_COR] [-X X_SCALE] [-Y Y_SCALE] [--check-zones] [--sp/--spn/--hdnp/--sdnp/--cdnp]
    [--drawing-sheet F] [--variant V] [-D K=V]`.

    mode="multi" (default) writes one .ps per layer into a directory; "single" writes one file
    with every layer in `layers`. -C/-X/-Y exist for etching accuracy: track width correction
    in mm and per-axis scale trim compensate for a specific printer. negative=True plots
    inverted for direct photoresist exposure.
    """
    if (err := _ready()):
        return err
    if mode not in ("multi", "single"):
        return _bad("mode", mode, ("multi", "single"))
    if drill_shape not in (0, 1, 2):
        return _bad("drill_shape", drill_shape, (0, 1, 2))
    board, err = _need(board_path, ".kicad_pcb")
    if err:
        return err

    single = mode == "single"
    if single:
        target = _out_path("ps", board.stem, ".ps", out_path)
        watch_dir = target.parent
        out_arg = str(target)
    else:
        target = None
        watch_dir = Path(out_path).expanduser() if out_path else _tag_dir("ps", board.stem)
        out_arg = _dir_arg(watch_dir)
    before = _snapshot([watch_dir])

    argv = [str(KICAD_CLI), "pcb", "export", "ps", "-o", out_arg,
            "--mode-single" if single else "--mode-multi",
            "--drill-shape-opt", str(drill_shape), "--scale", str(scale),
            "--track-width-correction", str(track_width_correction),
            "--x-scale-factor", str(x_scale_factor),
            "--y-scale-factor", str(y_scale_factor)]
    _opt(argv, "--layers", layers)
    if not single:
        _opt(argv, "--common-layers", common_layers)
    _opt(argv, "--theme", theme)
    _flag(argv, "--mirror", mirror)
    _flag(argv, "--negative", negative)
    _flag(argv, "--black-and-white", black_and_white)
    _flag(argv, "--include-border-title", include_border_title)
    _flag(argv, "--exclude-refdes", exclude_refdes)
    _flag(argv, "--exclude-value", exclude_value)
    _flag(argv, "--subtract-soldermask", subtract_soldermask)
    _flag(argv, "--force-a4", force_a4)
    _flag(argv, "--check-zones", check_zones)
    _flag(argv, "--sketch-pads-on-fab-layers", sketch_pads_on_fab)
    _flag(argv, "--sketch-pad-numbers", sketch_pad_numbers)
    _flag(argv, "--hide-DNP-footprints-on-fab-layers", hide_dnp_on_fab)
    _flag(argv, "--sketch-DNP-footprints-on-fab-layers", sketch_dnp_on_fab)
    _flag(argv, "--crossout-DNP-footprints-on-fab-layers", crossout_dnp_on_fab)
    _opt(argv, "--drawing-sheet", drawing_sheet)
    _opt(argv, "--variant", variant)
    for var in define_vars or []:
        argv += ["-D", var]
    argv.append(str(board))

    res = _run(argv, timeout=T_EXPORT, env=_cli_env())
    res["board"] = str(board)
    res["mode"] = mode
    res = _finish(res, watch=[watch_dir], before=before, require=target)
    pss = [p for p in res["outputs"] if p.lower().endswith(".ps")]
    res["report"] = {"ps_count": len(pss), "ps_files": [Path(p).name for p in pss[:40]],
                     "bytes_total": sum(_size(Path(p)) for p in pss)}
    return res


def pcb_export_stackup(board_path: str, out_path: str | None = None, fmt: str = "json",
                       units: str = "mm", exclude_color: bool = False,
                       exclude_material: bool = False, exclude_thickness: bool = False,
                       exclude_epsilon_r: bool = False, exclude_loss_tangent: bool = False,
                       exclude_finish: bool = False,
                       exclude_board_options: bool = False) -> dict[str, Any]:
    """Board stackup report — layer order, material, thickness, Er, loss tangent, finish.

    `kicad-cli pcb export stackup -o <file> --format {csv,json} --units {mm,in}
    [--exclude-color] [--exclude-material] [--exclude-thickness] [--exclude-epsilon-r]
    [--exclude-loss-tangent] [--exclude-finish] [--exclude-board-options]`. Written to
    <out>/stackup/<board>.stackup.{json,csv}. `report.layer_count` (and, for json,
    `report.layers` / `report.copper_layers`) is the number to check against the impedance
    assumptions a design was routed under. The --exclude-* flags only affect CSV columns.
    """
    if (err := _ready()):
        return err
    fmt = (fmt or "json").lower()
    if fmt not in ("csv", "json"):
        return _bad("fmt", fmt, ("csv", "json"))
    units = (units or "mm").lower()
    if units not in ("mm", "in"):
        return _bad("units", units, ("mm", "in"))
    board, err = _need(board_path, ".kicad_pcb")
    if err:
        return err

    out = _out_path("stackup", board.stem, f".stackup.{fmt}", out_path)
    before = _snapshot([out.parent])

    argv = [str(KICAD_CLI), "pcb", "export", "stackup", "-o", str(out),
            "--format", fmt, "--units", units]
    _flag(argv, "--exclude-color", exclude_color)
    _flag(argv, "--exclude-material", exclude_material)
    _flag(argv, "--exclude-thickness", exclude_thickness)
    _flag(argv, "--exclude-epsilon-r", exclude_epsilon_r)
    _flag(argv, "--exclude-loss-tangent", exclude_loss_tangent)
    _flag(argv, "--exclude-finish", exclude_finish)
    _flag(argv, "--exclude-board-options", exclude_board_options)
    argv.append(str(board))

    res = _run(argv, timeout=T_EXPORT, env=_cli_env())
    res["board"] = str(board)
    res["format"] = fmt
    res = _finish(res, watch=[out.parent], before=before, require=out)
    if out.exists():
        res["report"] = _stackup_report(out, fmt)
    return res


def pcb_export_3d(board_path: str, fmt: str = "glb", out_path: str | None = None,
                  board_only: bool = False, no_components: bool = False,
                  no_board_body: bool = False, no_unspecified: bool = False,
                  no_dnp: bool = False, include_tracks: bool = False,
                  include_pads: bool = False, include_zones: bool = False,
                  include_inner_copper: bool = False, include_silkscreen: bool = False,
                  include_soldermask: bool = False, subst_models: bool = False,
                  fuse_shapes: bool = False, fill_all_vias: bool = False,
                  cut_vias_in_body: bool = False, no_extra_pad_thickness: bool = False,
                  net_filter: str | None = None, component_filter: str | None = None,
                  grid_origin: bool = False, drill_origin: bool = False,
                  user_origin: str | None = None, min_distance: str = "0.01mm",
                  no_optimize_step: bool = False, vrml_units: str = "in",
                  vrml_models_dir: str | None = None, vrml_models_relative: bool = False,
                  variant: str | None = None,
                  define_vars: list[str] | None = None) -> dict[str, Any]:
    """Every 3D export except plain STEP (that is server.py's pcb_export_step).

    One tool over the whole `pcb export <fmt>` 3D family: fmt is glb, stl, ply, brep, xao,
    stpz, u3d, 3dpdf, vrml — or "step" if you want this tool's fuller flag set.
    `kicad-cli pcb export <fmt> -o <file> --force [--board-only] [--no-components]
    [--no-board-body] [--no-unspecified] [--no-dnp] [--include-tracks/-pads/-zones/
    -inner-copper/-silkscreen/-soldermask] [--subst-models] [--fuse-shapes] [--fill-all-vias]
    [--cut-vias-in-body] [--no-extra-pad-thickness] [--net-filter W] [--component-filter W]
    [--grid-origin|--drill-origin|--user-origin 25.4x25.4mm] [--min-distance 0.01mm]
    [--no-optimize-step] [--variant V] [-D K=V]`.

    --force is always passed, so an existing file is overwritten rather than failing the run.
    The geometry flags are OCC-only: fmt="vrml" ignores them and takes --units
    {mm,m,in,tenths} / --models-dir / --models-relative instead (mapped from vrml_*).
    STEP-family (step/stpz) additionally honours no_optimize_step. These exports are the
    slowest in the CLI — mesh formats on a dense board can take minutes; timeout is 20 min.
    """
    if (err := _ready()):
        return err
    fmt = (fmt or "glb").lower()
    if fmt not in FMT_3D:
        return _bad("fmt", fmt, sorted(FMT_3D))
    if vrml_units not in ("mm", "m", "in", "tenths"):
        return _bad("vrml_units", vrml_units, ("mm", "m", "in", "tenths"))
    board, err = _need(board_path, ".kicad_pcb")
    if err:
        return err

    out = _out_path("export3d", board.stem, FMT_3D[fmt], out_path)
    before = _snapshot([out.parent])

    argv = [str(KICAD_CLI), "pcb", "export", fmt, "-o", str(out), "--force"]
    _flag(argv, "--no-unspecified", no_unspecified)
    _flag(argv, "--no-dnp", no_dnp)
    _opt(argv, "--user-origin", user_origin)
    _opt(argv, "--variant", variant)

    if fmt == "vrml":
        argv += ["--units", vrml_units]
        _opt(argv, "--models-dir", vrml_models_dir)
        _flag(argv, "--models-relative", vrml_models_relative)
    else:
        _flag(argv, "--board-only", board_only)
        _flag(argv, "--no-components", no_components)
        _flag(argv, "--no-board-body", no_board_body)
        _flag(argv, "--include-tracks", include_tracks)
        _flag(argv, "--include-pads", include_pads)
        _flag(argv, "--include-zones", include_zones)
        _flag(argv, "--include-inner-copper", include_inner_copper)
        _flag(argv, "--include-silkscreen", include_silkscreen)
        _flag(argv, "--include-soldermask", include_soldermask)
        _flag(argv, "--subst-models", subst_models)
        _flag(argv, "--fuse-shapes", fuse_shapes)
        _flag(argv, "--fill-all-vias", fill_all_vias)
        _flag(argv, "--cut-vias-in-body", cut_vias_in_body)
        _flag(argv, "--no-extra-pad-thickness", no_extra_pad_thickness)
        _flag(argv, "--grid-origin", grid_origin)
        _flag(argv, "--drill-origin", drill_origin)
        _opt(argv, "--net-filter", net_filter)
        _opt(argv, "--component-filter", component_filter)
        _opt(argv, "--min-distance", min_distance)
        if fmt in ("step", "stpz"):
            _flag(argv, "--no-optimize-step", no_optimize_step)
    for var in define_vars or []:
        argv += ["-D", var]
    argv.append(str(board))

    res = _run(argv, timeout=T_HEAVY, env=_cli_env())
    res["board"] = str(board)
    res["format"] = fmt
    res = _finish(res, watch=[out.parent], before=before, require=out)
    res["report"] = {"bytes": _size(out), "format": fmt,
                     "board_only": bool(board_only or no_components)}
    return res


# NOTE there is deliberately no pcb_export_hpgl tool.  `pcb export hpgl` parses and then exits 1
# ("Plotting to HPGL is no longer supported as of KiCad 10.0."), so a tool for it could only ever
# return ok=false -- and on an agent's tool list a name that looks callable is a trap, not
# information.  The knowledge lives in REMOVED_SUBCOMMANDS above, is reported by
# kicad_cli_help("pcb export"), and is stated in the server's own instructions.


# ======================================================================================
# Format upgraders — `kicad-cli {pcb,sch} upgrade`
#
# Both take only INPUT_FILE plus --force: the CLI rewrites the file it is given, in place,
# with no -o at all (kicad/cli/command_pcb_upgrade.cpp, command_sch_upgrade.cpp).  So the
# safe default here is "copy the input (plus its project sidecars) under <out>/upgrade/ and
# upgrade the copy"; in_place=True is the opt-in that touches the original.
# ======================================================================================


def _upgrade_file(kind: str, path: str, suffix: str, out_path: str | None,
                  in_place: bool, force: bool) -> dict[str, Any]:
    """Shared body for pcb_upgrade / sch_upgrade — same argparse surface for both."""
    if (err := _ready()):
        return err
    src, err = _need(path, suffix)
    if err:
        return err

    carried: list[str] = []
    if in_place:
        target = src
    else:
        if out_path:
            requested = Path(out_path).expanduser()
            if not requested.is_absolute():
                requested = _out_dir("upgrade") / requested
        else:
            requested = _out_dir("upgrade") / src.name
        if requested.resolve() == src.resolve():
            return {"ok": False,
                    "error": "out_path equals the input; pass in_place=True to mean that",
                    "path": str(src)}
        target, carried = _copy_for_edit(src, requested.parent)
        if target.name != requested.name:
            target = target.rename(requested)

    before_bytes = target.stat().st_size
    before_version = _sexp_version(target)
    before = _snapshot([target.parent])

    argv = [str(KICAD_CLI), kind, "upgrade"]
    _flag(argv, "--force", force)
    argv.append(str(target))
    res = _run(argv, timeout=T_EXPORT, env=_cli_env())

    res["input"] = str(src)
    res["upgraded_file"] = str(target)
    res["in_place"] = bool(in_place)
    res = _finish(res, watch=[target.parent], before=before)
    # The upgraded file is not "new" (the copy predates the run), so report it explicitly.
    res["ok"] = res.get("exit_code") == 0 and target.exists()
    if in_place:
        res["outputs"] = []
        res["output_bytes"] = {str(target): _size(target)}
        res["mutated"] = [str(target)]
    else:
        res["outputs"] = sorted(set(res.get("outputs") or []) | {str(target)} | set(carried))
        res["output_bytes"] = {p: _size(Path(p)) for p in res["outputs"]}
        res["mutated"] = []
    after_version = _sexp_version(target)
    res["report"] = {
        "version_before": before_version,
        "version_after": after_version,
        "version_changed": before_version != after_version,
        "bytes_before": before_bytes,
        "bytes_after": _size(target),
        "sidecars_copied": [Path(p).name for p in carried],
        "forced": bool(force),
    }
    return res


def _sexp_version(path: Path) -> int | None:
    """The `(version NNNNNNNN)` stamp near the top of a KiCad s-expression file."""
    try:
        with path.open(encoding="utf-8", errors="replace") as fh:
            head_text = fh.read(4096)
    except OSError:
        return None
    m = re.search(r"\(\s*version\s+(\d+)\s*\)", head_text)
    return int(m.group(1)) if m else None


def pcb_upgrade(board_path: str, out_path: str | None = None, in_place: bool = False,
                force: bool = False) -> dict[str, Any]:
    """Rewrite a .kicad_pcb in the current board file format.

    `kicad-cli pcb upgrade [--force] <board>` — the CLI has no -o, it rewrites the file it is
    handed. So by default this copies the board (plus any .kicad_pro/.kicad_dru/.kicad_prl
    sidecar, which the loader wants) to <out>/upgrade/ and upgrades the COPY; the original is
    never touched. `in_place=True` upgrades `board_path` itself and lists it under `mutated`
    (the server's git journal snapshots that). Without --force an already-current board is
    left alone and still reports ok=True — check `report.version_before` /
    `report.version_after` / `report.version_changed` for what actually happened.
    """
    return _upgrade_file("pcb", board_path, ".kicad_pcb", out_path, in_place, force)


def sch_upgrade(sch_path: str, out_path: str | None = None, in_place: bool = False,
                force: bool = False) -> dict[str, Any]:
    """Rewrite a .kicad_sch in the current schematic file format.

    `kicad-cli sch upgrade [--force] <sch>`. Same in-place-only CLI as pcb_upgrade, so the
    same safety: a copy under <out>/upgrade/ by default, `in_place=True` to rewrite the input
    (reported in `mutated`). NOTE this upgrades ONE sheet file; a hierarchical design has a
    .kicad_sch per sheet and each must be upgraded (the sub-sheets are not followed here).
    `report.version_changed` tells you whether the format stamp moved.
    """
    return _upgrade_file("sch", sch_path, ".kicad_sch", out_path, in_place, force)


# ======================================================================================
# Foreign-format importers — `kicad-cli {pcb,sch} import`
#
# One file in, one KiCad file out. `kicad-cli import` (the top-level classifier that builds a
# whole project) is a different command and is NOT wrapped here; these two are the precise
# ones. `pcb import-ses` (Specctra session back-annotation) lives in tools_autoroute.
# ======================================================================================


def _import_file(kind: str, source_path: str, out_file: str | None, fmt: str,
                 formats: tuple[str, ...], out_suffix: str,
                 report_format: str, report_file: str | None,
                 layer_map: str | None = None) -> dict[str, Any]:
    """Shared body for pcb_import / sch_import (command_pcb_import.cpp / _sch_import.cpp)."""
    if (err := _ready()):
        return err
    fmt = (fmt or "auto").lower()
    if fmt not in formats:
        return _bad("fmt", fmt, formats)
    report_format = (report_format or "none").lower()
    if report_format not in ("none", "json", "text"):
        return _bad("report_format", report_format, ("none", "json", "text"))

    src, err = _need(source_path)
    if err:
        return err

    out = _out_path("import", src.stem, out_suffix, out_file)
    rep_path: Path | None = None
    if report_format != "none":
        rep_path = (Path(report_file).expanduser() if report_file
                    else out.with_suffix(f".import.{'json' if report_format == 'json' else 'txt'}"))
    before = _snapshot([out.parent])

    argv = [str(KICAD_CLI), kind, "import", "-o", str(out), "--format", fmt,
            "--report-format", report_format]
    if rep_path is not None:
        argv += ["--report-file", str(rep_path)]
    if layer_map:
        lm, err = _need(layer_map, ".json")
        if err:
            return err
        argv += ["--layer-map", str(lm)]
    argv.append(str(src))

    res = _run(argv, timeout=T_HEAVY, env=_cli_env())
    res["source"] = str(src)
    res["format_hint"] = fmt
    res["out_file"] = str(out) if out.exists() else None
    res = _finish(res, watch=[out.parent], before=before, require=out)
    res["mutated"] = []

    parsed: Any = None
    if rep_path is not None and rep_path.exists():
        parsed = _read_json(rep_path) if report_format == "json" else None
        res["report_file"] = str(rep_path)
    report: dict[str, Any] = {"bytes": _size(out) if out.exists() else 0,
                              "files_written": len(res.get("outputs") or [])}
    if isinstance(parsed, dict):
        report.update(_import_summary(parsed))
    res["report"] = report
    return res


def _import_summary(parsed: dict[str, Any]) -> dict[str, Any]:
    """Condense the importer's JSON report — the raw layer_mapping alone runs to 100+ rows.

    Keeps the numbers worth acting on: what was imported, what the importer complained about,
    and which source layers it could NOT place (`layers_unmapped`, the ones that silently lose
    geometry). The full report is still on disk at `report_file`.
    """
    out: dict[str, Any] = {"source_format": parsed.get("sourceFormat")
                           or parsed.get("source_format")}
    stats = parsed.get("statistics")
    if isinstance(stats, dict):
        out["statistics"] = stats
    for key in ("errors", "warnings"):
        vals = parsed.get(key)
        if isinstance(vals, list):
            out[f"{key}_count"] = len(vals)
            if vals:
                out[key] = [str(v)[:200] for v in vals[:10]]
    mapping = parsed.get("layer_mapping") or parsed.get("layerMapping")
    if isinstance(mapping, dict):
        mapped, unmapped = {}, []
        for src, info in mapping.items():
            dest = info.get("kicad_layer") if isinstance(info, dict) else info
            if not dest or str(dest).upper() == "UNDEFINED":
                unmapped.append(str(src))
            else:
                mapped[str(src)] = str(dest)
        out["layers_mapped"] = mapped
        out["layers_mapped_count"] = len(mapped)
        out["layers_unmapped_count"] = len(unmapped)
        out["layers_unmapped"] = sorted(unmapped)[:30]
    return {k: v for k, v in out.items() if v is not None}


def pcb_import(source_path: str, out_board: str | None = None, fmt: str = "auto",
               report_format: str = "none", report_file: str | None = None,
               layer_map: str | None = None) -> dict[str, Any]:
    """Convert a non-KiCad board file into a .kicad_pcb.

    `kicad-cli pcb import -o <out.kicad_pcb> --format {auto,pads,altium,eagle,cadstar,
    fabmaster,pcad,solidworks} [--report-format {none,json,text}] [--report-file F]
    [--layer-map F.json] <input>`. `auto` sniffs the file by extension/content, which is
    right nearly always; name the format when a vendor reuses an extension. `layer_map` is a
    JSON file mapping source layer names to KiCad layer names — unmapped layers fall back to
    the importer's best guess, and `report_format="json"` gets you the condensed verdict:
    `report.statistics` (footprints/tracks/vias/zones imported), `report.layers_mapped`,
    `report.layers_unmapped` (source layers whose geometry had nowhere to go) and
    errors/warnings counts; the untruncated report stays on disk at `report_file`.

    SIDE EFFECT worth knowing: the board importers also write a footprint cache library
    `<source>-import-fps.pretty` plus an `fp-lib-table` NEXT TO THE OUTPUT, so give each
    import its own directory — re-importing into a directory that already has that cache
    logs `File exists` errors even though the board itself is written. Everything that
    appeared is in `outputs`. Writes <out>/import/<name>.kicad_pcb by default and never
    modifies the source. ok means the .kicad_pcb exists and is non-empty.

    This is the single-file converter. `kicad-cli import` (whole-project, board+schematic at
    once) and `pcb import-ses` (Specctra routing session, see pcb_import_ses) are separate.
    """
    return _import_file("pcb", source_path, out_board, fmt, PCB_IMPORT_FORMATS,
                        ".kicad_pcb", report_format, report_file, layer_map)


def sch_import(source_path: str, out_sch: str | None = None, fmt: str = "auto",
               report_format: str = "none",
               report_file: str | None = None) -> dict[str, Any]:
    """Convert a non-KiCad schematic file into a .kicad_sch.

    `kicad-cli sch import -o <out.kicad_sch> --format {auto,altium,eagle,cadstar,easyeda,
    easyedapro,ltspice,pads,diptrace,pcad,orcad} [--report-format {none,json,text}]
    [--report-file F] <input>`. No --layer-map (that is board-only). A hierarchical source
    design produces the root sheet PLUS one `<source>_N.kicad_sch` per sub-sheet beside it
    (a 7-sheet DipTrace file gives 1 root + 6 sub-sheets), so read `report.files_written` /
    `outputs` rather than assuming a single file, and point each import at an empty directory.
    Writes under <out>/import/ by default; the source is read-only. Symbol libraries the
    import needs land next to the output as the importer decides.
    """
    return _import_file("sch", source_path, out_sch, fmt, SCH_IMPORT_FORMATS,
                        ".kicad_sch", report_format, report_file)


# ======================================================================================
# Schematic plotting and BOM — `kicad-cli sch export {svg,dxf,ps,png,hpgl,python-bom,bom}`
#
# All the plot formats share command_sch_export_plot.cpp: -o is a DIRECTORY and the CLI names
# the files (one per page, with the sheet name in it).  `sch export pdf` is server.py's
# sch_export_pdf and is not duplicated here.
# ======================================================================================


def sch_export_plot(sch_path: str, fmt: str = "svg", out_dir: str | None = None,
                    black_and_white: bool = False, pages: str | None = None,
                    exclude_drawing_sheet: bool = False, theme: str | None = None,
                    no_background_color: bool = False, drawing_sheet: str | None = None,
                    default_font: str | None = None, draw_hop_over: bool = False,
                    dpi: int = 300, no_antialias: bool = False, variant: str | None = None,
                    define_vars: list[str] | None = None) -> dict[str, Any]:
    """Plot a schematic to SVG / DXF / PS / PNG / HPGL — one file per page.

    `kicad-cli sch export <fmt> -o <dir> [-b] [-e] [-t THEME] [-n] [--pages 1,3]
    [--drawing-sheet F] [--default-font NAME] [--draw-hop-over] [--variant V] [-D K=V]`
    plus `--dpi/--no-antialias` for png only, and `-n/--no-background-color` for svg/ps/png
    (dxf and hpgl reject it). Output is a directory (default <out>/sch-plot/<sheet>/) because
    a multi-page schematic yields several files; `report.file_count` and `report.files` are
    the answer to "did every page come out". `pages` is a comma list of page numbers, blank
    for all. Read-only on the schematic. For PDF use sch_export_pdf (server.py); HPGL is
    accepted by the schematic side (unlike pcb export hpgl) but its --pen-size/--origin flags
    are documented as having no effect.
    """
    if (err := _ready()):
        return err
    fmt = (fmt or "svg").lower()
    if fmt not in FMT_SCH_PLOT:
        return _bad("fmt", fmt, tuple(FMT_SCH_PLOT))
    sch, err = _need(sch_path, ".kicad_sch")
    if err:
        return err

    dest = Path(out_dir).expanduser() if out_dir else _tag_dir("sch-plot", sch.stem)
    before = _snapshot([dest])

    argv = [str(KICAD_CLI), "sch", "export", fmt, "-o", _dir_arg(dest)]
    _flag(argv, "--black-and-white", black_and_white)
    _flag(argv, "--exclude-drawing-sheet", exclude_drawing_sheet)
    _flag(argv, "--draw-hop-over", draw_hop_over)
    _opt(argv, "--theme", theme)
    _opt(argv, "--pages", pages)
    _opt(argv, "--drawing-sheet", drawing_sheet)
    _opt(argv, "--default-font", default_font)
    _opt(argv, "--variant", variant)
    if fmt in ("svg", "ps", "png"):
        _flag(argv, "--no-background-color", no_background_color)
    if fmt == "png":
        argv += ["--dpi", str(dpi)]
        _flag(argv, "--no-antialias", no_antialias)
    for var in define_vars or []:
        argv += ["-D", var]
    argv.append(str(sch))

    res = _run(argv, timeout=T_EXPORT, env=_cli_env())
    res["schematic"] = str(sch)
    res["format"] = fmt
    res["out_dir"] = str(dest)
    res = _finish(res, watch=[dest], before=before)
    ext = FMT_SCH_PLOT[fmt]
    made = [p for p in res["outputs"] if p.lower().endswith(ext)]
    res["report"] = {"format": fmt, "file_count": len(made),
                     "files": [Path(p).name for p in made[:40]],
                     "bytes_total": sum(_size(Path(p)) for p in made),
                     "pages_requested": pages or "all"}
    if fmt == "hpgl" and not made:
        # The subcommand parses (and even documents --pen-size/--origin) but the plotter is
        # gone: "Plotting to HPGL is no longer supported as of KiCad 10.0.", exit 1.
        res["removed_in"] = "10.0"
        res["alternatives"] = ["sch_export_plot(fmt='ps')", "sch_export_plot(fmt='dxf')",
                              "sch_export_plot(fmt='svg')"]
        res["error"] = "HPGL plotting was removed in KiCad/PhiCad 10.0"
    return res


def sch_export_pythonbom(sch_path: str, out_path: str | None = None) -> dict[str, Any]:
    """Export the legacy BOM **XML** intermediate netlist (the `python-bom` format).

    `kicad-cli sch export python-bom -o <file> <sch>`. This is the XML that the classic
    eeschema "BOM script" plugins (bom2grouped_csv.py and friends) consume: a <export> tree
    with <components>, <libparts>, <nets>. It is NOT the modern grouped CSV — use
    sch_export_bom (server.py) or export_bom for that. Useful when an existing Python BOM
    generator has to be driven from an agent, or when you want per-component library and pin
    data in one file. Written to <out>/bom/<sheet>.python-bom.xml by default;
    `report.component_count` / `report.libpart_count` / `report.net_count` come from a cheap
    tag count, so they are exact but shallow.
    """
    if (err := _ready()):
        return err
    sch, err = _need(sch_path, ".kicad_sch")
    if err:
        return err

    out = _out_path("bom", sch.stem, ".python-bom.xml", out_path)
    before = _snapshot([out.parent])
    res = _run([str(KICAD_CLI), "sch", "export", "python-bom", "-o", str(out), str(sch)],
               timeout=T_EXPORT, env=_cli_env())
    res["schematic"] = str(sch)
    res = _finish(res, watch=[out.parent], before=before, require=out)
    if out.exists():
        text = out.read_text(errors="replace")
        res["report"] = {
            "bytes": _size(out),
            "component_count": len(re.findall(r"<comp\b", text)),
            "libpart_count": len(re.findall(r"<libpart\b", text)),
            "net_count": len(re.findall(r"<net\b", text)),
            "refs": re.findall(r'<comp ref="([^"]+)"', text)[:20],
        }
    return res


def export_bom(input_path: str, out_path: str | None = None,
               fields: str = "Reference,Value,Footprint,${QUANTITY},DNP",
               labels: str | None = None, group_by: str | None = None,
               sort_field: str | None = None, sort_asc: bool = True,
               filter_string: str | None = None, filter_scope: str = "reference",
               exclude_dnp: bool = False, preset: str | None = None,
               format_preset: str | None = None, field_delimiter: str | None = None,
               string_delimiter: str | None = None, ref_delimiter: str | None = None,
               ref_range_delimiter: str | None = None, keep_tabs: bool = False,
               keep_line_breaks: bool = False, include_byte_order_mark: bool = False,
               variant: str | None = None) -> dict[str, Any]:
    """Full-control BOM export — the same EXPORT_BOM_COMMAND behind `sch export bom` AND
    `pcb export bom`, dispatched on the input's extension.

    kicad/kicad_cli.cpp registers ONE class twice: `EXPORT_BOM_COMMAND{KIWAY::FACE_SCH}` as
    `sch export bom` and `EXPORT_BOM_COMMAND{KIWAY::FACE_PCB}` as `pcb export bom`. The flag
    set is identical (command_export_bom.cpp); only the reader differs — the schematic one
    walks the netlist, the board one walks footprints. So this tool takes a .kicad_sch or a
    .kicad_pcb and picks the matching subcommand, rather than pretending there is a separate
    top-level `kicad-cli export bom` (there is not; `kicad-cli export` does not exist).

    Beyond server.py's sch_export_bom (which only exposes --fields) this reaches every knob:
    --labels, --group-by, --sort-field, --filter/--filter-scope
    {reference,visible,all}, --exclude-dnp, --preset/--format-preset (named presets stored in
    the project), the four delimiters, --keep-tabs/--keep-line-breaks,
    --include-byte-order-mark and --variant. Generated fields (QUANTITY, ITEM_NUMBER, DNP,
    EXCLUDE_FROM_BOM, EXCLUDE_FROM_BOARD, EXCLUDE_FROM_SIM) work with or without ${}.

    `sort_asc=False` is REFUSED with an explanation instead of being sent to the CLI:
    --sort-asc is declared with a bool default but no .flag(), so handing it ANY value token
    crashes the command (std::bad_any_cast, exit 255 — verified on this fork). Sorting is
    always ascending; reverse the rows downstream if you need the other order.
    Written to <out>/bom/<stem>.bom.csv unless out_path says otherwise;
    `report.row_count`/`header`/`preview` come from the file itself.
    """
    if (err := _ready()):
        return err
    filter_scope = (filter_scope or "reference").lower()
    if filter_scope not in ("reference", "visible", "all"):
        return _bad("filter_scope", filter_scope, ("reference", "visible", "all"))
    src, err = _need(input_path, ".kicad_sch", ".kicad_pcb")
    if err:
        return err
    kind = "sch" if src.suffix == ".kicad_sch" else "pcb"

    out = _out_path("bom", src.stem, ".bom.csv", out_path)
    before = _snapshot([out.parent])

    argv = [str(KICAD_CLI), kind, "export", "bom", "-o", str(out)]
    _opt(argv, "--fields", fields)
    _opt(argv, "--labels", labels)
    _opt(argv, "--group-by", group_by)
    _opt(argv, "--sort-field", sort_field)
    # --sort-asc is declared with default_value(true) and no .flag(), so ANY value token makes
    # argparse store a std::string and the handler's get<bool> throws bad_any_cast -> exit 255.
    # Verified on the fork: `--sort-asc true`, `--sort-asc=false` both die; bare `--sort-asc`
    # and omitting it both mean ascending. Descending is therefore unreachable via the CLI.
    if not sort_asc:
        return {"ok": False,
                "error": "descending BOM sort is not reachable through kicad-cli",
                "detail": ("--sort-asc takes no value in practice: passing one triggers an "
                           "upstream std::bad_any_cast (exit 255), so the flag only ever "
                           "means ascending"),
                "hint": "export ascending and reverse the rows in the consumer"}
    _opt(argv, "--filter", filter_string)
    argv += ["--filter-scope", filter_scope]
    _flag(argv, "--exclude-dnp", exclude_dnp)
    _opt(argv, "--preset", preset)
    _opt(argv, "--format-preset", format_preset)
    _opt(argv, "--field-delimiter", field_delimiter)
    _opt(argv, "--string-delimiter", string_delimiter)
    _opt(argv, "--ref-delimiter", ref_delimiter)
    if ref_range_delimiter is not None:
        argv += ["--ref-range-delimiter", ref_range_delimiter]
    _flag(argv, "--keep-tabs", keep_tabs)
    _flag(argv, "--keep-line-breaks", keep_line_breaks)
    _flag(argv, "--include-byte-order-mark", include_byte_order_mark)
    _opt(argv, "--variant", variant)
    argv.append(str(src))

    res = _run(argv, timeout=T_EXPORT, env=_cli_env())
    res["input"] = str(src)
    res["subcommand"] = f"{kind} export bom"
    res["source_kind"] = "schematic" if kind == "sch" else "board"
    res = _finish(res, watch=[out.parent], before=before, require=out)
    if out.exists():
        res["report"] = _csv_report(out)
        res["report"]["bytes"] = _size(out)
    return res


# ======================================================================================
# Self-service helpers — the board's layer table, the CLI's own help, and one escape hatch.
# ======================================================================================

#: Layer types the board format uses in `(N "Name" <type> ["User Name"])`.
LAYER_TYPES = ("signal", "power", "mixed", "jumper", "user")

_LAYER_BLOCK_RE = re.compile(r"^\s*\(layers\b", re.M)
_LAYER_ROW_RE = re.compile(
    r'\(\s*(\d+)\s+"([^"]+)"\s+([A-Za-z]+)\s*(?:"([^"]*)")?\s*\)')


def _load_pylib(module: str, alias: str) -> Any:
    """Import a phicad/pylib/*.py module by path (they are stdlib-only and not a package)."""
    if REPO_ROOT is None:
        return None
    path = REPO_ROOT / "phicad" / "pylib" / f"{module}.py"
    if not path.exists():
        return None
    import importlib.util
    import sys as _sys
    spec = importlib.util.spec_from_file_location(alias, path)
    if spec is None or spec.loader is None:
        return None
    mod = importlib.util.module_from_spec(spec)
    _sys.modules.setdefault(alias, mod)
    try:
        spec.loader.exec_module(mod)
    except Exception:
        return None
    return mod


def _load_sexp() -> Any:
    """Import phicad/pylib/sexp.py by path (stdlib-only parser shared with the plugins)."""
    global _sexp
    if _sexp is None:
        _sexp = _load_pylib("sexp", "phicad_sexp_cli")
    return _sexp


def _load_ibom_json() -> Any:
    """Import phicad/pylib/ibom_json.py by path (the generic-JSON builder)."""
    global _ibom
    if _ibom is None:
        _ibom = _load_pylib("ibom_json", "phicad_ibom_json")
    return _ibom


def _layers_from_text(text: str) -> list[dict[str, Any]]:
    """Pull the (layers …) block out of a board file with a bracket scan, then parse rows.

    Bracket-matching rather than a single regex over the whole file: `(layers …)` also appears
    inside footprints and graphics, and only the FIRST top-level one is the board's table.
    """
    m = _LAYER_BLOCK_RE.search(text)
    if not m:
        return []
    start = text.index("(", m.start())
    depth = 0
    end = len(text)
    in_string = False
    for i in range(start, len(text)):
        c = text[i]
        if in_string:                    # a ')' inside a layer name must not close the block
            if c == '"' and text[i - 1] != "\\":
                in_string = False
            continue
        if c == '"':
            in_string = True
        elif c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                end = i + 1
                break
    block = text[start:end]
    rows: list[dict[str, Any]] = []
    for idx, canon, ltype, user in _LAYER_ROW_RE.findall(block):
        rows.append({"index": int(idx), "canonical": canon,
                     "type": ltype, "user_name": user or canon})
    return rows


def pcb_layers(board_path: str) -> dict[str, Any]:
    """The board's layer table — parsed straight out of the .kicad_pcb, no kicad-cli, no API.

    Every plotting tool here (`pcb_export_svg/png/ps/dxf/pdf/gerbers`) takes a `layers`
    string of CANONICAL names, and getting one wrong is the most common way those calls fail.
    This reads the `(layers …)` block from the file with the stdlib s-expression parser
    (phicad/pylib/sexp.py, with a bracket-scan + regex fallback) and returns the table in
    board order:

        layers        [{index, canonical, user_name, type}]  — `canonical` is what -l wants
        copper_count  number of *.Cu layers enabled on this board (the "layer count" a fab
                      quotes), and `copper` lists them in stack order
        by_type       {signal: n, user: n, …}
        renamed       the layers whose user_name differs from the canonical name

    Costs milliseconds, needs no build, and works on a board the editor has open. ok=False
    only if the file cannot be read or has no layer table.
    """
    if (err := _unconfigured()):
        return err
    board, err = _need(board_path, ".kicad_pcb")
    if err:
        return err
    try:
        text = board.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return {"ok": False, "error": f"cannot read {board}: {exc}"}

    rows: list[dict[str, Any]] = []
    parser = _load_sexp()
    used = "regex"
    if parser is not None:
        try:
            doc = parser.parse(text)
            node = parser.child(doc, "layers")
            if node:
                for item in node[1:]:
                    if not isinstance(item, list) or len(item) < 3:
                        continue
                    idx, canon, ltype = str(item[0]), str(item[1]), str(item[2])
                    if not idx.isdigit():
                        continue
                    user = str(item[3]) if len(item) > 3 else canon
                    rows.append({"index": int(idx), "canonical": canon,
                                 "type": ltype, "user_name": user})
                used = "sexp"
        except Exception:
            rows = []
    if not rows:
        rows = _layers_from_text(text)
        used = "regex"
    if not rows:
        return {"ok": False, "error": f"no (layers ...) block found in {board}",
                "board": str(board)}

    copper = [r for r in rows if r["canonical"].endswith(".Cu")]
    by_type: dict[str, int] = {}
    for r in rows:
        by_type[r["type"]] = by_type.get(r["type"], 0) + 1
    version = _sexp_version(board)
    gen = re.search(r'\(generator\s+"?([\w.-]+)"?\)', text[:2048])
    return {
        "ok": True,
        "board": str(board),
        "parser": used,
        "layers": rows,
        "layer_count": len(rows),
        "copper_count": len(copper),
        "copper": [r["canonical"] for r in copper],
        "by_type": by_type,
        "renamed": [{"canonical": r["canonical"], "user_name": r["user_name"]}
                    for r in rows if r["user_name"] != r["canonical"]],
        "layers_arg_all_copper": ",".join(r["canonical"] for r in copper),
        "file_version": version,
        "generator": gen.group(1) if gen else None,
    }


_HELP_FLAG_RE = re.compile(
    r"^\s{2,}((?:-\w, )?--?[\w-]+(?:, --?[\w-]+)*)\s{2,}(.*?)\s*$")
_HELP_SUBS_RE = re.compile(r"^\s{2,}(\w[\w.-]*)\s{2,}(.*?)\s*$")


def _parse_help(text: str) -> dict[str, Any]:
    """Split a kicad-cli --help page into usage / flags / positionals / subcommands."""
    usage = ""
    section = ""
    flags: list[dict[str, str]] = []
    positionals: list[dict[str, str]] = []
    subs: list[dict[str, str]] = []
    for line in (text or "").splitlines():
        low = line.strip().lower()
        if low.startswith("usage:"):
            usage = line.strip()[len("usage:"):].strip()
            continue
        if low.endswith("arguments:") or low == "subcommands:":
            section = low
            continue
        if not line.strip():
            continue
        if section == "subcommands:":
            m = _HELP_SUBS_RE.match(line)
            if m:
                subs.append({"name": m.group(1), "help": m.group(2)})
            continue
        m = _HELP_FLAG_RE.match(line)
        if m and section.endswith("arguments:"):
            names = [n.strip() for n in m.group(1).split(",")]
            default = re.search(r"\[default: (.*?)\]\s*$", m.group(2))
            flags.append({"names": ",".join(names), "flag": names[-1],
                          "help": re.sub(r"\s*\[(nargs|default|may be repeated).*$", "",
                                         m.group(2)).strip(),
                          "default": default.group(1) if default else ""})
            continue
        if section == "positional arguments:":
            m2 = _HELP_SUBS_RE.match(line)
            if m2:
                positionals.append({"name": m2.group(1), "help": m2.group(2)})
    return {"usage": usage, "flags": flags, "positionals": positionals,
            "subcommands": subs}


def kicad_cli_help(command: str | list[str] = "") -> dict[str, Any]:
    """The fork's own `--help` for any kicad-cli subcommand, parsed into a flag list.

    Accepts `"pcb export gerbers"` or `["pcb","export","gerbers"]`; empty means the top-level
    command list. Returns `help_text` verbatim plus:

        usage         the Usage: line
        flags         [{names, flag, help, default}] — every optional argument
        positionals   [{name, help}]
        subcommands   [{name, help}] when the command is a group (e.g. `pcb export`)
        is_group      True when it only dispatches to subcommands
        unknown       True when argparse fell back to the PARENT's help, i.e. the last token
                      is not a real subcommand (kicad-cli exits 0 either way, so this flag is
                      the only way to tell)
        removed       subcommands under this one that the fork still PARSES and then refuses,
                      with what to use instead — `pcb export hpgl` is gone in 10.0 (use
                      pcb_export_ps / pcb_export_dxf / pcb_export_svg) yet still shows up in
                      --help, which is exactly the trap this field exists to close

    This is the ground truth for flags: the typed tools here were written against
    kicad/cli/command_*.cpp, but a fork change lands in --help first. Use it before reaching
    for `kicad_cli()`, and cross-check anything surprising in a typed tool's docstring.
    """
    if (err := _ready()):
        return err
    parts = ([p for p in command if str(p).strip()] if isinstance(command, list)
             else [p for p in str(command or "").split() if p])
    parts = [str(p) for p in parts]
    if any(p.startswith("-") for p in parts):
        return {"ok": False, "error": "pass only subcommand words, no flags",
                "got": parts}

    res = _run([str(KICAD_CLI), *parts, "--help"], timeout=60, env=_cli_env())
    text = res.get("stdout") or ""
    parsed = _parse_help(text)
    unknown = bool(parts) and parsed["usage"] and parts[-1] not in parsed["usage"].split()
    return {
        "ok": bool(text.strip()),
        "command": " ".join(["kicad-cli", *parts]) or "kicad-cli",
        "exit_code": res.get("exit_code"),
        "help_text": text,
        "usage": parsed["usage"],
        "flags": parsed["flags"],
        "flag_count": len(parsed["flags"]),
        "positionals": parsed["positionals"],
        "subcommands": parsed["subcommands"],
        "is_group": bool(parsed["subcommands"]),
        "unknown": unknown,
        "removed": removed_notes(parts),
        "stderr": res.get("stderr", ""),
    }


def kicad_cli(args: list[str], timeout_s: int = 300, cwd: str | None = None) -> dict[str, Any]:
    """Escape hatch: run the fork's kicad-cli with an arbitrary argv and report what appeared.

    `args` is the argv AFTER the binary, e.g.
    `["pcb","export","gerbers","-o","/tmp/g/","b.kicad_pcb"]`.
    Use this only for something no typed tool covers (a brand-new fork flag, an odd flag
    combination, `version --format json`); the typed tools give better validation, defaults,
    output bookkeeping and parsed reports. Discover flags with kicad_cli_help first.

    Behaviour and guard rails:
      * `api-server` is REFUSED — the IPC server is lifecycle-managed by api_server_start /
        api_server_stop, and a stray one here would leak a process holding a socket.
      * empty argv is refused (that would just print help; call kicad_cli_help).
      * cwd defaults to <out>/cli/ so a command that writes beside its input cannot litter the
        repo; pass cwd explicitly when the command must run next to a project.
      * files that newly appear in cwd and in the directory of every `-o/--output` value are
        detected and returned in `outputs` (+ `output_bytes`); nothing is assumed about naming.
        A value ending in `/` is treated as a directory and created; otherwise only its parent
        is created, because for several subcommands (`pcb export svg` in its default
        mode-single, `pcb export pdf`) `-o` is a FILE and a directory in the way is an error.
      * `mutated` is left empty: this tool cannot know which pre-existing file the command
        rewrote, so treat an in-place command run here as unjournaled.
      * ok mirrors exit code 0. stdout is returned in full (capped by the runner) because for
        the escape hatch the text IS the result.
    """
    if (err := _ready()):
        return err
    argv = [str(a) for a in (args or []) if str(a) != ""]
    if not argv:
        return {"ok": False, "error": "empty args",
                "hint": "pass the argv after kicad-cli, e.g. ['version','--format','json']"}
    if argv[0] in ("api-server",) or "api-server" in argv[:2]:
        return {"ok": False, "error": "refusing to run 'api-server' here",
                "hint": "use api_server_start / api_server_status / api_server_stop"}

    work = Path(cwd).expanduser() if cwd else _out_dir("cli")
    work.mkdir(parents=True, exist_ok=True)

    watch = [work]
    for i, tok in enumerate(argv):
        val: str | None = None
        if tok in ("-o", "--output") and i + 1 < len(argv):
            val = argv[i + 1]
        elif tok.startswith("--output="):
            val = tok.split("=", 1)[1]
        if not val:
            continue
        p = Path(val).expanduser()
        if not p.is_absolute():
            p = work / p
        target = p if (val.endswith("/") or p.is_dir()) else p.parent
        target.mkdir(parents=True, exist_ok=True)
        if target not in watch:
            watch.append(target)
    before = _snapshot(watch)

    res = _run([str(KICAD_CLI), *argv], timeout=max(1, int(timeout_s)), env=_cli_env(),
               cwd=work)
    res["argv"] = argv
    res["cwd"] = str(work)
    exit_code = res.get("exit_code")
    res = _finish(res, watch=watch, before=before, keep_stdout=True)
    res["ok"] = exit_code == 0
    res["mutated"] = []
    res["watched"] = [str(p) for p in watch]
    return res


# ======================================================================================
# Registration — the server imports this tuple and hands each entry to mcp.tool().
# Order is the order an agent meets them: libraries, gerber toolbox, board exports,
# schematic exports, format conversion, then the three self-service helpers.
# ======================================================================================

TOOLS: tuple[Callable[..., dict[str, Any]], ...] = (
    # footprint libraries
    fp_export_svg,
    fp_upgrade,
    fp_diff,
    # symbol libraries
    sym_export_svg,
    sym_upgrade,
    sym_diff,
    # gerber / excellon toolbox (no board file involved)
    gerber_info,
    gerber_diff,
    gerber_convert_png,
    # board exports: fabrication
    pcb_export_drill,
    pcb_export_gencad,
    pcb_export_ipcd356,
    pcb_export_ipc2581,
    pcb_export_odb,
    pcb_export_stackup,
    # board exports: assembly
    pcb_export_ibom,
    # board exports: artwork / mechanical / 3D
    pcb_export_dxf,
    pcb_export_png,
    pcb_export_ps,
    pcb_export_3d,
    # schematic exports
    sch_export_plot,
    sch_export_pythonbom,
    export_bom,
    # format conversion
    pcb_upgrade,
    sch_upgrade,
    pcb_import,
    sch_import,
    # self-service
    pcb_layers,
    kicad_cli_help,
    kicad_cli,
)
