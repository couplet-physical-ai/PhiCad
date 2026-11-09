#
# This program source code file is part of PhiCad, a fork of KiCad.
#
# Copyright (C) 2026 Exowatt, Inc.
# Copyright The KiCad Developers, see AUTHORS.txt for contributors.
#
# SPDX-License-Identifier: GPL-2.0-or-later
#
"""Findings carry evidence, and their identity is stable.

The identity contract is the whole reason this module exists: if an id moved when a message was
reworded or a track shifted by a nanometre, a granted waiver would silently come back as a new
violation, which is worse than having no waivers at all.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import findings as f  # noqa: E402


# A real `kicad-cli pcb drc` fragment: the VIN track running into U1's 3V3 pad.
DRC_REPORT = {
    "violations": [
        {
            "type": "shorting_items",
            "severity": "error",
            "description": "Items shorting two nets (nets 3V3 and VIN)",
            "items": [
                {"description": "Pad 2 [3V3] of U1 on F.Cu",
                 "pos": {"x": 114.69, "y": 111.53}},
                {"description": "Track [VIN] on F.Cu, length 9.0510 mm",
                 "pos": {"x": 120.0, "y": 111.53}},
            ],
        },
    ],
    "unconnected_items": [
        {
            "severity": "error",
            "description": "Missing connection between items",
            "items": [{"description": "Pad 2 [3V3] of U1 on F.Cu"}],
        },
    ],
}


class FindingsTest(unittest.TestCase):

    def test_drc_violation_keeps_the_geometry_that_produced_it(self):
        got = f.from_drc_report(DRC_REPORT)
        short = next(x for x in got if x.code == "drc.shorting_items")
        self.assertEqual(short.severity, f.ERROR)
        self.assertEqual(short.at, (114.69, 111.53),
                         msg="the coordinate is the difference between a message and a fix")
        self.assertEqual(short.where, "F.Cu")
        self.assertIn("Pad 2 [3V3] of U1 on F.Cu", short.refs)
        self.assertTrue(short.fix, msg="a shorting_items violation has specific advice")

    def test_identity_survives_rewording(self):
        a = f.Finding(code="drc.clearance", severity=f.ERROR, message="Clearance violation",
                      refs=["U1.2"], where="F.Cu", at=(10.0, 20.0))
        b = f.Finding(code="drc.clearance", severity=f.ERROR,
                      message="Totally different wording, same violation",
                      refs=["U1.2"], where="F.Cu", at=(10.0, 20.0))
        self.assertEqual(a.id, b.id,
                         msg="rewording a message must not invalidate a granted waiver")

    def test_identity_survives_a_sub_micron_nudge(self):
        a = f.Finding(code="drc.clearance", severity=f.ERROR, message="x",
                      refs=["U1.2"], at=(10.0, 20.0))
        b = f.Finding(code="drc.clearance", severity=f.ERROR, message="x",
                      refs=["U1.2"], at=(10.0000004, 20.0000004))
        self.assertEqual(a.id, b.id,
                         msg="a re-run that nudges a track by a nanometre is the same violation")

    def test_identity_changes_when_the_violation_moves(self):
        a = f.Finding(code="drc.clearance", severity=f.ERROR, message="x",
                      refs=["U1.2"], at=(10.0, 20.0))
        b = f.Finding(code="drc.clearance", severity=f.ERROR, message="x",
                      refs=["U1.2"], at=(40.0, 20.0))
        self.assertNotEqual(a.id, b.id,
                            msg="a violation 30 mm away is a different violation")

    def test_identity_ignores_ref_order(self):
        a = f.Finding(code="drc.shorting_items", severity=f.ERROR, message="x",
                      refs=["U1.2", "VIN"])
        b = f.Finding(code="drc.shorting_items", severity=f.ERROR, message="x",
                      refs=["VIN", "U1.2"])
        self.assertEqual(a.id, b.id,
                         msg="which item the tool listed first is not part of the identity")

    def test_worst_first_ordering_is_independent_of_discovery_order(self):
        items = [
            f.Finding(code="b.info", severity=f.INFO, message="i"),
            f.Finding(code="a.error", severity=f.ERROR, message="e"),
            f.Finding(code="c.warning", severity=f.WARNING, message="w"),
        ]
        self.assertEqual([x.severity for x in f.sort_findings(items)],
                         [f.ERROR, f.WARNING, f.INFO])
        self.assertEqual([x.severity for x in f.sort_findings(list(reversed(items)))],
                         [f.ERROR, f.WARNING, f.INFO])

    def test_summarise_pluralises_and_says_nothing_when_there_is_nothing(self):
        self.assertEqual(f.summarise([]), "no problems")
        self.assertEqual(
            f.summarise([f.Finding(code="x", severity=f.ERROR, message="m")]), "1 error")
        self.assertEqual(
            f.summarise([f.Finding(code="x", severity=f.ERROR, message="m"),
                         f.Finding(code="y", severity=f.ERROR, message="m"),
                         f.Finding(code="z", severity=f.WARNING, message="m")]),
            "2 errors, 1 warning")

    def test_unconnected_items_become_findings_too(self):
        got = f.from_drc_report(DRC_REPORT)
        self.assertTrue(any(x.code == "drc.unconnected" for x in got),
                        msg="an open is a finding, not just a count")

    def test_as_dict_round_trips_through_json(self):
        import json
        got = f.from_drc_report(DRC_REPORT)
        parsed = json.loads(f.to_json(got))
        self.assertEqual(len(parsed), len(got))
        self.assertEqual(parsed[0]["id"], got[0].id)
        self.assertIn("severity", parsed[0])


if __name__ == "__main__":
    unittest.main(verbosity=2)
