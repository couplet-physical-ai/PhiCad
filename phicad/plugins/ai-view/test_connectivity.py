#!/usr/bin/env python3
#
# This program source code file is part of PhiCad, a fork of KiCad.
#
# Copyright (C) 2026 Exowatt, Inc.
# Copyright The KiCad Developers, see AUTHORS.txt for contributors.
#
# SPDX-License-Identifier: GPL-2.0-or-later

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import project_history
import web_view

# The host-terminal session bridge is not included in this build (host_bridge.py is an
# inert stand-in), so its transcript/transport/descriptor tests are not shipped either.


class IgnoreListParityTests(unittest.TestCase):
    """Two modules are named project_history; their ignore lists must not drift apart.

    This plugin ships a small self-contained git layer (it is copied into KiCad's plugin
    directory, so it cannot import from the source tree), while `phicad/pylib` has the
    richer one behind `phicad project` and the MCP journal hook. Both initialise board
    repositories, so an entry added to one and forgotten in the other means the same board
    gets a different ignore list depending on which surface touched it first -- which is how
    KiCad's `.history/` mirror, and the nested repository inside it, got committed once
    already.
    """

    #: pylib alone owns the journal/watch state; the plugin has no journal.
    PYLIB_ONLY = {".phicad/journal.jsonl", ".phicad/watch.json"}

    def _entries(self, path: Path) -> set:
        import re
        text = path.read_text(encoding="utf-8")
        match = re.search(r'_GITIGNORE_BLOCK = """(.*?)"""', text, re.S)
        self.assertIsNotNone(match, f"no _GITIGNORE_BLOCK in {path}")
        return {line.strip() for line in match.group(1).splitlines()
                if line.strip() and not line.startswith("#")}

    def test_both_project_history_modules_ignore_the_same_files(self) -> None:
        here = Path(__file__).resolve().parent
        pylib = here.parent.parent / "pylib" / "project_history.py"
        if not pylib.is_file():
            self.skipTest("phicad/pylib/project_history.py is not present")
        plugin_entries = self._entries(here / "project_history.py")
        pylib_entries = self._entries(pylib)

        missing_in_pylib = plugin_entries - pylib_entries
        self.assertEqual(missing_in_pylib, set(),
                         f"phicad/pylib/project_history.py is missing {sorted(missing_in_pylib)}")
        missing_in_plugin = pylib_entries - plugin_entries - self.PYLIB_ONLY
        self.assertEqual(missing_in_plugin, set(),
                         f"this plugin's project_history.py is missing "
                         f"{sorted(missing_in_plugin)}")

    def test_the_kicad_history_mirror_is_ignored_by_both(self) -> None:
        """The specific entry whose absence nests a foreign git repo inside a board repo."""
        here = Path(__file__).resolve().parent
        pylib = here.parent.parent / "pylib" / "project_history.py"
        for path in [here / "project_history.py"] + ([pylib] if pylib.is_file() else []):
            self.assertIn(".history/", self._entries(path),
                          f"{path} does not ignore KiCad's local-history mirror")


