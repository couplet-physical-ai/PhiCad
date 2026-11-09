#!/usr/bin/env python3
#
# This program source code file is part of PhiCad, a fork of KiCad.
#
# Copyright (C) 2026 Exowatt, Inc.
# Copyright The KiCad Developers, see AUTHORS.txt for contributors.
#
# This program is free software; you can redistribute it and/or
# modify it under the terms of the GNU General Public License
# as published by the Free Software Foundation; either version 2
# of the License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, you may find one here:
# http://www.gnu.org/licenses/gpl-2.0.html
#
"""Read a structured, human-readable snapshot of the OPEN board over the IPC API.

This module is the "see everything" half of the AI View plugin.  It is deliberately
free of GUI code so it can be exercised from a terminal against a headless
`kicad-cli api-server` (fast iteration) as well as from the toolbar button inside
the running editor.

DESIGN NOTES (all verified against this fork, PhiCad 10.99, kipy 0.8.0)

  * NEVER call KiCad().check_version().  kipy is generated against 10.0.6 and raises
    against this fork's 10.99.  We report both versions instead and let the operator
    see the mismatch.
  * kipy getters return COPIES.  This module is read-only, so that trap does not bite
    here, but do not "fix" anything below into an in-place mutation.
  * get_board() returns a SNAPSHOT.  Every refresh re-calls it.
  * Boards containing item types newer than kipy's bindings (e.g. ReferencePoint)
    make get_footprints() raise NotImplementedError.  install_fork_compat() below
    registers the missing wrapper; it is the same shim as
    phicad/mcp/examples/session_lifecycle.py, duplicated (not imported) so the
    installed plugin has no dependency on a path outside its own directory.
  * With no document open, get_board() raises an ApiError mentioning
    GetOpenDocuments.  That is "no board open", not a fatal error.
  * There is NO run-DRC command in the IPC API (only InjectDrcError), so the
    "DRC-ish" facts here are computed CLIENT-SIDE from pads/tracks and are clearly
    labelled as heuristics rather than a real DRC run.
  * Selection / zoom / active-layer / visible-layer commands are GUI-ONLY: a headless
    api-server answers "<X> is not available in headless mode" (the gate is
    checkForHeadless, pcbnew/api/api_handler_board.cpp:105).  Everything in
    collect_board_context() is therefore restricted to commands that work in BOTH
    modes.  The GUI-only side of the plugin is editor_control.py, which does not
    depend on any of them: it raises the editor window through the window manager,
    and only probes GetActiveLayer to report honestly which kind of socket it has.
"""

from __future__ import annotations

import math
import os
import time
from dataclasses import dataclass, field
from typing import Any, Optional

NM_PER_MM = 1_000_000.0

# ---------------------------------------------------------------------------
# Fork compatibility shim (mirrors phicad/mcp/examples/session_lifecycle.py)
# ---------------------------------------------------------------------------

_COMPAT_INSTALLED = False
_REFERENCE_POINT_URL = "type.googleapis.com/kiapi.board.types.ReferencePoint"


