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
"""One managed IPC session: open a board, import a netlist, place parts, save.

Runs inside the automation venv (it needs `kipy`); everything else in `synth/` is
stdlib-only and runs on any python3.  Keeping the kipy dependency in exactly one module is
deliberate — the IPC client is the part with version traps, and a trap is easier to fix in
one place than in five.

The traps encoded here, all hit for real (`phicad/ai-interfacing.md`)
-----------------------------------------------------------------------
* `KICAD_API_SOCKET` must be the `ipc://` **URL**, while the server's `--socket` takes a
  plain path.  Both halves are handled by `socket_url()` / `socket_path()`.
* The socket file exists *before* the server can answer — poll `Ping` until it stops
  returning `AS_NOT_READY` rather than trusting the file.
* Never call `check_version()`: kipy 0.8's guard targets KiCad 10.0.6 and raises
  `FutureVersionError` against this fork, even though every command works.
* `get_footprints()` raises `NotImplementedError` on fork boards until kipy is taught about
  `kiapi.board.types.ReferencePoint` — `install_fork_compat()`.
* kipy's `.position` getter returns a **copy**, so `fp.position.x = …` is a silent no-op.
  Always assign a whole `Vector2`.
* `ISC_OK` is returned for edits the board discarded, so every write here is read back from
  a fresh `get_board()` and compared before it is reported as done.
* One connection per session; rapid reconnects wedge NNG's req/rep state.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

DEFAULT_SOCKET = "/tmp/phicad-synth.sock"

# How long to wait for a freshly launched server to answer Ping.  A small board is ready in
# about a second; the ceiling is generous because a cold app bundle on a busy machine is
# much slower, and a spurious timeout looks exactly like a broken build.
CONNECT_TIMEOUT_S = 30.0

# kipy defaults every request to a 2 s NNG send/recv timeout, which is fine for `Ping` and
# far too short for the heavy synchronous commands this pipeline depends on: importing a
# netlist, refilling zones, or saving a board routinely takes longer than that on a busy
# machine, and NNG surfaces the expiry as a bare "Timed out" ConnectionError.  That failure
# is indistinguishable from a dead server, so a loaded laptop looked like a broken build.
# Every request is one blocking round trip against a server we own, so a generous ceiling
# costs nothing when things are healthy and prevents the false negative when they are slow.
REQUEST_TIMEOUT_MS = int(os.environ.get("PHICAD_IPC_TIMEOUT_MS", "120000"))

# Position/angle agreement thresholds when reading a write back.  Board internal units are
# nanometres, so anything at or below this is representation noise rather than a discard.
EPS_MM = 1e-4
EPS_DEG = 1e-3

_COMPAT_INSTALLED = False
_REFERENCE_POINT_URL = "type.googleapis.com/kiapi.board.types.ReferencePoint"


class SessionError(RuntimeError):
    """The IPC session could not be established or a command failed."""


def socket_path(value: Optional[str] = None) -> str:
    """The plain filesystem path form, which is what the server's `--socket` wants."""
    raw = value or os.environ.get("KICAD_API_SOCKET") or DEFAULT_SOCKET
    return raw[len("ipc://"):] if raw.startswith("ipc://") else raw


def socket_url(value: Optional[str] = None) -> str:
    """The `ipc://` URL form, which is what kipy reads from the environment verbatim."""
    return "ipc://" + socket_path(value)


