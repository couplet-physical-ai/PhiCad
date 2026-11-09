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
"""Regression tests for source and native version reporting."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


HERE = Path(__file__).resolve().parent
WRAPPER = HERE / "phicad"


class VersionCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "phicad/bin").mkdir(parents=True)
        shutil.copy2(WRAPPER, self.root / "phicad/bin/phicad")
        subprocess.run(["git", "init", "-q", "-b", "dev"], cwd=self.root, check=True)
        (self.root / "README").write_text("fixture\n", encoding="utf-8")
        (self.root / ".gitignore").write_text("/build/\n", encoding="utf-8")
        subprocess.run(
            ["git", "add", "README", ".gitignore", "phicad/bin/phicad"],
            cwd=self.root,
            check=True,
        )
        subprocess.run(
            [
                "git",
                "-c",
                "user.name=PhiCad Test",
                "-c",
                "user.email=phicad-test@example.invalid",
                "commit",
                "-q",
                "-m",
                "fixture",
            ],
            cwd=self.root,
            check=True,
        )
        subprocess.run(["git", "tag", "exo-v1-test"], cwd=self.root, check=True)
        self.build = self.root / "build/mac-dev"
        self.cli = self.build / "kicad/PhiCad.app/Contents/MacOS/kicad-cli"
        self.cli.parent.mkdir(parents=True)
        self.cli.write_text(
            "#!/bin/sh\n[ \"$#\" -eq 1 ] && [ \"$1\" = version ] || exit 64\n"
            "printf '10.99.0-phicad\\n'\n",
            encoding="utf-8",
        )
        self.cli.chmod(0o755)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def run_version(self, *args: str) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env.pop("PHICAD_KICAD_CLI", None)
        env["PHICAD_BUILD"] = str(self.build)
        return subprocess.run(
            [str(self.root / "phicad/bin/phicad"), *args],
            cwd=self.root,
            env=env,
            capture_output=True,
            text=True,
        )

    def test_version_reports_release_commit_native_and_clean_state(self) -> None:
        result = self.run_version("version")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("PhiCad exo-v1-test", result.stdout)
        self.assertRegex(result.stdout, r"commit\s+: [0-9a-f]{12}")
        self.assertIn("native     : 10.99.0-phicad", result.stdout)
        self.assertIn("state      : clean", result.stdout)

    def test_dash_dash_version_is_the_same_public_command(self) -> None:
        named = self.run_version("version")
        flag = self.run_version("--version")
        self.assertEqual(flag.returncode, 0, flag.stderr)
        self.assertEqual(flag.stdout, named.stdout)

    def test_json_is_machine_readable(self) -> None:
        result = self.run_version("version", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report["schema_version"], 1)
        self.assertEqual(report["product"], "PhiCad")
        self.assertEqual(report["release"], "exo-v1-test")
        self.assertEqual(report["native_version"], "10.99.0-phicad")
        self.assertFalse(report["dirty"])
        self.assertRegex(report["commit"], r"^[0-9a-f]{12}$")

    def test_dirty_source_and_unbuilt_native_are_explicit(self) -> None:
        self.cli.unlink()
        (self.root / "README").write_text("changed\n", encoding="utf-8")
        result = self.run_version("version", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertTrue(report["dirty"])
        self.assertIsNone(report["native_version"])

    def test_untagged_commit_is_unreleased(self) -> None:
        (self.root / "NEXT").write_text("next\n", encoding="utf-8")
        subprocess.run(["git", "add", "NEXT"], cwd=self.root, check=True)
        subprocess.run(
            [
                "git",
                "-c",
                "user.name=PhiCad Test",
                "-c",
                "user.email=phicad-test@example.invalid",
                "commit",
                "-q",
                "-m",
                "next",
            ],
            cwd=self.root,
            check=True,
        )
        result = self.run_version("version", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report["release"], "unreleased")


if __name__ == "__main__":
    unittest.main(verbosity=2)