def install_fork_compat() -> bool:
    """Teach kipy about proto messages this fork sends but kipy cannot unwrap.

    Returns True if a shim was added, False if kipy already knew everything (i.e. a
    future kipy release fixed it and this can be deleted).

    Without this, get_footprints() raises NotImplementedError on any board whose
    footprints contain `(point ...)` children, and the failure looks like a transport
    bug rather than a missing wrapper.

    The proto class itself moved once already: kipy <=0.7 generated
    `board_types_pb2.ReferencePoint`, kipy 0.8 dropped the message entirely while
    the fork kept sending it.  So the class is taken from kipy when it exists and
    rebuilt from the fork's own descriptor set (pylib/sch_api.py, compiled by
    `phicad venv`) when it does not — and when neither source has it, the shim
    declines instead of raising, because compat code that crashes on the next
    kipy release is worse than the incompatibility it papers over.
    """
    global _COMPAT_INSTALLED
    if _COMPAT_INSTALLED:
        return False

    from kipy import board_types as _bt
    from kipy.proto.board import board_types_pb2
    from kipy.util import proto as _kproto

    added = False

    proto_cls = getattr(board_types_pb2, "ReferencePoint", None)
    if proto_cls is None:
        try:
            from pylib_path import add_pylib

            add_pylib()
            import sch_api

            proto_cls = sch_api.message_class("kiapi.board.types.ReferencePoint")
        except Exception:
            proto_cls = None
    if proto_cls is None:
        # Nothing to register a wrapper around; kipy will raise its own
        # NotImplementedError if the fork sends one.  Same behaviour as before
        # the shim existed, minus a misleading crash inside the shim.
        _COMPAT_INSTALLED = True
        return False

    if _REFERENCE_POINT_URL not in _kproto._any_urls:

        class ReferencePoint(_bt.BoardItem):
            """Minimal read-only wrapper for kiapi.board.types.ReferencePoint."""

            def __init__(self, proto=None, proto_ref=None):
                self._proto = (
                    proto_ref if proto_ref is not None else proto_cls()
                )
                if proto is not None:
                    self._proto.CopyFrom(proto)

            def __repr__(self) -> str:
                p = self._proto.position
                return f"ReferencePoint(position=({p.x_nm}, {p.y_nm}))"

            @property
            def layer(self):
                return self._proto.layer

            @layer.setter
            def layer(self, value):
                self._proto.layer = value

        _kproto._any_urls[_REFERENCE_POINT_URL] = proto_cls
        _bt._proto_to_object[proto_cls] = ReferencePoint
        added = True

    _COMPAT_INSTALLED = True
    return added


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class NoBoardOpen(RuntimeError):
    """The editor/server is reachable but has no PCB open."""


class NotConnected(RuntimeError):
    """Could not reach the IPC API at all."""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def layer_name(layer: int) -> str:
    """'BL_F_Cu' (enum 3) -> 'F.Cu'.

    board.get_layer_name() answers 'undefined' for plain layer ids on this fork, so
    the enum is the reliable source.
    """
    try:
        from kipy.proto.board.board_types_pb2 import BoardLayer

        raw = BoardLayer.Name(int(layer))
    except Exception:
        return f"layer#{layer}"
    if raw.startswith("BL_"):
        raw = raw[3:]
    return raw.replace("_", ".", 1)


def stackup_layer_type(type_id: int) -> str:
    """'BSLT_DIELECTRIC' -> 'dielectric'.

    Dielectric (core/prepreg) entries carry no ``user_name`` and their layer enum is
    ``BL_UNDEFINED``, so the type is the only thing that can label them.
    """
    try:
        from kipy.proto.board.board_pb2 import BoardStackupLayerType

        raw = BoardStackupLayerType.Name( int( type_id ) )
    except Exception:
        return ""

    if raw.startswith( "BSLT_" ):
        raw = raw[5:]

    return raw.lower()


def mm(nanometres: float) -> float:
    return round(nanometres / NM_PER_MM, 4)


def _safe(fn, default=None):
    """Run an API getter, swallowing the errors we expect on some modes/boards."""
    try:
        return fn()
    except Exception:
        return default


def _socket_path() -> str:
    return os.environ.get("KICAD_API_SOCKET", "") or "(unset - kipy default)"


# ---------------------------------------------------------------------------
# The context object
# ---------------------------------------------------------------------------


