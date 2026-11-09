#!/usr/bin/env python3
#
# This program source code file is part of PhiCad, a fork of KiCad.
#
# Copyright (C) 2026 Exowatt, Inc.
# Copyright The KiCad Developers, see AUTHORS.txt for contributors.
#
# SPDX-License-Identifier: GPL-2.0-or-later
#
"""Circuit JSON -> Design IR translation is closed, checked and deterministic.

The contract these tests defend: every element type is mapped, ignored with a reason, or
rejected with a typed reason; an unknown type fails closed; every coordinate crosses exactly
one checked nm boundary; and the same bytes produce the same plan evidence, twice.

Pure stdlib: no venv, no built kicad-cli, no network — safe for `phicad check fast`.
"""

from __future__ import annotations

import copy
import json
import math
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from circuit_json import ELEMENT_POLICIES, Rejected, to_nm, translate  # noqa: E402


def fixture() -> list:
    """A minimal but complete two-resistor Circuit JSON document."""
    elements: list = [
        {"type": "source_group", "source_group_id": "g", "is_subcircuit": True},
        {"type": "source_board", "source_board_id": "sb"},
        {"type": "pcb_board", "pcb_board_id": "b", "center": {"x": 0, "y": 0},
         "width": 10, "height": 6, "thickness": 1.6, "num_layers": 2,
         "min_trace_width": 0.2, "min_board_edge_clearance": 0.2,
         "min_pad_edge_to_pad_edge_clearance": 0.1},
        {"type": "source_trace", "source_trace_id": "tr",
         "connected_source_port_ids": ["sp02", "sp11"], "name": "LINK"},
    ]
    for index, (name, x) in enumerate((("R1", -3), ("R2", 3))):
        sid = f"s{index}"
        elements.append({"type": "source_component", "source_component_id": sid,
                         "ftype": "simple_resistor", "name": name,
                         "resistance": 1000, "display_resistance": "1k"})
        elements.append({"type": "cad_component", "cad_component_id": f"c{index}",
                         "source_component_id": sid, "pcb_component_id": f"p{index}",
                         "position": {"x": x, "y": 0, "z": 0},
                         "rotation": {"x": 0, "y": 0, "z": 0},
                         "footprinter_string": "res0402"})
        elements.append({"type": "pcb_component", "pcb_component_id": f"p{index}",
                         "source_component_id": sid, "center": {"x": x, "y": 0},
                         "width": 1.86, "height": 0.94, "layer": "top", "rotation": 0})
        for pin, dx in ((1, -0.51), (2, 0.51)):
            sp, pp = f"sp{index}{pin}", f"pp{index}{pin}"
            elements.append({"type": "source_port", "source_port_id": sp,
                             "source_component_id": sid, "pin_number": pin,
                             "name": f"pin{pin}"})
            elements.append({"type": "pcb_port", "pcb_port_id": pp,
                             "pcb_component_id": f"p{index}", "source_port_id": sp,
                             "layers": ["top"], "x": x + dx, "y": 0})
            elements.append({"type": "pcb_smtpad", "pcb_smtpad_id": f"pad{index}{pin}",
                             "pcb_component_id": f"p{index}", "pcb_port_id": pp,
                             "layer": "top", "shape": "rect", "width": 0.54,
                             "height": 0.64, "x": x + dx, "y": 0})
    return elements


class ToNmBoundary(unittest.TestCase):
    def test_nan_is_rejected(self):
        with self.assertRaises(Rejected) as ctx:
            to_nm(float("nan"))
        self.assertEqual(ctx.exception.code, "non_finite_coordinate")

    def test_infinity_is_rejected(self):
        for value in (math.inf, -math.inf):
            with self.assertRaises(Rejected) as ctx:
                to_nm(value)
            self.assertEqual(ctx.exception.code, "non_finite_coordinate")

    def test_overflow_is_rejected(self):
        # 2**47 nm is about 140 m of board; anything beyond it is an error, not a big board.
        with self.assertRaises(Rejected) as ctx:
            to_nm(1e12)
        self.assertEqual(ctx.exception.code, "coordinate_overflow")
        self.assertEqual(to_nm(140_737.0), 140_737_000_000)

    def test_rounding_is_half_even_once(self):
        # 0.0000005 mm is exactly 0.5 nm; banker's rounding takes it to 0, and 1.5 nm to 2.
        self.assertEqual(to_nm(0.0000005), 0)
        self.assertEqual(to_nm(0.0000015), 2)
        self.assertEqual(to_nm(1.0), 1_000_000)
        self.assertEqual(to_nm(-0.51), -510_000)


