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
"""End-to-end test of the whole phicad MCP tool surface, in-process.

WHAT THIS IS FOR
    test_handshake.py proves the SERVER works (stdio transport, a client can list and call
    tools).  test_registry.py proves the SHAPE of the surface is legal (docstrings, hints,
    schemas).  This file proves the TOOLS THEMSELVES do their job: it imports server.py in
    the same process, takes the live registry, and runs one representative tool per section
    against /tmp copies of demos/, asserting the PARSED numbers — violation counts, drill
    holes, the layer table, the symbol list, a semantic diff — not just `ok: true`.

    It also proves the piece nothing else covers: the coordinator's project-git journal hook.
    A throwaway KiCad project is created in /tmp, `project_init`-ed, mutated through a real
    tool, and then the project's Git history scope is inspected for the snapshot commit and
    the journal entry that mutation must have produced.

RULES IT FOLLOWS
    * every input is a COPY under a scratch directory (the fork rewrites files it saves, so a
      test that touched demos/ in place would dirty the source tree — that has happened);
    * no pytest: plain asserts through check(), a summary line, exit 0 (pass) / 1 (fail);
    * an api-server is started only in --full, on a socket path unique to this test, and is
      stopped again; the final sweep kills nothing that does not carry THAT socket path;
    * EVERY path this run touches lives under one per-run directory (mkdtemp, pid in the
      name): the lab, the api-server socket and its pidfile, and the server's export root
      (PHICAD_MCP_OUT).  Two runs of this file — two CI jobs, two worktrees, or one shell
      that starts both with `&` — therefore cannot see, wipe, or bind over each other's
      state.  The `isolation` stage is the gate that proves it;
    * a tool that is legitimately unavailable (no java for Freerouting, no network for parts)
      is reported as SKIP, not failed — an unavailable dependency is not a broken tool.

Run:  build/mcp-venv/bin/python phicad/mcp/test_tools.py [--quick | --full]
      --quick  cheap checks only (no exports, no IPC, no jobset)   ~30 s
      (default) every section, fast tools                          ~2 min
      --full   adds gerbers/step/render, the jobset, and the managed IPC session  ~5 min
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import warnings
from pathlib import Path
from typing import Any, Callable

MCP_DIR = Path(__file__).resolve().parent
REPO_ROOT = MCP_DIR.parent.parent

if str(MCP_DIR) not in sys.path:
    sys.path.insert(0, str(MCP_DIR))

# authlib installs its own "always" warning filter at import time; import it first, then hush.
try:
    import authlib.deprecate                                            # noqa: F401
except Exception:                                                       # noqa: BLE001
    pass
warnings.simplefilter("ignore", DeprecationWarning)

import registry                                                         # noqa: E402

QUICK = "--quick" in sys.argv
FULL = "--full" in sys.argv


# --------------------------------------------------------------------------------------
# per-run isolation (audit finding P0-5)
#
# This file used to write to $TMPDIR/phicad-mcp-tools-test and rmtree it at startup, and to
# let the server default its export root to $TMPDIR/phicad-mcp-out.  Both are FIXED paths, so
# a second concurrent run (a CI runner with two jobs, or a sibling worktree on one machine)
# deleted the first run's fixtures mid-flight — the auditor got 3 phantom failures in 6 runs,
# with a different symptom set each time.  Everything is now derived from ONE per-run
# directory, and the isolation stage below asserts that nothing escaped it.
# --------------------------------------------------------------------------------------


def new_run_root() -> Path:
    """Create a directory no other run of this file can be using, and return it.

    mkdtemp gives the uniqueness (it creates the directory O_EXCL, so even two runs started in
    the same millisecond get different names); the pid in the prefix is for the human reading
    `lsof`/`pgrep` output afterwards.  `PHICAD_MCP_TEST_ROOT` lets a caller park all run roots
    somewhere it can inspect, without giving up the per-run suffix.
    """
    base = Path(os.environ.get("PHICAD_MCP_TEST_ROOT") or tempfile.gettempdir())
    base.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix=f"phicad-mcp-tools-{os.getpid()}-", dir=str(base)))


#: The one directory this run owns. Removed on success, KEPT on failure (a wiped fixture dir is
#: why a failing run used to look like 11 "file not found" errors on the NEXT run).
RUN_ROOT = new_run_root()
#: Everything this test writes lives here; inside RUN_ROOT, so wiping it hits nobody else.
LAB = RUN_ROOT / "lab"
#: The api-server socket THIS test owns. The cleanup sweep matches on this string alone.
TEST_SOCKET = str(RUN_ROOT / "api.sock")

# server.py computes its export root at IMPORT time:
#     OUT_ROOT = Path(os.environ.get("PHICAD_MCP_OUT", gettempdir())) / "phicad-mcp-out"
# so the knob has to be set here, at module scope, BEFORE registry.load_server_module() runs
# in main(). Setting it after the import would silently leave every export in the shared
# $TMPDIR/phicad-mcp-out that concurrent runs overwrite.
os.environ["PHICAD_MCP_OUT"] = str(RUN_ROOT)
#: What server.OUT_ROOT must therefore be — asserted, not assumed, by test_isolation().
EXPECTED_OUT_ROOT = RUN_ROOT / "phicad-mcp-out"
#: The fixed paths this fix removed; the gate refuses to let them come back.
LEGACY_LAB = Path(tempfile.gettempdir()) / "phicad-mcp-tools-test"
LEGACY_OUT_ROOT = Path(tempfile.gettempdir()) / "phicad-mcp-out"
LEGACY_SOCKET = Path(tempfile.gettempdir()) / "phicad-mcp-api.sock"

DEMO_MICROWAVE = REPO_ROOT / "demos" / "microwave" / "microwave.kicad_pcb"
DEMO_PROJECT = REPO_ROOT / "demos" / "pic_programmer"
JOBSET = MCP_DIR / "examples" / "fab_pack.kicad_jobset"
DRC_FIXTURE_DIR = REPO_ROOT / "qa" / "data" / "pcbnew" / "line_ending_drc"

FAILURES: list[str] = []
SKIPS: list[str] = []
CHECKS = 0
TIMINGS: list[tuple[str, float]] = []


# --------------------------------------------------------------------------------------
# tiny harness
# --------------------------------------------------------------------------------------


def check(condition: Any, message: str) -> bool:
    """Record one assertion; returns the verdict so a caller can bail out of a dead branch."""
    global CHECKS
    CHECKS += 1
    if not condition:
        FAILURES.append(message)
        print(f"   FAIL  {message}")
    return bool(condition)


def skip(what: str, why: str) -> None:
    SKIPS.append(f"{what}: {why}")
    print(f"   SKIP  {what} — {why}")


def section(title: str) -> None:
    print(f"\n-- {title}")


def num(value: Any) -> str:
    return json.dumps(value, default=str)


class Surface:
    """The live tool surface: name -> ToolSpec, plus a calling convention that never raises."""

    def __init__(self, specs: list[registry.ToolSpec]) -> None:
        self.specs = specs
        self.by_name = {s.name: s for s in specs}

    def has(self, name: str) -> bool:
        return name in self.by_name

    def call(self, _name: str, /, **kwargs: Any) -> dict[str, Any]:
        """Invoke a tool exactly as the MCP client would (through the registered callable).

        `spec.fn` is the wrapped function when the coordinator journaled it, so calling it here
        exercises the same journal hook a chat client triggers — which is the point of the
        project-history stage below.

        The tool name is POSITIONAL-ONLY: several tools take a parameter called `name`
        (project_tag, sch_symbol_lib_get), and a keyword-able first argument would collide.
        """
        name = _name
        spec = self.by_name.get(name)
        if spec is None:
            return {"ok": False, "error": f"tool not registered: {name}"}
        started = time.monotonic()
        try:
            res = spec.fn(**kwargs)
            if not spec.sync:                       # no async tools today; be future-proof
                import asyncio

                res = asyncio.run(res)
        except Exception as exc:                                        # noqa: BLE001
            res = {"ok": False, "error": f"tool RAISED {type(exc).__name__}: {exc}",
                   "raised": True}
        TIMINGS.append((name, round(time.monotonic() - started, 2)))
        return res if isinstance(res, dict) else {"ok": False, "error": "non-dict result",
                                                  "result": str(res)[:300]}


def report(name: str, res: dict[str, Any], keys: list[str]) -> None:
    print(f"   {name}: ok={res.get('ok')}" + "".join(
        f"  {k}={num(res[k])[:90]}" for k in keys if k in res))
    for k in ("error", "hint"):
        if res.get(k):
            print(f"      {k}: {str(res[k])[:200]}")


def ok_tool(name: str, res: dict[str, Any]) -> bool:
    """`ok: true` is the contract; a raised exception is always a hard failure."""
    if res.get("raised"):
        return check(False, f"{name} raised: {res.get('error')}")
    return check(res.get("ok"), f"{name} failed: {res.get('error') or res}")


# --------------------------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------------------------


def stage_drc_project(kind: str) -> dict[str, Path]:
    """Stage one upstream pass/fail DRC fixture with the fork's starter fabrication jobset."""
    stem = f"line_ending_drc_{kind}"
    project = LAB / f"jobset_{kind}"
    project.mkdir()
    for suffix in (".kicad_pcb", ".kicad_pro", ".kicad_dru"):
        shutil.copy2(DRC_FIXTURE_DIR / f"{stem}{suffix}", project / f"{stem}{suffix}")
    shutil.copy2(JOBSET, project / JOBSET.name)
    return {
        "project": project,
        "pro": project / f"{stem}.kicad_pro",
        "jobset": project / JOBSET.name,
        "report": project / "fab" / "drc.json",
    }