def _fork_messages(module: str, package: str, *names):
    """Resolve protobuf classes that PyPI kipy may not have.

    kipy on PyPI is generated from *upstream* protos, so fork additions -- OpenDocument,
    ImportNetlist, ReferencePoint -- are simply absent, and a plain import raises
    ImportError in the middle of a build. `phicad venv` compiles this fork's proto tree to a
    descriptor set for exactly this reason; load fork messages from there when kipy comes up
    short, which is what plugins/ai-view/board_context.py already does.

    An enum such as NetlistMatchMode is a container, not a message, so it is resolved
    through its descriptor rather than as a message class.
    """
    import importlib
    import sys as _sys

    try:
        mod = importlib.import_module(module)
    except ImportError:
        mod = None

    if any(mod is None or not hasattr(mod, n) for n in names):
        # phicad/pylib is a sibling of phicad/synth; import by path so this stays runnable
        # straight from a checkout, the way library.py does it.
        pylib = str(Path(__file__).resolve().parent.parent / "pylib")
        if pylib not in _sys.path:
            _sys.path.insert(0, pylib)

    resolved = []
    for name in names:
        if mod is not None and hasattr(mod, name):
            resolved.append(getattr(mod, name))
            continue
        import sch_api

        full = f"{package}.{name}"
        try:
            resolved.append(sch_api.message_class(full))
        except Exception:
            resolved.append(_pool_enum(full))
    return resolved[0] if len(resolved) == 1 else tuple(resolved)


def _pool_enum(full_name: str):
    """An enum from the fork descriptor set, as an object with its values as attributes."""
    import sch_api

    desc = sch_api._Pool.get().pool.FindEnumTypeByName(full_name)
    return type(desc.name, (), {v.name: v.number for v in desc.values})


def install_fork_compat() -> bool:
    """Teach kipy about proto messages this fork sends but kipy cannot unwrap.

    Mirrors `plugins/ai-view/board_context.install_fork_compat()`.  Returns True if a shim
    was added, False if kipy already knew — so the day a kipy release fixes this, the
    return value says so and the shim can go.
    """
    global _COMPAT_INSTALLED
    if _COMPAT_INSTALLED:
        return False

    from kipy import board_types as _bt
    from kipy.proto.board import board_types_pb2
    from kipy.util import proto as _kproto

    added = False

    # ReferencePoint is a fork addition, so a kipy installed from PyPI does not have it.
    # Registering the Any URL without checking that first passed the guard below and then
    # died on the attribute lookup, which took out the whole synth pipeline on a stock venv.
    # board_context.install_fork_compat() already got this right; this copy had not.
    proto_cls = getattr(board_types_pb2, "ReferencePoint", None)
    if proto_cls is None:
        # Nothing to wrap. kipy raises its own NotImplementedError if the fork sends one,
        # which is the pre-shim behaviour and a great deal clearer than an AttributeError
        # from inside the compatibility shim itself.
        _COMPAT_INSTALLED = True
        return False

    if _REFERENCE_POINT_URL not in _kproto._any_urls:

        class ReferencePoint(_bt.BoardItem):
            """Minimal read-only wrapper for kiapi.board.types.ReferencePoint."""

            def __init__(self, proto=None, proto_ref=None):
                self._proto = (proto_ref if proto_ref is not None
                               else proto_cls())
                if proto is not None:
                    self._proto.CopyFrom(proto)

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


@dataclass
class ImportReport:
    """What `ImportNetlist` did, as data rather than as a wall of text."""

    error_count: int
    warning_count: int
    new_footprints: int
    report: str = ""

    @property
    def ok(self) -> bool:
        return self.error_count == 0

    def warnings(self) -> List[str]:
        """The report's warning-ish lines.

        The updater prints one line per action; the ones that matter to a caller are the
        refusals.  `No net found for component X pad N` is the important one — it means a
        pad the design never mentioned, which `ir.validate()` should already have caught.
        Seeing it here means the design and the footprint disagree.
        """
        keep = ("No net found", "Cannot add", "Cannot remove", "Cannot update",
                "not found", "Failed", "Error", "Warning")
        return [line for line in self.report.splitlines()
                if any(marker in line for marker in keep)]

    def summary(self) -> str:
        return (f"{self.new_footprints} footprints added, "
                f"{self.error_count} errors, {self.warning_count} warnings")