class ElementPolicyTable(unittest.TestCase):
    def test_every_policy_action_is_known(self):
        for name, policy in ELEMENT_POLICIES.items():
            self.assertIn(policy.action, ("mapped", "ignored", "rejected"), name)
            self.assertTrue(policy.reason, f"{name} needs a reason a human can read")

    def test_unknown_element_type_fails_closed(self):
        elements = fixture() + [{"type": "pcb_paint_splatter", "x": 1, "y": 1}]
        result = translate(elements, "t")
        self.assertFalse(result.ok)
        codes = {(r["code"], r["element"]) for r in result.rejections}
        self.assertIn(("unknown_element_type", "pcb_paint_splatter"), codes)

    def test_missing_type_fails_closed(self):
        result = translate(fixture() + [{"width": 5}], "t")
        self.assertFalse(result.ok)
        self.assertIn("unknown_element_type", {r["code"] for r in result.rejections})

    def test_rejected_policy_is_a_typed_rejection(self):
        via = {"type": "pcb_via", "x": 1, "y": 1, "hole_diameter": 0.3,
               "outer_diameter": 0.6, "layers": ["top", "bottom"]}
        result = translate(fixture() + [via], "t")
        self.assertFalse(result.ok)
        entry = next(r for r in result.rejections if r["element"] == "pcb_via")
        self.assertEqual(entry["code"], "unsupported_required_geometry")
        self.assertIn("pcb_via", result.rejected_types)
        self.assertNotIn("pcb_via", result.supported)


class CoordinateFailures(unittest.TestCase):
    def test_non_finite_coordinate_is_rejected(self):
        elements = fixture()
        elements[2] = dict(elements[2])
        elements[2]["width"] = float("nan")
        result = translate(elements, "t")
        self.assertFalse(result.ok)
        self.assertIn("non_finite_coordinate", {r["code"] for r in result.rejections})

    def test_overflowing_coordinate_is_rejected(self):
        elements = fixture()
        elements[2] = dict(elements[2], center={"x": 1e12, "y": 0})
        result = translate(elements, "t")
        self.assertFalse(result.ok)
        self.assertIn("coordinate_overflow", {r["code"] for r in result.rejections})

    def test_non_finite_hides_nowhere(self):
        # Even an IGNORED element's geometry is checked: a NaN behind a policy decision must
        # not survive into anything downstream.
        elements = fixture() + [{"type": "pcb_fabrication_note_path", "layer": "top",
                                 "stroke_width": float("inf"),
                                 "route": [{"x": 0, "y": 0}]}]
        result = translate(elements, "t")
        self.assertIn("non_finite_coordinate", {r["code"] for r in result.rejections})

    def test_invalid_layer_is_rejected(self):
        elements = fixture()
        for element in elements:
            if element["type"] == "pcb_component":
                element["layer"] = "inner1"
                break
        result = translate(elements, "t")
        self.assertIn("invalid_layer", {r["code"] for r in result.rejections})