def stage() -> dict[str, Any]:
    """Fresh /tmp copies of everything the test touches. Never reads demos/ in place.

    LAB lives inside RUN_ROOT, which mkdtemp just created for this process alone, so the wipe
    below can only ever destroy this run's own fixtures.
    """
    if LAB.exists():
        shutil.rmtree(LAB)
    LAB.mkdir(parents=True)
    board = LAB / "microwave.kicad_pcb"
    shutil.copy2(DEMO_MICROWAVE, board)
    shutil.copy2(DEMO_MICROWAVE.with_suffix(".kicad_pro"), board.with_suffix(".kicad_pro"))

    # A second, perturbed copy so pcb_diff has something real to find.
    moved = LAB / "microwave_moved.kicad_pcb"
    text = board.read_text()
    marker = text.index("(at ", text.index("(footprint "))
    end = text.index(")", marker)
    parts = text[marker + 4:end].split()
    shifted = f"(at {float(parts[0]) + 5:.5f} {' '.join(parts[1:])}"
    moved.write_text(text[:marker] + shifted + text[end:])

    project = LAB / "pic_programmer"
    shutil.copytree(DEMO_PROJECT, project)

    # The jobset must pass a known-clean board and reject the paired deliberately-invalid board.
    jobset_pass = stage_drc_project("pass")
    jobset_fail = stage_drc_project("fail")

    # The journal stage needs a project nothing else writes to, so it gets its own copy.
    journal_project = LAB / "journal_project"
    shutil.copytree(DEMO_PROJECT, journal_project)

    return {"lab": LAB, "board": board, "moved": moved, "project": project,
            "sch": project / "pic_programmer.kicad_sch",
            "pcb": project / "pic_programmer.kicad_pcb",
            "sym": project / "pic_programmer.kicad_sym",
            "jobset_pass": jobset_pass, "jobset_fail": jobset_fail,
            "journal_project": journal_project}


# --------------------------------------------------------------------------------------
# stage 0a — isolation: nothing this run writes may be shared with a concurrent run
# --------------------------------------------------------------------------------------


def test_isolation(server: Any) -> None:
    """Gate for audit finding P0-5: prove this run's state is unreachable by any other run.

    The bug was not a wrong number, it was SHARING: fixed lab paths plus the server's default
    export root and default socket meant run B wiped run A's fixtures and fought for run A's
    socket.  So the gate checks containment (every writable path under RUN_ROOT), uniqueness
    (RUN_ROOT freshly created by this pid, and a second call to the same allocator hands back
    a different directory), and that the legacy shared paths are not in use.
    """
    section("isolation — every writable path is inside this run's own directory (P0-5)")
    print(f"   run root      {RUN_ROOT}")
    print(f"   lab           {LAB}")
    print(f"   socket        {TEST_SOCKET}")
    print(f"   server.OUT_ROOT {getattr(server, 'OUT_ROOT', None)}")

    check(RUN_ROOT.is_dir(), f"the per-run root was not created: {RUN_ROOT}")
    check(str(os.getpid()) in RUN_ROOT.name,
          f"the per-run root {RUN_ROOT.name} does not carry this pid, so a human cannot tell "
          f"whose it is")
    check(LAB.is_relative_to(RUN_ROOT), f"the lab {LAB} escaped the per-run root {RUN_ROOT}")
    check(Path(TEST_SOCKET).parent.is_relative_to(RUN_ROOT),
          f"the api-server socket {TEST_SOCKET} is not inside the per-run root {RUN_ROOT}")

    # OUT_ROOT is computed at server import time from PHICAD_MCP_OUT, so this is the check that
    # the env var was exported EARLY enough. If the import order ever regresses, OUT_ROOT lands
    # back in the shared $TMPDIR/phicad-mcp-out and this fails.
    out_root = getattr(server, "OUT_ROOT", None)
    check(isinstance(out_root, Path), f"server has no OUT_ROOT Path: {out_root!r}")
    if isinstance(out_root, Path):
        check(out_root == EXPECTED_OUT_ROOT,
              f"server.OUT_ROOT is {out_root}, expected {EXPECTED_OUT_ROOT} — PHICAD_MCP_OUT "
              f"must be set BEFORE registry.load_server_module() imports server.py")
        check(out_root.is_relative_to(RUN_ROOT),
              f"exports would land in {out_root}, outside this run's root {RUN_ROOT} — a "
              f"concurrent run overwrites them")
        check(out_root != LEGACY_OUT_ROOT,
              f"server.OUT_ROOT is still the shared default {LEGACY_OUT_ROOT}")
    check(os.environ.get("PHICAD_MCP_OUT") == str(RUN_ROOT),
          f"PHICAD_MCP_OUT is {os.environ.get('PHICAD_MCP_OUT')!r}, not this run's root")

    # No fixed path may be in play any more, whatever $TMPDIR happens to be.
    check(LAB != LEGACY_LAB, f"the lab is back on the fixed shared path {LEGACY_LAB}")
    check(Path(TEST_SOCKET) != LEGACY_SOCKET,
          f"the socket is back on server.py's shared DEFAULT_SOCKET {LEGACY_SOCKET}")
    default_socket = getattr(server, "DEFAULT_SOCKET", None)
    check(TEST_SOCKET != default_socket,
          f"this run would use server.py's DEFAULT_SOCKET {default_socket}; every "
          f"api_server_start() call in this file must pass socket=TEST_SOCKET")

    # Uniqueness, demonstrated rather than asserted from the name: ask the same allocator for a
    # second root and prove it is a different directory (this is what makes two simultaneous
    # runs safe). Removed immediately — it holds no fixtures.
    peer = new_run_root()
    try:
        check(peer != RUN_ROOT,
              f"new_run_root() handed out {peer} twice — concurrent runs would share a lab")
        check(not LAB.is_relative_to(peer),
              f"this run's lab {LAB} is inside a peer run's root {peer}")
    finally:
        shutil.rmtree(peer, ignore_errors=True)


# --------------------------------------------------------------------------------------
# stage 0 — the server imports and every extension module is wired
# --------------------------------------------------------------------------------------


def test_wiring(server: Any, surface: Surface) -> None:
    """Every tools_*.py module the server tries to import must have configured cleanly.

    Reported, never crashed on: a module mid-write by another agent must show up as a named
    failure with its own error message, because "which module is broken" is the answer you
    want from this test, not a traceback out of the import.
    """
    section("extension modules wired (server._EXTENSIONS)")
    extensions: dict[str, dict[str, Any]] = getattr(server, "_EXTENSIONS", {})
    check(bool(extensions), "server._EXTENSIONS is empty — no extension module was imported")
    broken = {name: info.get("error") for name, info in extensions.items()
              if not info.get("ok")}
    for name, info in extensions.items():
        count = len(info.get("tools") or ())
        print(f"   {'ok  ' if info.get('ok') else 'FAIL'} {name:18} "
              f"{str(count) + ' tools' if info.get('ok') else info.get('error')}")
    check(not broken, f"extension modules failed to configure: {broken}")

    section("registry sees the whole surface")
    print(f"   {len(surface.specs)} tools in {len(registry.sections(surface.specs))} sections")
    check(len(surface.specs) >= 60,
          f"only {len(surface.specs)} tools registered — an extension module is missing")
    # Every module that configured ok must have all of its tools actually registered.
    for name, info in extensions.items():
        if not info.get("ok"):
            continue
        missing = [t for t in info.get("tools") or () if not surface.has(t)]
        check(not missing, f"{name} configured but these tools are not registered: {missing}")
    # Two modules are named project_history (the ai-view plugin copy and phicad/pylib), so
    # asserting "some module got imported" is not enough: importing the wrong one is exactly
    # the bug that left every mutating tool unjournaled while this check still passed. Assert
    # the hook's own module, that it is the pylib file, and that it has the function called.
    journal = getattr(server, "_journal_history", None)
    check(journal is not None,
          "server could not load phicad/pylib/project_history.py — the journal hook is dead")
    if journal is not None:
        src = Path(getattr(journal, "__file__", "") or "")
        check(src.parent.name == "pylib",
              f"the journal hook loaded {src} — it must be the phicad/pylib copy, not the "
              f"ai-view plugin's own project_history.py")
        check(hasattr(journal, "record_and_snapshot"),
              "the journal hook's module has no record_and_snapshot(); every mutating tool "
              "would report an AttributeError instead of journaling")


# --------------------------------------------------------------------------------------
# stage 1 — gates: DRC and ERC parse into numbers
# --------------------------------------------------------------------------------------


