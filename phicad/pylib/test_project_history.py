#!/usr/bin/env python3
#
# This program source code file is part of PhiCad, a fork of KiCad.
#
# Copyright (C) 2026 Exowatt, Inc.
# Copyright The KiCad Developers, see AUTHORS.txt for contributors.
#
# SPDX-License-Identifier: GPL-2.0-or-later
#
"""Hermetic repository-discovery tests for the shared project-history layer."""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

import project_history


class MergeDriverResourceTests(unittest.TestCase):
    def test_build_driver_migrates_only_generated_commands(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            project = root / "board"
            project.mkdir()
            (project / "b.kicad_pcb").write_text("board\n")
            subprocess.run(["git", "init", "-q", str(project)], check=True)
            cli = root / "build with spaces" / "kicad" / "kicad-cli"
            cli.parent.mkdir(parents=True)
            cli.touch()
            history = project_history.ProjectHistory(project)
            history._merge_driver_config(cli)
            key = "merge.kicad-pcb.driver"

            def value(name):
                return subprocess.check_output(
                    ["git", "-C", str(project), "config", "--get", name], text=True).strip()

            self.assertFalse(value(key).startswith("env "))
            (cli.parent.parent / "CMakeCache.txt").touch()
            (cli.parent.parent / "pcbnew").mkdir()
            subprocess.run(["git", "-C", str(project), "config", "merge.kicad-sch.driver",
                            "custom-driver %O %A %B"], check=True)
            changed = history._merge_driver_config(cli)
            self.assertIn(key, changed)
            self.assertTrue(value(key).startswith("env KICAD_RUN_FROM_BUILD_DIR=1 "))
            self.assertIn('"' + str(cli) + '"', value(key))
            self.assertEqual(value("merge.kicad-sch.driver"), "custom-driver %O %A %B")
            self.assertTrue(value("mergetool.kicad.cmd").startswith("env KICAD_RUN_FROM_BUILD_DIR=1 "))
            self.assertEqual(history._merge_driver_config(cli), [])


class ProjectHistoryLocationTests(unittest.TestCase):
    @staticmethod
    def _init_git(path: Path) -> None:
        subprocess.run(["git", "init", "-q", str(path)], check=True)

    def test_standalone_directory_initializes_at_project_root(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp) / "board"
            project.mkdir()
            (project / "board.kicad_pcb").write_text("board\n", encoding="utf-8")

            history = project_history.ProjectHistory(project)
            status = history.ensure(message="standalone baseline")

            self.assertTrue(status.enabled)
            self.assertEqual(history.repository_dir, project.resolve())
            self.assertFalse(history.uses_enclosing_repository)
            self.assertTrue((project / ".git").is_dir())

    def test_board_subdirectory_reuses_enclosing_repo_and_scopes_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            checkout = Path(temp)
            self._init_git(checkout)
            (checkout / "README.md").write_text("unrelated\n", encoding="utf-8")
            project = checkout / "hardware" / "board"
            project.mkdir(parents=True)
            board = project / "board.kicad_pcb"
            board.write_text("v1\n", encoding="utf-8")

            history = project_history.ProjectHistory(project)
            history.ensure(message="monorepo baseline")
            self.assertFalse((project / ".git").exists())
            self.assertEqual(history.repository_dir, checkout.resolve())

            board.write_text("v2\n", encoding="utf-8")
            (checkout / "README.md").write_text("unrelated edit\n", encoding="utf-8")
            snapshot = history.snapshot("board edit")
            self.assertTrue(snapshot["committed"])
            changed = subprocess.run(
                ["git", "show", "--format=", "--name-only", "HEAD"], cwd=checkout,
                capture_output=True, text=True, check=True).stdout.splitlines()
            self.assertIn("hardware/board/board.kicad_pcb", changed)
            self.assertNotIn("README.md", changed)

    def test_existing_nested_repo_is_selected_over_outer_repo(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            outer = Path(temp)
            self._init_git(outer)
            project = outer / "nested" / "board"
            project.mkdir(parents=True)
            self._init_git(project)
            (project / "board.kicad_pcb").write_text("board\n", encoding="utf-8")

            history = project_history.ProjectHistory(project)
            history.ensure(message="nested baseline")

            self.assertEqual(history.repository_dir, project.resolve())
            self.assertFalse(history.uses_enclosing_repository)

    def test_refuses_ambiguous_checkout_and_source_tree(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            checkout = root / "checkout"
            checkout.mkdir()
            self._init_git(checkout)
            with self.assertRaises(project_history.HistoryError) as raised:
                project_history.ProjectHistory(checkout).ensure()
            self.assertIn("does not identify one KiCad project", str(raised.exception))
            self.assertFalse((checkout / ".gitignore").exists())

            source = root / "source"
            (source / "phicad" / "bin").mkdir(parents=True)
            (source / "phicad" / "bin" / "phicad").write_text("source\n", encoding="utf-8")
            self._init_git(source)
            board = source / "demos" / "board"
            board.mkdir(parents=True)
            (board / "board.kicad_pcb").write_text("board\n", encoding="utf-8")
            with self.assertRaises(project_history.HistoryError) as raised:
                project_history.ProjectHistory(board).ensure()
            self.assertIn("PhiCad source repository", str(raised.exception))
            self.assertFalse((board / ".git").exists())

    def test_restore_refuses_dirty_worktree_and_preserves_user_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp) / "board"
            project.mkdir()
            board = project / "board.kicad_pcb"
            board.write_text("revision one\n", encoding="utf-8")
            history = project_history.ProjectHistory(project)
            history.ensure(message="baseline")
            baseline = subprocess.run(
                ["git", "rev-list", "--max-parents=0", "HEAD"], cwd=project,
                capture_output=True, text=True, check=True).stdout.strip()

            board.write_text("revision two\n", encoding="utf-8")
            self.assertTrue(history.snapshot("committed edit"))
            board.write_text("UNSAVED-USER-WORK\n", encoding="utf-8")
            before = board.read_bytes()

            dry = history.restore(baseline, paths=[board], dry_run=True)
            self.assertTrue(dry["ok"])
            self.assertEqual(dry["would_change"], ["board.kicad_pcb"])
            self.assertEqual(board.read_bytes(), before)

            result = history.restore(baseline, paths=[board], dry_run=False)
            self.assertFalse(result["ok"])
            self.assertIn("working tree is dirty", result["error"])
            self.assertEqual(board.read_bytes(), before)

    def test_restore_rejects_wholly_outside_path_list(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp) / "board"
            project.mkdir()
            board = project / "board.kicad_pcb"
            schematic = project / "support.kicad_sch"
            board.write_text("board revision one\n", encoding="utf-8")
            schematic.write_text("schematic revision one\n", encoding="utf-8")
            history = project_history.ProjectHistory(project)
            history.ensure(message="baseline")
            baseline = subprocess.run(
                ["git", "rev-list", "--max-parents=0", "HEAD"], cwd=project,
                capture_output=True, text=True, check=True).stdout.strip()

            board.write_text("board revision two\n", encoding="utf-8")
            schematic.write_text("schematic revision two\n", encoding="utf-8")
            self.assertTrue(history.snapshot("committed edit"))
            before = {"board": board.read_bytes(), "schematic": schematic.read_bytes()}
            outside = Path(temp) / "not-in-project.kicad_pcb"

            dry = history.restore(baseline, paths=[outside], dry_run=True)
            self.assertFalse(dry["ok"])
            self.assertIn("outside the project", dry["error"])
            self.assertEqual({"board": board.read_bytes(), "schematic": schematic.read_bytes()}, before)

            result = history.restore(baseline, paths=[outside], dry_run=False)
            self.assertFalse(result["ok"])
            self.assertIn("outside the project", result["error"])
            self.assertEqual({"board": board.read_bytes(), "schematic": schematic.read_bytes()}, before)


if __name__ == "__main__":
    unittest.main()