class HappyPath(unittest.TestCase):
    def test_translation_is_complete_and_placed(self):
        result = translate(fixture(), "two-r")
        self.assertTrue(result.ok, result.rejections)
        self.assertEqual(result.counts["components"], 2)
        self.assertEqual(result.counts["pads"], 4)
        self.assertEqual(result.counts["nets"], 1)
        self.assertEqual(result.design_ir["nets"], {"LINK": ["R1.2", "R2.1"]})
        self.assertEqual(sorted(result.design_ir["no_connect"]), ["R1.1", "R2.2"])
        # Board centre (0,0), 10x6: x=-3 lands at 2 mm from the left edge.
        self.assertEqual(result.design_ir["placement"]["R1"]["at"], [2.0, 3.0])
        self.assertEqual(result.design_ir["placement"]["R2"]["at"], [8.0, 3.0])
        self.assertTrue(all(p["lock"] for p in result.design_ir["placement"].values()))
        ops = [op for op in result.operations if op["op"] == "create_footprint"]
        self.assertEqual([op["at_nm"] for op in ops],
                         [[2_000_000, 3_000_000], [8_000_000, 3_000_000]])

    def test_every_seen_type_is_accounted_for(self):
        result = translate(fixture(), "t")
        seen = {e["type"] for e in fixture()}
        accounted = set(result.supported) | set(result.rejected_types)
        self.assertEqual(seen - accounted, set(),
                         "an element type present in the input fell through the census")

    def test_translation_is_deterministic(self):
        elements = fixture()
        first = translate(copy.deepcopy(elements), "t")
        second = translate(copy.deepcopy(elements), "t")
        self.assertEqual(json.dumps(first.design_ir, sort_keys=True),
                         json.dumps(second.design_ir, sort_keys=True))
        self.assertEqual(json.dumps(first.operations, sort_keys=True),
                         json.dumps(second.operations, sort_keys=True))

    def test_pad_geometry_must_match_the_native_footprint(self):
        elements = fixture()
        for element in elements:
            if element["type"] == "pcb_smtpad" and element["pcb_smtpad_id"] == "pad01":
                element["width"] = 0.80        # a 0603 pad on a part claiming res0402
        result = translate(elements, "t")
        self.assertFalse(result.ok)
        entry = next(r for r in result.rejections if r["element"] == "pcb_smtpad")
        self.assertEqual(entry["code"], "unsupported_required_geometry")

    def test_unmappable_footprint_is_rejected_not_approximated(self):
        elements = fixture()
        for element in elements:
            if element["type"] == "cad_component" and element["cad_component_id"] == "c0":
                element["footprinter_string"] = "sot23"
        result = translate(elements, "t")
        self.assertFalse(result.ok)
        self.assertIn("unsupported_required_geometry", {r["code"] for r in result.rejections})

    def _with_cad(self, **changes):
        elements = fixture()
        for element in elements:
            if element["type"] == "cad_component" and element["cad_component_id"] == "c0":
                element.update(changes)
        return elements

    def test_a_body_height_is_ignored_with_a_recorded_reason(self):
        # z is where the engine puts the 3D body. The native stock footprint carries its own
        # `(model ...)` node for that exact package and this pipeline never rewrites footprints,
        # so not importing the engine's offset loses nothing -- but it is still a deliberate
        # omission, so it has to appear in the plan.
        result = translate(self._with_cad(position={"x": -3, "y": 0, "z": 0.7}), "t")
        self.assertTrue(result.ok, result.rejections)
        self.assertIn("cad_component.position.z", result.ignored)
        self.assertIn({"op": "ignore_field", "element": "cad_component.position.z",
                       "reason": result.ignored["cad_component.position.z"]}, result.operations)

    def test_a_custom_3d_model_is_rejected_never_silently_dropped(self):
        # The measured defect this defends: attaching a caller's model means rewriting the
        # footprint's `(model ...)` node, and that round-trip destroys model nodes.
        for key in ("model_obj_url", "model_step_url", "model_jscad"):
            result = translate(self._with_cad(**{key: "https://example.invalid/part.obj"}), "t")
            self.assertFalse(result.ok, key)
            entry = next(r for r in result.rejections if r["element"] == "cad_component")
            self.assertEqual(entry["code"], "unsupported_required_geometry")
            self.assertIn(key, entry["detail"])

    def test_a_tilted_body_is_rejected(self):
        for axis in ("x", "y"):
            result = translate(self._with_cad(rotation={"x": 0, "y": 0, "z": 0, axis: 90}), "t")
            self.assertFalse(result.ok, axis)
            entry = next(r for r in result.rejections if r["element"] == "cad_component")
            self.assertIn(f"rotated about {axis}", entry["detail"])

    def test_a_body_below_the_board_is_rejected(self):
        result = translate(self._with_cad(position={"x": -3, "y": 0, "z": -0.5}), "t")
        self.assertFalse(result.ok)
        entry = next(r for r in result.rejections if r["element"] == "cad_component")
        self.assertIn("below the board", entry["detail"])

    def test_root_must_be_a_list(self):
        with self.assertRaises(Rejected) as ctx:
            translate({"type": "pcb_board"}, "t")  # type: ignore[arg-type]
        self.assertEqual(ctx.exception.code, "invalid_circuit_json")

    def test_net_names_are_sanitized_without_collisions(self):
        elements = fixture()
        for element in elements:
            if element["type"] == "source_trace":
                element["name"] = "3v3 rail!"
        result = translate(elements, "t")
        self.assertTrue(result.ok, result.rejections)
        self.assertEqual(list(result.design_ir["nets"]), ["N_3v3_rail"])


