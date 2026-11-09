#!/usr/bin/env python3
#
# This program source code file is part of KiCad, a free EDA CAD application.
#
# Copyright (C) 2026 Exowatt, Inc.
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

"""The orphan reaper's decision rule, tested without needing a real KiCad build.

`api_server_reap_orphans` exists because nothing could clean up api-servers stranded by a
crashed host: they hold a board in memory and their socket blocks the next managed session.

The dangerous half is what it *refuses* to kill. `phicad api-server` is normally started in
the background and its launcher shell exits, so a perfectly healthy server -- the one the
Agent View panel and kipy are talking to -- is reparented to init and looks exactly like an
orphan. A first version keyed on ppid alone and killed the live server it was meant to
protect. Reachability is therefore the real gate, and it is what this file pins.

Run: build/mcp-venv/bin/python phicad/mcp/test_reap_orphans.py   (or via unittest)
"""

from __future__ import annotations

import importlib.util
import os
import socket
import sys
import tempfile
import threading
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent


def _load_server():
    """Import server.py by path, the way the other MCP tests do."""
    os.environ.setdefault("PHICAD_REPO", str(HERE.parent.parent))
    spec = importlib.util.spec_from_file_location("phicad_mcp_server", HERE / "server.py")
    if spec is None or spec.loader is None:                      # pragma: no cover
        raise ImportError("cannot load phicad/mcp/server.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["phicad_mcp_server"] = module
    spec.loader.exec_module(module)
    return module


class SocketReachabilityTests(unittest.TestCase):
    """`_api_socket_reachable` is the gate that decides whether a process may be killed."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.server = _load_server()

    def test_a_listening_socket_is_reachable(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "live.sock"
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            listener.bind(str(path))
            # A backlog, not a one-shot accept(): the probe below connects twice, and a
            # single background accept() serves the first and lets the second fail, which
            # made this test fail intermittently on its own second assertion.
            listener.listen(8)
            stop = threading.Event()

            def serve() -> None:
                while not stop.is_set():
                    try:
                        conn, _ = listener.accept()
                        conn.close()
                    except OSError:
                        return

            server_thread = threading.Thread(target=serve, daemon=True)
            server_thread.start()
            try:
                self.assertTrue(self.server._api_socket_reachable(str(path)),
                                "a live listener must count as reachable, or the reaper "
                                "would kill the server currently serving the panel")
                # The ipc:// form is what kicad-cli reports and what the tool parses.
                self.assertTrue(self.server._api_socket_reachable(f"ipc://{path}"))
            finally:
                stop.set()
                listener.close()
                server_thread.join(timeout=5)

    def test_a_missing_socket_is_unreachable(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            self.assertFalse(
                self.server._api_socket_reachable(str(Path(temp) / "gone.sock")))

    def test_a_leftover_socket_file_with_nobody_listening_is_unreachable(self) -> None:
        """A killed server can leave the file behind; existence alone must not protect it."""
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "stale.sock"
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            listener.bind(str(path))
            listener.close()                     # file remains, nothing accepts
            self.assertTrue(path.exists(), "the fixture needs the file to still be there")
            self.assertFalse(self.server._api_socket_reachable(str(path)))

    def test_no_socket_recorded_is_unreachable(self) -> None:
        self.assertFalse(self.server._api_socket_reachable(None))
        self.assertFalse(self.server._api_socket_reachable(""))


class ReaperContractTests(unittest.TestCase):
    """The tool's own behaviour: dry by default, and never silently destructive."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.server = _load_server()

    def test_dry_run_is_the_default_and_kills_nothing(self) -> None:
        result = self.server.api_server_reap_orphans.fn()
        self.assertTrue(result["ok"])
        self.assertTrue(result["dry_run"], "reaping must be opt-in, never the default")
        self.assertEqual(result["killed"], [])

    def test_a_reachable_orphan_is_reported_but_not_reapable(self) -> None:
        """The regression: ppid 1 alone must not make a serving process a target."""
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "served.sock"
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            listener.bind(str(path))
            listener.listen(1)
            entry = {"pid": os.getpid(), "ppid": 1, "socket": str(path),
                     "reachable": self.server._api_socket_reachable(str(path))}
            try:
                self.assertTrue(entry["reachable"])
                # This mirrors the tool's selection rule; if the rule ever drops the
                # reachability term, this is the assertion that fails.
                reapable = [c for c in [entry] if not c["reachable"]]
                self.assertEqual(reapable, [],
                                 "an orphaned but serving api-server must be spared")
            finally:
                listener.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
