#!/usr/bin/env python3
# This program source code file is part of KiCad, a free EDA CAD application.
#
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
"""Offline tests for the KLC subprocess adapter and output parser."""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import klc_check


# Captured excerpt from kicad-library-tools check_symbol.py -v (the final message's
# trailing remediation sentence is omitted). The checker uses ANSI colours, rather
# than words, to distinguish errors from warnings.
CHECKER_OUTPUT = """\
\x1b[0;32mChecking component 'Warn__S4.1__MissingPinName:Warn__S4.1__MissingPinName':\x1b[0m
\x1b[1;33m  Violating S3.1 - https://klc.kicad.org/symbol/s3/s3.1/\x1b[0m
\x1b[1;34m    Origin is centered on the middle of the symbol - S3.1 - https://klc.kicad.org/symbol/s3/s3.1/\x1b[0m
\x1b[0;31m    Symbol unit 1 not centered on origin\x1b[0m
\x1b[1;33m  Violating S4.1 - https://klc.kicad.org/symbol/s4/s4.1/\x1b[0m
\x1b[1;34m    General pin requirements - S4.1 - https://klc.kicad.org/symbol/s4/s4.1/\x1b[0m
\x1b[0;33m    Pin Pin  (3) @ (-500,0) does not have a name\x1b[0m
\x1b[1;33m  Violating S4.5 - https://klc.kicad.org/symbol/s4/s4.5/\x1b[0m
\x1b[1;34m    Pins not connected on the footprint may be omitted from the symbol - S4.5 -\x1b[0m
\x1b[1;34m    https://klc.kicad.org/symbol/s4/s4.5/\x1b[0m
\x1b[0;33m    Pins 4, 5 are missing.\x1b[0m
\x1b[1;33m  Violating S5.2 - https://klc.kicad.org/symbol/s5/s5.2/\x1b[0m
\x1b[1;34m    Footprint filters should match all appropriate footprints - S5.2 -\x1b[0m
\x1b[1;34m    https://klc.kicad.org/symbol/s5/s5.2/\x1b[0m
\x1b[0;33m    No footprint filters defined\x1b[0m
\x1b[1;33m  Violating S6.2 - https://klc.kicad.org/symbol/s6/s6.2/\x1b[0m
\x1b[1;34m    Symbol fields and metadata filled out as required - S6.2 - https://klc.kicad.org/symbol/s6/s6.2/\x1b[0m
\x1b[0;31m    Datasheet field must not be EMPTY\x1b[0m
\x1b[0;33m    Missing or empty Keywords field on 'Properties' tab.\x1b[0m
"""


class FakeRunner:
    def __init__(self, returncode: int, stdout: str = "", stderr: str = ""):
        self.result = SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)
        self.commands = []

    def __call__(self, command, **kwargs):
        self.commands.append((command, kwargs))
        return self.result


class ParserTests(unittest.TestCase):
    def test_captured_symbol_output(self):
        source = Path("lib/project.kicad_sym")
        verdict = klc_check.parse_checker_output(
            CHECKER_OUTPUT, "check_symbol.py", source, returncode=3
        )

        self.assertEqual(verdict["tool"], "check_symbol.py")
        self.assertEqual(verdict["checked"], 1)
        self.assertFalse(verdict["ok"])
        expected = [
            ("S3.1", "error", "Symbol unit 1 not centered on origin"),
            ("S4.1", "warning", "Pin Pin  (3) @ (-500,0) does not have a name"),
            ("S4.5", "warning", "Pins 4, 5 are missing."),
            ("S5.2", "warning", "No footprint filters defined"),
            ("S6.2", "error", "Datasheet field must not be EMPTY"),
            ("S6.2", "warning", "Missing or empty Keywords field on 'Properties' tab."),
        ]
        self.assertEqual(
            [
                (item["rule"], item["severity"], item["message"])
                for item in verdict["violations"]
            ],
            expected,
        )
        self.assertEqual({item["file"] for item in verdict["violations"]}, {str(source)})


class ExitCodeTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.symbol = Path(self.tempdir.name) / "project.kicad_sym"
        self.symbol.write_text("(kicad_symbol_lib)\n")

    def tearDown(self):
        self.tempdir.cleanup()

    @staticmethod
    def found(_kind):
        return Path("/fake/klc-check/check_symbol.py")

    def invoke(self, runner, finder=None):
        stdout = io.StringIO()
        stderr = io.StringIO()
        result = klc_check.main(
            [str(self.symbol), "--json"],
            runner=runner,
            finder=finder or self.found,
            out=stdout,
            err=stderr,
        )
        return result, stdout.getvalue(), stderr.getvalue()

    def test_clean_is_zero(self):
        output = "Checking component 'project:Sensor':\n"
        runner = FakeRunner(0, output)
        result, stdout, stderr = self.invoke(runner)

        self.assertEqual(result, 0)
        self.assertTrue(json.loads(stdout)["ok"])
        self.assertEqual(stderr, "")
        self.assertIn("-v", runner.commands[0][0])
        self.assertEqual(runner.commands[0][1]["capture_output"], True)

    def test_violations_are_one(self):
        result, stdout, stderr = self.invoke(FakeRunner(3, CHECKER_OUTPUT))

        self.assertEqual(result, 1)
        self.assertEqual(len(json.loads(stdout)["violations"]), 6)
        self.assertEqual(stderr, "")

    def test_missing_checker_is_two_with_one_install_command(self):
        result, stdout, stderr = self.invoke(FakeRunner(0), finder=lambda _kind: None)

        self.assertEqual(result, 2)
        self.assertEqual(stdout, "")
        self.assertEqual(stderr.count("git clone "), 1)
        self.assertIn("build/kicad-library-tools", stderr)


if __name__ == "__main__":
    unittest.main()
