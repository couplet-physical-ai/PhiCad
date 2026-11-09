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
"""Shape gate for the InteractiveHtmlBom generic-JSON builder (phicad/pylib/ibom_json.py).

WHY THESE FIXTURES
    The three inputs are CAPTURED output of this fork's own kicad-cli, run on
    demos/ecc83/ecc83-pp.kicad_pcb (through-hole, one side) with a synthetic SMD/bottom-side
    pair spliced in, because the interesting cases -- 327 SMD records, an access code that
    means "bottom", a rotated rect pad -- do not all occur on one small demo board.  Keeping
    them as text rather than as a board means `phicad check fast` gates the translation with
    no venv, no build tree and no InteractiveHtmlBom checkout. Real kicad-cli plus ibom
    integration is verified separately with dependency-enabled smoke calls.

WHAT IS ASSERTED
    The field-by-field contract ibom's parser and its jsonschema actually enforce: spec_version,
    required keys, components/footprints length equality, Y flipped exactly once, IPC-D-356
    units converted to mm, through vs SMD pads with the drill keys a `th` pad requires, layer
    letters, nets collected, the outline taken from the SVG when there is one and flagged as a
    guess when there is not.  validate_document() is exercised against deliberately broken
    documents too, because a validator that never says no is worse than none.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import ibom_json  # noqa: E402  (path juggling above is deliberate)

# Captured from: kicad-cli pcb export pos --format csv --side both --units mm
POS_CSV = '''Ref,Val,Package,PosX,PosY,Rot,Side
"C1","10uF","CP_Radial_D10.0mm_P5.00mm",141.605000,-99.695000,90.000000,top
"R1","1.5K","R_Axial_DIN0207_L6.3mm_D2.5mm_P7.62mm_Horizontal",136.271000,-107.950000,-90.000000,top
"U1","ECC83","Valve_Noval_P15.24mm_Vertical",149.352000,-115.062000,0.000000,top
"D4","LED","LED_0603_1608Metric",150.401000,-96.299000,90.000000,bottom
'''

# Captured from: kicad-cli pcb export ipcd356 (records 317 = through, 327 = SMD; the D4 pair
# and the VIA line are spliced in from a second board to cover SMD, bottom access and a via).
D356 = '''P  CODE 00
P  UNITS CUST 0
P  arrayDim   N
317NET-(P3-P1)      C1    -1    D0394PA00X+055750Y-039250X0787Y0787R270S0
317GND              C1    -2    D0394PA00X+055750Y-037281X0787Y0000R270S0
317NET-(U1A-G)      R1    -1    D0315PA00X+053650Y-042500X0630Y0000R090S0
317NET-(U1A-K)      R1    -2    D0315PA00X+053650Y-045500X0630Y0630R090S0
317NET-(U1A-G)      U1    -1    D0402PA00X+060108Y-046640X0799Y0000R000S0
317NET-(P1-PM)      U1    -2    D0402PA00X+060955Y-045486X0799Y0000R000S0
327GND              D4    -1          A02X+059213Y-037913X0157Y0236R090S2
327N/C              D4    -2          A02X+059213Y-038268X0157Y0236R090S2
317GND              VIA        MD0118PA00X+061909Y-040335X0197Y0000R000S3
'''

# Captured from: kicad-cli pcb export svg --mode-single -l Edge.Cuts --exclude-drawing-sheet
# --page-size-mode 1 --black-and-white (trimmed to the outline group plus one drill circle,
# which must NOT become an edge, and one arc path, which must survive as an svgpath).
EDGE_SVG = '''<?xml version="1.0" standalone="no"?>
<svg xmlns="http://www.w3.org/2000/svg" version="1.1"
  width="297.0022mm" height="210.0072mm" viewBox="0.0000 0.0000 297.0022 210.0072">
<g style="fill:none;
stroke:#000000; stroke-width:0.1270; stroke-opacity:1;
stroke-linecap:round; stroke-linejoin:round;stroke-dasharray:none;">
<path d="M173.3550 90.1700
L173.3550 136.5250
" />
<path d="M121.2850 90.1700
L121.2850 136.5250
" />
<path d="M173.3550 90.1700
L121.2850 90.1700
" />
<path d="M121.2850 136.5250
A 2.0 2.0 0 0 1 173.3550 136.5250
" />
</g>
<g style="fill:#000000; fill-opacity:1.0000; stroke:none;">
<circle cx="141.6050" cy="99.6950" r="0.5000" />
</g>
</svg>
'''


def build_full() -> dict:
    return ibom_json.build_from_text(POS_CSV, D356, EDGE_SVG,
                                     {"title": "ecc83-pp", "revision": "A",
                                      "company": "Example Co", "date": "2026-01-01"})


class ParsePosCsv(unittest.TestCase):
    def test_rows_and_y_flip(self):
        places = ibom_json.parse_pos_csv(POS_CSV)
        self.assertEqual([p.ref for p in places], ["C1", "R1", "U1", "D4"])
        c1 = places[0]
        self.assertAlmostEqual(c1.x, 141.605)
        # pos files are Y-up; ibom (like the board file) is Y-down.
        self.assertAlmostEqual(c1.y, 99.695)
        self.assertEqual(c1.val, "10uF")
        self.assertEqual(c1.package, "CP_Radial_D10.0mm_P5.00mm")
        self.assertEqual(c1.layer, "F")
        self.assertEqual(places[3].layer, "B")

    def test_rejects_non_pos_input(self):
        with self.assertRaises(ValueError):
            ibom_json.parse_pos_csv("hello,world\n1,2\n")
        with self.assertRaises(ValueError):
            ibom_json.parse_pos_csv("")
        with self.assertRaises(ValueError):
            ibom_json.parse_pos_csv("Ref,Val,Package,PosX,PosY,Rot,Side\n")


class ParseD356(unittest.TestCase):
    def setUp(self):
        self.records = ibom_json.parse_d356(D356)

    def test_header_lines_are_not_records(self):
        self.assertEqual(len(self.records), 9)   # 8 pads + 1 via; the three P-lines are dropped

    def test_units_and_geometry(self):
        c1p1 = self.records[0]
        self.assertEqual((c1p1.ref, c1p1.pin), ("C1", "1"))
        self.assertEqual(c1p1.net, "NET-(P3-P1)")
        # 0.0001 inch integers -> mm, and the same Y flip as the pos file.
        self.assertAlmostEqual(c1p1.x, 141.605, places=3)
        self.assertAlmostEqual(c1p1.y, 99.695, places=3)
        self.assertAlmostEqual(c1p1.size_x, 1.99898, places=4)
        self.assertAlmostEqual(c1p1.drill, 1.00076, places=4)
        self.assertEqual(c1p1.rot, 270.0)
        self.assertTrue(c1p1.through)
        self.assertEqual(c1p1.layers, ["F", "B"])

    def test_auxiliary_origin_restores_absolute_pad_coordinates(self):
        shifted = ibom_json.parse_d356(D356, aux_origin=(10.0, 20.0))
        self.assertAlmostEqual(shifted[0].x, self.records[0].x + 10)
        self.assertAlmostEqual(shifted[0].y, self.records[0].y + 20)
        doc = ibom_json.build_from_text(POS_CSV, D356, EDGE_SVG, aux_origin=(10.0, 20.0))
        self.assertAlmostEqual(doc["pcbdata"]["footprints"][0]["pads"][0]["pos"][0], shifted[0].x)

    def test_smd_bottom_and_nc(self):
        smd = [r for r in self.records if r.record == "327"]
        self.assertEqual(len(smd), 2)
        self.assertFalse(smd[0].through)
        self.assertEqual(smd[0].layers, ["B"])   # access 02 on a 2-layer board
        self.assertEqual(smd[0].net, "GND")
        self.assertEqual(smd[1].net, "")         # N/C means "no net", not a net called N/C

    def test_vias_are_dropped_from_components(self):
        via = [r for r in self.records if r.is_via]
        self.assertEqual(len(via), 1)
        self.assertNotIn("VIA", ibom_json.pads_by_ref(self.records))
        self.assertEqual(sorted(ibom_json.pads_by_ref(self.records)), ["C1", "D4", "R1", "U1"])

    def test_garbage_is_ignored(self):
        self.assertEqual(ibom_json.parse_d356("nonsense\n317short\n"), [])


class ParseEdgeSvg(unittest.TestCase):
    def test_segments_arc_and_bbox(self):
        outline = ibom_json.parse_edge_svg(EDGE_SVG)
        self.assertTrue(outline.known)
        kinds = [d["type"] for d in outline.edges]
        self.assertEqual(kinds.count("segment"), 3)
        self.assertEqual(kinds.count("arc"), 1)
        arc = [d for d in outline.edges if d["type"] == "arc"][0]
        self.assertIn("svgpath", arc)
        self.assertIn("A 2.0 2.0", arc["svgpath"])
        for dwg in outline.edges:
            self.assertAlmostEqual(dwg["width"], 0.127)
        self.assertAlmostEqual(outline.minx, 121.285)
        self.assertAlmostEqual(outline.maxx, 173.355)
        self.assertAlmostEqual(outline.miny, 90.17)
        self.assertAlmostEqual(outline.maxy, 136.525)

    def test_drill_circles_are_not_edges(self):
        outline = ibom_json.parse_edge_svg(EDGE_SVG)
        self.assertTrue(all(d["type"] != "circle" for d in outline.edges))

    def test_circular_outline_survives_but_filled_drill_is_ignored(self):
        svg = '<svg><g style="fill:none;stroke-width:0.2">' \
              '<circle cx="10" cy="20" r="5"/></g>' \
              '<circle fill="black" cx="50" cy="50" r="1"/></svg>'
        outline = ibom_json.parse_edge_svg(svg)
        self.assertEqual(len(outline.edges), 1)
        self.assertEqual(outline.edges[0]["type"], "circle")
        self.assertEqual((outline.minx, outline.miny, outline.maxx, outline.maxy), (5, 15, 15, 25))

    def test_arc_bounds_include_extrema_and_closed_subpaths_survive(self):
        outline = ibom_json.parse_edge_svg('<svg><path d="M 0 0 A 5 5 0 0 1 10 0"/></svg>')
        self.assertAlmostEqual(outline.maxy - outline.miny, 5)
        outline = ibom_json.parse_edge_svg('<svg><path d="M 0 0 L 1 0 L 1 1 Z M 5 5 L 6 5"/></svg>')
        self.assertEqual(len(outline.edges), 1)
        self.assertIn("Z M", outline.edges[0]["svgpath"])

    def test_empty_svg_is_unknown(self):
        self.assertFalse(ibom_json.parse_edge_svg("<svg></svg>").known)


class Document(unittest.TestCase):
    def setUp(self):
        self.doc = build_full()

    def test_valid_and_top_level_shape(self):
        self.assertEqual(ibom_json.validate_document(self.doc), [])
        self.assertEqual(self.doc["spec_version"], 1)
        self.assertEqual(sorted(ibom_json.strip_private(self.doc)),
                         ["components", "pcbdata", "spec_version"])

    def test_components_match_footprints(self):
        comps = self.doc["components"]
        fps = self.doc["pcbdata"]["footprints"]
        self.assertEqual(len(comps), 4)
        self.assertEqual([c["ref"] for c in comps], [f["ref"] for f in fps])
        self.assertEqual([c["layer"] for c in comps], ["F", "F", "F", "B"])
        self.assertEqual(comps[0]["footprint"], "CP_Radial_D10.0mm_P5.00mm")
        self.assertEqual(comps[0]["attr"], "th")
        self.assertEqual(comps[3]["attr"], "smd")

    def test_pads_and_bbox_from_netlist(self):
        c1 = self.doc["pcbdata"]["footprints"][0]
        self.assertEqual(len(c1["pads"]), 2)
        pad = c1["pads"][0]
        self.assertEqual(pad["type"], "th")
        self.assertEqual(pad["shape"], "rect")
        self.assertEqual(pad["drillshape"], "circle")
        self.assertEqual(pad["pin1"], 1)
        self.assertEqual(pad["net"], "NET-(P3-P1)")
        self.assertEqual(c1["pads"][1]["shape"], "circle")   # size Y 0000 means round
        # The box must contain every pad centre it owns.
        box = c1["bbox"]
        x0 = box["pos"][0] + box["relpos"][0]
        y0 = box["pos"][1] + box["relpos"][1]
        for pad in c1["pads"]:
            self.assertGreaterEqual(pad["pos"][0], x0)
            self.assertGreaterEqual(pad["pos"][1], y0)
            self.assertLessEqual(pad["pos"][0], x0 + box["size"][0])
            self.assertLessEqual(pad["pos"][1], y0 + box["size"][1])

    def test_nets_and_metadata(self):
        self.assertIn("GND", self.doc["pcbdata"]["nets"])
        self.assertNotIn("N/C", self.doc["pcbdata"]["nets"])
        self.assertEqual(self.doc["pcbdata"]["metadata"]["title"], "ecc83-pp")
        self.assertEqual(self.doc["pcbdata"]["metadata"]["revision"], "A")

    def test_drawings_groups_are_present_and_empty(self):
        drawings = self.doc["pcbdata"]["drawings"]
        self.assertEqual(sorted(drawings), ["fabrication", "silkscreen"])
        for group in drawings.values():
            self.assertEqual(sorted(group), ["B", "F"])
            self.assertEqual(group["F"], [])

    def test_summary(self):
        rep = ibom_json.summary(self.doc)
        self.assertEqual(rep["components"], 4)
        self.assertEqual(rep["top"], 3)
        self.assertEqual(rep["bottom"], 1)
        self.assertEqual(rep["pads"], 8)
        self.assertEqual(rep["sides"], ["B", "F"])
        self.assertTrue(rep["outline_from_svg"])

    def test_edges_from_svg(self):
        bbox = self.doc["pcbdata"]["edges_bbox"]
        self.assertAlmostEqual(bbox["minx"], 121.285)
        self.assertAlmostEqual(bbox["maxy"], 136.525)


class Degraded(unittest.TestCase):
    def test_without_netlist(self):
        doc = ibom_json.build_from_text(POS_CSV, None, EDGE_SVG)
        self.assertEqual(ibom_json.validate_document(doc), [])
        self.assertEqual(ibom_json.summary(doc)["pads"], 0)
        self.assertEqual(doc["pcbdata"].get("nets"), None)
        self.assertNotIn("attr", doc["components"][0])
        self.assertTrue(any("IPC-D-356" in w for w in ibom_json.summary(doc)["warnings"]))
        # Every footprint still gets a usable click target.
        for fp in doc["pcbdata"]["footprints"]:
            self.assertGreater(fp["bbox"]["size"][0], 0)

    def test_without_outline(self):
        doc = ibom_json.build_from_text(POS_CSV, D356, None)
        self.assertEqual(ibom_json.validate_document(doc), [])
        rep = ibom_json.summary(doc)
        self.assertFalse(rep["outline_from_svg"])
        self.assertTrue(any("outline unknown" in w for w in rep["warnings"]))
        self.assertEqual(rep["edges"], 4)          # a rectangle around the placements
        bbox = doc["pcbdata"]["edges_bbox"]
        self.assertLess(bbox["minx"], 136.271)
        self.assertGreater(bbox["maxx"], 150.401)

    def test_no_placements_refuses(self):
        with self.assertRaises(ValueError):
            ibom_json.build_document([])


class Validator(unittest.TestCase):
    def test_wrong_spec_version(self):
        doc = build_full()
        doc["spec_version"] = 2
        self.assertTrue(any("spec_version" in p for p in ibom_json.validate_document(doc)))

    def test_length_mismatch_is_caught(self):
        doc = build_full()
        doc["components"].pop()
        self.assertTrue(any("same length" in p for p in ibom_json.validate_document(doc)))

    def test_bad_layer_and_pad_type(self):
        doc = build_full()
        doc["components"][0]["layer"] = "Top"
        doc["pcbdata"]["footprints"][0]["pads"][0]["type"] = "press-fit"
        problems = ibom_json.validate_document(doc)
        self.assertTrue(any("layer must be" in p for p in problems))
        self.assertTrue(any("type must be" in p for p in problems))

    def test_through_pad_needs_drill_keys(self):
        doc = build_full()
        doc["pcbdata"]["footprints"][0]["pads"][0].pop("drillsize")
        self.assertTrue(any("drillsize" in p for p in ibom_json.validate_document(doc)))

    def test_unexpected_key_and_missing_metadata(self):
        doc = build_full()
        doc["extra"] = 1
        doc["pcbdata"]["metadata"].pop("date")
        problems = ibom_json.validate_document(doc)
        self.assertTrue(any("unexpected top-level key" in p for p in problems))
        self.assertTrue(any("metadata.date" in p for p in problems))

    def test_empty_edges_is_a_problem(self):
        doc = build_full()
        doc["pcbdata"]["edges"] = []
        self.assertTrue(any("edges is empty" in p for p in ibom_json.validate_document(doc)))

    def test_tracks_without_zones(self):
        doc = build_full()
        doc["pcbdata"]["tracks"] = {"F": [], "B": []}
        self.assertTrue(any("tracks and zones" in p for p in ibom_json.validate_document(doc)))

    def test_non_object(self):
        self.assertEqual(ibom_json.validate_document([1, 2]), ["document is not an object"])


class WriteAndCli(unittest.TestCase):
    def test_written_file_has_no_private_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "nested" / "b.ibom.json"
            size = ibom_json.write_document(build_full(), out)
            data = json.loads(out.read_text())
            self.assertNotIn("_phicad", data)
            self.assertEqual(ibom_json.validate_document(data), [])
            self.assertEqual(size, len(out.read_text().encode("utf-8")))
            # summary() still works for a document read back from disk.
            self.assertEqual(ibom_json.summary(data)["components"], 4)

    def test_cli_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            (tmp / "p.csv").write_text(POS_CSV)
            (tmp / "n.d356").write_text(D356)
            (tmp / "e.svg").write_text(EDGE_SVG)
            out = tmp / "out.json"
            rc = ibom_json.main(["--pos", str(tmp / "p.csv"), "--d356", str(tmp / "n.d356"),
                                 "--edges", str(tmp / "e.svg"), "--title", "t",
                                 "-o", str(out)])
            self.assertEqual(rc, 0)
            self.assertEqual(ibom_json.validate_document(json.loads(out.read_text())), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