def test_gates(s: Surface, fx: dict[str, Path]) -> None:
    section("gates — pcb_drc / sch_erc")
    drc = s.call("pcb_drc", board_path=str(fx["board"]))
    report("pcb_drc", drc, ["exit_code", "report_path"])
    if ok_tool("pcb_drc", drc):
        summary = (drc.get("summary") or {}).get("violations") or {}
        total = summary.get("total")
        by_sev = summary.get("by_severity") or {}
        by_type = summary.get("by_type") or {}
        print(f"      violations={total} by_severity={num(by_sev)} types={num(sorted(by_type))}")
        check(isinstance(total, int) and total > 0,
              f"the microwave demo has known violations; got total={total}")
        check(total == sum(by_sev.values()),
              f"by_severity {by_sev} does not add up to total {total}")
        check(total == sum(by_type.values()),
              f"by_type {by_type} does not add up to total {total}")
        check("error" in by_sev and "warning" in by_sev,
              f"expected both error and warning buckets on this board: {by_sev}")
        check(Path(drc.get("report_path") or "/nonexistent").exists(),
              "pcb_drc reported a report_path that does not exist")

    erc = s.call("sch_erc", sch_path=str(fx["sch"]))
    report("sch_erc", erc, ["exit_code", "sheet_count", "report_path"])
    if ok_tool("sch_erc", erc):
        summary = (erc.get("summary") or {}).get("violations") or {}
        print(f"      violations={summary.get('total')} "
              f"by_severity={num(summary.get('by_severity'))} sheets={erc.get('sheet_count')}")
        check((erc.get("sheet_count") or 0) >= 2,
              f"pic_programmer is a 2-sheet hierarchy; sch_erc saw {erc.get('sheet_count')}")
        check(isinstance(summary.get("total"), int),
              f"sch_erc did not parse a violation total: {summary}")


# --------------------------------------------------------------------------------------
# stage 2 — semantic diffs: the exit-5 path is a result, not an error
# --------------------------------------------------------------------------------------


def test_diffs(s: Surface, fx: dict[str, Path]) -> None:
    section("semantic diffs — pcb_diff differs / identical")
    diff = s.call("pcb_diff", a=str(fx["board"]), b=str(fx["moved"]))
    report("pcb_diff(differs)", diff, ["exit_code", "differs", "change_count",
                                       "changes_by_kind"])
    if ok_tool("pcb_diff", diff):
        check(diff.get("exit_code") == 5,
              f"a differing pair must exit 5, got {diff.get('exit_code')}")
        check(diff.get("differs") is True, "the 5 mm footprint shift was not reported as differs")
        check((diff.get("change_count") or 0) >= 1,
              f"differs with change_count={diff.get('change_count')}")
        # `changes_by_kind` buckets modified/added/removed; the item TYPE lives in
        # changes_by_type, and that is where a moved footprint has to show up.
        types = diff.get("changes_by_type") or {}
        print(f"      changes_by_type={num(types)}")
        check(types.get("FOOTPRINT") == 1,
              f"a moved footprint should appear as one FOOTPRINT change: {types}")
        check((diff.get("changes_by_kind") or {}).get("modified", 0) >= 1,
              f"a shifted position is a modification: {diff.get('changes_by_kind')}")

    same = s.call("pcb_diff", a=str(fx["board"]), b=str(fx["board"]))
    report("pcb_diff(identical)", same, ["exit_code", "identical", "change_count"])
    if ok_tool("pcb_diff(identical)", same):
        check(same.get("exit_code") == 0, f"identical pair exited {same.get('exit_code')}")
        check(same.get("identical") is True, "a file compared with itself was not identical")


# --------------------------------------------------------------------------------------
# stage 3 — board summary and the CLI section (drill holes, layer table)
# --------------------------------------------------------------------------------------


def test_board_summary(s: Surface, fx: dict[str, Path]) -> None:
    section("board summary — kicad-tools parse in the automation venv")
    res = s.call("board_summary", board_path=str(fx["board"]))
    report("board_summary", res, ["footprint_count"])
    if not ok_tool("board_summary", res):
        return
    summary = res.get("summary") or {}
    details = res.get("details") or {}
    print(f"      {summary.get('width_mm')} x {summary.get('height_mm')} mm, "
          f"{summary.get('copper_layers')} copper layers, "
          f"{res.get('footprint_count')} footprints, "
          f"{details.get('layer_count')} layers")
    check(abs(float(summary.get("width_mm") or 0) - 292.1) < 0.5,
          f"microwave demo is 292.1 mm wide; got {summary.get('width_mm')}")
    check(summary.get("copper_layers") == 2,
          f"microwave demo is 2-layer; got {summary.get('copper_layers')}")
    check(res.get("footprint_count") == len(res.get("footprints") or []),
          "footprint_count disagrees with the footprint list")


def test_cli_section(s: Surface, fx: dict[str, Path]) -> None:
    section("cli section — pcb_export_drill (hole count) + pcb_layers (layer table)")
    drill = s.call("pcb_export_drill", board_path=str(fx["pcb"]))
    report("pcb_export_drill", drill, ["out_dir"])
    if ok_tool("pcb_export_drill", drill):
        rep = drill.get("report") or {}
        print(f"      holes={rep.get('holes')} tools={rep.get('tools')} "
              f"format={rep.get('format')} outputs={len(drill.get('outputs') or [])}")
        check((rep.get("holes") or 0) > 100,
              f"pic_programmer is a through-hole board; drill report says {rep.get('holes')}")
        check((rep.get("tools") or 0) > 1, f"expected several drill tools: {rep.get('tools')}")
        outs = drill.get("outputs") or []
        check(bool(outs) and all(Path(p).exists() for p in outs),
              f"pcb_export_drill listed outputs that are not on disk: {outs}")

    layers = s.call("pcb_layers", board_path=str(fx["board"]))
    report("pcb_layers", layers, ["copper_count", "layer_count", "parser",
                                   "layers_arg_all_copper"])
    if ok_tool("pcb_layers", layers):
        check(layers.get("copper") == ["F.Cu", "B.Cu"],
              f"expected a 2-layer stack F.Cu/B.Cu; got {layers.get('copper')}")
        check(layers.get("layer_count") == len(layers.get("layers") or []),
              "layer_count disagrees with the layer table")
        check(layers.get("layers_arg_all_copper") == "F.Cu,B.Cu",
              f"the ready-made --layers argument is wrong: "
              f"{layers.get('layers_arg_all_copper')}")
        check(sum((layers.get("by_type") or {}).values()) == layers.get("layer_count"),
              f"by_type does not add up to layer_count: {layers.get('by_type')}")


# --------------------------------------------------------------------------------------
# stage 4 — the schematic surface (text-level reads, then a verified write)
# --------------------------------------------------------------------------------------


def test_schematic(s: Surface, fx: dict[str, Path]) -> None:
    section("schematic — sch_list_symbols / sch_list_sheets / sch_symbol_lib_list")
    syms = s.call("sch_list_symbols", sch_path=str(fx["sch"]))
    report("sch_list_symbols", syms, ["symbol_count", "power_symbol_count", "scope"])
    if ok_tool("sch_list_symbols", syms):
        symbols = syms.get("symbols") or []
        print(f"      symbols={len(symbols)} unique_refs={syms.get('unique_refs')} "
              f"power={syms.get('power_symbol_count')}")
        # 105 symbols in the root sheet of the demo — the count is the point: an earlier bug
        # truncated the worker's stdout at 20 000 chars and returned "produced no JSON".
        check(len(symbols) >= 100,
              f"pic_programmer's root sheet holds ~105 symbols; got {len(symbols)}")
        check(syms.get("symbol_count") == len(symbols),
              "symbol_count disagrees with the returned list")
        refs = {sym.get("ref") for sym in symbols}
        # Root-sheet refs only: U1 lives on the pic_sockets child sheet, which is exactly the
        # scope distinction `scope` reports and sch_annotate_check crosses.
        check({"R1", "C1", "U2"} <= refs,
              f"expected R1/C1/U2 on the root sheet; sample={sorted(refs)[:8]}")
        check(syms.get("scope") == "this file only",
              f"sch_list_symbols defaults to one file; scope={syms.get('scope')!r}")
        check((syms.get("power_symbol_count") or 0) > 0,
              "the demo is full of power flags; none were classified as power symbols")
        first = symbols[0] if symbols else {}
        check({"ref", "value", "lib_id", "at", "uuid"} <= set(first),
              f"a symbol entry is missing structural keys: {sorted(first)}")

    sheets = s.call("sch_list_sheets", sch_path=str(fx["sch"]))
    report("sch_list_sheets", sheets, ["file_count", "is_flat", "missing_sheet_files"])
    if ok_tool("sch_list_sheets", sheets):
        check(sheets.get("file_count") == 2,
              f"pic_programmer is root + pic_sockets = 2 files; got {sheets.get('file_count')}")
        check(sheets.get("is_flat") is False, "a hierarchy was reported as flat")
        check(not sheets.get("missing_sheet_files"),
              f"unresolved child sheets: {sheets.get('missing_sheet_files')}")

    ann = s.call("sch_annotate_check", sch_path=str(fx["sch"]))
    report("sch_annotate_check", ann, ["annotated", "symbol_count", "duplicate_count",
                                        "unannotated_count"])
    if ok_tool("sch_annotate_check", ann):
        check(ann.get("annotated") is True,
              f"the demo is fully annotated; unannotated={ann.get('unannotated')}")
        check((ann.get("symbol_count") or 0) > (syms.get("symbol_count") or 0),
              "the hierarchy-wide symbol count should exceed the root sheet's")

    lib = s.call("sch_symbol_lib_list", sym_path=str(fx["sym"]))
    report("sch_symbol_lib_list", lib, ["library", "symbol_count", "version"])
    if ok_tool("sch_symbol_lib_list", lib):
        names = [d.get("name") for d in lib.get("symbols") or []]
        check(lib.get("symbol_count") == len(names),
              "symbol_count disagrees with the symbol list")
        check(len(names) >= 20, f"the demo library holds 26 symbols; got {len(names)}")
        check("24C16" in names, f"expected 24C16 in the demo library: {names[:6]}")

    section("schematic write — sch_set_field to a NEW file, verified by the fork's own reader")
    edited = fx["lab"] / "sch_write_check.kicad_sch"
    wrote = s.call("sch_set_field", sch_path=str(fx["sch"]), ref="R1", field="MPN",
                   value="PHICAD-TEST-MPN", out_path=str(edited), verify=True)
    report("sch_set_field", wrote, ["in_place", "change_count", "loads", "bytes_after"])
    if ok_tool("sch_set_field", wrote):
        check(wrote.get("in_place") is False,
              "writing to a different out_path must not report in_place")
        check(wrote.get("loads") is True,
              f"the fork could not re-read the written schematic: {wrote.get('load_check')}")
        check(edited.exists() and edited.stat().st_size > 0, f"no file written at {edited}")
        check((wrote.get("changes") or [{}])[0].get("after") == "PHICAD-TEST-MPN",
              f"the change record does not show the new value: {wrote.get('changes')}")
        check("PHICAD-TEST-MPN" in edited.read_text(),
              "the value is not in the file the tool claims to have written")
        check(fx["sch"].read_text().find("PHICAD-TEST-MPN") == -1,
              "an out_path write MUTATED THE INPUT — that is the one thing it must never do")