class WebViewTests(unittest.TestCase):
    """The browser panel: it serves a real board and can drive a real agent turn."""

    def _board(self, root: Path) -> Path:
        board = root / "panel.kicad_pcb"
        board.write_text("(kicad_pcb)\n", encoding="utf-8")
        return board

    def test_serves_only_loopback_and_mints_a_token(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            board = self._board(Path(temp))
            server, url = web_view.serve(board, port=0)
            try:
                host, port = server.server_address[:2]
                self.assertEqual(host, "127.0.0.1")
                self.assertIn(f":{port}/", url)
                self.assertIn("token=", url)
                # A guessable token would expose an agent-turn trigger to any local page.
                self.assertGreaterEqual(len(url.split("token=")[1]), 20)
            finally:
                server.server_close()

    def test_data_routes_reject_a_missing_or_wrong_token(self) -> None:
        import urllib.error
        import urllib.request

        with tempfile.TemporaryDirectory() as temp:
            board = self._board(Path(temp))
            server, url = web_view.serve(board, port=0)
            base = url.split("/?")[0]
            token = url.split("token=")[1]
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                for attempt in (f"{base}/api/status",
                                f"{base}/api/status?token=wrong"):
                    with self.assertRaises(urllib.error.HTTPError) as caught:
                        urllib.request.urlopen(attempt, timeout=10)
                    self.assertEqual(caught.exception.code, 403)

                ok = urllib.request.urlopen(
                    f"{base}/api/status?ipc=0&token={token}", timeout=30)
                payload = json.loads(ok.read().decode("utf-8"))
                self.assertTrue(payload["ok"])
                self.assertEqual(payload["board"], "panel.kicad_pcb")
                # Not connected yet, so the panel must say so rather than pretend.
                self.assertFalse(payload["bridge"]["connected"])
                self.assertNotIn("hint", payload["bridge"],
                                 "no connect command exists to suggest in this build")
            finally:
                server.shutdown()
                server.server_close()

    def test_page_allows_blob_images_so_renders_can_display(self) -> None:
        """Renders reach <img> as blob: URLs; a CSP without blob: silently shows 0x0."""
        import urllib.request

        with tempfile.TemporaryDirectory() as temp:
            board = self._board(Path(temp))
            server, url = web_view.serve(board, port=0)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                # `url` already carries the token. Fetching a bare "/" used to work only
                # because the shell page was served unauthenticated -- which leaked the token
                # it embeds (see test_web_view_auth.py). This test is about the page's CSP,
                # not its gate, so it authenticates properly.
                page = urllib.request.urlopen(url, timeout=10)
                csp = page.headers.get("Content-Security-Policy") or ""
                self.assertIn("blob:", csp)
                body = page.read().decode("utf-8")
                # The token belongs in the page (it is how the browser authenticates),
                # but nothing else secret should be.
                self.assertIn("PhiCad Agent View", body)
            finally:
                server.shutdown()
                server.server_close()

    def test_every_launch_path_resolves_to_the_board(self) -> None:
        """The plugin is launchable from pcbnew, eeschema and the project manager.

        Everything downstream is keyed off the board and derives the schematic from it, so a
        .kicad_sch or .kicad_pro argument has to resolve to the sibling board -- otherwise the
        PCB and 3D views try to load a schematic as a board and fail with 'Failed to load
        board' while the schematic view works, which looks like a broken renderer.
        """
        with tempfile.TemporaryDirectory() as temp:
            # resolve(): on macOS $TMPDIR is /var/... which is a symlink to /private/var,
            # and serve() resolves its argument.
            root = Path(temp).resolve()
            board = root / "proj.kicad_pcb"
            board.write_text("(kicad_pcb)\n", encoding="utf-8")
            (root / "proj.kicad_sch").write_text("(kicad_sch)\n", encoding="utf-8")
            (root / "proj.kicad_pro").write_text("{}\n", encoding="utf-8")

            for entry in (board, root / "proj.kicad_sch", root / "proj.kicad_pro", root):
                server, _ = web_view.serve(entry, port=0)
                try:
                    self.assertEqual(server.RequestHandlerClass.session.board, board,
                                     f"{entry.name} did not resolve to the board")
                finally:
                    server.server_close()

    def test_a_schematic_with_no_sibling_board_is_refused_clearly(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            lone = Path(temp) / "only.kicad_sch"
            lone.write_text("(kicad_sch)\n", encoding="utf-8")
            with self.assertRaises(web_view.ViewError) as caught:
                web_view.serve(lone, port=0)
            self.assertIn("no .kicad_pcb", str(caught.exception))

    def test_discovery_finds_a_single_board_in_the_directory(self) -> None:
        """A launcher entry runs with no path, so cwd discovery is the normal case."""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            board = self._board(root)
            found, source = web_view.discover_board(root)
            self.assertEqual(found, board.resolve())
            self.assertEqual(source, "cwd")

    def test_discovery_finds_a_board_one_level_down(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            nested = root / "project"
            nested.mkdir()
            board = self._board(nested)
            found, source = web_view.discover_board(root)
            self.assertEqual(found, board.resolve())
            self.assertEqual(source, "search")

    def test_discovery_refuses_rather_than_guessing_between_boards(self) -> None:
        """Opening the wrong board reads as a broken panel, not as a wrong choice.

        The user cannot tell from a render which project they got, so an ambiguous
        directory must be an error naming the candidates.
        """
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "alpha.kicad_pcb").write_text("(kicad_pcb)\n", encoding="utf-8")
            (root / "beta.kicad_pcb").write_text("(kicad_pcb)\n", encoding="utf-8")
            with self.assertRaises(web_view.ViewError) as caught:
                web_view.discover_board(root)
            message = str(caught.exception)
            self.assertIn("alpha.kicad_pcb", message)
            self.assertIn("beta.kicad_pcb", message)

    def test_discovery_says_so_when_there_is_nothing_to_open(self) -> None:
        with tempfile.TemporaryDirectory() as temp, \
                tempfile.TemporaryDirectory() as state:
            os.environ["PHICAD_WEB_VIEW_LAST_BOARD"] = str(Path(state) / "none.txt")
            try:
                with self.assertRaises(web_view.ViewError) as caught:
                    web_view.discover_board(Path(temp))
                self.assertIn("no .kicad_pcb", str(caught.exception))
            finally:
                os.environ.pop("PHICAD_WEB_VIEW_LAST_BOARD", None)

    def test_ambiguity_here_outranks_the_remembered_board(self) -> None:
        """A worktree with several boards must ask, not reuse another project's board.

        First cut had the fallback win, so launching from a KiCad checkout silently opened
        whatever board was last viewed — the panel looked like it ignored the directory.
        """
        with tempfile.TemporaryDirectory() as temp, \
                tempfile.TemporaryDirectory() as elsewhere, \
                tempfile.TemporaryDirectory() as state:
            root = Path(temp)
            (root / "one").mkdir()
            (root / "two").mkdir()
            (root / "one" / "a.kicad_pcb").write_text("(kicad_pcb)\n", encoding="utf-8")
            (root / "two" / "b.kicad_pcb").write_text("(kicad_pcb)\n", encoding="utf-8")
            os.environ["PHICAD_WEB_VIEW_LAST_BOARD"] = str(Path(state) / "last.txt")
            try:
                web_view.remember_board(self._board(Path(elsewhere)))
                with self.assertRaises(web_view.ViewError) as caught:
                    web_view.discover_board(root)
                self.assertIn("several boards", str(caught.exception))
            finally:
                os.environ.pop("PHICAD_WEB_VIEW_LAST_BOARD", None)

    def test_discovery_falls_back_to_the_remembered_board(self) -> None:
        """An empty worktree reuses the last board rather than dead-ending.

        Safe because the panel header always names the board it opened, so the fallback is
        visible; and it is consulted only when the tree gives no single answer.
        """
        with tempfile.TemporaryDirectory() as temp, \
                tempfile.TemporaryDirectory() as elsewhere, \
                tempfile.TemporaryDirectory() as state:
            board = self._board(Path(elsewhere))
            hint = Path(state) / "last.txt"
            os.environ["PHICAD_WEB_VIEW_LAST_BOARD"] = str(hint)
            try:
                web_view.remember_board(board)
                found, source = web_view.discover_board(Path(temp))
                self.assertEqual(found, board)
                self.assertEqual(source, "last-used")
            finally:
                os.environ.pop("PHICAD_WEB_VIEW_LAST_BOARD", None)

    def test_discovery_skips_build_and_vcs_trees(self) -> None:
        """A bounded walk: an unbounded one finds hundreds of qa fixtures and is slow."""
        with tempfile.TemporaryDirectory() as temp, \
                tempfile.TemporaryDirectory() as state:
            root = Path(temp)
            for noisy in (".git", "build", "node_modules", "qa"):
                (root / noisy).mkdir()
                (root / noisy / "ignored.kicad_pcb").write_text("(kicad_pcb)\n",
                                                                encoding="utf-8")
            os.environ["PHICAD_WEB_VIEW_LAST_BOARD"] = str(Path(state) / "none.txt")
            try:
                with self.assertRaises(web_view.ViewError) as caught:
                    web_view.discover_board(root)
                # Must be the "nothing here" error, NOT "several boards": if the skip list
                # stops working the walk still raises, just a different error, so asserting
                # ViewError alone would pass with the bug present.
                self.assertIn("no .kicad_pcb", str(caught.exception),
                              "boards under .git/build/node_modules/qa must be invisible")
            finally:
                os.environ.pop("PHICAD_WEB_VIEW_LAST_BOARD", None)

    def test_page_carries_the_board_path(self) -> None:
        """An unsubstituted placeholder would tell the user to run a literal __BOARD_PATH__."""
        import urllib.request

        with tempfile.TemporaryDirectory() as temp:
            board = self._board(Path(temp))
            server, url = web_view.serve(board, port=0)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                # Authenticated fetch: the token-bearing `url`, not a bare "/" (see the note
                # in test_page_allows_blob_images_so_renders_can_display).
                body = urllib.request.urlopen(url, timeout=10).read().decode("utf-8")
                self.assertNotIn("__BOARD_PATH__", body)
                self.assertNotIn("__BOARD__", body)
                self.assertIn(str(board), body)
                self.assertNotIn("agent-view connect", body,
                                 "the page must not suggest a command that does not exist")
            finally:
                server.shutdown()
                server.server_close()

    def test_chat_payload_reports_disconnected_instead_of_failing(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            board = self._board(Path(temp))
            session = web_view.Session(board=board, token="t")
            payload = web_view._chat_payload(session)
            self.assertTrue(payload["ok"])
            self.assertFalse(payload["connected"])
            self.assertEqual(payload["messages"], [])


class ProjectHistoryTests(unittest.TestCase):
    def setUp(self) -> None:
        if subprocess.run(["git", "--version"], capture_output=True).returncode != 0:
            self.skipTest("git is unavailable")

    def test_standalone_directory_initializes_and_snapshots_changes(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            parent = Path(temp)
            project = parent / "board"
            project.mkdir()
            board = project / "board.kicad_pcb"
            board.write_text("board v1\n", encoding="utf-8")
            design = parent / "source.design.json"
            design.write_text(json.dumps({"project": "board", "parts": {}, "nets": {}}),
                              encoding="utf-8")

            history = project_history.ProjectHistory(project)
            status = history.ensure(design, message="initial")
            self.assertTrue(status.enabled)
            self.assertTrue(status.clean)
            self.assertTrue((project / ".git").exists())
            self.assertTrue((project / "board.design.json").exists())
            self.assertIn(".phicad/host-bridge.json",
                          (project / ".gitignore").read_text(encoding="utf-8"))

            board.write_text("board v2\n", encoding="utf-8")
            self.assertFalse(history.autosnapshot(stable_seconds=2.0, now=10.0))
            self.assertFalse(history.autosnapshot(stable_seconds=2.0, now=11.0))
            self.assertTrue(history.autosnapshot(stable_seconds=2.0, now=12.1))
            self.assertTrue(history.status().clean)
            count = subprocess.run(
                ["git", "rev-list", "--count", "HEAD"], cwd=project,
                capture_output=True, text=True, check=True).stdout.strip()
            self.assertEqual(count, "2")

    def test_kicad_local_history_mirror_is_never_tracked(self) -> None:
        """KiCad's own `.history/` mirror keeps a nested repo; ours must ignore it.

        Left tracked, every save committed a second copy of the project plus another
        repository's internals, which is what a real board save produced before this.
        """
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp) / "board"
            project.mkdir()
            (project / "board.kicad_pcb").write_text("board v1\n", encoding="utf-8")
            history = project_history.ProjectHistory(project)
            history.ensure(message="initial")

            mirror = project / ".history"
            (mirror / ".git").mkdir(parents=True)
            (mirror / ".git" / "config").write_text("[core]\n", encoding="utf-8")
            (mirror / "board.kicad_pcb").write_text("mirrored\n", encoding="utf-8")
            (project / "board.kicad_prl").write_text("{}\n", encoding="utf-8")
            (project / "fp-info-cache").write_text("cache\n", encoding="utf-8")

            self.assertTrue(history.status().clean)
            self.assertFalse(history.snapshot("should have nothing to commit"))

    def test_board_subdir_reuses_enclosing_repo_and_scopes_commits(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            parent = Path(temp)
            subprocess.run(["git", "init", "-q", str(parent)], check=True)
            (parent / "README.md").write_text("software checkout\n", encoding="utf-8")
            project = parent / "hardware" / "board"
            project.mkdir(parents=True)
            board = project / "x.kicad_pcb"
            board.write_text("x v1\n", encoding="utf-8")

            history = project_history.ProjectHistory(project)
            history.ensure(message="baseline")

            self.assertFalse((project / ".git").exists(),
                             "Agent View must not silently nest a repository")
            self.assertEqual(history.repository_dir, parent.resolve())
            self.assertTrue(history.uses_enclosing_repository)
            tracked = subprocess.run(["git", "ls-files"], cwd=parent,
                                     capture_output=True, text=True, check=True).stdout.splitlines()
            self.assertTrue(any(path.endswith("x.kicad_pcb") for path in tracked))
            self.assertNotIn("README.md", tracked)

            (parent / "README.md").write_text("unrelated edit\n", encoding="utf-8")
            board.write_text("x v2\n", encoding="utf-8")
            self.assertTrue(history.snapshot("board edit"))
            changed = subprocess.run(
                ["git", "show", "--format=", "--name-only", "HEAD"], cwd=parent,
                capture_output=True, text=True, check=True).stdout.splitlines()
            self.assertTrue(any(path.endswith("x.kicad_pcb") for path in changed))
            self.assertNotIn("README.md", changed)

    def test_nested_existing_repo_wins_over_enclosing_repo(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            outer = Path(temp)
            subprocess.run(["git", "init", "-q", str(outer)], check=True)
            project = outer / "vendor" / "board"
            project.mkdir(parents=True)
            subprocess.run(["git", "init", "-q", str(project)], check=True)
            (project / "board.kicad_pcb").write_text("board\n", encoding="utf-8")

            history = project_history.ProjectHistory(project)
            history.ensure(message="nested baseline")

            self.assertEqual(history.repository_dir, project.resolve())
            self.assertFalse(history.uses_enclosing_repository)
            self.assertTrue((project / ".git").exists())

    def test_refuses_ambiguous_checkout_and_phi_source_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            ambiguous = root / "checkout"
            subprocess.run(["git", "init", "-q", str(ambiguous)], check=True)
            history = project_history.ProjectHistory(ambiguous)
            with self.assertRaises(project_history.HistoryError) as raised:
                history.ensure(message="must refuse")
            self.assertIn("does not identify one KiCad project", str(raised.exception))
            self.assertFalse((ambiguous / ".phicad").exists())

            source = root / "source"
            (source / "phicad" / "bin").mkdir(parents=True)
            (source / "phicad" / "bin" / "phicad").write_text("#!/bin/sh\n", encoding="utf-8")
            subprocess.run(["git", "init", "-q", str(source)], check=True)
            board = source / "demos" / "board"
            board.mkdir(parents=True)
            (board / "board.kicad_pcb").write_text("board\n", encoding="utf-8")
            with self.assertRaises(project_history.HistoryError) as raised:
                project_history.ProjectHistory(board).ensure(message="must refuse")
            self.assertIn("PhiCad source repository", str(raised.exception))
            self.assertFalse((board / ".git").exists())


if __name__ == "__main__":
    unittest.main()
