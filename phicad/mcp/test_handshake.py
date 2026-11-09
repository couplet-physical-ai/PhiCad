#!/usr/bin/env python3
"""Real end-to-end smoke test for the phicad MCP server (stdio transport).

Spawns `server.py` as an MCP subprocess over stdio, does initialize + tools/list, then calls
real tools: integration_capabilities, phicad_status, board_summary, pcb_drc, pcb_diff
(exit-5 path), pcb_render.
Boards are COPIES under /tmp — the fork upgrades files it saves (version 20260831) and
stable KiCad 10.0 then refuses them, so never point this at the repo's demos in place.

Run:  build/mcp-venv/bin/python phicad/mcp/test_handshake.py [--full]
      --full also exercises render/svg/step (slower).
      --sections lists the checks this file runs, and which flag turns each one on.

This file proves the SERVER and its transport. The tools themselves — every section, with the
parsed numbers asserted — are covered by test_tools.py; the registry's shape by test_registry.py.

ISOLATION (audit finding P0-5)
    Every path this run writes — the board copies, the staged projects, the api-server socket,
    and the server child's export root (PHICAD_MCP_OUT) — lives under one per-run directory
    created by mkdtemp.  Nothing is a fixed $TMPDIR name any more, and the --full IPC stage
    passes an explicit socket instead of taking server.py's shared DEFAULT_SOCKET, so two runs
    of this file at once (two CI jobs, two worktrees, `&` in one shell) cannot wipe each other's
    fixtures or fight over one socket.  The run directory is removed on success and KEPT on
    failure, because that is the evidence.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

MCP_DIR = Path(__file__).resolve().parent
REPO_ROOT = MCP_DIR.parent.parent
SERVER = MCP_DIR / "server.py"
DEMO_PCB = REPO_ROOT / "demos" / "microwave" / "microwave.kicad_pcb"
DEMO_PROJECT = REPO_ROOT / "demos" / "pic_programmer"     # has a schematic + project + board
JOBSET = MCP_DIR / "examples" / "fab_pack.kicad_jobset"
VALID_DRC_PROJECT = REPO_ROOT / "qa" / "data" / "pcbnew" / "line_ending_drc"


def new_run_root() -> Path:
    """Create a directory no other run of this file can be using, and return it.

    mkdtemp is what makes it unique (created O_EXCL, so two runs started in the same
    millisecond still differ); the pid is in the name so a human can match a leftover
    directory to a process.  `PHICAD_MCP_TEST_ROOT` parks all run roots somewhere inspectable
    without giving up the per-run suffix.
    """
    base = Path(os.environ.get("PHICAD_MCP_TEST_ROOT") or tempfile.gettempdir())
    base.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix=f"phicad-mcp-handshake-{os.getpid()}-", dir=str(base)))


#: The one directory this run owns; removed on success, kept on failure.
RUN_ROOT = new_run_root()
#: Staged boards and projects. Inside RUN_ROOT, so a concurrent run cannot see or wipe them.
LAB = RUN_ROOT / "lab"
#: The api-server socket THIS run owns. Passed explicitly to api_server_start(): the default
#: ($TMPDIR/phicad-mcp-api.sock) is shared, and two runs on it produce a bind failure that
#: looks like a broken IPC layer. The cleanup sweep matches on this string alone.
TEST_SOCKET = str(RUN_ROOT / "api.sock")

# server.py computes OUT_ROOT = $PHICAD_MCP_OUT/phicad-mcp-out at IMPORT time, and the server
# here is a CHILD PROCESS spawned further down. Set the knob at module scope so it is already
# in os.environ when child_env is built for PythonStdioTransport; set later and every export
# from this run lands in the shared $TMPDIR/phicad-mcp-out that concurrent runs overwrite.
os.environ["PHICAD_MCP_OUT"] = str(RUN_ROOT)
#: What the child's server.OUT_ROOT must therefore be — verified against phicad_status below.
EXPECTED_OUT_ROOT = RUN_ROOT / "phicad-mcp-out"
#: The fixed paths this fix removed; the isolation check refuses to let them come back.
LEGACY_LAB = Path(tempfile.gettempdir()) / "phicad-mcp-lab"
LEGACY_OUT_ROOT = Path(tempfile.gettempdir()) / "phicad-mcp-out"
LEGACY_SOCKET = Path(tempfile.gettempdir()) / "phicad-mcp-api.sock"

FULL = "--full" in sys.argv
AUTOROUTE = "--autoroute" in sys.argv     # runs Freerouting (needs java + jar, ~2 min)
SECTIONS = "--sections" in sys.argv

#: (name, flag that enables it, what it calls) — printed by --sections and the single place
#: this file's coverage is described, so adding a check means adding a row here.
CHECK_SECTIONS: list[tuple[str, str, str]] = [
    ("isolation", "always", "per-run lab/socket/PHICAD_MCP_OUT containment (audit P0-5)"),
    ("handshake", "always", "initialize + tools/list over stdio"),
    ("status", "always", "phicad_status"),
    ("board-summary", "always", "board_summary"),
    ("gates", "always", "pcb_drc"),
    ("semantic-diffs", "always", "pcb_diff (differs, exit 5) + pcb_diff (identical)"),
    ("schematic-read", "always", "sch_list_symbols, sch_list_sheets"),
    ("git", "always", "project_init, project_status (throwaway /tmp project)"),
    ("ipc-idle", "always", "api_server_status, kipy_exec refusal without a server"),
    ("exports", "--full", "render, svg, gerbers, stats, pos, dsn, pdf, bom, netlist, step"),
    ("erc", "--full", "sch_erc on the demo hierarchy"),
    ("schematic-write", "--full", "sch_set_field, sch_add_label (new file, load-checked)"),
    ("git-snapshot", "--full", "project_snapshot, project_log after a real edit"),
    ("synth", "--full", "synth_verify against an existing board"),
    ("jobset", "--full", "jobset_run accepts a known-valid board with the starter fab pack"),
    ("session", "--full", "api-server lifecycle, kipy_exec, session edits, stop"),
    ("autoroute", "--autoroute", "autoroute_freerouting, 3 passes (needs java + jar)"),
]


def print_sections() -> int:
    """`--sections`: what this file checks and how to turn each part on."""
    print("test_handshake.py checks (real MCP stdio calls against this build tree)\n")
    print(f"  {'section':18} {'flag':12} calls")
    for name, flag, calls in CHECK_SECTIONS:
        print(f"  {name:18} {flag:12} {calls}")
    print("\nflags : --full (everything but Freerouting), --autoroute (adds a real route),"
          "\n        --sections (this list)")
    print("related: test_tools.py    in-process, asserts the PARSED numbers per section"
          "\n         test_registry.py registry + phicad-tool CLI contract")
    return 0


def hr(title: str) -> None:
    print(f"\n{'=' * 78}\n== {title}\n{'=' * 78}")


def check_isolation() -> list[str]:
    """Gate for audit finding P0-5: prove nothing this run writes is shared with another run.

    Returns the list of failures (empty when isolated) so main() can fold it into its own.
    The bug was SHARING, not a wrong number: fixed lab paths plus server.py's default socket and
    default export root meant a concurrent run wiped this run's fixtures and bound its socket.
    So this checks containment, uniqueness, and that none of the legacy shared paths are in use.
    """
    hr("isolation — per-run lab, socket and export root (audit P0-5)")
    bad: list[str] = []

    def want(condition: object, message: str) -> None:
        print(f"   {'ok  ' if condition else 'FAIL'} {message}")
        if not condition:
            bad.append(f"isolation({message})")

    print(f"   run root   {RUN_ROOT}")
    print(f"   lab        {LAB}")
    print(f"   socket     {TEST_SOCKET}")
    # .get(), not […]: if the knob is ever missing this must be a NAMED gate failure below,
    # not a KeyError out of a diagnostic print.
    print(f"   exports    {EXPECTED_OUT_ROOT}  "
          f"(PHICAD_MCP_OUT={os.environ.get('PHICAD_MCP_OUT')!r})")

    want(RUN_ROOT.is_dir(), "the per-run root exists")
    want(str(os.getpid()) in RUN_ROOT.name, "the per-run root carries this pid")
    want(LAB.is_relative_to(RUN_ROOT), "the lab is inside the per-run root")
    want(Path(TEST_SOCKET).parent.is_relative_to(RUN_ROOT),
         "the api-server socket is inside the per-run root")
    want(EXPECTED_OUT_ROOT.is_relative_to(RUN_ROOT),
         "the server's export root is inside the per-run root")
    want(os.environ.get("PHICAD_MCP_OUT") == str(RUN_ROOT),
         "PHICAD_MCP_OUT points at the per-run root (set before the server child is spawned)")
    want(LAB != LEGACY_LAB, f"the lab is not the old shared path {LEGACY_LAB}")
    want(Path(TEST_SOCKET) != LEGACY_SOCKET,
         f"the socket is not server.py's shared DEFAULT_SOCKET {LEGACY_SOCKET}")
    want(EXPECTED_OUT_ROOT != LEGACY_OUT_ROOT,
         f"the export root is not the shared default {LEGACY_OUT_ROOT}")

    # Uniqueness demonstrated, not inferred from the name: ask the same allocator again and show
    # it hands back a different directory. That is exactly what makes two simultaneous runs safe.
    peer = new_run_root()
    try:
        want(peer != RUN_ROOT, "new_run_root() never hands the same directory to two runs")
        want(not LAB.is_relative_to(peer), "this run's lab is not inside a peer run's root")
    finally:
        shutil.rmtree(peer, ignore_errors=True)
    return bad


def sweep() -> None:
    """Kill any api-server THIS run left behind, matched by its socket path and nothing else.

    `pkill -f "kicad-cli api-server"` would take out the api-server a colleague is using from
    their own editor, or the one a concurrent run of this same file is talking to. TEST_SOCKET
    is unique to this run, so it appears in no other process's command line.
    """
    proc = subprocess.run(["pgrep", "-f", TEST_SOCKET], capture_output=True, text=True)
    pids = [p for p in proc.stdout.split() if p.isdigit() and int(p) != os.getpid()]
    if not pids:
        return
    print(f"   killing leftover api-server pids matching {TEST_SOCKET}: {pids}")
    for pid in pids:
        subprocess.run(["kill", "-TERM", pid], capture_output=True)
    time.sleep(1)
    still = subprocess.run(["pgrep", "-f", TEST_SOCKET], capture_output=True, text=True)
    for pid in [p for p in still.stdout.split() if p.isdigit()]:
        subprocess.run(["kill", "-KILL", pid], capture_output=True)
    Path(TEST_SOCKET).unlink(missing_ok=True)


def show(name: str, result: dict, keys: list[str]) -> None:
    print(f"\n-- {name}")
    for k in keys:
        if k in result:
            v = result[k]
            s = json.dumps(v, default=str) if not isinstance(v, str) else v
            print(f"   {k:22} {s[:600]}")
    for k in ("error", "hint", "stderr"):
        if result.get(k):
            print(f"   {k:22} {str(result[k])[:400]}")


def stage_boards() -> tuple[Path, Path]:
    """Copy the demo board to /tmp twice; move one footprint in the second so pcb_diff differs."""
    LAB.mkdir(parents=True, exist_ok=True)
    a = LAB / "handshake_a.kicad_pcb"
    b = LAB / "handshake_b.kicad_pcb"
    shutil.copy2(DEMO_PCB, a)
    text = a.read_text()
    # shift the first footprint's own (at x y) by 5 mm -> a real geometric change
    fp = text.index("(footprint ")
    m = re.compile(r"\(at (-?[\d.]+) (-?[\d.]+)([^)]*)\)").search(text, fp)
    if not m:
        raise SystemExit("could not find a footprint position token in the demo board")
    moved = f"(at {float(m.group(1)) + 5:.5f} {float(m.group(2)) + 5:.5f}{m.group(3)})"
    b.write_text(text[: m.start()] + moved + text[m.end():])
    print(f"perturbation: {m.group(0)}  ->  {moved}")
    return a, b


def stage_project() -> Path:
    """Copy a full demo project (sch + pcb + pro) to /tmp."""
    dest = LAB / "handshake_project"
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(DEMO_PROJECT, dest)
    return dest


def stage_valid_jobset_project() -> tuple[Path, Path]:
    """Stage the upstream DRC-pass fixture with the fork's starter fabrication jobset."""
    dest = LAB / "handshake_jobset_valid"
    dest.mkdir()
    stem = "line_ending_drc_pass"
    for suffix in (".kicad_pcb", ".kicad_pro", ".kicad_dru"):
        shutil.copy2(VALID_DRC_PROJECT / f"{stem}{suffix}", dest / f"{stem}{suffix}")
    shutil.copy2(JOBSET, dest / JOBSET.name)
    return dest / JOBSET.name, dest / f"{stem}.kicad_pro"