# --------------------------------------------------------------------------------------
# stage 5 — the git surface + THE INTEGRATION PROOF (server.py's journal hook)
# --------------------------------------------------------------------------------------


def git(project: Path, *args: str) -> str:
    """Read the project's Git history scope directly — the tools must agree with real git."""
    proc = subprocess.run(["git", "-C", str(project), *args], capture_output=True, text=True)
    return proc.stdout.strip()


def test_project_history(s: Surface, fx: dict[str, Path]) -> None:
    project = fx["journal_project"]
    section(f"git surface — project_init on a throwaway project ({project.name})")
    init = s.call("project_init", path=str(project))
    report("project_init", init, ["created", "branch", "head", "commits", "journal"])
    if not ok_tool("project_init", init):
        return
    check((project / ".git").is_dir(), f"no git repository at {project}/.git")
    check(init.get("commits") == 1, f"init should leave exactly one commit; got "
                                    f"{init.get('commits')}")
    baseline = git(project, "rev-parse", "HEAD")
    check(len(baseline) == 40, f"could not read the baseline commit: {baseline!r}")
    tracked = git(project, "ls-files").splitlines()
    check(any(f.endswith(".kicad_pcb") for f in tracked),
          f"the baseline commit does not track the board: {tracked[:6]}")

    section("INTEGRATION PROOF — a mutating tool must journal AND snapshot by itself")
    sch = project / "pic_programmer.kicad_sch"
    before_commits = int(git(project, "rev-list", "--count", "HEAD"))
    # In-place: out_path == sch_path is the documented way to mutate the caller's file, and
    # in-place is exactly what the journal hook exists to capture.
    edit = s.call("sch_set_field", sch_path=str(sch), ref="R1", field="MPN",
                  value="JOURNAL-PROOF-1", out_path=str(sch), verify=True)
    report("sch_set_field(in place)", edit, ["in_place", "change_count", "loads", "mutated"])
    if not ok_tool("sch_set_field(in place)", edit):
        return
    check(edit.get("in_place") is True, "an out_path == sch_path write did not report in_place")
    check("JOURNAL-PROOF-1" in sch.read_text(), "the in-place edit did not reach the file")

    history = edit.get("project_history") or {}
    print(f"   project_history: {num(history)[:300]}")
    check(history.get("journaled") is True,
          f"server.py's journal hook did not journal the edit: {history.get('reason')}")
    check(history.get("committed") is True,
          f"the hook did not commit a snapshot: {num(history)}")
    commit = history.get("commit") or ""
    check(len(commit) == 40, f"the hook reported no commit sha: {commit!r}")

    # ... and now the same facts read out of real git, not out of the tool's own result.
    after_commits = int(git(project, "rev-list", "--count", "HEAD"))
    check(after_commits == before_commits + 1,
          f"expected exactly one new commit, went {before_commits} -> {after_commits}")
    head = git(project, "rev-parse", "HEAD")
    check(head == commit, f"HEAD {head[:10]} is not the commit the hook reported {commit[:10]}")
    subject = git(project, "log", "-1", "--format=%s")
    print(f"   HEAD subject: {subject}")
    check("sch_set_field" in subject,
          f"the snapshot commit message does not name the tool: {subject!r}")
    changed = git(project, "show", "--name-only", "--format=", "HEAD").split()
    check("pic_programmer.kicad_sch" in changed,
          f"the snapshot did not capture the edited schematic: {changed}")

    journal_path = project / ".phicad" / "journal.jsonl"
    check(journal_path.exists(), f"no journal file at {journal_path}")
    entries = [json.loads(line) for line in journal_path.read_text().splitlines() if line.strip()]
    last = entries[-1] if entries else {}
    print(f"   journal entries: {len(entries)}, last action={last.get('action')}")
    check(bool(entries), "the journal file is empty after a mutating tool ran")
    check(last.get("action") == "sch_set_field",
          f"the journal's last action is {last.get('action')!r}, not sch_set_field")
    check(str(last.get("actor") or "").startswith("mcp:"),
          f"the journal did not record an mcp actor: {last.get('actor')!r}")
    check(last.get("commit") == commit,
          f"the journal entry points at {str(last.get('commit'))[:10]}, the hook committed "
          f"{commit[:10]}")

    section("git surface — status / log / journal / diff / tag / restore / undo")
    status = s.call("project_status", path=str(project))
    report("project_status", status, ["branch", "head", "commits", "journal_entries"])
    if ok_tool("project_status", status):
        check(status.get("commits") == after_commits,
              f"project_status says {status.get('commits')} commits, git says {after_commits}")
        check(status.get("head") == head[:7],
              f"project_status HEAD {status.get('head')} vs git {head[:7]}")
        check(status.get("journal_entries") == len(entries),
              "project_status miscounts the journal")

    log = s.call("project_log", path=str(project), limit=10)
    report("project_log", log, ["count"])
    if ok_tool("project_log", log):
        commits = log.get("commits") or []
        check(len(commits) == after_commits,
              f"project_log returned {len(commits)} of {after_commits} commits")
        check(commits[0].get("actor") == "mcp:sch_set_field",
              f"the newest commit's actor is {commits[0].get('actor')!r}")
        check(commits[0].get("sha") == head, "project_log's newest sha is not HEAD")

    journal = s.call("project_journal", path=str(project), limit=10)
    report("project_journal", journal, ["count", "total", "exists"])
    if ok_tool("project_journal", journal):
        check(journal.get("total") == len(entries), "project_journal miscounts the journal")

    diff = s.call("project_diff", path=str(project), rev_a="HEAD~1", rev_b="HEAD")
    report("project_diff", diff, ["changed", "semantic_change_count", "stat"])
    if ok_tool("project_diff", diff):
        check(diff.get("changed") == 1, f"one file changed; project_diff says "
                                        f"{diff.get('changed')}")
        files = diff.get("files") or []
        check(files and files[0].get("path") == "pic_programmer.kicad_sch",
              f"project_diff named the wrong file: {files}")
        semantic = diff.get("semantic") or []
        check(bool(semantic), "project_diff produced no SEMANTIC diff — the fork's own "
                              "sch diff is what makes this more than `git diff`")
        if semantic:
            first = semantic[0]
            print(f"      semantic: kind={first.get('kind')} exit={first.get('exit_code')} "
                  f"differs={first.get('differs')}")
            check(first.get("differs") is True,
                  f"the semantic diff of a real edit says differs={first.get('differs')}")
            check(first.get("exit_code") == 5,
                  f"a differing sch diff exits 5; got {first.get('exit_code')}")

    tag = s.call("project_tag", path=str(project), name="test-milestone",
                 message="test_tools.py milestone")
    report("project_tag", tag, ["tag", "short"])
    if ok_tool("project_tag", tag):
        check("test-milestone" in git(project, "tag", "--list"),
              "project_tag did not create a real git tag")

    # restore: dry-run first (must change nothing), then apply and check the bytes came back.
    dry = s.call("project_restore", path=str(project), rev="HEAD~1", dry_run=True)
    report("project_restore(dry)", dry, ["dry_run", "would_change"])
    if ok_tool("project_restore(dry)", dry):
        check(dry.get("dry_run") is True, "a restore without dry_run=False must be a dry run")
        check(dry.get("would_change") == ["pic_programmer.kicad_sch"],
              f"the dry run named the wrong files: {dry.get('would_change')}")
        check("JOURNAL-PROOF-1" in sch.read_text(),
              "a DRY RUN restore changed the working tree")
    applied = s.call("project_restore", path=str(project), rev="HEAD~1",
                     files=["pic_programmer.kicad_sch"], dry_run=False)
    report("project_restore(apply)", applied, ["dry_run", "restored", "snapshot"])
    if ok_tool("project_restore(apply)", applied):
        check("JOURNAL-PROOF-1" not in sch.read_text(),
              "restore with dry_run=False did not bring the pre-edit schematic back")
        check(applied.get("restored") is True,
              f"the restore reported restored={applied.get('restored')}")
        # A restore is itself committed, so the audit trail keeps the rollback.
        check("restore" in git(project, "log", "-1", "--format=%s").lower(),
              f"the restore was not committed: HEAD is "
              f"{git(project, 'log', '-1', '--format=%s')!r}")

    undo = s.call("project_undo_last", path=str(project), dry_run=True)
    report("project_undo_last(dry)", undo, ["dry_run", "head_short", "head_subject"])
    if ok_tool("project_undo_last(dry)", undo):
        commits_now = int(git(project, "rev-list", "--count", "HEAD"))
        check(undo.get("dry_run") is True, "undo without dry_run=False must not rewrite history")
        check(undo.get("reverted") is False, "a dry-run undo reported an actual revert")
        check(int(git(project, "rev-list", "--count", "HEAD")) == commits_now,
              "a dry-run undo changed the commit count")

    watch = s.call("project_watch_once", path=str(project), stable_seconds=0.1)
    report("project_watch_once", watch, ["committed", "reason", "short"])
    check(isinstance(watch, dict) and "ok" in watch,
          "project_watch_once did not return a structured result")