class FootprintDerivedArtifacts(unittest.TestCase):
    """Courtyard, paste and package silk are footprint OUTPUTS, and the split is per element.

    Attached to a component whose package the pads already pin to within 1 nm, the native
    footprint emits the same artifact, so ignoring the engine's copy loses nothing.  Free
    standing, the same element is intent the Design IR cannot express, and it must reject --
    otherwise a board-level silk logo or a hand-drawn courtyard would vanish silently.
    """

    def _component_owned(self):
        return fixture() + [
            {"type": "pcb_courtyard_rect", "pcb_component_id": "p0", "layer": "top",
             "center": {"x": -3, "y": 0}, "width": 1.86, "height": 0.94},
            {"type": "pcb_solder_paste", "pcb_component_id": "p0", "pcb_smtpad_id": "pad01",
             "layer": "top", "shape": "rect", "width": 0.54, "height": 0.64,
             "x": -3.51, "y": 0},
            {"type": "pcb_silkscreen_path", "pcb_component_id": "p0", "layer": "top",
             "route": [{"x": -3.15, "y": 0.38}, {"x": -2.84, "y": 0.38}], "stroke_width": 0.05},
            {"type": "pcb_silkscreen_text", "pcb_component_id": "p0", "layer": "top",
             "text": "R1", "font_size": 0.4, "anchor_position": {"x": -3, "y": 1.22}},
        ]

    def test_component_owned_artifacts_are_ignored_with_reasons(self):
        result = translate(self._component_owned(), "t")
        self.assertTrue(result.ok, result.rejections)
        for name in ("pcb_courtyard_rect", "pcb_solder_paste", "pcb_silkscreen_path",
                     "pcb_silkscreen_text"):
            self.assertIn(name, result.ignored, f"{name} must be accounted for, not silent")
            self.assertIn({"op": "ignore_element", "element": name,
                           "reason": result.ignored[name]}, result.operations)

    def test_free_standing_artifacts_are_rejected(self):
        for element in self._component_owned()[len(fixture()):]:
            orphan = {k: v for k, v in element.items() if k != "pcb_component_id"}
            result = translate(fixture() + [orphan], "t")
            self.assertFalse(result.ok, orphan["type"])
            entry = next(r for r in result.rejections if r["element"] == orphan["type"])
            self.assertEqual(entry["code"], "unsupported_required_geometry")

    def test_silkscreen_text_that_is_not_the_refdes_is_rejected(self):
        # A marking someone chose. The native silk stage emits reference designators, so this
        # would disappear -- which is exactly the silent drop the contract forbids.
        elements = self._component_owned()
        elements[-1] = dict(elements[-1], text="DO NOT POPULATE")
        result = translate(elements, "t")
        self.assertFalse(result.ok)
        entry = next(r for r in result.rejections if r["element"] == "pcb_silkscreen_text")
        self.assertIn("DO NOT POPULATE", entry["detail"])

    def test_paste_that_is_not_a_validated_pad_is_rejected(self):
        elements = self._component_owned()
        elements[-3] = dict(elements[-3], pcb_smtpad_id="a_pad_that_does_not_exist")
        result = translate(elements, "t")
        self.assertFalse(result.ok)
        entry = next(r for r in result.rejections if r["element"] == "pcb_solder_paste")
        self.assertEqual(entry["code"], "unsupported_required_geometry")

    def test_a_rejected_artifact_is_never_also_reported_as_ignored(self):
        # The census must not claim both, or a reviewer reading `ignored` would think a rejected
        # element had been handled.
        elements = self._component_owned()
        elements.append({"type": "pcb_courtyard_rect", "layer": "top",
                         "center": {"x": 0, "y": 0}, "width": 1, "height": 1})
        result = translate(elements, "t")
        self.assertIn("pcb_courtyard_rect", result.rejected_types)
        self.assertNotIn("pcb_courtyard_rect", result.ignored)
        self.assertNotIn({"op": "ignore_element", "element": "pcb_courtyard_rect",
                          "reason": "courtyard is emitted by the native footprint for the "
                                    "package the pads pin exactly"}, result.operations)


class RealEngineFixture(unittest.TestCase):
    """The pinned engine's own output must translate to a *typed* answer, not a crash.

    That real document (blinky: one resistor, one LED, engine-routed copper, engine
    silkscreen) is expected to be REJECTED — pcb_trace, pcb_silkscreen_*, solder paste and
    courtyard geometry have no lossless Design IR representation — and the rejection must say
    all of that rather than approximating.
    """

    FIXTURE = Path("/tmp/platform-real-circuit.json")

    def test_real_engine_output_is_typed(self):
        if not self.FIXTURE.is_file():
            self.skipTest(f"real engine fixture not present at {self.FIXTURE}")
        elements = json.loads(self.FIXTURE.read_text(encoding="utf-8"))
        result = translate(elements, "blinky")
        self.assertEqual(result.counts["components"], 2)
        self.assertEqual(result.counts["pads"], 4)
        for rejection in result.rejections:
            self.assertIn(rejection["code"],
                          ("unsupported_required_geometry", "unknown_element_type"))
        # The engine already routed this board and already laid out a schematic. Copper is the
        # board, so an engine route cannot be silently re-routed; the schematic elements carry the
        # engine's own symbol coordinates, which native symbols do not share.
        self.assertEqual(result.rejected_types,
                         ["pcb_trace", "schematic_component", "schematic_group",
                          "schematic_port", "schematic_trace"])
        # Everything the native footprints own IS accounted for rather than rejected.
        for name in ("pcb_courtyard_rect", "pcb_solder_paste", "pcb_silkscreen_path",
                     "pcb_silkscreen_text", "cad_component.position.z"):
            self.assertIn(name, result.ignored)


if __name__ == "__main__":
    unittest.main(verbosity=2)