def stage_git_project() -> Path:
    """A project copy dedicated to the git tools, so no other check perturbs its history."""
    dest = LAB / "handshake_git_project"
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(DEMO_PROJECT, dest)
    return dest


async def main() -> int:
    from fastmcp import Client

    if SECTIONS:
        return print_sections()
    if not DEMO_PCB.exists():
        print(f"FAIL: demo board missing: {DEMO_PCB}")
        return 2
    isolation_failures = check_isolation()
    board_a, board_b = stage_boards()
    print(f"server : {SERVER}")
    print(f"board A: {board_a}")
    print(f"board B: {board_b} (perturbed copy)")

    failures: list[str] = list(isolation_failures)

    def unwrap(res) -> dict:
        data = getattr(res, "data", None)
        if isinstance(data, dict):
            return data
        try:
            return json.loads(res.content[0].text)
        except Exception:
            return {"ok": False, "error": "unparsable result", "raw": str(res)[:500]}

    # The MCP stdio client sanitises the child's environment down to HOME/PATH/USER/..., so
    # the PHICAD_* overrides that point the server at another worktree's build tree would
    # never reach it. Forward them explicitly, the same way a client config's `env` block does.
    # PHICAD_MCP_OUT rides along on the PHICAD_ prefix, which is what puts the child server's
    # OUT_ROOT inside this run's directory instead of the shared $TMPDIR/phicad-mcp-out.
    from fastmcp.client.transports import PythonStdioTransport

    child_env = {k: v for k, v in os.environ.items()
                 if k.startswith("PHICAD_") or k in ("HOME", "PATH", "USER", "LOGNAME",
                                                      "SHELL", "TERM", "TMPDIR")}
    transport = PythonStdioTransport(str(SERVER), env=child_env, python_cmd=sys.executable)

    async with Client(transport) as client:
        hr("initialize + tools/list")
        tools = await client.list_tools()
        print(f"connected, {len(tools)} tools:")
        for t in tools:
            print(f"  - {t.name:22} {(t.description or '').splitlines()[0][:88]}")

        hr("integration_capabilities()")
        caps = unwrap(await client.call_tool("integration_capabilities"))
        show("integration_capabilities", caps,
             ["ok", "protocol_version", "command_plane", "workflow",
              "model_adapter", "schemas", "missing"])
        if not caps.get("ok"):
            failures.append("integration_capabilities")

        hr("phicad_status()")
        st = unwrap(await client.call_tool("phicad_status"))
        show("phicad_status", st, ["ok", "repo_root", "kicad_cli_built", "status",
                                  "automation_venv", "mcp_out_root"])
        if not st.get("ok"):
            failures.append("phicad_status")
        # The isolation gate, measured in the CHILD: phicad_status reports the server's own
        # OUT_ROOT, so this proves PHICAD_MCP_OUT reached the subprocess before it computed the
        # path at import time — not merely that this process exported it.
        child_out = st.get("mcp_out_root")
        print(f"   child OUT_ROOT         {child_out}")
        print(f"   expected               {EXPECTED_OUT_ROOT}")
        if Path(child_out or "/nonexistent").resolve() != EXPECTED_OUT_ROOT.resolve():
            failures.append(f"isolation(child server OUT_ROOT is {child_out}, not "
                            f"{EXPECTED_OUT_ROOT} — PHICAD_MCP_OUT did not reach the child)")

        hr("board_summary(board A)")
        bs = unwrap(await client.call_tool("board_summary", {"board_path": str(board_a)}))
        show("board_summary", bs, ["ok", "summary", "copper_layers", "footprint_count",
                                  "footprints", "details"])
        if not bs.get("ok"):
            failures.append("board_summary")

        hr("pcb_drc(board A)")
        drc = unwrap(await client.call_tool("pcb_drc", {"board_path": str(board_a)}))
        show("pcb_drc", drc, ["ok", "exit_code", "report_path", "elapsed_s"])
        summary = drc.get("summary") or {}
        print(f"   total_violations       {summary.get('total_violations')}")
        print(f"   by_severity            {json.dumps(summary.get('by_severity'))}")
        v = summary.get("violations") or {}
        print(f"   violation types        {json.dumps(v.get('by_type'))}")
        for ex in (v.get("examples") or [])[:3]:
            print(f"     * [{ex['severity']}] {ex['type']}: {str(ex['description'])[:90]}")
        if not drc.get("ok"):
            failures.append("pcb_drc")

        hr("pcb_diff(A, B)  — exit 5 must be a normal 'differs' result")
        d = unwrap(await client.call_tool("pcb_diff", {"a": str(board_a), "b": str(board_b)}))
        show("pcb_diff", d, ["ok", "exit_code", "differs", "identical", "doc_type",
                             "change_count", "changes_by_kind", "changes_by_type",
                             "changes", "report_path"])
        if not (d.get("ok") and d.get("differs")):
            failures.append("pcb_diff(differs)")

        hr("pcb_diff(A, A) — identical")
        same = unwrap(await client.call_tool("pcb_diff", {"a": str(board_a), "b": str(board_a)}))
        show("pcb_diff same", same, ["ok", "exit_code", "differs", "identical", "change_count"])
        if not (same.get("ok") and same.get("identical")):
            failures.append("pcb_diff(identical)")

        # --- tools_schematic: two READ tools over the transport (their write side is under
        # --- --full below, because it needs a project copy nobody else is diffing).
        hr("sch_list_symbols(demo schematic) — the text-level schematic surface")
        demo_sch = stage_project() / "pic_programmer.kicad_sch"
        syms = unwrap(await client.call_tool("sch_list_symbols", {"sch_path": str(demo_sch)}))
        show("sch_list_symbols", syms, ["ok", "symbol_count", "power_symbol_count",
                                       "unique_refs", "scope"])
        # 105 symbols serialise to ~34 kB of JSON: this also proves the result survives the
        # worker -> server -> stdio path intact (a truncating hop showed up here first).
        if not (syms.get("ok") and len(syms.get("symbols") or []) >= 100):
            failures.append("sch_list_symbols")

        hr("sch_list_sheets(demo schematic) — hierarchy walk")
        sheets = unwrap(await client.call_tool("sch_list_sheets", {"sch_path": str(demo_sch)}))
        show("sch_list_sheets", sheets, ["ok", "file_count", "is_flat",
                                         "missing_sheet_files"])
        if not (sheets.get("ok") and sheets.get("file_count") == 2
                and sheets.get("is_flat") is False):
            failures.append("sch_list_sheets")

        # --- tools_git: a project directory gets a safe Git history scope.
        hr("project_init / project_status — the per-project git journal")
        gitproj = stage_git_project()
        init = unwrap(await client.call_tool("project_init", {"path": str(gitproj)}))
        show("project_init", init, ["ok", "created", "branch", "head", "commits", "journal"])
        if not (init.get("ok") and (gitproj / ".git").is_dir() and init.get("commits") == 1):
            failures.append("project_init")
        gstat = unwrap(await client.call_tool("project_status", {"path": str(gitproj)}))
        show("project_status", gstat, ["ok", "branch", "head", "commits", "clean",
                                       "journal_entries", "display"])
        if not (gstat.get("ok") and gstat.get("branch") and gstat.get("commits") == 1):
            failures.append("project_status")

        hr("api_server_status() — nothing started yet")
        show("api_server_status", unwrap(await client.call_tool("api_server_status")),
             ["ok", "running", "pid", "socket"])

        hr("kipy_exec() without a server — must refuse cleanly")
        ke = unwrap(await client.call_tool("kipy_exec", {"script_path": str(SERVER)}))
        show("kipy_exec", ke, ["ok", "error"])

        if FULL:
            hr("pcb_render(board A)")
            r = unwrap(await client.call_tool("pcb_render", {"board_path": str(board_a)}))
            show("pcb_render", r, ["ok", "png_path", "png_bytes", "elapsed_s"])
            if not r.get("ok"):
                failures.append("pcb_render")

            hr("pcb_export_svg(board A)")
            s = unwrap(await client.call_tool("pcb_export_svg", {"board_path": str(board_a)}))
            show("pcb_export_svg", s, ["ok", "out_dir", "svg_files", "elapsed_s"])
            if not s.get("ok"):
                failures.append("pcb_export_svg")

            hr("pcb_export_gerbers(board A)")
            g = unwrap(await client.call_tool("pcb_export_gerbers", {"board_path": str(board_a)}))
            show("pcb_export_gerbers", g, ["ok", "out_dir", "file_count", "files", "elapsed_s"])
            if not g.get("ok"):
                failures.append("pcb_export_gerbers")

            hr("pcb_export_stats(board A)")
            stt = unwrap(await client.call_tool("pcb_export_stats", {"board_path": str(board_a)}))
            show("pcb_export_stats", stt, ["ok", "report_path"])
            stats = stt.get("stats") or {}
            print(f"   stats keys             {sorted(stats)[:12]}")
            if not stt.get("ok"):
                failures.append("pcb_export_stats")

            hr("pcb_export_pos(board A)")
            pos = unwrap(await client.call_tool("pcb_export_pos", {"board_path": str(board_a)}))
            show("pcb_export_pos", pos, ["ok", "pos_path", "row_count", "preview"])
            if not pos.get("ok"):
                failures.append("pcb_export_pos")

            hr("pcb_export_dsn(board A)")
            dsn = unwrap(await client.call_tool("pcb_export_dsn", {"board_path": str(board_a)}))
            show("pcb_export_dsn", dsn, ["ok", "dsn_path", "dsn_bytes", "nets", "placements"])
            if not dsn.get("ok"):
                failures.append("pcb_export_dsn")

            hr("autoroute_cleanup(board A, report only)")
            cl = unwrap(await client.call_tool("autoroute_cleanup", {"board_path": str(board_a)}))
            show("autoroute_cleanup", cl, ["ok", "apply", "report"])
            if not cl.get("ok"):
                failures.append("autoroute_cleanup")

            if AUTOROUTE:
                hr("autoroute_freerouting(board A, 3 passes)")
                ar = unwrap(await client.call_tool(
                    "autoroute_freerouting",
                    {"board_path": str(board_a), "passes": 3, "timeout_s": 900},
                ))
                show("autoroute_freerouting", ar, ["ok", "out_board", "unconnected_before",
                                                   "unconnected_after", "violations_before",
                                                   "violations_after", "elapsed_s", "error"])
                if not ar.get("ok"):
                    failures.append("autoroute_freerouting")

            hr("pcb_export_pdf(board A)")
            ppdf = unwrap(await client.call_tool("pcb_export_pdf", {"board_path": str(board_a)}))
            show("pcb_export_pdf", ppdf, ["ok", "pdf_path", "pdf_bytes", "elapsed_s"])
            if not ppdf.get("ok"):
                failures.append("pcb_export_pdf")

            proj = stage_project()
            sch = proj / "pic_programmer.kicad_sch"
            pcb = proj / "pic_programmer.kicad_pcb"
            print(f"\nstaged project: {proj}")

            hr("sch_erc(demo schematic)")
            e = unwrap(await client.call_tool("sch_erc", {"sch_path": str(sch)}))
            show("sch_erc", e, ["ok", "exit_code", "report_path", "sheet_count"])
            esum = e.get("summary") or {}
            print(f"   total_violations       {esum.get('total_violations')}")
            print(f"   by_severity            {json.dumps(esum.get('by_severity'))}")
            print(f"   types                  "
                  f"{json.dumps((esum.get('violations') or {}).get('by_type'))}")
            if not e.get("ok"):
                failures.append("sch_erc")

            hr("sch_export_bom(demo schematic)")
            bom = unwrap(await client.call_tool("sch_export_bom", {"sch_path": str(sch)}))
            show("sch_export_bom", bom, ["ok", "csv_path", "row_count", "header", "preview"])
            if not bom.get("ok"):
                failures.append("sch_export_bom")

            hr("sch_export_netlist(demo schematic)")
            net = unwrap(await client.call_tool("sch_export_netlist", {"sch_path": str(sch)}))
            show("sch_export_netlist", net, ["ok", "netlist_path", "netlist_bytes",
                                            "component_count", "net_count"])
            if not net.get("ok"):
                failures.append("sch_export_netlist")

            hr("sch_export_pdf(demo schematic)")
            spdf = unwrap(await client.call_tool("sch_export_pdf", {"sch_path": str(sch)}))
            show("sch_export_pdf", spdf, ["ok", "pdf_path", "pdf_bytes", "elapsed_s"])
            if not spdf.get("ok"):
                failures.append("sch_export_pdf")

            hr("synth_verify(demo board)")
            sv = unwrap(await client.call_tool("synth_verify", {"board_path": str(pcb)}))
            show("synth_verify", sv, ["ok", "exit_code"])
            rep = sv.get("report") or {}
            print(f"   stages                 {[s.get('stage') for s in rep.get('stages', [])]}")
            print(f"   failed                 {rep.get('failed')}")
            # A demo board may legitimately fail the DRC gate; the tool is healthy when it
            # produced a parsed report with stages, whatever the verdict.
            if not rep.get("stages"):
                failures.append("synth_verify")

            hr("sch_set_field + sch_add_label(demo schematic) — the schematic WRITE surface")
            edited = LAB / "handshake_edited.kicad_sch"
            setf = unwrap(await client.call_tool(
                "sch_set_field",
                {"sch_path": str(sch), "ref": "R1", "field": "MPN",
                 "value": "HANDSHAKE-MPN", "out_path": str(edited)},
            ))
            show("sch_set_field", setf, ["ok", "in_place", "change_count", "loads",
                                         "bytes_before", "bytes_after", "changes"])
            if not (setf.get("ok") and setf.get("loads") and edited.exists()
                    and "HANDSHAKE-MPN" in edited.read_text()):
                failures.append("sch_set_field")
            labelled = LAB / "handshake_labelled.kicad_sch"
            lab = unwrap(await client.call_tool(
                "sch_add_label",
                {"sch_path": str(sch), "text": "HANDSHAKE_NET", "x_mm": 50.0, "y_mm": 50.0,
                 "kind": "local", "out_path": str(labelled)},
            ))
            show("sch_add_label", lab, ["ok", "in_place", "loads", "added", "kind",
                                        "bytes_after"])
            if not (lab.get("ok") and lab.get("loads") and labelled.exists()):
                failures.append("sch_add_label")

            hr("project_snapshot / project_log — a journaled revision of a real edit")
            # Write a schematic variant INTO the project directory, so the git tools have a
            # real, fork-readable file to version rather than a hand-forged byte change.
            variant = unwrap(await client.call_tool(
                "sch_add_label",
                {"sch_path": str(gitproj / "pic_programmer.kicad_sch"),
                 "text": "SNAPSHOT_NET", "x_mm": 60.0, "y_mm": 60.0,
                 "out_path": str(gitproj / "variant.kicad_sch")},
            ))
            show("sch_add_label (into the project)", variant, ["ok", "loads", "out_path"])
            print(f"   journal hook           {json.dumps(variant.get('project_history'))[:300]}")
            if not variant.get("ok"):
                failures.append("sch_add_label(project variant)")
            # That write was itself journaled and auto-snapshotted by server.py's hook, so the
            # tree is already clean here. project_snapshot needs its own pending change: a
            # plain note file, which is also the "designer left a comment" case.
            (gitproj / "NOTES.md").write_text("handshake: staged a manual note\n")
            snap = unwrap(await client.call_tool(
                "project_snapshot",
                {"path": str(gitproj), "message": "handshake: touched the schematic",
                 "actor": "test_handshake"},
            ))
            show("project_snapshot", snap, ["ok", "committed", "commit", "short",
                                           "changed_files", "insertions", "reason"])
            if not (snap.get("ok") and snap.get("committed")):
                failures.append("project_snapshot")
            glog = unwrap(await client.call_tool("project_log", {"path": str(gitproj),
                                                                "limit": 5}))
            show("project_log", glog, ["ok", "count"])
            for row in (glog.get("commits") or [])[:3]:
                print(f"     * {row.get('short')} {row.get('subject')} "
                      f"[{row.get('actor') or '-'}]")
            if not (glog.get("ok") and (glog.get("count") or 0) >= 2):
                failures.append("project_log")

            hr("pcb_export_step(demo board)")
            stp = unwrap(await client.call_tool("pcb_export_step", {"board_path": str(pcb)}))
            show("pcb_export_step", stp, ["ok", "exit_code", "step_path", "step_bytes",
                                          "missing_model_count"])
            if not stp.get("ok"):
                failures.append("pcb_export_step")

            hr("jobset_run(examples/fab_pack.kicad_jobset, known-valid board)")
            valid_jobset, valid_pro = stage_valid_jobset_project()
            js = unwrap(await client.call_tool(
                "jobset_run",
                {"jobset_path": str(valid_jobset), "project_path": str(valid_pro)},
            ))
            show("jobset_run", js, ["ok", "exit_code", "jobs", "output_paths",
                                    "jobs_succeeded", "jobs_failed", "failures", "elapsed_s"])
            fab = valid_pro.parent / "fab"
            print(f"   fab/ contents          "
                  f"{sorted(p.name for p in fab.iterdir())[:8] if fab.exists() else 'MISSING'}")
            if not js.get("ok"):
                failures.append("jobset_run")

            hr("api_server_start / kipy_exec / api_server_stop (IPC lifecycle)")
            # socket= is MANDATORY here: without it server.py falls back to DEFAULT_SOCKET
            # ($TMPDIR/phicad-mcp-api.sock), which every other run on this machine also uses,
            # and the loser reports "already running" as if the IPC layer were broken.
            st1 = unwrap(await client.call_tool("api_server_start",
                                                {"file_path": str(pcb),
                                                 "socket": TEST_SOCKET}))
            show("api_server_start", st1, ["ok", "pid", "socket", "client_socket_url",
                                           "file", "ping", "log_path"])
            if not st1.get("ok"):
                failures.append("api_server_start")
            if st1.get("socket") != TEST_SOCKET:
                # Printed here, not just collected for RESULT: if a later --full check crashes,
                # the reason two concurrent runs fought must still be visible in this log.
                print(f"   FAIL isolation: the child took socket {st1.get('socket')}, "
                      f"not this run's {TEST_SOCKET}")
                failures.append(f"isolation(the api-server child took socket "
                                f"{st1.get('socket')}, not this run's {TEST_SOCKET})")

            dbl = unwrap(await client.call_tool("api_server_start",
                                                {"file_path": str(pcb),
                                                 "socket": TEST_SOCKET}))
            show("api_server_start (must refuse)", dbl, ["ok", "error", "pid"])
            if dbl.get("ok"):
                failures.append("api_server_start(double-start not refused)")

            probe = LAB / "handshake_kipy_probe.py"
            probe.write_text(
                "import json\n"
                "from kipy import KiCad          # never call check_version() on the fork\n"
                "b = KiCad().get_board()\n"
                "print(json.dumps({'nets': len(b.get_nets()),\n"
                "                  'file': str(b.document.board_filename)}))\n"
            )
            ex = unwrap(await client.call_tool("kipy_exec", {"script_path": str(probe)}))
            show("kipy_exec", ex, ["ok", "exit_code", "socket", "board_file", "stdout"])
            if not ex.get("ok"):
                failures.append("kipy_exec")

            hr("session_board_query(footprints) + session edits with read-back")
            q = unwrap(await client.call_tool("session_board_query",
                                              {"what": "footprints", "limit": 3}))
            show("session_board_query", q, ["ok", "footprints_total", "footprints"])
            if not q.get("ok"):
                failures.append("session_board_query")
            ref = None
            for fp in q.get("footprints") or []:
                if fp.get("reference"):
                    ref = fp["reference"]
                    break
            if ref:
                # The move/rotate/value trio was folded into session_update_footprint (absolute
                # by default, `relative=True` for deltas). Pick whichever the live surface
                # actually offers, by NAME, and never call a tool that is not registered: an
                # unknown tool makes the fastmcp client RAISE, which would abort the rest of
                # --full — including the api_server_stop that releases this run's socket.
                names = {t.name for t in tools}
                if "session_update_footprint" in names:
                    mv = unwrap(await client.call_tool(
                        "session_update_footprint",
                        {"ref": ref, "x_mm": 1.0, "y_mm": -0.5, "relative": True}))
                    show("session_update_footprint (move)", mv,
                         ["ok", "applied", "before", "resolved_mm", "delta", "after_mm"])
                    if not (mv.get("ok") and mv.get("applied")):
                        failures.append("session_update_footprint(move)")
                    rt = unwrap(await client.call_tool(
                        "session_update_footprint",
                        {"ref": ref, "rot_deg": 90, "relative": True}))
                    show("session_update_footprint (rotate)", rt,
                         ["ok", "applied", "resolved_deg", "after_deg"])
                    if not (rt.get("ok") and rt.get("applied")):
                        failures.append("session_update_footprint(rotate)")
                    sv2 = unwrap(await client.call_tool(
                        "session_update_footprint",
                        {"ref": ref, "value": "PHICAD-MCP-SMOKE"}))
                    show("session_update_footprint (value)", sv2,
                         ["ok", "applied", "after", "unapplied"])
                    if not (sv2.get("ok") and sv2.get("applied")):
                        failures.append("session_update_footprint(value)")
                    bad = unwrap(await client.call_tool(
                        "session_update_footprint",
                        {"ref": "NOPE999", "x_mm": 1, "y_mm": 1}))
                    show("session_update_footprint (unknown ref must fail)", bad,
                         ["ok", "error"])
                    if bad.get("ok"):
                        failures.append("session_update_footprint(unknown ref accepted)")
                else:
                    print("   (no session footprint-edit tool on this surface; skipped)")
            else:
                print("   (no footprint with a reference on this board; edit tools skipped)")

            stp2 = unwrap(await client.call_tool("api_server_stop"))
            show("api_server_stop", stp2, ["ok", "state", "pid", "exit_code", "socket"])
            after = unwrap(await client.call_tool("api_server_status"))
            show("api_server_status (after stop)", after, ["ok", "running", "pid"])
            if after.get("running"):
                failures.append("api_server_stop(child still running)")

    hr("RESULT")
    if failures:
        print(f"FAILED: {', '.join(failures)}")
        return 1
    enabled = [n for n, f, _ in CHECK_SECTIONS
               if f == "always" or (f == "--full" and FULL) or (f == "--autoroute" and AUTOROUTE)]
    print(f"all checks passed — {len(enabled)}/{len(CHECK_SECTIONS)} sections: "
          f"{', '.join(enabled)}")
    print("  (re-run with --full for export tools)" if not FULL else "", end="")
    print("  (--autoroute adds a real Freerouting run)" if not AUTOROUTE else "")
    return 0


if __name__ == "__main__":
    exit_code = 2
    try:
        exit_code = asyncio.run(main())
    finally:
        sweep()
        if exit_code == 0:
            # Only a PASS discards the evidence. A failing run keeps its fixtures, exports and
            # api-server log: deleting them is how a real failure came back as a pile of
            # "file not found" errors on the next run.
            shutil.rmtree(RUN_ROOT, ignore_errors=True)
            print(f"cleaned: {RUN_ROOT}")
        else:
            print(f"kept   : {RUN_ROOT}  (this run failed — fixtures and logs left in place)")
    raise SystemExit(exit_code)