# --------------------------------------------------------------------------------------
# stage 6 — the remaining read-only sections: synthesis, swarm, parts, repo state
# --------------------------------------------------------------------------------------


def test_synth(s: Surface) -> None:
    section("design synthesis — synth_check on the shipped design IR")
    design = REPO_ROOT / "phicad" / "synth" / "examples" / "sensor-node.json"
    if not design.exists():
        skip("synth_check", f"no design IR at {design}")
        return
    res = s.call("synth_check", design_json=str(design))
    report("synth_check", res, ["exit_code"])
    if ok_tool("synth_check", res):
        rep = res.get("report") or {}
        print(f"      project={rep.get('project')} parts={rep.get('parts')} "
              f"nets={rep.get('nets')} problems={len(rep.get('problems') or [])}")
        check(rep.get("ok") is True, f"the shipped design IR does not validate: {rep}")
        check((rep.get("parts") or 0) > 0 and (rep.get("nets") or 0) > 0,
              f"synth_check parsed no parts/nets: {rep}")
        check(not rep.get("problems"), f"problems on a known-good design: {rep.get('problems')}")


def test_swarm(s: Surface, fx: dict[str, Path]) -> None:
    section("layout swarm — swarm_partition / swarm_score")
    board = fx["pcb"]
    part = s.call("swarm_partition", board_path=str(board), groups=2)
    report("swarm_partition", part, ["plan_path", "exit_code"])
    if ok_tool("swarm_partition", part):
        rep = part.get("report") or {}
        groups = rep.get("groups") or []
        print(f"      groups={len(groups)} axis={rep.get('axis')} "
              f"nets={sum(len(g.get('nets') or []) for g in groups)}")
        check(len(groups) == 2, f"asked for 2 groups, got {len(groups)}")
        check(sum(len(g.get("nets") or []) for g in groups) > 0, "the plan partitioned no nets")
        plan = part.get("plan_path")
        check(plan and Path(plan).exists(), f"no plan written at {plan}")

    score = s.call("swarm_score", board_path=str(board))
    report("swarm_score", score, ["exit_code"])
    if ok_tool("swarm_score", score):
        rep = score.get("report") or {}
        print(f"      score={rep.get('score')} unconnected={rep.get('unconnected')} "
              f"errors={rep.get('drc_errors')} segments={rep.get('segments')}")
        check(isinstance(rep.get("score"), (int, float)),
              f"swarm_score produced no comparable float: {rep.get('score')}")
        check((rep.get("segments") or 0) > 0,
              f"the routed demo board should report copper: {rep.get('segments')}")


def test_repo_state(s: Surface) -> None:
    section("repo / build state — phicad_status")
    res = s.call("phicad_status")
    report("phicad_status", res, ["repo_root", "kicad_cli_built", "managed_api_server_running"])
    if ok_tool("phicad_status", res):
        check(res.get("kicad_cli_built") is True,
              f"phicad_status says kicad-cli is not built at {res.get('kicad_cli')}")
        check(res.get("project_history") == "available",
              f"phicad_status reports the journal layer as {res.get('project_history')!r}")
        extensions = res.get("extensions") or {}
        check(bool(extensions), "phicad_status does not report the extension modules")
        broken = {k: v.get("error") for k, v in extensions.items() if not v.get("ok")}
        check(not broken, f"phicad_status reports broken extensions: {broken}")


# --------------------------------------------------------------------------------------
# stage 7 — exports (--full): the artifacts that go to a fab house
# --------------------------------------------------------------------------------------


def test_exports(s: Surface, fx: dict[str, Path]) -> None:
    section("exports — gerbers / render / stackup / pos")
    gerb = s.call("pcb_export_gerbers", board_path=str(fx["pcb"]))
    report("pcb_export_gerbers", gerb, ["file_count", "out_dir"])
    if ok_tool("pcb_export_gerbers", gerb):
        files = gerb.get("files") or []
        check(len(files) >= 5, f"a 2-layer board with drill should yield >5 files; got "
                              f"{len(files)}")
        check(all(Path(gerb["out_dir"], f).exists() or Path(f).exists() for f in files),
              "pcb_export_gerbers listed files that are not on disk")

    render = s.call("pcb_render", board_path=str(fx["board"]))
    report("pcb_render", render, ["png_path", "png_bytes"])
    if ok_tool("pcb_render", render):
        check((render.get("png_bytes") or 0) > 1000,
              f"a raytraced render should be more than a stub: {render.get('png_bytes')} bytes")
        check(Path(render.get("png_path") or "/nonexistent").exists(), "no PNG on disk")

    stack = s.call("pcb_export_stackup", board_path=str(fx["board"]))
    report("pcb_export_stackup", stack, ["format", "outputs"])
    if ok_tool("pcb_export_stackup", stack):
        rep = stack.get("report") or {}
        check((rep.get("copper_layers") or 0) == 2,
              f"the stackup report disagrees with the 2-layer board: {rep}")

    pos = s.call("pcb_export_pos", board_path=str(fx["pcb"]))
    report("pcb_export_pos", pos, ["pos_path", "row_count"])
    if ok_tool("pcb_export_pos", pos):
        check((pos.get("row_count") or 0) > 0,
              f"the pick-and-place file has no rows: {pos.get('row_count')}")

    bom = s.call("sch_export_bom", sch_path=str(fx["sch"]))
    report("sch_export_bom", bom, ["csv_path", "row_count"])
    if ok_tool("sch_export_bom", bom):
        check((bom.get("row_count") or 0) > 10,
              f"the demo BOM should have dozens of rows: {bom.get('row_count')}")


def check_jobset_drc_report(path: Path, *, expect_violations: bool) -> None:
    """Assert that a jobset report actually checked actionable severities before trusting it."""
    if not check(path.exists(), f"jobset did not write its DRC report: {path}"):
        return
    payload = json.loads(path.read_text(encoding="utf-8"))
    included = set(payload.get("included_severities") or [])
    check({"error", "warning", "exclusion"} <= included,
          f"DRC report omitted an actionable severity: included_severities={sorted(included)}")
    violations = len(payload.get("violations") or []) + len(payload.get("unconnected_items") or [])
    if expect_violations:
        check(violations > 0, "deliberately-invalid board produced an empty DRC report")
    else:
        check(violations == 0, f"known-valid board produced {violations} DRC findings")