@dataclass
class PlacementResult:
    """Per-part outcome of a placement pass."""

    moved: List[str] = field(default_factory=list)
    unchanged: List[str] = field(default_factory=list)
    missing: List[str] = field(default_factory=list)
    rejected: Dict[str, str] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.missing and not self.rejected

    def summary(self) -> str:
        parts = [f"{len(self.moved)} placed"]
        if self.unchanged:
            parts.append(f"{len(self.unchanged)} already in position")
        if self.missing:
            parts.append(f"{len(self.missing)} not on the board: "
                         + ", ".join(sorted(self.missing)))
        if self.rejected:
            parts.append(f"{len(self.rejected)} rejected")
        return "; ".join(parts)


class ApiServer:
    """A `kicad-cli api-server` this process owns, as a context manager.

    Used when no server is already listening.  Attaching to a running GUI instead is a
    deliberate choice the caller makes by pointing `KICAD_API_SOCKET` at the app's socket;
    this class never takes that over, because a headless save would rewrite a board the
    user has open.
    """

    def __init__(self, cli: str, socket: Optional[str] = None,
                 log: Optional[Path] = None):
        self.cli = cli
        self.socket = socket_path(socket)
        self.log = Path(log) if log else None
        self.proc: Optional[subprocess.Popen] = None

    def __enter__(self) -> "ApiServer":
        env = dict(os.environ)
        env["KICAD_RUN_FROM_BUILD_DIR"] = "1"
        stdout = open(self.log, "wb") if self.log else subprocess.DEVNULL
        self._log_handle = stdout if self.log else None
        self.proc = subprocess.Popen(
            [self.cli, "api-server", "--socket", self.socket],
            env=env, stdout=stdout,
            stderr=subprocess.STDOUT if self.log else subprocess.DEVNULL)
        return self

    def __exit__(self, *exc) -> None:
        if self.proc is not None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        if getattr(self, "_log_handle", None):
            self._log_handle.close()
        # Leave no orphan socket: the server recovers from one, but a stale file makes
        # "is a server running?" unanswerable for anything else looking at the path.
        try:
            os.unlink(self.socket)
        except OSError:
            pass