@dataclass
class BoardContext:
    """Everything the AI View knows about the live board, plus how it learned it."""

    # wiring
    socket: str = ""
    token_present: bool = False
    connected: bool = False
    kicad_version: str = "?"
    api_version: str = "?"
    binary_path: str = ""
    collect_seconds: float = 0.0
    errors: list[str] = field(default_factory=list)

    # document
    # Which editor process answered the IPC socket.  "pcb" means the live board
    # data below was read from the running PCB editor; "schematic" means the AI
    # View was launched from eeschema and the board (if any) was found on DISK —
    # copper facts are absent and edit commands need the PCB editor.
    host: str = "pcb"
    board_name: str = ""
    project_name: str = ""
    project_path: str = ""
    title: str = ""
    revision: str = ""
    company: str = ""

    # geometry / stack
    copper_layers: int = 0
    enabled_layers: list[str] = field(default_factory=list)
    stackup: list[dict[str, Any]] = field(default_factory=list)
    board_size_mm: Optional[tuple[float, float]] = None
    board_size_basis: str = ""

    # counts
    counts: dict[str, int] = field(default_factory=dict)

    # detail
    footprints: list[dict[str, Any]] = field(default_factory=list)
    nets: list[dict[str, Any]] = field(default_factory=list)
    netclasses: list[str] = field(default_factory=list)
    track_widths_mm: list[float] = field(default_factory=list)
    via_sizes_mm: list[float] = field(default_factory=list)
    layer_track_counts: dict[str, int] = field(default_factory=dict)

    # heuristics (NOT a DRC run - see module docstring)
    unrouted_nets: list[str] = field(default_factory=list)
    single_pad_nets: list[str] = field(default_factory=list)
    unassigned_pads: int = 0

    # live schematic facts — host == "schematic" only, read from the running
    # eeschema over this fork's schematic IPC commands (phicad/pylib/sch_api.py).
    # sch_sheets: [{name, filename, page, depth}], depth-first sheet tree.
    # sch_nets:   [{name, members, sheets}], members = connectable items.
    sch_sheets: list[dict[str, Any]] = field(default_factory=list)
    sch_nets: list[dict[str, Any]] = field(default_factory=list)

    # ------------------------------------------------------------------
    def has_board(self) -> bool:
        return bool(self.board_name)

    def summary_lines(self) -> list[str]:
        """A compact, readable report for the GUI's context pane."""
        L: list[str] = []
        add = L.append

        add("=" * 62)
        add(" LIVE BOARD CONTEXT (read over the KiCad IPC API)")
        add("=" * 62)
        add("")
        add(f"Board file      : {self.board_name or '(none open)'}")
        if self.host != "pcb":
            add(f"Editor host     : {self.host} editor (board facts read from disk, "
                "not a live PCB session)")
        if self.project_name:
            add(f"Project         : {self.project_name}  [{self.project_path}]")
        if self.title:
            add(f"Title           : {self.title}")
        if self.revision or self.company:
            add(f"Rev / Company   : {self.revision or '-'} / {self.company or '-'}")
        add(f"Read in         : {self.collect_seconds:.2f}s")
        add("")

        add("-- Layer stack " + "-" * 47)
        add(f"Copper layers   : {self.copper_layers}")
        if self.board_size_mm:
            w, h = self.board_size_mm
            add(f"Board size      : {w} x {h} mm   ({self.board_size_basis})")
        else:
            add(f"Board size      : unknown ({self.board_size_basis or 'no geometry'})")
        if self.enabled_layers:
            add(f"Enabled layers  : {len(self.enabled_layers)}")
            add("                  " + ", ".join(self.enabled_layers))
        if self.stackup:
            add("Stackup         :")
            for s in self.stackup:
                th = f"{s['thickness_mm']:.4f}mm" if s.get("thickness_mm") else "-"
                mat = f"  {s['material']}" if s.get("material") else ""
                add(f"    {s['name']:<14} {th:>10}{mat}")
        add("")

        add("-- Contents " + "-" * 49)
        order = [
            "footprints", "pads", "nets", "tracks", "vias", "zones",
            "shapes", "text", "dimensions", "groups",
        ]
        for key in order:
            if key in self.counts:
                add(f"{key.capitalize():<16}: {self.counts[key]}")
        add("")

        if self.layer_track_counts:
            add("-- Routing per layer " + "-" * 40)
            for lname, n in sorted(self.layer_track_counts.items(), key=lambda kv: -kv[1]):
                add(f"{lname:<16}: {n} segments")
            if self.track_widths_mm:
                add(f"{'Track widths':<16}: " + ", ".join(f"{w}mm" for w in self.track_widths_mm))
            if self.via_sizes_mm:
                add(f"{'Via diameters':<16}: " + ", ".join(f"{v}mm" for v in self.via_sizes_mm))
            add("")

        if self.netclasses:
            add("-- Net classes " + "-" * 46)
            add("                  " + ", ".join(self.netclasses))
            add("")

        add("-- Connectivity heuristics (client-side, NOT a DRC run) " + "-" * 5)
        add(f"{'Unrouted nets':<16}: {len(self.unrouted_nets)}"
            + ("  -> " + ", ".join(self.unrouted_nets[:12]) if self.unrouted_nets else ""))
        if len(self.unrouted_nets) > 12:
            add(f"{'':<16}    (+{len(self.unrouted_nets) - 12} more)")
        add(f"{'Single-pad nets':<16}: {len(self.single_pad_nets)}"
            + ("  -> " + ", ".join(self.single_pad_nets[:12]) if self.single_pad_nets else ""))
        add(f"{'Pads with no net':<16}: {self.unassigned_pads}")
        add("")

        if self.footprints:
            add(f"-- Footprints ({len(self.footprints)}) " + "-" * 42)
            add(f"    {'REF':<10} {'VALUE':<16} {'LAYER':<8} {'X,Y (mm)':<20} LIBRARY ID")
            for f in self.footprints:
                pos = f"{f['x_mm']}, {f['y_mm']}"
                add(f"    {f['reference']:<10} {f['value'][:16]:<16} {f['layer']:<8} "
                    f"{pos:<20} {f['lib_id']}")
            add("")

        if self.nets:
            add(f"-- Nets ({len(self.nets)}) " + "-" * 47)
            add(f"    {'NET':<26} {'PADS':>5} {'SEGS':>5} {'LEN(mm)':>9}  STATUS")
            for n in self.nets:
                add(f"    {n['name'][:26]:<26} {n['pads']:>5} {n['segments']:>5} "
                    f"{n['length_mm']:>9.3f}  {n['status']}")
            add("")

        if self.sch_sheets or self.sch_nets:
            add("-- Live schematic (read from the running schematic editor) " + "-" * 3)
            if self.sch_sheets:
                add(f"{'Sheets':<16}: {len(self.sch_sheets)}")
                for s in self.sch_sheets[:20]:
                    add("                  " + "  " * s["depth"]
                        + f"{s['name']}  [{s['filename']}, page {s['page']}]")
                if len(self.sch_sheets) > 20:
                    add(f"                  (+{len(self.sch_sheets) - 20} more)")
            if self.sch_nets:
                add(f"{'Nets':<16}: {len(self.sch_nets)}")
                add(f"    {'NET':<36} {'MEMBERS':>8} {'SHEETS':>7}")
                for n in self.sch_nets[:40]:
                    add(f"    {n['name'][:36]:<36} {n['members']:>8} {n['sheets']:>7}")
                if len(self.sch_nets) > 40:
                    add(f"    (+{len(self.sch_nets) - 40} more)")
            add("")

        if self.errors:
            add("-- Notes / partial reads " + "-" * 36)
            for e in self.errors:
                add(f"    ! {e}")
            add("")

        return L

    def summary_text(self) -> str:
        return "\n".join(self.summary_lines())

    def ai_digest(self, max_footprints: int = 60, max_nets: int = 60) -> str:
        """A trimmed, token-conscious version of the context for the AI backend."""
        L: list[str] = []
        add = L.append
        add("### Live KiCad board context (read over the IPC API just now)")
        add(f"- PhiCad version: {self.kicad_version} (kipy built for {self.api_version})")
        add(f"- Board file: {self.board_name or '(none open)'}")
        if self.host != "pcb":
            add(f"- Editor host: the {self.host} editor. The board file was found on "
                "disk; copper/net facts below are absent, and /move-style edit "
                "commands need the PCB editor open.")
        if self.project_name:
            add(f"- Project: {self.project_name} at {self.project_path}")
        if self.title:
            add(f"- Title block: title={self.title!r} rev={self.revision!r} company={self.company!r}")
        add(f"- Copper layers: {self.copper_layers}")
        if self.board_size_mm:
            add(f"- Board size: {self.board_size_mm[0]} x {self.board_size_mm[1]} mm ({self.board_size_basis})")
        if self.enabled_layers:
            add(f"- Enabled layers ({len(self.enabled_layers)}): {', '.join(self.enabled_layers)}")
        if self.stackup:
            add("- Stackup: " + "; ".join(
                f"{s['name']}" + (f" {s['thickness_mm']:.4f}mm" if s.get("thickness_mm") else "")
                for s in self.stackup))
        add("- Counts: " + ", ".join(f"{k}={v}" for k, v in self.counts.items()))
        if self.layer_track_counts:
            add("- Segments per layer: " + ", ".join(
                f"{k}={v}" for k, v in sorted(self.layer_track_counts.items())))
        if self.track_widths_mm:
            add(f"- Track widths in use (mm): {', '.join(str(w) for w in self.track_widths_mm)}")
        if self.via_sizes_mm:
            add(f"- Via diameters in use (mm): {', '.join(str(v) for v in self.via_sizes_mm)}")
        if self.netclasses:
            add(f"- Net classes: {', '.join(self.netclasses)}")

        add("- Connectivity heuristics computed client-side (the IPC API cannot run DRC):")
        add(f"  - unrouted nets (>=2 pads, 0 track segments): {len(self.unrouted_nets)}"
            + (f" -> {', '.join(self.unrouted_nets[:30])}" if self.unrouted_nets else ""))
        add(f"  - single-pad nets: {len(self.single_pad_nets)}"
            + (f" -> {', '.join(self.single_pad_nets[:30])}" if self.single_pad_nets else ""))
        add(f"  - pads with no net assigned: {self.unassigned_pads}")

        if self.footprints:
            shown = self.footprints[:max_footprints]
            add(f"- Footprints ({len(self.footprints)} total"
                + (f", first {len(shown)} shown" if len(shown) < len(self.footprints) else "")
                + "): ref | value | layer | x,y mm | lib id")
            for f in shown:
                add(f"  - {f['reference']} | {f['value']} | {f['layer']} | "
                    f"{f['x_mm']},{f['y_mm']} | {f['lib_id']}")
        if self.nets:
            shown = self.nets[:max_nets]
            add(f"- Nets ({len(self.nets)} total"
                + (f", first {len(shown)} shown" if len(shown) < len(self.nets) else "")
                + "): name | pads | segments | routed length mm | status")
            for n in shown:
                add(f"  - {n['name'] or '(no net)'} | {n['pads']} | {n['segments']} | "
                    f"{n['length_mm']:.3f} | {n['status']}")
        if self.sch_sheets:
            add(f"- Live schematic hierarchy ({len(self.sch_sheets)} sheet instances, "
                "read from the running schematic editor): name | file | page")
            for s in self.sch_sheets[:30]:
                add(f"  - {'  ' * s['depth']}{s['name']} | {s['filename']} | {s['page']}")
        if self.sch_nets:
            shown = self.sch_nets[:max_nets]
            add(f"- Live schematic nets ({len(self.sch_nets)} total"
                + (f", first {len(shown)} shown" if len(shown) < len(self.sch_nets) else "")
                + "): name | connectable members | sheet instances")
            for n in shown:
                add(f"  - {n['name']} | {n['members']} | {n['sheets']}")
        if self.errors:
            add("- Partial reads / notes: " + " | ".join(self.errors))
        return "\n".join(L)


# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------


def connect(timeout_ms: int = 30000):
    """Return a live kipy.KiCad, or raise NotConnected.

    Deliberately does NOT call check_version(): kipy is generated against 10.0.6 and
    raises against this fork's 10.99.  A generous timeout is required because cold
    operations on a freshly launched editor take seconds.
    """
    install_fork_compat()
    try:
        from kipy import KiCad

        kicad = KiCad(timeout_ms=timeout_ms)
        kicad.ping()
        return kicad
    except Exception as exc:
        raise NotConnected(
            f"cannot reach the KiCad IPC API on {_socket_path()}: "
            f"{type(exc).__name__}: {exc}"
        ) from exc


def collect_schematic_host_context(kicad, timeout_note: str = "") -> BoardContext:
    """Context when the IPC socket is answered by the SCHEMATIC editor.

    Launched from eeschema's toolbar, KICAD_API_SOCKET points at the eeschema
    process, which serves schematic documents and cannot answer GetBoard.  The
    project is discovered from the open schematic instead, and the project's
    board — if one exists — is located ON DISK, which is exactly what the
    render tabs, Git history and the host bridge key off.  No copper facts
    are fabricated: counts/nets/heuristics stay empty, has_board() reflects
    the presence of the saved file, and `host` says which editor answered so
    every downstream surface can be honest about what it knows.

    What IS live here is the schematic itself: nets and the sheet tree come
    from the running editor via this fork's schematic IPC commands
    (pylib/sch_api.py, dynamic bindings from the descriptor set `phicad venv`
    compiles).  When that plumbing is absent — no descriptor set, an upstream
    server without the commands — the facts are simply omitted and a note
    says why; nothing is guessed from files.

    Raises NoBoardOpen when the host serves no open schematic either.
    """
    started = time.monotonic()
    ctx = BoardContext(socket=_socket_path(),
                       token_present=bool(os.environ.get("KICAD_API_TOKEN")))
    ctx.connected = True
    ctx.host = "schematic"
    ctx.kicad_version = _safe(kicad.get_version, "?") or "?"
    ctx.api_version = _safe(kicad.get_api_version, "?") or "?"
    ctx.binary_path = _safe(lambda: kicad.get_kicad_binary_path("kicad-cli"), "") or ""

    try:
        from kipy.proto.common.types import DocumentType

        docs = kicad.get_open_documents(DocumentType.DOCTYPE_SCHEMATIC)
    except Exception as exc:
        raise NoBoardOpen(
            "connected, but the host reports neither an open PCB nor an open "
            f"schematic ({type(exc).__name__}: {exc})") from exc
    if not docs:
        raise NoBoardOpen("connected to the schematic editor, but it reports "
                          "no open schematic")

    proj = docs[0].project
    ctx.project_name = getattr(proj, "name", "") or ""
    ctx.project_path = getattr(proj, "path", "") or ""
    # kipy's Project.path convention (see collect_board_context) is a DIRECTORY;
    # be tolerant if a file path ever shows up here.
    if ctx.project_path and not os.path.isdir(ctx.project_path):
        ctx.project_path = os.path.dirname(ctx.project_path)

    try:
        from pylib_path import add_pylib

        add_pylib()
        import sch_api

        ctx.sch_nets = sch_api.netlist(kicad)
        ctx.sch_sheets = sch_api.hierarchy(kicad)
    except Exception as exc:  # degrade, never fabricate
        ctx.errors.append("live schematic facts unavailable "
                          f"({type(exc).__name__}: {exc})")

    if ctx.project_name and ctx.project_path:
        candidate = os.path.join(ctx.project_path, ctx.project_name + ".kicad_pcb")
        if os.path.isfile(candidate):
            ctx.board_name = os.path.basename(candidate)
        else:
            ctx.errors.append(
                f"project has no saved board yet ({ctx.project_name}.kicad_pcb "
                "not found next to the schematic)")
    ctx.errors.append(
        "AI View was launched from the schematic editor: board facts come from "
        "the last saved board file, not a live PCB session, and board edit "
        "commands (/move, /place, ...) need the PCB editor open")
    if timeout_note:
        ctx.errors.append(timeout_note)

    ctx.collect_seconds = round(time.monotonic() - started, 3)
    return ctx


