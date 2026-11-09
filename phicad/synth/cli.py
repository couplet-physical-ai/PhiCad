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
"""`phicad synth` — design IR in, verified project out.

    phicad synth check  design.json            # validate only; no files written
    phicad synth build  design.json -o out/    # scaffold, netlist, import, place, verify
    phicad synth verify out/board.kicad_pcb --design design.json

Every stage ends in a gate, and a failed gate stops the pipeline rather than handing the
next stage a broken input.  The gates in order:

    validate      every pad netted or declared no-connect; every footprint resolvable
    place         no courtyard overlaps            (milliseconds)
    import        KiCad's own updater reports 0 errors
    connectivity  the board's nets == the design's nets, pad for pad
    route         every connection made, or the ones that were not, and why
    silk          every reference designator clears the pads, the silk and the edge
    fill          the ground pour is filled ON DISK, not just for the checker
    drc           kicad-cli pcb drc, the authority   (~25 s)

Everything before `drc` runs in a couple of seconds, which is the point: DRC is the
authority but it is also the slow, late feedback that made board generation an
hours-long iteration loop.  Catching a design hole before a board exists costs nothing.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import traceback
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))
_PYLIB = _HERE.parent / "pylib"
if str(_PYLIB) not in sys.path:
    sys.path.insert(0, str(_PYLIB))

import emit  # noqa: E402
import findings as findings_mod  # noqa: E402
import netlist as netlist_mod  # noqa: E402
import schematic as schematic_mod  # noqa: E402
import place as place_mod  # noqa: E402
import project as project_mod  # noqa: E402
import route as route_mod  # noqa: E402
import silk as silk_mod  # noqa: E402
from drc_opens import classify_unconnected_report  # noqa: E402
from ir import Design, IRError, Library, errors, format_problems, load  # noqa: E402


def find_kicad_cli() -> Optional[str]:
    """Locate the fork's kicad-cli — never the stable app's.

    The version boundary makes this a data-loss question, not a convenience one: this
    fork writes `version 20260831`, and a stable KiCad 10.0 binary that *can* open such a
    file would rewrite it with the 10.0 serializer and drop fork constructs.  So an
    explicit env var wins, then the build tree next to this checkout, and
    `/Applications/KiCad.app` is never consulted.
    """
    explicit = os.environ.get("PHICAD_KICAD_CLI", "").strip()
    if explicit:
        return explicit if os.access(explicit, os.X_OK) else None

    # Only the build tree of THIS checkout.  A hard-coded absolute path to some other
    # worktree is worse than no fallback: those come and go (an earlier `~/Documents/PhiCad/Main`
    # entry outlived the worktree it named), and silently resolving to a stranger's build tree
    # is how you plot a board with a binary you did not mean to use.  Set PHICAD_KICAD_CLI to
    # borrow a sibling's build deliberately.
    repo = _HERE.parent.parent
    import build_layout  # phicad/pylib, on sys.path above
    candidates = [build_layout.kicad_cli_in(build_layout.default_build_dir(repo))]
    for path in candidates:
        if path.is_file() and os.access(path, os.X_OK):
            return str(path)
    return None


def run_cli(cli: str, args: List[str], timeout: int = 300) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["KICAD_RUN_FROM_BUILD_DIR"] = "1"
    return subprocess.run([cli] + args, capture_output=True, text=True,
                          timeout=timeout, env=env)


# ---------------------------------------------------------------------------------------
# stages
# ---------------------------------------------------------------------------------------

class Stage:
    """One pipeline step's outcome, printed as it happens and collected for the summary."""

    def __init__(self, name: str):
        self.name = name
        self.ok = False
        self.detail = ""
        # Per-item diagnostics and the fix hint.  A gate's one-line summary is enough for a
        # human watching the table scroll by, but "1 UNROUTED" does not say WHICH connection
        # or what to change -- and in --json mode the explanatory lines that follow the
        # summary were only ever printed for humans, so an automatic repair loop received
        # the count and nothing else.  Carrying them in the report is what makes a failed
        # gate machine-actionable.
        self.items: List[str] = []
        self.hint = ""
        # Typed findings, when the gate's underlying tool gave us something structured.
        # `items` above is the human-readable form and stays for the printed table; these
        # carry the refs, geometry and stable identity a caller needs to act -- see
        # findings.py for why identity matters more than wording.
        self.findings: List[findings_mod.Finding] = []

    def pass_(self, detail: str = "") -> "Stage":
        self.ok, self.detail = True, detail
        return self

    def found(self, items: Sequence["findings_mod.Finding"]) -> "Stage":
        """Attach typed findings. A gate may pass and still carry warnings."""
        self.findings = findings_mod.sort_findings(list(items))
        return self

    def fail(self, detail: str, items: Optional[List[str]] = None,
             hint: str = "") -> "Stage":
        self.ok, self.detail = False, detail
        self.items = [str(item) for item in (items or [])]
        self.hint = hint
        return self

    def __str__(self) -> str:
        mark = "PASS" if self.ok else "FAIL"
        return f"{mark}  {self.name}" + (f"  —  {self.detail}" if self.detail else "")

    def as_dict(self) -> Dict[str, object]:
        out: Dict[str, object] = {"stage": self.name, "ok": self.ok, "detail": self.detail}
        if self.items:
            out["items"] = list(self.items)
        if self.hint:
            out["hint"] = self.hint
        if self.findings:
            out["findings"] = [f.as_dict() for f in self.findings]
            out["finding_counts"] = findings_mod.counts(self.findings)
        return out


