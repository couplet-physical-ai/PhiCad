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
"""Platform-independent stock footprint discovery regressions."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import library


class LibraryPaths(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def footprint(self, root):
        path = root / "Example.pretty" / "Part.kicad_mod"
        path.parent.mkdir(parents=True)
        path.write_text('(footprint "Part"\n)\n')
        return path

    def test_linux_roots_are_deterministic(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(library.sys, "platform", "linux"):
            self.assertEqual(library.Library.default().roots,
                             [Path("/usr/local/share/kicad/footprints"),
                              Path("/usr/share/kicad/footprints")])

    def test_macos_keeps_existing_stock_location(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(library.sys, "platform", "darwin"):
            self.assertEqual(library.Library.default().roots, [library.STOCK_DIR])

    def test_project_precedes_override_with_spaces_on_both_platforms(self):
        project = self.root / "project parts"
        stock = self.root / "stock parts"
        expected = self.footprint(project)
        stock_part = self.footprint(stock)
        for platform in ("linux", "darwin"):
            with self.subTest(platform=platform), patch.dict(os.environ,
                    {"PHICAD_FOOTPRINT_DIR": str(stock)}, clear=True), \
                    patch.object(library.sys, "platform", platform):
                lib = library.Library.default([project])
                self.assertEqual(lib.resolve("Example:Part").path, expected)
                self.assertEqual(library.Library.default().resolve("Example:Part").path, stock_part)

    def test_missing_override_does_not_fall_back(self):
        stock = self.root / "stock"
        self.footprint(stock)
        absent = self.root / "missing"
        with patch.dict(os.environ, {"PHICAD_FOOTPRINT_DIR": str(absent)}, clear=True), \
                patch.object(library, "STOCK_DIR", stock), patch.object(library.sys, "platform", "darwin"):
            lib = library.Library.default()
            self.assertEqual(lib.roots, [absent])
            with self.assertRaisesRegex(library.LibraryError, "PHICAD_FOOTPRINT_DIR"):
                lib.resolve("Example:Part")

    def test_missing_default_roots_report_searched_paths(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(library.sys, "platform", "linux"):
            with self.assertRaises(library.LibraryError) as caught:
                library.Library.default().resolve("NoSuchLibrary:NoSuchPart")
            self.assertIn("/usr/share/kicad/footprints/NoSuchLibrary.pretty", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