def _get_board(kicad):
    try:
        return kicad.get_board()
    except Exception as exc:
        msg = str(exc)
        if "GetOpenDocuments" in msg or "no handler available" in msg:
            raise NoBoardOpen(
                "connected, but no PCB is open in the editor "
                "(the API reports no open documents)"
            ) from exc
        if "not available" in msg and "headless" in msg:
            raise NoBoardOpen(f"connected, but the server refused the request: {msg}") from exc
        raise


# ---------------------------------------------------------------------------
# Collection
# ---------------------------------------------------------------------------


def collect_board_context(kicad=None, max_footprints: int = 400,
                          max_nets: int = 400) -> BoardContext:
    """Build a BoardContext from the currently open board.

    Only uses API commands that work in BOTH the GUI and a headless api-server, so
    this same function backs the terminal smoke test and the toolbar button.
    """
    started = time.monotonic()
    ctx = BoardContext(socket=_socket_path(),
                       token_present=bool(os.environ.get("KICAD_API_TOKEN")))

    if kicad is None:
        kicad = connect()
    ctx.connected = True
    ctx.kicad_version = _safe(kicad.get_version, "?") or "?"
    ctx.api_version = _safe(kicad.get_api_version, "?") or "?"
    ctx.binary_path = _safe(lambda: kicad.get_kicad_binary_path("kicad-cli"), "") or ""

    board = _get_board(kicad)

    ctx.board_name = _safe(lambda: board.name, "") or ""
    proj = _safe(board.get_project)
    if proj is not None:
        ctx.project_name = getattr(proj, "name", "") or ""
        ctx.project_path = getattr(proj, "path", "") or ""

    tb = _safe(board.get_title_block_info)
    if tb is not None:
        ctx.title = getattr(tb, "title", "") or ""
        ctx.revision = getattr(tb, "revision", "") or ""
        ctx.company = getattr(tb, "company", "") or ""

    ctx.copper_layers = _safe(board.get_copper_layer_count, 0) or 0

    enabled = _safe(board.get_enabled_layers, []) or []
    ctx.enabled_layers = [layer_name(l) for l in enabled]

    stack = _safe(board.get_stackup)
    if stack is not None:
        for lyr in getattr(stack, "layers", []) or []:
            try:
                if not getattr(lyr, "enabled", True):
                    continue
                # Dielectric layers have no user_name and report BL_UNDEFINED, which would
                # otherwise render a stack of anonymous "UNDEFINED" rows; fall back to the
                # stackup layer TYPE so the reader sees "dielectric".
                name = getattr(lyr, "user_name", "") or ""

                if not name:
                    name = layer_name(getattr(lyr, "layer", 0))

                    if name.upper().startswith("UNDEFINED"):
                        name = stackup_layer_type(getattr(lyr, "type", 0)) or name

                ctx.stackup.append({
                    "name": name,
                    "thickness_mm": mm(getattr(lyr, "thickness", 0) or 0),
                    "material": getattr(lyr, "material_name", "") or "",
                })
            except Exception:
                continue
    else:
        ctx.errors.append("stackup unavailable on this board/mode")

    # ---- collections -------------------------------------------------
    footprints = _safe(board.get_footprints, None)
    if footprints is None:
        footprints = []
        ctx.errors.append(
            "get_footprints() failed - if this says 'Missing type mapping', the board "
            "holds an item type newer than kipy's bindings and the compat shim needs "
            "another entry"
        )
    pads = _safe(board.get_pads, []) or []
    nets = _safe(board.get_nets, []) or []
    tracks = _safe(board.get_tracks, []) or []
    vias = _safe(board.get_vias, []) or []

    ctx.counts = {
        "footprints": len(footprints),
        "pads": len(pads),
        "nets": len(nets),
        "tracks": len(tracks),
        "vias": len(vias),
        "zones": len(_safe(board.get_zones, []) or []),
        "shapes": len(_safe(board.get_shapes, []) or []),
        "text": len(_safe(board.get_text, []) or []),
        "dimensions": len(_safe(board.get_dimensions, []) or []),
        "groups": len(_safe(board.get_groups, []) or []),
    }

    # ---- footprints --------------------------------------------------
    for fp in footprints[:max_footprints]:
        try:
            pos = fp.position
            lib = _safe(lambda fp=fp: fp.definition.id)
            lib_id = ""
            if lib is not None:
                nick = getattr(lib, "library_nickname", "") or ""
                entry = getattr(lib, "entry_name", "") or ""
                lib_id = f"{nick}:{entry}" if nick or entry else str(lib)
            ctx.footprints.append({
                "reference": _safe(lambda fp=fp: fp.reference_field.text.value, "?") or "?",
                "value": _safe(lambda fp=fp: fp.value_field.text.value, "") or "",
                "layer": layer_name(_safe(lambda fp=fp: fp.layer, 0) or 0),
                "x_mm": mm(pos.x),
                "y_mm": mm(pos.y),
                "lib_id": lib_id,
                "orientation": str(_safe(lambda fp=fp: fp.orientation, "")),
            })
        except Exception as exc:
            ctx.errors.append(f"footprint skipped: {type(exc).__name__}: {exc}")
    if len(footprints) > max_footprints:
        ctx.errors.append(
            f"footprint list truncated to {max_footprints} of {len(footprints)}")

    # ---- per-net rollup ---------------------------------------------
    pads_per_net: dict[str, int] = {}
    for pad in pads:
        try:
            net = getattr(pad, "net", None)
            name = getattr(net, "name", "") if net is not None else ""
        except Exception:
            name = ""
        if name:
            pads_per_net[name] = pads_per_net.get(name, 0) + 1
        else:
            ctx.unassigned_pads += 1

    segs_per_net: dict[str, int] = {}
    len_per_net: dict[str, float] = {}
    widths: set[float] = set()
    for tr in tracks:
        try:
            net = getattr(tr, "net", None)
            name = getattr(net, "name", "") if net is not None else ""
            segs_per_net[name] = segs_per_net.get(name, 0) + 1
            start, end = tr.start, tr.end
            seg = math.hypot(end.x - start.x, end.y - start.y)
            len_per_net[name] = len_per_net.get(name, 0.0) + seg
            widths.add(mm(tr.width))
            lname = layer_name(tr.layer)
            ctx.layer_track_counts[lname] = ctx.layer_track_counts.get(lname, 0) + 1
        except Exception:
            continue
    ctx.track_widths_mm = sorted(widths)

    via_d: set[float] = set()
    for via in vias:
        try:
            ps = getattr(via, "padstack", None)
            diam = None
            if ps is not None:
                for cl in getattr(ps, "copper_layers", []) or []:
                    size = getattr(getattr(cl, "shape", None), "size", None)
                    if size is not None and getattr(size, "x", 0):
                        diam = size.x
                        break
            if diam:
                via_d.add(mm(diam))
        except Exception:
            continue
    ctx.via_sizes_mm = sorted(via_d)

    for net in nets[:max_nets]:
        name = getattr(net, "name", "") or ""
        npads = pads_per_net.get(name, 0)
        nsegs = segs_per_net.get(name, 0)
        nlen = mm(len_per_net.get(name, 0.0))
        if not name:
            status = "no net"
        elif npads >= 2 and nsegs == 0:
            status = "UNROUTED"
            ctx.unrouted_nets.append(name)
        elif npads == 1:
            status = "single pad"
            ctx.single_pad_nets.append(name)
        elif npads == 0 and nsegs == 0:
            status = "empty"
        else:
            status = "routed"
        ctx.nets.append({
            "name": name, "pads": npads, "segments": nsegs,
            "length_mm": nlen, "status": status,
        })
    if len(nets) > max_nets:
        ctx.errors.append(f"net list truncated to {max_nets} of {len(nets)}")

    # ---- net classes -------------------------------------------------
    if nets:
        ncs = _safe(lambda: board.get_netclass_for_nets(*nets))
        if isinstance(ncs, dict):
            names = set()
            for val in ncs.values():
                nm = getattr(val, "name", None)
                if nm:
                    names.add(nm)
            ctx.netclasses = sorted(names)

    # ---- board size --------------------------------------------------
    _compute_board_size(board, ctx, footprints, tracks)

    ctx.collect_seconds = time.monotonic() - started
    return ctx