def stage_validate(design: Design, library: Library, verbose: bool) -> Stage:
    stage = Stage("validate")
    problems = design.validate(library)
    hard = errors(problems)
    if verbose and problems:
        _say(format_problems(problems))
    if hard:
        return stage.fail(f"{len(hard)} errors in the design "
                          "(run `synth check` for the full list)",
                          items=[f"{p.where}: {p.message}" for p in hard],
                          hint="fix the fields named above; `synth check --json` "
                               "reports each problem with its own hint")
    warned = len(problems) - len(hard)
    return stage.pass_(f"{len(design.parts)} parts, {len(design.nets)} nets"
                       + (f", {warned} warnings" if warned else ""))


def stage_place(design: Design, library: Library) -> Tuple[Stage, Optional[place_mod.Layout]]:
    stage = Stage("place")
    layout = place_mod.solve(design, library)
    collisions = layout.overlaps(0.0)
    if collisions:
        worst = collisions[0]
        return stage.fail(
            f"{len(collisions)} courtyard overlaps; worst "
            f"{worst[0]}/{worst[1]} by {worst[2]:.3f} mm",
            items=[f"{a}/{b} overlap by {mm:.3f} mm" for a, b, mm in collisions],
            hint="increase board.part_gap, or give the crowded parts placement hints "
                 "that separate them"), layout
    assert layout.outline is not None
    return stage.pass_(f"{layout.outline.width:.1f} x {layout.outline.height:.1f} mm "
                       f"({layout.outline.area:.0f} mm^2), no courtyard overlaps"), layout


def stage_drc(cli: str, board: Path, out_json: Path) -> Stage:
    stage = Stage("drc")
    proc = run_cli(cli, ["pcb", "drc", "--format", "json", "--severity-all",
                         "--refill-zones", "-o", str(out_json), str(board)])
    if not out_json.is_file():
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-3:]
        return stage.fail("kicad-cli produced no report: " + " / ".join(tail))

    report = json.loads(out_json.read_text(encoding="utf-8"))
    violations = report.get("violations", [])
    unconnected = report.get("unconnected_items", [])
    footprint_issues = report.get("schematic_parity", [])

    def count(items, severity: str) -> int:
        return sum(1 for v in items if v.get("severity") == severity)

    n_err = count(violations, "error")
    n_warn = count(violations, "warning")
    classification = classify_unconnected_report(report, board)
    detail = (f"{n_err} errors, {n_warn} warnings, {len(unconnected)} unconnected; "
              f"{classification['real_open_count']} real opens, "
              f"{classification['zone_island_count']} zone/island artifacts"
              + (f", {classification['ambiguous_count']} ambiguous" if classification["ambiguous_count"]
                 else "")
              + (f", {len(footprint_issues)} parity" if footprint_issues else ""))
    # The typed form carries what the summary throws away: which two items short, on which
    # layer, at which coordinate, and what to change. Zone-island artifacts are excluded for
    # the same reason they are excluded from the verdict -- they are a fill artifact, not an
    # open -- but everything else keeps its geometry.
    stage.found(findings_mod.from_drc_report(report))
    if n_err or not classification["no_real_opens"]:
        return stage.fail(detail, items=[item["reason"] for item in classification["items"]
                                         if item["classification"] != "zone_island"])
    return stage.pass_(detail)


