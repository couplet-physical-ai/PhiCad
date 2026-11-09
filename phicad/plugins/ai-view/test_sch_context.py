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
"""Live schematic facts: the sch_api plumbing and how the context renders them.

Two layers, tested separately so CI needs neither a gateway nor a running
editor:

  * sch_api's descriptor-pool mechanics — loading the fork descriptor set,
    building fork messages by full name, echoing a document specifier across
    pools byte-for-byte.  Skipped when no descriptor set exists (CI runners
    have no venv); `phicad venv` + this suite locally covers it.
  * BoardContext rendering of sch_nets/sch_sheets — pure data, always runs:
    both renderers show live schematic facts when present and omit the
    section entirely when absent (absence of facts, not fabricated zeros).
"""

from __future__ import annotations

import unittest
from pathlib import Path

import board_context


def _desc_present() -> bool:
    try:
        import sys
        sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "pylib"))
        import sch_api
        return sch_api.available()
    except Exception:
        return False


class RenderingTests(unittest.TestCase):
    def _ctx(self) -> board_context.BoardContext:
        ctx = board_context.BoardContext()
        ctx.host = "schematic"
        ctx.project_name = "amp"
        ctx.sch_sheets = [
            {"name": "amp", "filename": "amp.kicad_sch", "page": "1", "depth": 0},
            {"name": "psu", "filename": "psu.kicad_sch", "page": "2", "depth": 1},
        ]
        ctx.sch_nets = [
            {"name": "+12V", "members": 28, "sheets": 3},
            {"name": "GND", "members": 40, "sheets": 3},
        ]
        return ctx

    def test_summary_shows_live_schematic_section(self) -> None:
        text = self._ctx().summary_text()
        self.assertIn("Live schematic", text)
        self.assertIn("+12V", text)
        self.assertIn("psu.kicad_sch", text)
        # the child sheet is indented under its parent
        self.assertRegex(text, r"\n\s+psu\s")

    def test_digest_shows_nets_and_hierarchy(self) -> None:
        digest = self._ctx().ai_digest()
        self.assertIn("Live schematic hierarchy (2 sheet instances", digest)
        self.assertIn("Live schematic nets (2 total", digest)
        self.assertIn("+12V | 28 | 3", digest)

    def test_absent_facts_render_nothing(self) -> None:
        ctx = board_context.BoardContext()
        ctx.host = "schematic"
        self.assertNotIn("Live schematic", ctx.summary_text())
        self.assertNotIn("Live schematic", ctx.ai_digest())


@unittest.skipUnless(_desc_present(), "no fork descriptor set (run: phicad venv)")
class DescriptorPoolTests(unittest.TestCase):
    """Message mechanics against the real compiled descriptor set."""

    @classmethod
    def setUpClass(cls) -> None:
        import sch_api
        cls.sch_api = sch_api

    def test_fork_messages_resolve_by_full_name(self) -> None:
        pool = self.sch_api._Pool.get()
        for name in ("kiapi.schematic.commands.GetSchematicNetlist",
                     "kiapi.schematic.commands.SchematicNetlistResponse",
                     "kiapi.schematic.commands.GetSchematicHierarchy",
                     "kiapi.schematic.commands.SchematicHierarchyResponse",
                     "kiapi.common.types.DocumentSpecifier"):
            msg = pool.message(name)
            self.assertEqual(msg.DESCRIPTOR.full_name, name)

    def test_document_specifier_round_trips_across_pools(self) -> None:
        # kipy's generated class -> bytes -> private-pool dynamic class: the wire
        # format is the contract, so the echo must survive unchanged.
        try:
            from kipy.proto.common.types import base_types_pb2
        except Exception as exc:  # pragma: no cover - kipy always present in venv
            self.skipTest(f"kipy not importable here: {exc}")
        theirs = base_types_pb2.DocumentSpecifier()
        theirs.type = self.sch_api.DOCTYPE_SCHEMATIC
        theirs.project.name = "amp"
        theirs.project.path = "/tmp/amp"
        pool = self.sch_api._Pool.get()
        mine = pool.message("kiapi.common.types.DocumentSpecifier")
        mine.ParseFromString(theirs.SerializeToString())
        self.assertEqual(mine.project.name, "amp")
        self.assertEqual(mine.SerializeToString(), theirs.SerializeToString())

    def test_netlist_command_packs_into_any(self) -> None:
        from google.protobuf import any_pb2
        pool = self.sch_api._Pool.get()
        cmd = pool.message("kiapi.schematic.commands.GetSchematicNetlist")
        cmd.document.type = self.sch_api.DOCTYPE_SCHEMATIC
        packed = any_pb2.Any()
        packed.Pack(cmd)
        self.assertTrue(packed.type_url.endswith(
            "kiapi.schematic.commands.GetSchematicNetlist"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