def test_jobset(s: Surface, fx: dict[str, Any]) -> None:
    section("jobsets — valid fixture accepted, deliberately-invalid fixture rejected")
    valid = fx["jobset_pass"]
    accepted = s.call("jobset_run", jobset_path=str(valid["jobset"]),
                      project_path=str(valid["pro"]))
    report("jobset_run(valid)", accepted, ["jobs_succeeded", "jobs_failed", "exit_code"])
    if ok_tool("jobset_run(valid)", accepted):
        check(accepted.get("jobs_succeeded") == 3,
              f"valid fab pack did not complete all jobs: {accepted.get('failures')}")
        check(accepted.get("jobs_failed") == 0,
              f"valid fab pack reported a failed job: {accepted.get('failures')}")
        check_jobset_drc_report(valid["report"], expect_violations=False)

    invalid = fx["jobset_fail"]
    rejected = s.call("jobset_run", jobset_path=str(invalid["jobset"]),
                      project_path=str(invalid["pro"]))
    report("jobset_run(invalid)", rejected, ["jobs_succeeded", "jobs_failed", "exit_code"])
    check(rejected.get("ok") is False,
          "deliberately-invalid board passed the starter fabrication jobset")
    check(rejected.get("jobs_succeeded") == 2 and rejected.get("jobs_failed") == 1,
          f"invalid fab pack tally did not isolate the DRC failure: {rejected}")
    check((rejected.get("exit_code") or 0) != 0,
          f"invalid fab pack returned a success exit code: {rejected}")
    check_jobset_drc_report(invalid["report"], expect_violations=True)


def test_autoroute(s: Surface, fx: dict[str, Path]) -> None:
    section("autoroute — DSN export (the loop's outbound leg) + cleanup report")
    dsn = s.call("pcb_export_dsn", board_path=str(fx["pcb"]))
    report("pcb_export_dsn", dsn, ["dsn_path", "dsn_bytes"])
    if ok_tool("pcb_export_dsn", dsn):
        rep = dsn.get("report") or {}
        print(f"      nets={rep.get('nets')} placements={rep.get('placements')} "
              f"layers={rep.get('copper_layers')}")
        check((rep.get("nets") or 0) > 10, f"the DSN carries no netlist: {rep}")
        check((rep.get("placements") or 0) > 10, f"the DSN carries no placements: {rep}")
        check(rep.get("has_structure") and rep.get("has_network") and rep.get("has_placement"),
              f"the DSN is missing a required section: {rep}")

    clean = s.call("autoroute_cleanup", board_path=str(fx["pcb"]), apply=False)
    report("autoroute_cleanup", clean, ["apply"])
    if ok_tool("autoroute_cleanup", clean):
        check(clean.get("apply") in (False, None),
              "a report-only cleanup must not claim it applied anything")
        check(isinstance(clean.get("report"), dict),
              f"autoroute_cleanup produced no report: {clean.get('report')}")


# --------------------------------------------------------------------------------------
# stage 8 — the managed headless session (--full only; starts a child process)
# --------------------------------------------------------------------------------------


def test_session(s: Surface, fx: dict[str, Path]) -> None:
    section("headless session — api_server_start / session_open / query / stop")
    started = s.call("api_server_start", socket=TEST_SOCKET, wait_s=40)
    report("api_server_start", started, ["pid", "socket", "ping"])
    if not ok_tool("api_server_start", started):
        return
    try:
        check(started.get("socket") == TEST_SOCKET,
              f"the child took socket {started.get('socket')}, not the test's {TEST_SOCKET}")
        double = s.call("api_server_start", socket=TEST_SOCKET)
        report("api_server_start(second)", double, ["error"])
        check(double.get("ok") is False, "a second api-server was allowed to start")

        opened = s.call("session_open", path=str(fx["pcb"]))
        report("session_open", opened, ["doc_type", "file"])
        if ok_tool("session_open", opened):
            query = s.call("session_board_query", what="footprints", limit=5)
            report("session_board_query", query, ["footprints_total"])
            if ok_tool("session_board_query", query):
                check((query.get("footprints_total") or 0) > 10,
                      f"the live board reports {query.get('footprints_total')} footprints")
                fps = query.get("footprints") or []
                check(len(fps) <= 5, "limit=5 was not honoured")
                check(all(fp.get("reference") for fp in fps),
                      f"a live footprint came back without a reference: {fps[:2]}")
            nets = s.call("session_board_query", what="nets")
            report("session_board_query(nets)", nets, ["nets_total"])
            ok_tool("session_board_query(nets)", nets)
            closed = s.call("session_close")
            report("session_close", closed, ["state"])
            ok_tool("session_close", closed)
    finally:
        stopped = s.call("api_server_stop")
        report("api_server_stop", stopped, ["state", "pid", "exit_code"])
        ok_tool("api_server_stop", stopped)
        after = s.call("api_server_status")
        report("api_server_status(after)", after, ["running", "pid"])
        check(not after.get("running"), "the managed api-server survived api_server_stop")


# --------------------------------------------------------------------------------------
# stage 9 — the registry listing itself (the surface an agent discovers)
# --------------------------------------------------------------------------------------


def test_registry_listing(s: Surface) -> None:
    section("registry listing — sections, mutating verdicts, JSON round trip")
    sections = registry.sections(s.specs)
    for name in sections:
        tools = [t.name for t in s.specs if t.section == name]
        writers = [t.name for t in s.specs if t.section == name and t.mutates]
        print(f"   {name:34} {len(tools):3} tools, {len(writers):2} write")
    check(len(sections) >= 12, f"only {len(sections)} sections — a module lost its banner")
    uncategorised = [t.name for t in s.specs if t.section.lower().startswith("uncategor")]
    check(not uncategorised, f"tools with no section: {uncategorised}")

    # Every section this wave added must be present and non-empty. Matched on the SECTION KEY,
    # because a banner headline is prose ("Gates — DRC / ERC") and the key is the stable handle
    # `phicad-tool list --section` takes.
    keys = {t.section_key for t in s.specs}
    for expected in ("git", "schematic", "cli", "autoroute", "gates", "semantic-diffs",
                     "headless-session-surface"):
        check(expected in keys, f"section key {expected!r} is missing: {sorted(keys)}")

    writers = [t.name for t in s.specs if t.mutates]
    check(len(writers) >= 15, f"only {len(writers)} tools are classified as writers")
    for name in ("sch_set_field", "project_snapshot", "pcb_import_ses", "swarm_merge"):
        if s.has(name):
            check(s.by_name[name].mutates, f"{name} is not classified as mutating")
    for name in ("pcb_drc", "sch_list_symbols", "board_summary", "project_log"):
        if s.has(name):
            check(not s.by_name[name].mutates, f"{name} is wrongly classified as mutating")

    payload = json.loads(registry.render_json(s.specs))
    check(payload.get("tool_count") == len(s.specs), "render_json miscounts the surface")
    check(len(payload.get("tools") or []) == len(s.specs), "render_json dropped tools")
    check(all(t.get("summary") and t.get("signature") for t in payload["tools"]),
          "render_json emitted a tool with no summary or signature")
    page = registry.render_markdown(s.specs)
    missing = [t.name for t in s.specs if f"### {t.name}\n" not in page]
    check(not missing, f"render_markdown skipped tools: {missing}")
    print(f"   render_json {len(registry.render_json(s.specs))} chars, "
          f"render_markdown {len(page)} chars, {len(writers)} writers")


# --------------------------------------------------------------------------------------
# regressions — one check per bug found by the 2026-09-07 surface audit.
# Each of these SHIPPED BROKEN and produced a silently wrong result, so each gets a gate.
# --------------------------------------------------------------------------------------