def stage_schematic(design: Design, library: Library, out_dir: Path,
                    overwrite_edits: bool = False) -> Tuple[Stage, Optional[Path]]:
    stage = Stage("schematic")
    try:
        path = schematic_mod.write(design, out_dir, library=library,
                                   overwrite_edits=overwrite_edits)
    except RuntimeError as exc:     # SchematicError, SymbolError, LibraryError
        return stage.fail(str(exc).splitlines()[0]), None
    labels = path.read_text(encoding="utf-8").count("(global_label ")
    return stage.pass_(f"{path.name}: {len(design.parts)} symbols, "
                       f"{labels} labels"), path


def stage_erc(cli: str, sch: Path, out_json: Path) -> Stage:
    """`kicad-cli sch erc` — exit code is 0 even with violations, so parse the JSON."""
    stage = Stage("erc")
    proc = run_cli(cli, ["sch", "erc", "--format", "json", "--severity-all",
                         "-o", str(out_json), str(sch)])
    if not out_json.is_file():
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-3:]
        return stage.fail("kicad-cli produced no ERC report: " + " / ".join(tail))
    report = json.loads(out_json.read_text(encoding="utf-8"))
    violations = [v for sheet in report.get("sheets", [])
                  for v in sheet.get("violations", [])]
    n_err = sum(1 for v in violations if v.get("severity") == "error")
    n_warn = sum(1 for v in violations if v.get("severity") == "warning")
    detail = f"{n_err} errors, {n_warn} warnings"
    stage.found(findings_mod.from_erc_report(report))
    if n_err:
        return stage.fail(detail)
    if n_warn:
        # Warnings are surfaced, not swallowed: a pin_to_pin warning here is usually a
        # design fact (a bidirectional pin strapped to a power rail), and the schematic
        # is the first artifact that can see it.
        notes = "; ".join(sorted({v.get("type", "?") for v in violations}))
        return stage.pass_(f"{detail} ({notes})")
    return stage.pass_(detail)


def _schematic_connectivity(cli: str, sch: Path, out_net: Path) -> Dict[str, List[str]]:
    """`net -> pins` as the generated schematic sees it, in the design's vocabulary.

    Pseudo-symbols (#FLG…) are dropped — they exist for ERC, not for the board.  And
    eeschema names a no-connect pad's net `unconnected-(U1-NC-Pad4)` — reference, pin
    NAME, pad — where synth's netlist writes `unconnected-(U1-Pad4)`; the name is
    normalised to synth's form so the comparison tests connectivity, not eeschema's
    pin-name embellishment (`sch_pin.cpp:1756`).
    """
    import re
    import sexp  # phicad/pylib, already on sys.path via library.py

    proc = run_cli(cli, ["sch", "export", "netlist", "--format", "kicadsexpr",
                         "-o", str(out_net), str(sch)])
    if not out_net.is_file():
        raise RuntimeError("kicad-cli exported no netlist: "
                           + (proc.stderr or proc.stdout or "").strip()[-200:])
    doc = sexp.parse_file(out_net)
    nets_node = sexp.child(doc, "nets")
    out: Dict[str, List[str]] = {}
    unconnected = re.compile(r"^unconnected-\(([^-]+)-(?:.*-)?Pad(.+)\)$")
    for net in sexp.children(nets_node or [], "net"):
        name = sexp.scalar(net, "name") or ""
        pins = []
        for node in sexp.children(net, "node"):
            ref = sexp.scalar(node, "ref") or ""
            pad = sexp.scalar(node, "pin") or ""
            if ref.startswith("#"):
                continue
            pins.append(f"{ref}.{pad}")
        if not pins:
            continue
        m = unconnected.match(name)
        if m:
            name = netlist_mod.unconnected_net_name(m.group(1), m.group(2))
        out[name] = sorted(pins)
    return out


