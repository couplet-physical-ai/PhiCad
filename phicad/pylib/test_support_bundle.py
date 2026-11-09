#!/usr/bin/env python3
#
# This program source code file is part of PhiCad, a fork of KiCad.
#
# Copyright (C) 2026 Exowatt, Inc.
# Copyright The KiCad Developers, see AUTHORS.txt for contributors.
#
# SPDX-License-Identifier: GPL-2.0-or-later
#
# This program is free software: you can redistribute it and/or modify it
# under the terms of the GNU General Public License as published by the
# Free Software Foundation, either version 2 of the License, or (at your
# option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.

"""Privacy and CLI tests for :mod:`support_bundle`.

These tests intentionally use canaries in three different places: an allowed
configuration path and an unapproved environment value, output from fake probe
commands, and a project path/file.  The assertion scans every ZIP member rather
than just the manifest, since a privacy regression in the notice or a filename is
just as real as one in a JSON fact.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import stat
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

import support_bundle


class SupportBundleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.old_cwd = Path.cwd()

    def tearDown(self) -> None:
        os.chdir(self.old_cwd)
        self.temp.cleanup()

    @staticmethod
    def _archive_bytes(path: Path) -> bytes:
        with zipfile.ZipFile(path) as archive:
            parts = []
            for name in archive.namelist():
                parts.append(name.encode("utf-8"))
                parts.append(archive.read(name))
            return b"\n".join(parts)

    @staticmethod
    def _fake_command(directory: Path, name: str, body: str) -> None:
        path = directory / name
        path.write_text("#!/bin/sh\n" + body + "\n", encoding="utf-8")
        path.chmod(path.stat().st_mode | stat.S_IXUSR)

    def test_default_archive_is_timestamped_and_has_fixed_members(self) -> None:
        os.chdir(self.root)
        result = support_bundle.create_bundle(cwd=self.root)
        archive = Path(result["output"])
        self.assertTrue(archive.exists())
        self.assertRegex(archive.name, r"^phicad-support-bundle-\d{8}-\d{6}(?:-\d+)?\.zip$")
        with zipfile.ZipFile(archive) as opened:
            self.assertEqual(
                opened.namelist(),
                [
                    "README.txt",
                    "app.json",
                    "build.json",
                    "commands.json",
                    "doctor.json",
                    "git.json",
                    "manifest.json",
                    "system.json",
                ],
            )
            self.assertEqual(opened.getinfo("README.txt").date_time, (1980, 1, 1, 0, 0, 0))
            manifest = json.loads(opened.read("manifest.json"))
            self.assertEqual(manifest["schema_version"], support_bundle.SCHEMA_VERSION)
            self.assertFalse(manifest["privacy"]["project_contents_included"])
            app = json.loads(opened.read("app.json"))
            self.assertIn("release", app)
            self.assertIn("source_commit", app)
            self.assertIsInstance(app["source_dirty"], bool)
            self.assertIn("native_version", app)
        self.assertEqual(stat.S_IMODE(archive.stat().st_mode), 0o600)
        self.assertEqual(result["output"], archive.name)
        self.assertRegex(result["sha256"], r"^[0-9a-f]{64}$")

    def test_repeated_explicit_archives_are_byte_stable(self) -> None:
        output = self.root / "stable.zip"
        support_bundle.create_bundle(output, cwd=self.root)
        first = output.read_bytes()
        support_bundle.create_bundle(output, cwd=self.root)
        self.assertEqual(first, output.read_bytes())

    def test_project_is_metadata_only_and_secret_canaries_are_absent(self) -> None:
        project = self.root / "path-canary-secret" / "board"
        project.mkdir(parents=True)
        design = project / "board.kicad_pcb"
        design_bytes = b"DESIGN_CONTENT_CANARY must never be copied\n"
        design.write_bytes(design_bytes)
        # This file is deliberately skipped as credential-like metadata too.
        (project / ".env").write_text("ENV_FILE_CONTENT_CANARY\n", encoding="utf-8")
        output = self.root / "bundle.zip"

        with mock.patch.dict(
            os.environ,
            {
                "PHICAD_BUILD": str(self.root / "env-path-canary-secret"),
                "UNAPPROVED_CANARY": "ENV_VALUE_CANARY_BEARER",
            },
            clear=False,
        ):
            result = support_bundle.create_bundle(output, project=project, cwd=self.root)

        archive_bytes = self._archive_bytes(output)
        for canary in (
            b"DESIGN_CONTENT_CANARY",
            b"ENV_FILE_CONTENT_CANARY",
            b"ENV_VALUE_CANARY_BEARER",
            b"path-canary-secret",
            b"env-path-canary-secret",
        ):
            self.assertNotIn(canary, archive_bytes)

        with zipfile.ZipFile(output) as opened:
            self.assertIn("project.json", opened.namelist())
            metadata = json.loads(opened.read("project.json"))
        entry = next(item for item in metadata["files"] if item["name"] == "board.kicad_pcb")
        self.assertEqual(entry["size"], len(design_bytes))
        self.assertEqual(entry["sha256"], hashlib.sha256(design_bytes).hexdigest())
        self.assertEqual(metadata["file_count"], 1)
        self.assertEqual(result["manifest"]["project_metadata_member"], "project.json")

    def test_project_walk_has_independent_entry_and_directory_bounds(self) -> None:
        project = self.root / "bounded-project"
        project.mkdir()
        (project / "board.kicad_pro").write_text("{}\n", encoding="utf-8")
        for index in range(6):
            (project / ("file-%d.txt" % index)).write_text("safe\n", encoding="utf-8")

        with mock.patch.object(support_bundle, "PROJECT_ENTRY_LIMIT", 2):
            metadata = support_bundle.collect_project_metadata(project)

        self.assertEqual(metadata["file_count"], 2)
        self.assertGreaterEqual(metadata["skipped_files"], 1)
        self.assertEqual(metadata["limits"]["entries"], 2)

    def test_project_opt_in_refuses_broad_or_unrelated_directories(self) -> None:
        unrelated = self.root / "unrelated"
        unrelated.mkdir()
        (unrelated / "notes.txt").write_text("personal notes\n", encoding="utf-8")

        self.assertEqual(support_bundle.collect_project_metadata(Path("/"))["status"], "blocked")
        self.assertEqual(support_bundle.collect_project_metadata(unrelated)["status"], "blocked")

    def test_command_output_and_failure_stderr_are_not_archived(self) -> None:
        command_dir = self.root / "command-canary-path"
        command_dir.mkdir()
        command = (
            'printf "%s\\n" "COMMAND_OUTPUT_CANARY_Bearer"; '
            'printf "%s\\n" "API_KEY=COMMAND_SECRET_CANARY" >&2; exit 7'
        )
        for name in ("git", "cmake", "ninja", "clang"):
            self._fake_command(command_dir, name, command)

        output = self.root / "commands.zip"
        with mock.patch.dict(os.environ, {"PATH": str(command_dir)}, clear=False):
            support_bundle.create_bundle(output, cwd=self.root)

        archive_bytes = self._archive_bytes(output)
        self.assertNotIn(b"COMMAND_OUTPUT_CANARY", archive_bytes)
        self.assertNotIn(b"COMMAND_SECRET_CANARY", archive_bytes)
        with zipfile.ZipFile(output) as opened:
            commands = json.loads(opened.read("commands.json"))["commands"]
        git_version = next(item for item in commands if item["id"] == "git_version")
        self.assertEqual(git_version["status"], "failed")
        self.assertTrue(git_version["stderr_present"])
        self.assertNotIn("stderr", git_version)

    def test_json_mode_writes_archive_and_prints_safe_summary(self) -> None:
        output = self.root / "json-output.zip"
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            status = support_bundle.main(["--output", str(output), "--json"])
        self.assertEqual(status, 0)
        self.assertTrue(output.exists())
        summary = json.loads(stdout.getvalue())
        self.assertEqual(summary["schema_version"], support_bundle.SCHEMA_VERSION)
        self.assertEqual(summary["manifest"]["bundle_type"], support_bundle.BUNDLE_TYPE)
        self.assertNotIn("HOME", stdout.getvalue())
        self.assertNotIn("token", stdout.getvalue().lower())

    def test_identifiers_accept_only_source_hashes_and_release_tags(self) -> None:
        self.assertEqual(support_bundle._safe_identifier(b"0123456789ab\n"), "0123456789ab")
        self.assertEqual(support_bundle._safe_identifier(b"exo-v1-beta2\n"), "exo-v1-beta2")
        self.assertIsNone(support_bundle._safe_identifier(b"feat/customer-secret-board\n"))
        self.assertIsNone(support_bundle._safe_identifier(b"0123456789ab\nextra\n"))

    def test_redactor_handles_credentials_urls_controls_and_paths(self) -> None:
        text = (
            "Bearer CANARY_BEARER api_key=CANARY_API_KEY "
            "https://user:CANARY_PASSWORD@example.invalid/path?token=CANARY_QUERY\x00\n"
            "/tmp/path-canary-secret"
        )
        clean = support_bundle.sanitize_text(text)
        self.assertNotIn("CANARY", clean)
        self.assertNotIn("\x00", clean)
        self.assertIn("<REDACTED_URL>", clean)

    def test_probe_timeout_and_output_are_bounded(self) -> None:
        script = "import sys,time; sys.stdout.write('X'*1000000); sys.stdout.flush(); time.sleep(30)"
        result = support_bundle.run_command(
            [sys.executable, "-c", script], cwd=self.root, timeout=0.2, output_limit=1024
        )
        self.assertEqual(result["status"], "timeout")
        self.assertTrue(result["output_truncated"])
        self.assertNotIn("X", json.dumps(result))


if __name__ == "__main__":
    unittest.main(verbosity=2)
