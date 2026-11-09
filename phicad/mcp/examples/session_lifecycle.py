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
"""Headless PhiCad session lifecycle — the reusable part of every kipy recipe.

WHY THIS EXISTS
    An agent should drive PhiCad in the background, not take over the user's screen.
    `kicad-cli api-server` is a fully headless IPC server: no GUI, no window, no focus
    stealing. This module owns the fiddly parts of talking to it so recipes stay short.

WHAT IT HANDLES (each one is a real trap, learned the hard way)
    1. The socket FILE APPEARS BEFORE THE SERVER IS READY. You must poll for the socket
       and THEN retry ping() in a loop. Connecting on first sight of the socket fails.
    2. NEVER call KiCad().check_version(). kipy 0.8.0 is generated against KiCad 10.0.6
       and raises on this fork's 10.99 version string. get_version() is fine and useful.
    3. kipy has NO WRAPPER for the `ReferencePoint` proto message (fork/upstream 10.x
       `PCB_POINT`, serialized by pcbnew/pcb_point.cpp). Any footprint containing a
       `(point ...)` child — e.g. demos/pic_programmer, which has 7 on F.Fab — makes
       board.get_footprints() die with
           NotImplementedError: Missing type mapping for
           type.googleapis.com/kiapi.board.types.ReferencePoint
       phicad/bin/regen-kipy-proto regenerates kipy's *_pb2.py bindings, but the
       type->wrapper maps (kipy/util/proto.py:_any_urls and
       kipy/board_types.py:_proto_to_object) are HAND-WRITTEN and stay stale.
       install_fork_compat() registers the missing wrapper at runtime.
    4. The server allows only ONE DOCUMENT PER TYPE at a time. Opening a second .kicad_pcb
       fails with "a document of this type is already open", so verifying a save by
       re-reading the file requires a SECOND, FRESH server (see session_edit_track.py).
    5. Opening a board REWRITES it on save to this fork's file version (20260831) and
       rewrites its .kicad_pro. Stable KiCad 10.0 then refuses the file. Always work on a
       copy: stage_fixture() exists so no recipe ever touches demos/ or qa/ in place.

USE AS A LIBRARY
    from session_lifecycle import HeadlessSession, stage_fixture
    work = stage_fixture("demos/pic_programmer/pic_programmer.kicad_pcb")
    with HeadlessSession() as sess:            # spawns + reaps the server
        board = sess.open_board(work)
        ...
        board.save()

USE AS A CLI (smoke test of the whole lifecycle)
    build/venv/bin/python phicad/mcp/examples/session_lifecycle.py \
        --board demos/pic_programmer/pic_programmer.kicad_pcb

    --attach ipc:///tmp/kicad/api.sock   reuse a server someone else started
    --keep-temp                          keep the staged copy for inspection

Verified against build/mac-dev, PhiCad 10.99.0 (the fork reports 10.99.x; the exact git
describe suffix moves with the build).
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Tree layout.  This file lives at phicad/mcp/examples/, so the repo root is
# three parents up.  Everything is resolved absolutely: the server resolves
# relative paths against its own cwd, which is never what a caller means.
# ---------------------------------------------------------------------------
HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[2]
KICAD_CLI = REPO_ROOT / "build/mac-dev/kicad/PhiCad.app/Contents/MacOS/kicad-cli"
VENV_PY = REPO_ROOT / "build/venv/bin/python"
SOCKET_DIR = Path("/tmp/kicad")

# Files that belong to the same document and must travel with a staged board.
FIXTURE_SIBLINGS = (".kicad_pro", ".kicad_prl", ".kicad_sch", ".kicad_dru")


class SessionError(RuntimeError):
    """Raised with an operator-readable message; callers turn it into exit != 0."""


# ---------------------------------------------------------------------------
# Fork compatibility shim
# ---------------------------------------------------------------------------

_COMPAT_INSTALLED = False
_REFERENCE_POINT_URL = "type.googleapis.com/kiapi.board.types.ReferencePoint"


def install_fork_compat() -> bool:
    """Teach kipy about proto messages this fork sends but kipy cannot unwrap.

    Returns True if a shim was added, False if kipy already knew everything
    (i.e. a future kipy release fixed it and this can be deleted).

    Only `ReferencePoint` (PCB_POINT) is missing as of kipy 0.8.0. Without this,
    get_footprints() raises NotImplementedError on any board whose footprints
    contain `(point ...)` children — and the failure looks like a transport bug,
    not a missing wrapper, which is why it costs so much time to diagnose.
    """
    global _COMPAT_INSTALLED
    if _COMPAT_INSTALLED:
        return False

    from kipy import board_types as _bt
    from kipy.proto.board import board_types_pb2
    from kipy.util import proto as _kproto

    added = False

    if _REFERENCE_POINT_URL not in _kproto._any_urls:

        class ReferencePoint(_bt.BoardItem):
            """Minimal read-only wrapper for kiapi.board.types.ReferencePoint.

            Enough to let unwrap() succeed so footprints holding points can be
            read. Position/size stay reachable through .proto for anyone who
            needs them.
            """

            def __init__(self, proto=None, proto_ref=None):
                self._proto = (
                    proto_ref if proto_ref is not None else board_types_pb2.ReferencePoint()
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

        _kproto._any_urls[_REFERENCE_POINT_URL] = board_types_pb2.ReferencePoint
        _bt._proto_to_object[board_types_pb2.ReferencePoint] = ReferencePoint
        added = True

    _COMPAT_INSTALLED = True
    return added


# ---------------------------------------------------------------------------
# Fixture staging — never mutate the repo
# ---------------------------------------------------------------------------

def stage_fixture(board_path: str | Path, keep: bool = False) -> Path:
    """Copy a board plus its project siblings into a fresh temp dir; return the copy.

    Opening + saving a board upgrades it to this fork's file version and rewrites
    its .kicad_pro, so recipes must never point a session at a tracked file.
    """
    src = Path(board_path).expanduser()
    if not src.is_absolute():
        src = (REPO_ROOT / src).resolve()
    if not src.is_file():
        raise SessionError(f"fixture board not found: {src}")

    work_dir = Path(tempfile.mkdtemp(prefix="phicad-session-"))
    dest = work_dir / src.name
    shutil.copy2(src, dest)
    for suffix in FIXTURE_SIBLINGS:
        sibling = src.with_suffix(suffix)
        if sibling.is_file():
            shutil.copy2(sibling, work_dir / sibling.name)
    if not keep:
        # Leave a breadcrumb so an operator can tell these dirs are disposable.
        (work_dir / "STAGED_COPY_SAFE_TO_DELETE").write_text(f"copied from {src}\n")
    return dest


# ---------------------------------------------------------------------------
# Server lifecycle
# ---------------------------------------------------------------------------

def wait_for_socket(socket_path: Path, timeout: float = 30.0) -> None:
    """Block until the server creates its socket file (it does so before it is ready)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if socket_path.exists():
            return
        time.sleep(0.1)
    raise SessionError(f"socket never appeared after {timeout:.0f}s: {socket_path}")