def stage_sch_netlist(cli: str, design: Design, sch: Path, out_net: Path) -> Stage:
    """The cross-check that is the point: the schematic must describe the same circuit
    the board is built from, pad for pad, through KiCad's own netlist exporter."""
    stage = Stage("sch-netlist")
    try:
        actual = _schematic_connectivity(cli, sch, out_net)
    except RuntimeError as exc:
        return stage.fail(str(exc))
    diffs = netlist_mod.compare_connectivity(netlist_mod.connectivity(design), actual)
    if diffs:
        stage.fail(f"{len(diffs)} nets differ between the schematic and the design",
                   items=[str(line) for line in diffs],
                   hint="the schematic and the design IR must agree pad for pad; correct "
                        "the net members named above")
        for line in diffs:
            _say(f"    {line}")
        return stage
    return stage.pass_(f"{len(design.nets)} nets match the design pad for pad")


# ---------------------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------------------

def cmd_check(args) -> int:
    design = load(args.design)
    library = Library.default(_project_libs(args.design))
    problems = design.validate(library)
    hard = errors(problems)
    _JSON_REPORT.update({
        "ok": not hard,
        "project": design.project,
        "parts": len(design.parts),
        "nets": len(design.nets),
        "no_connect": len(design.no_connect),
        "problems": [p.as_dict() for p in problems],
    })
    if not _JSON_REPORT.get("quiet"):
        print(f"{design.project}: {len(design.parts)} parts, {len(design.nets)} nets, "
              f"{len(design.no_connect)} no-connect")
        print(format_problems(problems))
    return 1 if hard else 0


def _project_libs(design_path) -> List[Path]:
    """Project-local `.pretty` roots: `lib/` beside the design, as the parts pipeline writes."""
    base = Path(design_path).resolve().parent
    return [p for p in (base / "lib", base) if p.is_dir()]


def _init_project_history(out_dir: Path, design_path: Path) -> None:
    """Give the generated project a Git history scope, with the design IR retained.

    A standalone output gets its own repository; output below an existing checkout reuses
    that repository and is committed with project-relative pathspecs.

    `prepare_generated_project` is the shared one-shot behind New Project and the MCP
    `synth_build` tool. The CLI used not to call it, so a project built with
    `phicad synth build` had no history: Agent View reported "Git not initialized" and its
    Snapshot button had nothing to commit to, purely because of which front door was used.

    Best-effort by design -- a missing or failing history module must not fail a build whose
    gates all passed. The reason is recorded in the JSON report instead.
    """
    tool = Path(__file__).resolve().parent.parent / "pylib" / "project_history.py"
    if not tool.is_file():
        _JSON_REPORT["project_history"] = {"ok": False, "error": f"not found: {tool}"}
        return
    try:
        spec = importlib.util.spec_from_file_location("phicad_project_history", tool)
        if spec is None or spec.loader is None:
            raise ImportError(f"cannot load {tool}")
        module = importlib.util.module_from_spec(spec)
        # Register BEFORE exec_module: @dataclass resolves its own module out of
        # sys.modules while the class body runs, and a module that is not there yet fails
        # with a bare "AttributeError: 'NoneType' object has no attribute '__dict__'".
        sys.modules.setdefault("phicad_project_history", module)
        spec.loader.exec_module(module)
        _JSON_REPORT["project_history"] = module.prepare_generated_project(
            out_dir, design_path)
    except Exception as exc:                      # never fail a green build over history
        _JSON_REPORT["project_history"] = {"ok": False,
                                           "error": f"{type(exc).__name__}: {exc}"}