def _compute_board_size(board, ctx: BoardContext, footprints, tracks) -> None:
    """Board extents from Edge.Cuts when present, else an approximation.

    get_item_bounding_box() is one API round-trip per item, so it is only used for
    Edge.Cuts graphics (a handful on a real board).  The fallback is computed locally
    from footprint origins and track endpoints - cheap, but it ignores courtyards, so
    it is labelled as approximate.
    """
    try:
        from kipy.proto.board.board_types_pb2 import BoardLayer

        edge_layer = BoardLayer.Value("BL_Edge_Cuts")
    except Exception:
        edge_layer = None

    shapes = _safe(board.get_shapes, []) or []
    edges = [s for s in shapes if edge_layer is not None
             and getattr(s, "layer", None) == edge_layer]

    if edges:
        xs: list[float] = []
        ys: list[float] = []
        for shape in edges[:200]:
            box = _safe(lambda shape=shape: board.get_item_bounding_box(shape))
            if box is None:
                continue
            try:
                xs += [box.pos.x, box.pos.x + box.size.x]
                ys += [box.pos.y, box.pos.y + box.size.y]
            except Exception:
                continue
        if xs and ys:
            ctx.board_size_mm = (mm(max(xs) - min(xs)), mm(max(ys) - min(ys)))
            ctx.board_size_basis = f"Edge.Cuts bounding box, {len(edges)} graphics"
            return
        ctx.errors.append("Edge.Cuts present but bounding boxes could not be read")

    xs, ys = [], []
    for fp in footprints:
        try:
            xs.append(fp.position.x)
            ys.append(fp.position.y)
        except Exception:
            continue
    for tr in tracks:
        try:
            xs += [tr.start.x, tr.end.x]
            ys += [tr.start.y, tr.end.y]
        except Exception:
            continue
    if xs and ys:
        ctx.board_size_mm = (mm(max(xs) - min(xs)), mm(max(ys) - min(ys)))
        ctx.board_size_basis = "approximate: no Edge.Cuts, extents of footprints+tracks"
    else:
        ctx.board_size_basis = "no Edge.Cuts and no placed items"


# ---------------------------------------------------------------------------
# CLI smoke test:  build/venv/bin/python board_context.py
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys

    try:
        print(collect_board_context().summary_text())
    except NoBoardOpen as exc:
        print(f"no board open: {exc}")
        sys.exit(2)
    except NotConnected as exc:
        print(f"not connected: {exc}")
        sys.exit(3)