class Session:
    """A connected board session."""

    def __init__(self, socket: Optional[str] = None):
        self.socket = socket_url(socket)
        self.kicad = None

    # ------------------------------------------------------------------ connection

    def connect(self, timeout_s: float = CONNECT_TIMEOUT_S) -> "Session":
        """Connect and wait until the server actually answers.

        The socket file appears before the server is ready, so its existence proves
        nothing; `Ping` returning without `AS_NOT_READY` does.
        """
        os.environ["KICAD_API_SOCKET"] = self.socket
        try:
            from kipy import KiCad
        except ImportError as exc:
            raise SessionError(
                f"kipy is not importable in this interpreter ({sys.executable}).\n"
                "Run through the automation venv: phicad py <script>") from exc

        install_fork_compat()

        deadline = time.time() + timeout_s
        last = ""
        while time.time() < deadline:
            try:
                kicad = KiCad(timeout_ms=REQUEST_TIMEOUT_MS)
                kicad.ping()               # never check_version(): see the module docstring
                self.kicad = kicad
                return self
            except Exception as exc:       # noqa: BLE001 — retried until the deadline
                last = f"{type(exc).__name__}: {exc}"
                time.sleep(0.25)
        raise SessionError(
            f"no API server answered on {self.socket} within {timeout_s:.0f}s.\n"
            f"last error: {last}\n"
            "Start one with: phicad api-server --socket " + socket_path(self.socket))

    def _require(self):
        if self.kicad is None:
            raise SessionError("not connected — call connect() first")
        return self.kicad

    # ------------------------------------------------------------------- documents

    def open_board(self, path) -> str:
        """Open a board by absolute path.  Returns the document's board filename.

        `OpenDocument` is a fork addition (`kicad/kicad.cpp`); kipy 0.8 has no high-level
        wrapper for it, so the command is sent directly.
        """
        kicad = self._require()
        OpenDocument, OpenDocumentResponse = _fork_messages(
            "kipy.proto.common.commands.project_commands_pb2",
            "kiapi.common.commands", "OpenDocument", "OpenDocumentResponse")
        from kipy.proto.common.types import DocumentType

        board = Path(path).resolve()
        if not board.is_file():
            raise SessionError(f"no board at {board}")

        reply = kicad._client.send(
            OpenDocument(type=DocumentType.DOCTYPE_PCB, path=str(board)),
            OpenDocumentResponse)
        return reply.document.board_filename

    def board(self):
        return self._require().get_board()

    def save(self) -> None:
        """Save the open board.

        `SaveDocument` is a *project* command and lives in
        `kipy.proto.common.commands.project_commands_pb2` — not under `kipy.proto.project`,
        which does not exist.  Its reply is `Empty`, not a `SaveDocumentResponse`.
        """
        kicad = self._require()
        from google.protobuf.empty_pb2 import Empty
        SaveDocument = _fork_messages(
            "kipy.proto.common.commands.project_commands_pb2",
            "kiapi.common.commands", "SaveDocument")

        doc = self.board().document
        request = SaveDocument()
        request.document.type = doc.type
        request.document.board_filename = doc.board_filename
        kicad._client.send(request, Empty)

    def refill_zones(self) -> int:
        """Fill every zone on the open board.  Returns how many zones the board has.

        This is not cosmetic and it is not something DRC does for you.  `kicad-cli pcb drc
        --refill-zones` fills in memory for the duration of the check, so a board with an
        empty pour passes with 0 unconnected and is then written to disk with the pour
        still empty — the ground stitching vias connect to nothing, and the exported B.Cu
        gerber carries no copper at all.  A board that passes every gate and cannot be
        fabricated is the worst failure mode this pipeline has, so the fill is a stage with
        its own evidence rather than a side effect of the checker.

        The handler is one of the few that names headless explicitly: with no event loop to
        defer to it fills synchronously through the same ZONE_FILLER_TOOL the CLI jobs use
        (`pcbnew/api/api_handler_pcb.cpp:1667-1700`).
        """
        kicad = self._require()
        RefillZones = _fork_messages(
            "kipy.proto.board.board_commands_pb2", "kiapi.board.commands", "RefillZones")
        from google.protobuf.empty_pb2 import Empty

        board = self.board()
        request = RefillZones()
        request.board.CopyFrom(board.document)
        # An empty `zones` list means "all of them".
        kicad._client.send(request, Empty)
        return len(board.get_zones())

    # --------------------------------------------------------------------- netlist

    def import_netlist(self, netlist_path, dry_run: bool = False,
                       match_by_uuid: bool = False,
                       delete_extra: bool = True,
                       update_footprints: bool = True) -> ImportReport:
        """Run KiCad's own `BOARD_NETLIST_UPDATER` against a netlist file.

        This is the whole point of the synth pipeline: footprints are loaded from the real
        libraries, references and values are set, and every pad is attached to its net by
        the same code path the GUI's "Update PCB from Schematic" uses.

        `match_by_uuid=False` (match by reference designator) is the default because the
        first import has no UUIDs on the board to match against.  Re-importing an updated
        design against a board that already carries placement should use it, so a renamed
        part keeps its position.
        """
        kicad = self._require()
        ImportNetlist, ImportNetlistResponse, NetlistMatchMode = _fork_messages(
            "kipy.proto.board.board_commands_pb2", "kiapi.board.commands",
            "ImportNetlist", "ImportNetlistResponse", "NetlistMatchMode")

        netlist = Path(netlist_path).resolve()
        if not netlist.is_file():
            raise SessionError(f"no netlist at {netlist}")

        request = ImportNetlist()
        # When ImportNetlist comes from the fork descriptor set it lives in a *private*
        # protobuf pool, so its DocumentSpecifier is a different class from the one on
        # kipy's board object and CopyFrom() rejects it. Bytes are the one representation
        # both pools agree on, so round-trip through the wire format.
        request.board.ParseFromString(self.board().document.SerializeToString())
        request.netlist_path = str(netlist)
        request.dry_run = dry_run
        request.match_mode = (NetlistMatchMode.NMM_UUID if match_by_uuid
                              else NetlistMatchMode.NMM_REFERENCE)
        request.delete_extra_footprints = delete_extra
        request.update_footprints = update_footprints

        reply = kicad._client.send(request, ImportNetlistResponse)
        return ImportReport(error_count=reply.error_count,
                            warning_count=reply.warning_count,
                            new_footprints=reply.new_footprint_count,
                            report=reply.report)

    # ------------------------------------------------------------------- placement

    def footprint_map(self) -> Dict[str, object]:
        """`reference -> footprint` for everything on the board."""
        out: Dict[str, object] = {}
        for fp in self.board().get_footprints():
            try:
                ref = fp.reference_field.text.value or ""
            except Exception:                                # noqa: BLE001
                ref = ""
            if ref:
                out[ref] = fp
        return out

    def place(self, positions: Dict[str, Tuple[float, float, float]]) -> PlacementResult:
        """Move footprints to absolute positions: `{ref: (x_mm, y_mm, rotation_deg)}`.

        Every move is read back from a fresh board before being reported, because a
        successful call does not mean a changed board.
        """
        from kipy.geometry import Vector2
        from kipy.geometry import Angle

        result = PlacementResult()
        board = self.board()
        placed = self.footprint_map()

        pending = []
        for ref, (x, y, rot) in sorted(positions.items()):
            fp = placed.get(ref)
            if fp is None:
                result.missing.append(ref)
                continue
            was = (fp.position.x / 1e6, fp.position.y / 1e6, float(fp.orientation.degrees))
            if (abs(was[0] - x) <= EPS_MM and abs(was[1] - y) <= EPS_MM
                    and _angle_close(was[2], rot)):
                result.unchanged.append(ref)
                continue
            # Assignment, never `fp.position.x = …`: the getter returns a copy.
            fp.position = Vector2.from_xy_mm(x, y)
            fp.orientation = Angle.from_degrees(rot)
            pending.append((ref, fp, x, y, rot))

        if pending:
            # One batched call: `update_items` takes a sequence, and a round trip per
            # footprint is the dominant cost on a board with sixty of them.
            board.update_items([item for _, item, _, _, _ in pending])

        # One fresh read for the whole batch rather than per part: a second round trip per
        # footprint is the dominant cost on a 60-part board.
        after = self.footprint_map()
        for ref, _, x, y, rot in pending:
            fp = after.get(ref)
            if fp is None:
                result.rejected[ref] = "disappeared from the board after the update"
                continue
            gx, gy = fp.position.x / 1e6, fp.position.y / 1e6
            gr = float(fp.orientation.degrees)
            if abs(gx - x) > EPS_MM or abs(gy - y) > EPS_MM or not _angle_close(gr, rot):
                result.rejected[ref] = (
                    f"wrote ({x:.4f}, {y:.4f}) @ {rot:g} deg but the board reads back "
                    f"({gx:.4f}, {gy:.4f}) @ {gr:g} deg — the edit was discarded")
                continue
            result.moved.append(ref)
        return result

    # ------------------------------------------------------------------ inspection

    def connectivity(self) -> Dict[str, List[str]]:
        """`net -> ["U1.1", …]` as the *board* sees it.

        Read from pads rather than from the net list, because a net with no pads is not a
        connection and should not look like one.  This is the actual side of the
        design-versus-board comparison in `netlist.compare_connectivity()`.
        """
        out: Dict[str, List[str]] = {}
        for fp in self.board().get_footprints():
            try:
                ref = fp.reference_field.text.value or ""
            except Exception:                                # noqa: BLE001
                continue
            for pad in fp.definition.pads:
                name = getattr(pad.net, "name", "") or ""
                if not name:
                    continue
                out.setdefault(name, []).append(f"{ref}.{pad.number}")
        return {net: sorted(pins) for net, pins in out.items()}


def _angle_close(a: float, b: float) -> bool:
    """Compare orientations on the circle: the board stores them normalised."""
    delta = abs(a - b) % 360.0
    return min(delta, 360.0 - delta) <= EPS_DEG