def cmd_build(args) -> int:
    design_path = Path(args.design).resolve()
    design = load(design_path)
    library = Library.default(_project_libs(design_path))
    out_dir = Path(args.output).resolve()

    stages: List[Stage] = []

    def finish(code_or_stages) -> int:
        """Single exit for every path, so no early return can skip history init."""
        _init_project_history(out_dir, design_path)
        return _summary(code_or_stages)

    def report(stage: Stage) -> bool:
        stages.append(stage)
        if _JSON_REPORT.get("quiet"):
            # JSON on stdout, progress on stderr.  A build takes tens of seconds and a
            # caller that only gets one object at the end has nothing to show for the
            # wait -- the AI View pane was reduced to watching for output files appearing
            # on disk and inferring the stage from them, which is guesswork dressed as
            # progress.  stderr keeps stdout a single parseable object while still giving
            # a live line per gate.
            print(stage, file=sys.stderr, flush=True)
        else:
            _say(stage, flush=True)
        return stage.ok

    if not report(stage_validate(design, library, verbose=True)):
        return finish(stages)

    stage, layout = stage_place(design, library)
    if not report(stage):
        return finish(stages)
    assert layout is not None

    # --- scaffold + netlist ------------------------------------------------------------
    scaffold_stage = Stage("scaffold")
    try:
        written = project_mod.scaffold(design, out_dir, overwrite=args.force)
    except FileExistsError as exc:
        report(scaffold_stage.fail(str(exc).splitlines()[0]))
        return finish(stages)
    net_path = out_dir / f"{design.project}.net"
    netlist_mod.write(design, net_path)
    _JSON_REPORT.update({
        "project": design.project,
        "board": str(written["board"]),
        "kicad_pro": str(written["project"]),
        "netlist": str(net_path),
        "drc_json": str(out_dir / "drc.json"),
        "board_mm": [round(layout.outline.width, 3), round(layout.outline.height, 3)],
    })
    report(scaffold_stage.pass_(f"{written['board'].name}, {written['project'].name}, "
                                f"{net_path.name}"))

    # --- schematic -----------------------------------------------------------------
    # Before the board: the .net already names <project>.kicad_sch as its source, and
    # from here on the file it names exists.
    sch_stage, sch_path = stage_schematic(design, library, out_dir,
                                          getattr(args, "overwrite_schematic", False))
    if not report(sch_stage):
        return finish(stages)
    assert sch_path is not None
    _JSON_REPORT["schematic"] = str(sch_path)

    if args.no_apply:
        _say("\n--no-apply: stopping before the board is touched.")
        return finish(stages)

    cli = find_kicad_cli()
    if cli is None:
        report(Stage("erc").fail(
            "no fork kicad-cli found. Set PHICAD_KICAD_CLI, or build it: "
            "phicad build kicad-cli"))
        return finish(stages)

    # --- schematic gates: ERC, then the netlist cross-check -------------------------
    # The cross-check is the prize: KiCad's own exporter reads the generated schematic
    # back, and the result must equal the design's connectivity pad for pad — the same
    # comparison the board passes, so sheet and board provably describe one circuit.
    if not report(stage_erc(cli, sch_path, out_dir / "erc.json")):
        return finish(stages)
    if not report(stage_sch_netlist(cli, design, sch_path,
                                    out_dir / f"{design.project}.exported.net")):
        return finish(stages)

    # --- import + place over IPC -------------------------------------------------------

    try:
        import session as session_mod
    except ImportError as exc:                                   # pragma: no cover
        report(Stage("import").fail(f"cannot import the session module: {exc}"))
        return finish(stages)

    board_path = written["board"]
    log_path = out_dir / "api-server.log"

    try:
        with session_mod.ApiServer(cli, args.socket, log=log_path):
            sess = session_mod.Session(args.socket).connect()
            sess.open_board(board_path)

            import_stage = Stage("import")
            result = sess.import_netlist(net_path)
            if not result.ok:
                report(import_stage.fail(
                    result.summary(),
                    items=[ln for ln in (result.report or "").splitlines() if ln.strip()],
                    hint="KiCad's own netlist updater rejected the netlist; the messages "
                         "above name the footprints or nets it could not apply"))
                _say(result.report)
                return finish(stages)
            noise = result.warnings()
            report(import_stage.pass_(result.summary()
                                      + (f"; {len(noise)} notes" if noise else "")))
            for line in noise:
                _say(f"    note: {line}")

            conn_stage = Stage("connectivity")
            diffs = netlist_mod.compare_connectivity(
                netlist_mod.connectivity(design), sess.connectivity())
            if diffs:
                report(conn_stage.fail(
                    f"{len(diffs)} nets differ from the design",
                    items=[str(line) for line in diffs],
                    hint="the board's connectivity must match the design IR pad for pad"))
                for line in diffs:
                    _say(f"    {line}")
                return finish(stages)
            report(conn_stage.pass_(
                f"{len(design.nets)} nets match the design pad for pad"))

            sess.save()
    except session_mod.SessionError as exc:
        report(Stage("import").fail(str(exc).splitlines()[0]))
        return finish(stages)

    # --- placement ----------------------------------------------------------------------
    # After the session closes, by rewriting each footprint's transform in the saved file.
    # Moving footprints through kipy destroys their 3D model references -- measured, every
    # `(model …)` node gone and nothing else changed -- which DRC cannot see and which shows
    # up much later as an empty 3D viewer.
    move_stage = Stage("position")
    moved = emit.set_positions(board_path, layout.positions())
    if moved != len(layout.parts):
        report(move_stage.fail(
            f"{moved}/{len(layout.parts)} footprints moved; the rest were not found on "
            "the board by reference designator"))
        return finish(stages)
    report(move_stage.pass_(f"{moved} placed"))

    # --- board furniture ---------------------------------------------------------------
    finish_stage = Stage("outline")
    emit.strip_generated(board_path)
    added = emit.splice(board_path, emit.board_items(design, layout))
    report(finish_stage.pass_(
        f"{added} items: edge cuts"
        + (", ground pour" if design.board.pour_ground
           and design.board.ground_net in design.nets else "")))

    # --- routing ------------------------------------------------------------------------
    # Before the silkscreen, because a reference designator's legal slots depend on what
    # copper is already there, and after the outline, because the router needs the board
    # edge to know where it may not go.
    if not args.no_route:
        route_stage = Stage("route")
        routed = route_mod.route(design, layout, library, grid_step=args.grid)
        emit.splice(board_path, route_mod.copper_items(routed))
        if routed.failures:
            hint = ("An unrouted connection is a placement problem more often than a "
                    "router one: give the design more room (board.part_gap), a narrower "
                    "track for that net (rules.track_widths), or a placement hint that "
                    "puts its ends nearer each other.")
            report(route_stage.fail(routed.summary(),
                                    items=[str(f) for f in routed.failures],
                                    hint=hint))
            for failure in routed.failures:
                _say(f"    {failure}")
            _say("\n    " + hint)
            return finish(stages)
        report(route_stage.pass_(routed.summary()))
        for note in routed.necked:
            _say(f"    note: {note}")

    # --- silkscreen ---------------------------------------------------------------------
    silk_stage = Stage("silk")
    assert layout.outline is not None
    labels = silk_mod.solve(design, layout, library, layout.outline)
    moved = emit.set_references(board_path, labels)
    title = silk_mod.title_label(design, layout, layout.outline, labels)
    if title is not None:
        emit.splice(board_path, [emit.title_item(title)])
    crowded = [ref for ref, label in sorted(labels.items()) if not label.clean]
    report(silk_stage.pass_(
        f"{moved} references placed"
        + (", title" if title is not None else ", no room for the title")
        + (f", {len(crowded)} crowded" if crowded else "")))
    for ref in crowded:
        label = labels[ref]
        _say(f"    note: {ref} has no clear slot; best is {label.slot} at cost "
              f"{label.cost:.0f} — expect a silkscreen warning")

    # --- fill the pour ------------------------------------------------------------------
    # Last, because a pour has to be poured around the copper that is already there, and
    # every earlier stage adds some.  A separate stage rather than a side effect of the
    # checker: `pcb drc --refill-zones` fills in memory for the length of the check, so a
    # board whose pour is empty on disk still passes with 0 unconnected, and is then handed
    # over with the ground stitching vias connected to nothing and a B.Cu gerber carrying no
    # copper at all.  That is the worst failure this pipeline can produce — every gate green
    # and the board unfabricable — so the fill reports its own evidence.
    if design.board.pour_ground and design.board.ground_net in design.nets:
        fill_stage = Stage("fill")
        try:
            with session_mod.ApiServer(cli, args.socket, log=log_path) as _srv:
                filler = session_mod.Session(args.socket).connect()
                filler.open_board(board_path)
                zones = filler.refill_zones()
                filler.save()
        except session_mod.SessionError as exc:
            report(fill_stage.fail(str(exc).splitlines()[0]))
            return finish(stages)

        poured = board_path.read_text(encoding="utf-8").count("filled_polygon")
        if not poured:
            report(fill_stage.fail(
                f"{zones} zone(s) on the board and none of them filled — the pour would "
                "be absent from the gerbers"))
            return finish(stages)
        report(fill_stage.pass_(f"{zones} zone(s), {poured} filled region(s)"))

    if args.no_drc:
        _say("\n--no-drc: skipping the authoritative gate.")
        return finish(stages)

    report(stage_drc(cli, board_path, out_dir / "drc.json"))
    return finish(stages)