def connect_with_retry(socket_url: str, timeout: float = 60.0, delay: float = 0.4):
    """Return a live kipy.KiCad, retrying while the server reports AS_NOT_READY.

    Deliberately does NOT call check_version(): kipy is generated against 10.0.6 and
    raises against this fork's 10.99.
    """
    from kipy import KiCad

    install_fork_compat()
    os.environ["KICAD_API_SOCKET"] = socket_url

    deadline = time.monotonic() + timeout
    last: Optional[BaseException] = None
    while time.monotonic() < deadline:
        try:
            kicad = KiCad()
            kicad.ping()  # raises until the server is actually serving
            return kicad
        except Exception as exc:  # noqa: BLE001 - transport errors are all retryable here
            last = exc
            time.sleep(delay)
    raise SessionError(f"server on {socket_url} never became ready: {last}")


class HeadlessSession:
    """Context manager owning one `kicad-cli api-server` process.

    Guarantees, via try/finally in __exit__, that the child is reaped and the socket
    file removed even when the body raises. A leaked server holds its document open
    and the next run fails with "a document of this type is already open".

    Pass attach=<ipc url> to reuse an existing server; then nothing is spawned and
    nothing is torn down.
    """

    def __init__(
        self,
        attach: Optional[str] = None,
        name: Optional[str] = None,
        ready_timeout: float = 60.0,
        log_path: Optional[Path] = None,
    ):
        self.attach = attach
        self.ready_timeout = ready_timeout
        self.proc: Optional[subprocess.Popen] = None
        self.kicad = None
        self.owns_server = attach is None
        self._opened: list[tuple[str, Path]] = []

        if self.owns_server:
            tag = name or f"session-{os.getpid()}-{uuid.uuid4().hex[:6]}"
            self.socket_path = SOCKET_DIR / f"{tag}.sock"
            self.socket_url = f"ipc://{self.socket_path}"
            self.log_path = log_path or Path(tempfile.gettempdir()) / f"phicad-{tag}.log"
        else:
            self.socket_url = attach
            self.socket_path = Path(attach.replace("ipc://", ""))
            self.log_path = None

    # -- plumbing ----------------------------------------------------------
    def _spawn(self) -> None:
        if not KICAD_CLI.is_file():
            raise SessionError(
                f"kicad-cli not built at {KICAD_CLI}\n"
                f"  build it with: phicad/bin/phicad build kicad-cli"
            )
        SOCKET_DIR.mkdir(parents=True, exist_ok=True)
        if self.socket_path.exists():
            self.socket_path.unlink()

        env = dict(os.environ)
        env["KICAD_RUN_FROM_BUILD_DIR"] = "1"
        self._log_fh = open(self.log_path, "w")  # noqa: SIM115 - lifetime is the session
        # start_new_session so terminate() cannot reach back into our own group.
        self.proc = subprocess.Popen(
            [str(KICAD_CLI), "api-server", "--socket", str(self.socket_path)],
            stdout=self._log_fh,
            stderr=subprocess.STDOUT,
            env=env,
            start_new_session=True,
        )

    def _server_log_tail(self, lines: int = 12) -> str:
        if not self.log_path or not Path(self.log_path).is_file():
            return "(no server log)"
        tail = Path(self.log_path).read_text(errors="replace").splitlines()[-lines:]
        return "\n".join(f"    | {ln}" for ln in tail) or "    | (empty)"

    # -- context manager ---------------------------------------------------
    def __enter__(self) -> "HeadlessSession":
        try:
            if self.owns_server:
                self._spawn()
                wait_for_socket(self.socket_path, timeout=self.ready_timeout)
                if self.proc and self.proc.poll() is not None:
                    raise SessionError(
                        f"server exited immediately (code {self.proc.returncode}):\n"
                        f"{self._server_log_tail()}"
                    )
            self.kicad = connect_with_retry(self.socket_url, timeout=self.ready_timeout)
            return self
        except BaseException:
            # Never leave a half-started server behind.
            self.close()
            raise

    def __exit__(self, exc_type, exc, tb) -> bool:
        self.close()
        return False  # never swallow the caller's exception

    def close(self) -> None:
        """Reap the server and delete the socket. Safe to call twice."""
        try:
            if self.owns_server and self.proc is not None and self.proc.poll() is None:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
                    self.proc.wait(timeout=5)
        finally:
            fh = getattr(self, "_log_fh", None)
            if fh is not None and not fh.closed:
                fh.close()
            if self.owns_server and self.socket_path.exists():
                try:
                    self.socket_path.unlink()
                except OSError:
                    pass

    # -- document operations ----------------------------------------------
    def open_board(self, board_path: str | Path):
        """OpenDocument(DOCTYPE_PCB) over IPC, then return the kipy Board.

        Requires an ABSOLUTE path — the server resolves relative paths against its own
        cwd. Only possible because phicad/bin/regen-kipy-proto rebuilt kipy's bindings
        from this fork's api/proto; stock kipy cannot construct OpenDocument.
        """
        from kipy.proto.common.commands.project_commands_pb2 import (
            OpenDocument,
            OpenDocumentResponse,
        )
        from kipy.proto.common.types.base_types_pb2 import DOCTYPE_PCB

        path = Path(board_path).expanduser()
        if not path.is_absolute():
            path = (REPO_ROOT / path).resolve()
        if not path.is_file():
            raise SessionError(f"board not found: {path}")

        request = OpenDocument()
        request.type = DOCTYPE_PCB
        request.path = str(path)
        try:
            self.kicad._client.send(request, OpenDocumentResponse)
        except Exception as exc:  # noqa: BLE001
            msg = str(exc)
            if "already open" in msg:
                raise SessionError(
                    f"the server already has a PCB open, so it refused {path.name}.\n"
                    f"  One document per type per server: start a SECOND HeadlessSession\n"
                    f"  to read a file back. ({msg})"
                ) from exc
            raise SessionError(f"OpenDocument failed for {path}: {msg}") from exc

        self._opened.append(("pcb", path))
        board = self.kicad.get_board()
        install_fork_compat()
        return board

    @staticmethod
    def save(board) -> None:
        """Persist the open board.

        board.save() sends kiapi.common.commands.SaveDocument (declared in
        project_commands.proto and re-exported by editor_commands_pb2 — the same
        message, so either import path dispatches correctly). Its DocumentSpecifier
        needs document.type plus a BASENAME in document.board_filename; after an IPC
        OpenDocument the server already reports the basename, so plain board.save()
        is correct and no hand-built proto is needed.
        """
        board.save()

    @property
    def opened(self) -> list[tuple[str, Path]]:
        """Our own bookkeeping of opened documents (GetOpenDocuments is not served here)."""
        return list(self._opened)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Smoke-test the headless PhiCad session lifecycle.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Run with build/venv/bin/python (that venv has kipy).",
    )
    parser.add_argument(
        "--board",
        default="demos/pic_programmer/pic_programmer.kicad_pcb",
        help="board to open; relative paths resolve against the repo root "
             "(default: %(default)s)",
    )
    parser.add_argument(
        "--attach",
        metavar="IPC_URL",
        help="reuse an already-running server (e.g. ipc:///tmp/kicad/api.sock) "
             "instead of spawning one",
    )
    parser.add_argument(
        "--in-place",
        action="store_true",
        help="DANGEROUS: open the board where it lies instead of a temp copy. "
             "Opening rewrites the file to this fork's version.",
    )
    parser.add_argument("--keep-temp", action="store_true",
                        help="do not delete the staged copy on success")
    parser.add_argument("--no-open", action="store_true",
                        help="with --attach, report the document already open instead of "
                             "opening one (a server holds one document per type)")
    parser.add_argument("--timeout", type=float, default=60.0,
                        help="seconds to wait for server readiness (default: %(default)s)")
    args = parser.parse_args()

    work_dir: Optional[Path] = None
    try:
        if args.in_place:
            board_path = Path(args.board)
            if not board_path.is_absolute():
                board_path = (REPO_ROOT / board_path).resolve()
            print(f"[stage]  IN-PLACE (will be rewritten): {board_path}")
        else:
            board_path = stage_fixture(args.board, keep=args.keep_temp)
            work_dir = board_path.parent
            print(f"[stage]  copied fixture -> {board_path}")

        step = "attach" if args.attach else "spawn"
        print(f"[{step}]  {'reusing' if args.attach else 'starting'} headless api-server")

        with HeadlessSession(attach=args.attach, ready_timeout=args.timeout) as sess:
            print(f"[ready]  socket   {sess.socket_url}")
            if sess.proc is not None:
                print(f"[ready]  server   pid {sess.proc.pid}, log {sess.log_path}")
            shim = "installed (ReferencePoint)" if _COMPAT_INSTALLED else "none"
            print(f"[ready]  compat   {shim}")
            print(f"[ready]  version  {sess.kicad.get_version()}   "
                  f"(check_version() is never called - it rejects 10.99)")

            if args.no_open:
                board = sess.kicad.get_board()
                print(f"[reuse]  document already open: {board.name}")
            else:
                board = sess.open_board(board_path)
                print(f"[open]   {board.name}")
            print(f"[open]   footprints={len(board.get_footprints())} "
                  f"tracks={len(board.get_tracks())} "
                  f"vias={len(board.get_vias())} nets={len(board.get_nets())}")

            if args.no_open:
                print("[save]   skipped (not our document)")
            else:
                sess.save(board)
                print("[save]   SaveDocument ok")
            print(f"[docs]   opened by us: {[(t, p.name) for t, p in sess.opened]}")

        print("[stop]   server terminated, socket removed")
        if work_dir and not args.keep_temp:
            shutil.rmtree(work_dir, ignore_errors=True)
            print(f"[clean]  removed {work_dir}")
        elif work_dir:
            print(f"[clean]  kept {work_dir}")
        print("\nOK: lifecycle completed")
        return 0

    except SessionError as exc:
        print(f"\nFAIL: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nFAIL: interrupted", file=sys.stderr)
        return 130
    except Exception as exc:  # noqa: BLE001
        print(f"\nFAIL: unexpected {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