def test_regressions(s: Surface, fx: dict[str, Path]) -> None:
    section("regressions (audit 2026-09-07)")

    # 1. _run() clipped stdout to 20 000 chars, so any JSON-emitting child was corrupted.
    #    session_list_items failed at its own DEFAULT limit of 100 on any real board.
    import server as _srv                                    # already imported by the harness

    big = s.call("kicad_cli_help") if s.has("kicad_cli_help") else {}
    del big
    res = _srv._run([sys.executable, "-c",
                     "print('x' * 60000)"], stdout_limit=None)
    check(len(res.get("stdout", "")) > 50000,
          "_run(stdout_limit=None) must not clip a JSON-sized payload "
          f"(got {len(res.get('stdout', ''))} chars)")
    check(not res.get("stdout_truncated"),
          "_run(stdout_limit=None) must not flag truncation")
    clipped = _srv._run([sys.executable, "-c", "print('x' * 60000)"])
    check(clipped.get("stdout_truncated") is True and clipped.get("stdout_bytes", 0) > 50000,
          "a clipped _run() must SAY it clipped, so a parse failure is never blamed on the child")

    # 1b. Pipeline JSON is a structured channel, not a presentation log.  In particular, the
    # parts audit is allowed to exit 1 when its report contains failures, but that status must
    # never become ok=true when a large report is parsed (or when parsing fails).
    old_venv = _srv.VENV_PY
    child = fx["lab"] / "venv-json-child.py"

    def run_child(stdout: str, exit_code: int, stderr: str = "") -> dict[str, Any]:
        child.write_text(
            "import sys\n"
            f"sys.stdout.write({stdout!r})\n"
            f"sys.stderr.write({stderr!r})\n"
            f"raise SystemExit({exit_code})\n",
            encoding="utf-8")
        _srv.VENV_PY = Path(sys.executable)
        return _srv._venv_script(child, [], ok_codes=(0, 1))

    try:
        large_success = {"ok": True, "payload": "success-" + "a" * 25000}
        success = run_child(json.dumps(large_success), 0,
                            stderr="warning from worker\n" * 10000)
        check(len(json.dumps(large_success)) > 20000,
              "the valid success fixture must exceed the old 20 KB display limit")
        check(success.get("ok") is True and success.get("report") == large_success,
              "a >20 KB valid JSON report must survive _venv_script intact")
        check("stdout" not in success and not success.get("stdout_truncated"),
              "parsed structured output must not be copied into a clipped presentation log")
        check(len(success.get("stderr", "")) <= 8000,
              "stderr noise must remain a separately capped presentation log")

        large_failure = {"ok": False, "failures": ["failure"] * 112,
                         "payload": "failure-" + "b" * 25000}
        failure = run_child(json.dumps(large_failure), 1)
        check(len(json.dumps(large_failure)) > 20000,
              "the valid nonzero fixture must exceed the old 20 KB display limit")
        check(failure.get("ok") is False and failure.get("exit_code") == 1,
              "a nonzero pipeline child must fail closed even when 1 is an allowed audit code")
        check(failure.get("report") == large_failure,
              "a >20 KB valid failure report must remain available for diagnosis")

        secret = "MALFORMED_OUTPUT_SECRET"
        truncated = json.dumps({"ok": True, "payload": secret + "c" * 25000})[:-1]
        malformed = run_child(truncated, 0)
        check(malformed.get("ok") is False and "report" not in malformed,
              "malformed/truncated structured output must fail closed without a report")
        check(malformed.get("stdout_truncated") is True
              and malformed.get("stdout_bytes") == len(truncated)
              and len(malformed.get("stdout", "")) <= 4000,
              "malformed output diagnostics must be capped independently of JSON capture")
        check(secret not in malformed.get("stdout", ""),
              "capped malformed-output diagnostics must not leak the discarded prefix")
    finally:
        _srv.VENV_PY = old_venv
        child.unlink(missing_ok=True)
    check(not child.exists(),
          "the venv JSON regression child must be cleaned up; no temporary fixture may leak")

    # 2. The journal swept whole directories: `out_dir`/`project_dir` = the project root meant
    #    `git add <dir>`, committing the user's unrelated dirty files under the tool's name.
    check(_srv._paths_in({"out_dir": str(fx["lab"])}) == [],
          "_paths_in must ignore DIRECTORIES (a directory in out_dir swept unrelated user work)")
    check(_srv._paths_in({"mutated": [str(fx["board"])]}) == [str(fx["board"].resolve())],
          "_paths_in must still report real files, resolved through symlinks")

    # 3. Two writers on one file lost an edit silently; the second reported ok=true.
    sys.path.insert(0, str(REPO_ROOT / "phicad" / "pylib"))
    import sexp_edit as _se

    race = fx["lab"] / "race.kicad_sch"
    shutil.copy2(fx["sch"], race)
    a = _se.Doc.load(race)
    b = _se.Doc.load(race)
    b.insert(len(b.text) - 1, "", "noop")
    b.save(race)                                             # B commits first
    a.insert(len(a.text) - 1, "", "noop")
    stale = False
    try:
        a.save(race)                                         # A is now stale
    except _se.StaleWrite:
        stale = True
    check(stale, "a stale in-place write must raise StaleWrite, not silently clobber the "
                 "other writer's edit")
    fresh = _se.Doc.load(race)
    fresh.insert(len(fresh.text) - 1, "", "noop")
    check(fresh.save(race) > 0, "a fresh load+save must still succeed (no false staleness)")

    # 4. pcb_drc could not run schematic parity, and always emitted a zero-filled
    #    `schematic_parity` block that READ as "parity is clean".
    plain = s.call("pcb_drc", board_path=str(fx["pcb"]))
    if ok_tool("pcb_drc", plain):
        check("schematic_parity" not in (plain.get("summary") or {}),
              "pcb_drc must NOT emit a schematic_parity key when parity was not requested "
              "(a zero there reads as 'clean')")
    parity = s.call("pcb_drc", board_path=str(fx["pcb"]), schematic_parity=True, units="in")
    if ok_tool("pcb_drc(schematic_parity)", parity):
        block = (parity.get("summary") or {}).get("schematic_parity")
        check(isinstance(block, dict), "pcb_drc(schematic_parity=True) must report the block")
        check(parity.get("coordinate_units") == "in", "pcb_drc(units=) must reach the report")
        report("pcb_drc parity", parity, ["schematic_parity_requested", "coordinate_units"])
        if isinstance(block, dict):
            print(f"      parity total={block.get('total')} by_severity={block.get('by_severity')}")
    bad = s.call("pcb_drc", board_path=str(fx["pcb"]), save_board=True)
    check(bad.get("ok") is False and "refill_zones" in str(bad.get("error", "")),
          "pcb_drc(save_board=True) without refill_zones must be refused with a clear reason")

    # 5. A killed server orphaned its api-server child: no pidfile, so a later process saw
    #    nothing to reap. The pidfile helpers are what make adoption possible.
    check(callable(getattr(_srv, "_foreign_api", None)),
          "server must expose _foreign_api() so a fresh process can adopt a live api-server")
    check(_srv._pidfile_for(Path("/tmp/x.sock")).name == "x.sock.pid",
          "the pidfile must sit beside its socket")
    check(_srv._pid_running(os.getpid()) and not _srv._pid_running(999_999),
          "_pid_running must distinguish a live pid from a dead one")

    # 6. project_init created repositories with git's DEFAULT TEXT MERGE on 600 KB
    #    s-expression files. A merge that touched one board wrote `<<<<<<<` INTO it and the
    #    result would not load at all -- so two agents on one project repo destroyed boards.
    # Use the module the SERVER loaded by path, not a bare `import project_history`: two
    # files share that name (the ai-view plugin copy and phicad/pylib), and a bare import
    # picks whichever is on sys.path first -- which is how this check first failed against a
    # module that had none of the code under test.
    _ph = getattr(_srv, "_journal_history", None)
    if not check(_ph is not None and hasattr(_ph, "ProjectHistory"),
                 "server._journal_history must expose ProjectHistory for the merge-driver check"):
        return

    merge_repo = fx["lab"] / "mergelab"
    shutil.rmtree(merge_repo, ignore_errors=True)
    merge_repo.mkdir(parents=True)
    shutil.copy2(fx["pcb"], merge_repo / "b.kicad_pcb")
    hist = _ph.ProjectHistory(merge_repo)
    hist.ensure(message="phicad: merge lab baseline")
    installed = dict(hist.last_merge_driver or {})
    attrs = (merge_repo / ".gitattributes")
    check(attrs.exists() and "*.kicad_pcb merge=kicad-pcb" in attrs.read_text(encoding="utf-8"),
          "project history must route *.kicad_pcb to the structural merge driver")
    check(git(merge_repo, "ls-files", ".gitattributes") == ".gitattributes",
          ".gitattributes must be COMMITTED -- it travels with the project, the config cannot")
    # Idempotent: a second call must be a no-op, not a duplicated block.
    again = _ph.ProjectHistory(merge_repo).install_merge_driver()
    check(again.get("gitattributes_added") == [] and again.get("config_keys") == [],
          "install_merge_driver must be idempotent (a second run added entries)")
    # It is public and callable without ensure(), and `_guard()` cannot protect it: that
    # returns early for an existing repository, and a software checkout IS one. Unguarded, a
    # direct call appends the four globs to the FORK'S OWN upstream-tracked .gitattributes.
    fake_src = fx["lab"] / "fakesrc"
    shutil.rmtree(fake_src, ignore_errors=True)
    (fake_src / "phicad").mkdir(parents=True)
    # `git init` matters: locate_project stops walking up at a repository boundary, so without
    # it the walk sails past this directory to the lab's own .kicad_pro and the `phicad/`
    # marker is never seen. A real software checkout is always a repo -- that IS the hazard.
    subprocess.run(["git", "init", "-q", "-b", "main", str(fake_src)],
                   capture_output=True, text=True)
    upstream_attrs = "# upstream attributes\n[attr]kicad-cpp-source text=auto\n"
    (fake_src / ".gitattributes").write_text(upstream_attrs, encoding="utf-8")
    refused = _ph.ProjectHistory(fake_src).install_merge_driver()
    check(refused.get("installed") is False and "phicad/" in str(refused.get("reason", "")),
          "install_merge_driver must REFUSE a software checkout, with the reason")
    check((fake_src / ".gitattributes").read_text(encoding="utf-8") == upstream_attrs,
          "a refused install must not touch the checkout's own .gitattributes")
    if not installed.get("installed"):
        print(f"      SKIP merge behaviour: driver not installed ({installed.get('reason')})")
        return
    # Two branches move the SAME footprint: git must still ask a human, but the board that
    # lands in the worktree has to remain loadable by KiCad.
    board = merge_repo / "b.kicad_pcb"
    original = board.read_text(encoding="utf-8")
    first_at = next((ln for ln in original.split("\n")[100:] if ln.strip().startswith("(at ")), "")
    check(bool(first_at), "fixture board has no footprint `(at ...)` line to move")

    def _move(to: str) -> None:
        board.write_text(original.replace(first_at, "\t\t(at " + to + " 78.867 180)", 1),
                         encoding="utf-8")

    git(merge_repo, "checkout", "-q", "-b", "agent-a")
    _move("117.49")
    git(merge_repo, "commit", "-qam", "agent A")
    git(merge_repo, "checkout", "-q", "main")
    git(merge_repo, "checkout", "-q", "-b", "agent-b")
    _move("121.49")
    git(merge_repo, "commit", "-qam", "agent B")
    git(merge_repo, "checkout", "-q", "agent-a")
    git(merge_repo, "merge", "agent-b")                      # expected to conflict
    merged = (merge_repo / "b.kicad_pcb").read_text(encoding="utf-8")
    check("<<<<<<<" not in merged,
          "a conflicting merge must NOT write conflict markers into the board (plain text "
          "merge did, and the board then failed to load: Got ''symbol'' ... line 136)")
    drc = s.call("pcb_drc", board_path=str(merge_repo / "b.kicad_pcb"))
    check(bool(drc.get("ok")),
          "the board left by a CONFLICTED merge must still be loadable by kicad-cli "
          f"(drc said: {str(drc.get('error'))[:80]})")

    # The check above passes VACUOUSLY if the driver never ran: a failing driver command
    # leaves %A as "ours", which has no markers and loads fine. (Found by mutation-testing
    # this very check with a bogus subcommand -- it stayed green.) So prove the driver did
    # structural work: two branches touching DIFFERENT footprints must merge with NO conflict
    # and BOTH edits present, which git's text merge and a broken driver both fail.
    git(merge_repo, "merge", "--abort")
    git(merge_repo, "checkout", "-q", "main")
    git(merge_repo, "checkout", "-q", "-b", "clean-a")
    _move("117.49")
    git(merge_repo, "commit", "-qam", "clean A")
    git(merge_repo, "checkout", "-q", "main")
    git(merge_repo, "checkout", "-q", "-b", "clean-b")
    # A DIFFERENT footprint's `(at ...)` line, far from the one agent A/B fought over, edited
    # whole-line so the file keeps its structure (an earlier version of this test sliced the
    # tail off the board instead, and "merge lost their edit" was really "their commit was
    # a 4160-line deletion").
    # Rewrite only the X coordinate, keeping the line's own token count: `(at x y)` and
    # `(at x y rot)` are different grammars, and pasting a rotation onto a two-token `(at ...)`
    # produced a board that would not parse -- which looked exactly like a driver failure.
    lines = original.split("\n")
    far = -1
    for idx in range(len(lines) - 1, -1, -1):
        stripped = lines[idx].strip()
        if stripped.startswith("(at ") and lines[idx] != first_at:
            parts = stripped[1:-1].split() if stripped.endswith(")") else []
            if len(parts) >= 3:
                parts[1] = "151.082"
                lines[idx] = "\t\t(" + " ".join(parts) + ")"
                far = idx
                break
    if far > 0:
        board.write_text("\n".join(lines), encoding="utf-8")
        git(merge_repo, "commit", "-qam", "clean B")
        git(merge_repo, "checkout", "-q", "clean-a")
        git(merge_repo, "merge", "clean-b")
        both = board.read_text(encoding="utf-8")
        check("<<<<<<<" not in both and "117.49" in both and "151.082" in both,
              "edits to DIFFERENT footprints must merge clean keeping both -- if this fails "
              "while the conflict case passes, the driver is configured but never runs")