def cmd_verify(args) -> int:
    """Run the gates against a board that already exists."""
    cli = find_kicad_cli()
    if cli is None:
        print("no fork kicad-cli found; set PHICAD_KICAD_CLI", file=sys.stderr)
        return 2
    board = Path(args.board).resolve()
    stages = [stage_drc(cli, board, board.with_suffix(".drc.json"))]
    for stage in stages:
        _say(stage)
    return _summary(stages)


# Filled by _summary() so the JSON report is produced on every exit path, including the
# early returns a failed gate takes.  A caller that asked for JSON and got prose because
# the pipeline stopped early would have to parse the prose, which is the thing --json
# exists to avoid.
_JSON_REPORT: Dict[str, object] = {}


def _say(*args, **kwargs) -> None:
    """print(), unless the caller asked for JSON.

    A JSON mode that still prints the human table puts prose in front of the object and
    every caller has to strip it back off — which is the parsing problem --json exists to
    remove.
    """
    if not _JSON_REPORT.get("quiet"):
        print(*args, **kwargs)


def _summary(stages: List[Stage]) -> int:
    failed = [s for s in stages if not s.ok]
    _JSON_REPORT.update({
        "ok": not failed,
        "stages": [s.as_dict() for s in stages],
        "failed": [s.name for s in failed],
    })
    if _JSON_REPORT.get("quiet"):
        return 1 if failed else 0
    print("\n" + "-" * 64)
    for stage in stages:
        print(stage)
    print("-" * 64)
    print(f"{len(stages) - len(failed)}/{len(stages)} stages passed")
    return 1 if failed else 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="phicad synth", description=__doc__.splitlines()[0])
    subs = parser.add_subparsers(dest="command", required=True)

    check = subs.add_parser("check", help="validate a design without writing anything")
    check.add_argument("design")
    check.add_argument("--json", action="store_true",
                       help="print the problems as JSON")
    check.set_defaults(func=cmd_check)

    build = subs.add_parser("build", help="design IR -> verified project")
    build.add_argument("design")
    build.add_argument("--output", "-o", required=True, help="project directory to write")
    build.add_argument("--overwrite-schematic", action="store_true",
                       help="discard hand edits to <project>.kicad_sch and regenerate it "
                            "(refused by default once the file has been edited)")
    build.add_argument("--force", action="store_true",
                       help="overwrite an existing project in the output directory")
    build.add_argument("--no-apply", action="store_true",
                       help="stop after writing the netlist; do not touch a board")
    build.add_argument("--no-drc", action="store_true",
                       help="skip the DRC gate (fast inner loop)")
    build.add_argument("--no-route", action="store_true",
                       help="place but do not route; leaves the ratsnest unconnected")
    build.add_argument("--grid", type=float, default=route_mod.DEFAULT_GRID,
                       help="router grid pitch in mm (default: %(default)s)")
    build.add_argument("--json", action="store_true",
                       help="print one JSON report instead of the stage table")
    build.add_argument("--socket", default=None,
                       help="IPC socket path (default: /tmp/phicad-synth.sock)")
    build.set_defaults(func=cmd_build)

    verify = subs.add_parser("verify", help="run the gates against an existing board")
    verify.add_argument("board")
    verify.add_argument("--json", action="store_true",
                        help="print one JSON report instead of the stage table")
    verify.set_defaults(func=cmd_verify)

    args = parser.parse_args(argv)
    wants_json = bool(getattr(args, "json", False))
    _JSON_REPORT["quiet"] = wants_json
    try:
        code = args.func(args)
    except IRError as exc:
        if wants_json:
            print(json.dumps({"ok": False, "error": str(exc)}, indent=2))
            return 2
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130
    except Exception as exc:                                  # noqa: BLE001
        # An unexpected failure (a wedged IPC session, a missing kiface) must not leave
        # `--json` callers with empty stdout: an agent or GUI parsing that sees malformed
        # output rather than a diagnosis, which is the worst possible failure to debug.
        # The traceback still goes to stderr for a human.
        if wants_json:
            _JSON_REPORT.pop("quiet", None)
            report = dict(_JSON_REPORT)
            report["ok"] = False
            report["error"] = f"{type(exc).__name__}: {exc}"
            print(json.dumps(report, indent=2))
            traceback.print_exc()
            return 2
        raise
    if wants_json:
        _JSON_REPORT.pop("quiet", None)
        print(json.dumps(_JSON_REPORT, indent=2))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