# --------------------------------------------------------------------------------------
# cleanup — only ever touch a process whose command line carries THIS test's socket
# --------------------------------------------------------------------------------------


def sweep() -> None:
    """Kill any api-server this test left behind. Matched by socket path, never by name.

    `pkill -f "kicad-cli api-server"` would take out the api-server a colleague is using from
    their own editor or MCP client; TEST_SOCKET appears in nobody else's command line.
    """
    proc = subprocess.run(["pgrep", "-f", TEST_SOCKET], capture_output=True, text=True)
    pids = [p for p in proc.stdout.split() if p.isdigit() and int(p) != os.getpid()]
    if not pids:
        print("   no leftover api-server (socket-matched sweep found nothing)")
        return
    print(f"   killing leftover api-server pids matching {TEST_SOCKET}: {pids}")
    for pid in pids:
        subprocess.run(["kill", "-TERM", pid], capture_output=True)
    time.sleep(1)
    still = subprocess.run(["pgrep", "-f", TEST_SOCKET], capture_output=True, text=True)
    for pid in [p for p in still.stdout.split() if p.isdigit()]:
        subprocess.run(["kill", "-KILL", pid], capture_output=True)
    Path(TEST_SOCKET).unlink(missing_ok=True)


# --------------------------------------------------------------------------------------


def main() -> int:
    mode = "quick" if QUICK else ("full" if FULL else "default")
    print(f"phicad MCP tool end-to-end test  [{mode}]")
    print(f"repo   : {REPO_ROOT}")
    print(f"python : {sys.executable}")
    print(f"run    : {RUN_ROOT}  (per-run root: lab, socket, exports — kept if this run fails)")
    print(f"lab    : {LAB}")
    print(f"socket : {TEST_SOCKET}")
    # .get(), not […]: a missing knob must surface as the isolation stage's named failure, not
    # as a KeyError out of this banner.
    print(f"exports: {EXPECTED_OUT_ROOT}  "
          f"(PHICAD_MCP_OUT={os.environ.get('PHICAD_MCP_OUT')!r})")
    if not DEMO_MICROWAVE.exists() or not DEMO_PROJECT.exists():
        print(f"FAIL: demo files missing ({DEMO_MICROWAVE}, {DEMO_PROJECT})")
        return 2

    started = time.monotonic()
    try:
        server = registry.load_server_module(MCP_DIR)
    except Exception as exc:                                            # noqa: BLE001
        print(f"FAIL: server.py does not import: {type(exc).__name__}: {exc}")
        print("      (another agent may be mid-write in phicad/mcp/ — re-run, do not edit)")
        return 2
    surface = Surface(registry.collect(server))
    fx = stage()
    print(f"staged : {len(list(LAB.iterdir()))} entries under {LAB}")

    stages: list[tuple[str, Callable[[], None]]] = [
        ("isolation", lambda: test_isolation(server)),
        ("wiring", lambda: test_wiring(server, surface)),
        ("registry listing", lambda: test_registry_listing(surface)),
        ("gates", lambda: test_gates(surface, fx)),
        ("semantic diffs", lambda: test_diffs(surface, fx)),
        ("board summary", lambda: test_board_summary(surface, fx)),
        ("cli section", lambda: test_cli_section(surface, fx)),
        ("schematic", lambda: test_schematic(surface, fx)),
        ("git journal", lambda: test_project_history(surface, fx)),
        ("synthesis", lambda: test_synth(surface)),
        ("repo state", lambda: test_repo_state(surface)),
        ("regressions", lambda: test_regressions(surface, fx)),
    ]
    if not QUICK:
        stages += [
            ("swarm", lambda: test_swarm(surface, fx)),
            ("autoroute", lambda: test_autoroute(surface, fx)),
        ]
    if FULL:
        stages += [
            ("exports", lambda: test_exports(surface, fx)),
            ("jobset", lambda: test_jobset(surface, fx)),
            ("session", lambda: test_session(surface, fx)),
        ]

    for name, fn in stages:
        try:
            fn()
        except Exception as exc:                                        # noqa: BLE001
            import traceback

            FAILURES.append(f"stage {name!r} raised {type(exc).__name__}: {exc}")
            traceback.print_exc()

    section("cleanup")
    sweep()

    slowest = sorted(TIMINGS, key=lambda t: -t[1])[:5]
    print(f"\n{'=' * 78}")
    print(f"{len(TIMINGS)} tool calls in {time.monotonic() - started:.1f}s; slowest: "
          + ", ".join(f"{n} {t}s" for n, t in slowest))
    if SKIPS:
        print(f"{len(SKIPS)} skipped: " + "; ".join(SKIPS))
    if FAILURES:
        print(f"FAILED — {len(FAILURES)} of {CHECKS} checks in {len(stages)} stages [{mode}]:")
        for f in FAILURES:
            print(f"  * {f}")
        return 1
    print(f"all {CHECKS} checks passed across {len(stages)} stages [{mode}]"
          + ("" if FULL else "  (--full adds exports, the jobset and a real IPC session)"))
    return 0


def discard_run_root() -> None:
    """Remove this run's directory. Only ever called after a PASS.

    A failing run keeps everything: the fixtures, the exports and the api-server log are the
    evidence, and wiping them is precisely how a real failure came back as a pile of
    "file not found" noise on the next run.
    """
    shutil.rmtree(RUN_ROOT, ignore_errors=True)
    print(f"cleaned: {RUN_ROOT}")


if __name__ == "__main__":
    code = 2
    try:
        code = main()
    finally:
        sweep()
        if code == 0:
            discard_run_root()
        else:
            print(f"kept   : {RUN_ROOT}  (this run failed — fixtures and logs left in place)")
    raise SystemExit(code)
