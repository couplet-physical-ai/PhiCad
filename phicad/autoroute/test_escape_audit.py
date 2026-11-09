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
"""The probe behind the fine-pitch BGA escape measurement.

`measurements/bga-escape-r1-2026-09-10.json` records what the router actually did to
a 196-ball 0.80 mm BGA on two signal layers.  That file is evidence, and evidence
that nothing checks decays: the numbers were measured on a board that lives in
another repository (`a private board project`), the run takes ~22 minutes, and it needs
freerouting, a JRE and a built `kicad-cli`.  None of that can run in this repo's CI.

So the claim is split in two, and this file gates both halves with **stdlib only**:

  1. **The instrument.**  `escape_audit.py` decides what "escaped" means, and it is
     the only thing standing between a real measurement and a plausible-looking
     number.  Every way it could lie is pinned here on synthetic boards small enough
     to reason about by hand -- a track that crosses a pad without ending on it, a
     no-connect ball with no `(net ...)`, a rotated footprint, a via that only
     touches the pad rim, a ball whose net is routed 30 mm away.
  2. **The record.**  The measurement file must stay internally consistent and must
     keep meaning what it says: totals that add up, stranded lists that match the
     detail table, an attribution for every stranded ball, and a comparator that
     actually fails when a number drifts. A gate that passes a mutated measurement
     is worse than no gate, so drift detection is tested by mutating.

What this does NOT do is re-route the board -- that is the ~22 minute run recorded
in the measurement's `reproduce` block, and it belongs to whoever re-measures.

Run: python3 phicad/autoroute/test_escape_audit.py
"""

from __future__ import annotations

import json
import math
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import escape_audit  # noqa: E402

MEASUREMENT = HERE / "measurements" / "bga-escape-r1-2026-09-10.json"


def board_text(footprints: str, copper: str = "") -> str:
    """A minimal fork-format board: enough for the audit, nothing more."""
    return (
        '(kicad_pcb\n'
        '\t(version 20260831)\n'
        '\t(generator "pcbnew")\n'
        '\t(layers (0 "F.Cu" signal) (2 "B.Cu" signal) (44 "Edge.Cuts" user))\n'
        + footprints + copper +
        ')\n'
    )


def bga(ref: str, ox: float, oy: float, rot: float, nets: dict, n: int = 4,
        pitch: float = 0.8, pad: float = 0.4) -> str:
    """An n x n ball grid centred on the footprint origin.

    `nets` maps a ball name (A1, A2, ... ) to a net name; a ball missing from it is
    a no-connect and gets NO `(net ...)` child at all, which is the case that breaks
    naive parsing.
    """
    rows = "ABCDEFGHJKLMNP"[:n]
    span = (n - 1) * pitch / 2.0
    out = [f'\t(footprint "T:{ref}"\n\t\t(layer "F.Cu")\n'
           f'\t\t(transform (translate {ox:g} {oy:g}) (rotate {rot:g}))\n'
           f'\t\t(property "Reference" "{ref}")\n']
    for ri, row in enumerate(rows):
        for ci in range(n):
            name = f"{row}{ci + 1}"
            px, py = -span + ci * pitch, -span + ri * pitch
            net = nets.get(name)
            out.append(f'\t\t(pad "{name}" smd circle\n'
                       f'\t\t\t(at {px:g} {py:g})\n'
                       f'\t\t\t(size {pad:g} {pad:g})\n'
                       f'\t\t\t(layers "F.Cu")\n'
                       + (f'\t\t\t(net "{net}")\n' if net else '')
                       + '\t\t)\n')
    out.append('\t)\n')
    return "".join(out)


def track(x1, y1, x2, y2, net, width=0.1, layer="F.Cu") -> str:
    return (f'\t(segment\n\t\t(start {x1:g} {y1:g})\n\t\t(end {x2:g} {y2:g})\n'
            f'\t\t(width {width:g})\n\t\t(layer "{layer}")\n\t\t(net "{net}")\n\t)\n')


def via(x, y, net, size=0.4) -> str:
    return (f'\t(via\n\t\t(at {x:g} {y:g})\n\t\t(size {size:g})\n\t\t(drill 0.2)\n'
            f'\t\t(layers "F.Cu" "B.Cu")\n\t\t(net "{net}")\n\t)\n')


def drc_report(open_pairs=(), violations=()) -> dict:
    """A DRC report shaped like `kicad-cli pcb drc --format json`."""
    return {
        "unconnected_items": [
            {"type": "unconnected_items", "severity": "error",
             "items": [{"description": f"Pad {pad} [{net}] of {ref}"}
                       for ref, pad, net in pair]}
            for pair in open_pairs
        ],
        "violations": [{"type": t, "severity": s, "description": t}
                       for t, s in violations],
    }


def audit(text: str, ref="U1", drc=None, prefixes=None):
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "b.kicad_pcb"
        path.write_text(text)
        return escape_audit.audit(path, ref, drc,
                                  prefixes or escape_audit.DEFAULT_POWER_PREFIXES)


class PackageReadingTest(unittest.TestCase):
    """The part has to be read correctly before anything about it can be measured."""

    def test_no_connect_balls_are_not_counted_as_netted(self) -> None:
        """A ball with no (net ...) must not inherit the next pad's net.

        This is the defect that turned 107 no-connects into 55 on the real part and
        had a fan-out tool dropping vias on dead balls -- 23 shorts, reported as
        GND against <no net>.  A netted count that is too high also inflates the
        escape denominator, which makes a bad result look like a good one.
        """
        result = audit(board_text(bga("U1", 10, 10, 0, {"A1": "GND", "B2": "SIG"})))
        self.assertEqual(result["package"]["balls"], 16)
        self.assertEqual(result["package"]["netted"], 2)
        self.assertEqual(result["package"]["no_connect"], 14)

    def test_pitch_and_grid_are_measured_not_assumed(self) -> None:
        result = audit(board_text(bga("U1", 20, 20, 0, {"A1": "GND"}, n=6, pitch=0.65)))
        self.assertEqual(result["package"]["grid"], "6x6")
        self.assertAlmostEqual(result["package"]["pitch_mm"], 0.65, places=3)

    def test_ring_depth_counts_inward_from_the_outermost_row(self) -> None:
        """Ring 0 is the outside edge; it is what separates capacity from other faults."""
        result = audit(board_text(bga("U1", 10, 10, 0,
                                      {"A1": "N1", "B2": "N2", "C3": "N3", "D4": "N4"})))
        rings = {b["pad"]: b["ring"] for b in result["balls"]}
        self.assertEqual(rings["A1"], 0)
        self.assertEqual(rings["B2"], 1)
        self.assertEqual(rings["C3"], 1)
        self.assertEqual(rings["D4"], 0)

    def test_a_rotated_footprint_puts_its_pads_in_the_right_place(self) -> None:
        """Rotation is applied to pads, not just the footprint origin.

        A board where 17 rotated parts had unrotated pads shipped once in this
        family of tools; the copper contact test is meaningless if the pad centre
        is wrong, and it fails silently in the safe direction (everything looks
        stranded).
        """
        pitch = 0.8
        span = (4 - 1) * pitch / 2.0          # A1 sits at (-span, -span) unrotated
        text = board_text(
            bga("U1", 10.0, 10.0, 90, {"A1": "SIG"}),
            # at +90 degrees, (-span, -span) maps to (+span, -span)
            track(10.0 + span, 10.0 - span, 12.0, 10.0 - span, "SIG"))
        result = audit(text)
        a1 = [b for b in result["balls"] if b["pad"] == "A1"][0]
        self.assertTrue(a1["escaped"], "a rotated footprint's pad was mislocated")

    def test_both_footprint_placement_spellings_are_accepted(self) -> None:
        """`(at X Y A)` and `(transform (translate ...) (rotate ...))` are the same thing.

        A board round-tripped through kicad-cli comes back in the transform form
        even if it went in as `(at ...)`, so a reader that knows only one spelling
        works before the routing run and not after it.
        """
        transform_form = audit(board_text(
            bga("U1", 10, 10, 0, {"A1": "SIG"}),
            track(9.0, 8.8, 10.0 - 1.2, 8.8, "SIG")))
        at_form_text = board_text(
            bga("U1", 10, 10, 0, {"A1": "SIG"}).replace(
                "(transform (translate 10 10) (rotate 0))", "(at 10 10 0)"),
            track(9.0, 8.8, 10.0 - 1.2, 8.8, "SIG"))
        at_form = audit(at_form_text)
        self.assertEqual([b["ring"] for b in transform_form["balls"]],
                         [b["ring"] for b in at_form["balls"]])
        self.assertEqual(transform_form["escape"]["all"], at_form["escape"]["all"])


class EscapeGeometryTest(unittest.TestCase):
    """What counts as "this ball got out". Every case here was a real judgement call."""

    def setUp(self) -> None:
        # One netted ball, A1, at (10 - 1.2, 10 - 1.2) = (8.8, 8.8) on a 4x4 0.8 grid.
        self.nets = {"A1": "SIG"}
        self.a1 = (8.8, 8.8)

    def one(self, copper: str):
        result = audit(board_text(bga("U1", 10, 10, 0, self.nets), copper))
        return [b for b in result["balls"] if b["pad"] == "A1"][0]

    def test_a_track_ending_on_the_pad_escapes_it(self) -> None:
        ball = self.one(track(self.a1[0], self.a1[1], 5.0, 5.0, "SIG"))
        self.assertTrue(ball["escaped"])
        self.assertEqual(ball["escape"], "track")

    def test_a_track_crossing_the_pad_escapes_it_too(self) -> None:
        """KiCad's connectivity is by overlap, not by endpoint.

        An endpoint-only test reports a ball as stranded when a track runs straight
        across its pad and out the other side -- which is exactly what an optimizer
        pass produces when it straightens a route through a ball field.
        """
        ball = self.one(track(self.a1[0] - 2.0, self.a1[1], self.a1[0] + 2.0, self.a1[1],
                              "SIG"))
        self.assertTrue(ball["escaped"])
        self.assertEqual(ball["escape"], "track")

    def test_a_via_overlapping_the_pad_escapes_it(self) -> None:
        """Via-in-pad: the ball's only copper is the via, and it counts."""
        ball = self.one(via(self.a1[0] + 0.25, self.a1[1], "SIG"))
        self.assertTrue(ball["escaped"])
        self.assertEqual(ball["escape"], "via")

    def test_a_dogbone_via_in_the_diagonal_gap_does_not_touch_the_pad(self) -> None:
        """The stitch via is NOT what connects a plane ball -- the stub to it is.

        `stitch.py` puts a 0.40 mm via in the diagonal gap between four balls, whose
        centre sits 0.566 mm from each ball centre: 0.366 mm beyond the pad rim, so
        via and pad do not overlap at all.  Crediting the via with the escape would
        report a ball as escaped when the segment that actually reaches it is missing
        -- which is precisely the fanout-stub-without-its-trace failure this whole
        measurement is about.  So: via alone is not contact; via plus its stub is.
        """
        diagonal = 0.8 / 2.0
        via_only = self.one(via(self.a1[0] + diagonal, self.a1[1] + diagonal, "SIG"))
        self.assertFalse(via_only["escaped"],
                         "a via 0.566 mm from the ball centre is not touching it")
        self.assertAlmostEqual(
            math.hypot(diagonal, diagonal) - 0.2 - 0.2, 0.1657, places=3,
            msg="the dogbone geometry changed; re-derive the clearance")

        with_stub = self.one(via(self.a1[0] + diagonal, self.a1[1] + diagonal, "SIG")
                             + track(self.a1[0], self.a1[1],
                                     self.a1[0] + diagonal, self.a1[1] + diagonal,
                                     "SIG", width=0.2))
        self.assertTrue(with_stub["escaped"])
        self.assertEqual(with_stub["escape"], "track")

    def test_copper_of_another_net_does_not_escape_the_ball(self) -> None:
        ball = self.one(track(self.a1[0], self.a1[1], 5.0, 5.0, "OTHER")
                        + via(self.a1[0], self.a1[1], "OTHER"))
        self.assertFalse(ball["escaped"])
        self.assertEqual(ball["escape"], "none")

    def test_same_net_copper_far_away_does_not_escape_the_ball(self) -> None:
        """The case that produced two of the seven stranded balls on the real board.

        `ONOFF` had 13 tracks on the board and its nearest copper was 34.669 mm from
        the ball.  Counting "the net has copper" as "the ball escaped" would have
        reported 36/36 signal balls escaped on a board where five had not.
        """
        ball = self.one(track(40.0, 40.0, 45.0, 45.0, "SIG"))
        self.assertFalse(ball["escaped"])

    def test_a_stub_that_stops_short_of_the_pad_does_not_escape_it(self) -> None:
        """DCDC_LP's 0.009 mm stub sat 0.009 mm from the ball and did not touch it.

        Sub-tolerance gaps are the difference between a connected pad and an
        unconnected one, so the contact test must not round them away.  0.05 mm of
        clear space beyond the pad+track envelope is not contact.
        """
        gap = 0.2 + 0.05 + 0.05          # pad half + track half + clear space
        ball = self.one(track(self.a1[0] + gap, self.a1[1],
                              self.a1[0] + gap + 0.5, self.a1[1], "SIG"))
        self.assertFalse(ball["escaped"])

    def test_touching_the_pad_edge_within_tolerance_still_escapes(self) -> None:
        """Board coordinates are finer than a router's rounding.

        Requiring exact overlap calls a track that visibly lands on the pad
        "stranded", which reads as a routing failure that is not there.
        """
        touch = 0.2 + 0.05 - 0.001       # pad half + track half, just inside
        ball = self.one(track(self.a1[0] + touch, self.a1[1],
                              self.a1[0] + touch + 0.5, self.a1[1], "SIG"))
        self.assertTrue(ball["escaped"])

    def test_a_no_connect_ball_is_neither_escaped_nor_stranded(self) -> None:
        """107 of the real part's 196 balls are no-connects; counting them either way
        would swamp the measurement."""
        result = audit(board_text(bga("U1", 10, 10, 0, {"A1": "SIG"})))
        nc = [b for b in result["balls"] if b["pad"] == "A2"][0]
        self.assertIsNone(nc["escaped"])
        self.assertIsNone(nc["role"])
        self.assertEqual(nc["escape"], "no-connect")
        self.assertEqual(result["escape"]["all"]["total"], 1)


class RoleSplitTest(unittest.TestCase):
    """Power and signal balls escape by different mechanisms and are reported apart."""

    def test_power_prefixes_classify_plane_balls(self) -> None:
        result = audit(board_text(bga("U1", 10, 10, 0,
                                      {"A1": "GND", "A2": "3V3", "A3": "VDD_SOC_IN",
                                       "A4": "SPI_SCK"})))
        roles = {b["pad"]: b["role"] for b in result["balls"] if b["net"]}
        self.assertEqual(roles, {"A1": "power", "A2": "power", "A3": "power",
                                 "A4": "signal"})
        self.assertEqual(result["escape"]["power"]["total"], 3)
        self.assertEqual(result["escape"]["signal"]["total"], 1)

    def test_power_prefixes_are_overridable(self) -> None:
        """A design with different rail names must not silently classify them as signal."""
        result = audit(board_text(bga("U1", 10, 10, 0, {"A1": "V_BATT", "A2": "SPI_SCK"})),
                       prefixes=("V_",))
        roles = {b["pad"]: b["role"] for b in result["balls"] if b["net"]}
        self.assertEqual(roles, {"A1": "power", "A2": "signal"})


class NetCompletionTest(unittest.TestCase):
    """Escape and net completion are different claims; conflating them is the bug."""

    def test_an_escaped_ball_whose_net_is_open_elsewhere_is_named_as_such(self) -> None:
        """The real board's L1: escaped, and still inside an unconnected item.

        Reading the unconnected count as an escape count makes this ball evidence of
        an escape failure.  It is not: its copper is on the pad, and the open link is
        somewhere else on the net.
        """
        text = board_text(bga("U1", 10, 10, 0, {"A1": "DCDC_IN", "A2": "DCDC_IN"}),
                          track(8.8, 8.8, 5.0, 5.0, "DCDC_IN"))
        drc = drc_report(open_pairs=[[("U1", "A1", "DCDC_IN"), ("U1", "A2", "DCDC_IN")]])
        result = audit(text, drc=drc)
        named = {b["pad"] for b in result["net_completion"]["escaped_but_net_open"]}
        self.assertEqual(named, {"A1"})
        self.assertEqual(result["escape"]["all"]["stranded"], ["A2"])
        self.assertEqual(result["net_completion"]["pads_in_open_items"], ["A1", "A2"])

    def test_open_items_on_other_footprints_are_not_attributed_here(self) -> None:
        text = board_text(bga("U1", 10, 10, 0, {"A1": "GND"}), via(9.2, 9.2, "GND"))
        drc = drc_report(open_pairs=[[("U9", "4", "USB1_DN"), ("U8", "3", "GND")]])
        result = audit(text, drc=drc)
        self.assertEqual(result["net_completion"]["pads_in_open_items"], [])
        self.assertEqual(result["net_completion"]["unconnected_items_board_wide"], 1)

    def test_errors_are_counted_apart_from_warnings(self) -> None:
        """"Zero errors" is the fab gate; a warning count does not answer it."""
        text = board_text(bga("U1", 10, 10, 0, {"A1": "GND"}), via(9.2, 9.2, "GND"))
        drc = drc_report(violations=[("track_width", "error"),
                                     ("track_width", "error"),
                                     ("silk_over_copper", "warning")])
        result = audit(text, drc=drc)
        self.assertEqual(result["net_completion"]["violations_board_wide"], 3)
        self.assertEqual(result["net_completion"]["errors_board_wide"], 2)

    def test_the_audit_runs_without_a_drc_report(self) -> None:
        """Escape is purely geometric, so it must be answerable with no DRC at all."""
        result = audit(board_text(bga("U1", 10, 10, 0, {"A1": "SIG"}),
                                  track(8.8, 8.8, 5.0, 5.0, "SIG")))
        self.assertNotIn("net_completion", result)
        self.assertEqual(result["escape"]["all"]["escaped"], 1)


@unittest.skipUnless(MEASUREMENT.is_file(),
                     "the measurement record was taken on a private board and is not published")
class MeasurementRecordTest(unittest.TestCase):
    """The committed evidence file: consistent, attributed, and gated."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.doc = json.loads(MEASUREMENT.read_text())

    def test_the_measurement_is_present_and_identifies_its_subject(self) -> None:
        for key in ("milestone", "proposal", "measured_utc", "subject", "environment",
                    "reproduce", "router_run", "prior_claim", "expect",
                    "stranded_attribution"):
            self.assertIn(key, self.doc, f"measurement lost its {key!r} block")
        subject = self.doc["subject"]
        for key in ("part", "board", "board_sha256", "source_repo", "source_commit",
                    "source_path", "rules_mm"):
            self.assertIn(key, subject)
        self.assertEqual(len(subject["board_sha256"]), 64)
        self.assertEqual(len(subject["source_commit"]), 40,
                         "an abbreviated commit cannot be resolved years later")

    def test_the_escape_totals_are_internally_consistent(self) -> None:
        """Sub-group totals must add to the whole, or the headline is arithmetic."""
        expect = self.doc["expect"]
        escape, package = expect["escape"], expect["package"]
        self.assertEqual(escape["signal"]["total"] + escape["power"]["total"],
                         escape["all"]["total"])
        self.assertEqual(escape["signal"]["escaped"] + escape["power"]["escaped"],
                         escape["all"]["escaped"])
        self.assertEqual(escape["all"]["total"], package["netted"])
        self.assertEqual(package["netted"] + package["no_connect"], package["balls"])
        for group in ("all", "signal", "power"):
            g = escape[group]
            self.assertEqual(len(g["stranded"]), g["total"] - g["escaped"],
                             f"{group}: stranded list does not match the count")
        self.assertEqual(sorted(escape["signal"]["stranded"] + escape["power"]["stranded"]),
                         sorted(escape["all"]["stranded"]))

    def test_every_stranded_ball_has_an_attributed_cause(self) -> None:
        """An unexplained stranded ball is the escape claim quietly failing.

        The whole verdict rests on each of the seven being something other than
        channel exhaustion, so each one needs a named cause and the copper evidence
        it was judged on.
        """
        stranded = set(self.doc["expect"]["escape"]["all"]["stranded"])
        attributed = {row["pad"] for row in self.doc["stranded_attribution"]}
        self.assertEqual(attributed, stranded,
                         "attribution and the stranded list have drifted apart")
        for row in self.doc["stranded_attribution"]:
            for key in ("net", "ring", "role", "same_net_copper_on_board", "cause"):
                self.assertIn(key, row, f"{row['pad']} is missing {key!r}")
            self.assertGreater(len(row["cause"]), 40,
                              f"{row['pad']}: 'cause' is a label, not an explanation")

    def test_the_verdict_rests_on_the_deepest_rings_being_clean(self) -> None:
        """Channel exhaustion strands the innermost rings first.

        That is the signature the prior claim asserted, so the refutation has to
        show it is absent: no stranded ball sits deeper than the deepest ring that
        did escape.
        """
        balls_by_ring = [row["ring"] for row in self.doc["stranded_attribution"]]
        self.assertTrue(balls_by_ring, "no stranded balls to reason about")
        self.assertLessEqual(max(balls_by_ring), 3,
                             "a ring-4+ ball is stranded -- re-examine the capacity claim")

    def test_the_prior_claim_is_recorded_with_its_source_and_a_reason(self) -> None:
        """A refuted claim must stay quotable, or the next agent re-litigates it."""
        prior = self.doc["prior_claim"]
        self.assertIn("ROUTING-ATTEMPTS.md", prior["source"])
        self.assertFalse(prior["still_holds"])
        self.assertGreater(len(prior["why"]), 120)
        self.assertIn("documented_numbers", prior)

    def test_the_run_improved_on_the_documented_plateau(self) -> None:
        """The measured headline, pinned: 18 open items against a documented 28."""
        measured = self.doc["expect"]["net_completion"]["unconnected_items_board_wide"]
        documented = self.doc["prior_claim"]["documented_numbers"]["unconnected_refilled"]
        self.assertLess(measured, documented)
        trajectory = self.doc["router_run"]["pass_trajectory_unrouted"]
        self.assertEqual(len(trajectory), self.doc["router_run"]["passes_requested"])
        self.assertEqual(trajectory[-1], self.doc["router_run"]["router_unrouted"])

    def test_reproduce_names_the_refill_the_numbers_depend_on(self) -> None:
        """Unfilled zones make every plane stitch read as dangling (258 vs 216 on r1).

        A reproduce block that omits --refill-zones sends the next person to a
        different, wrong number and they conclude the measurement was fabricated.
        """
        block = "\n".join(self.doc["reproduce"])
        self.assertIn("--refill-zones", block)
        self.assertIn("--passes 15", block)
        self.assertIn("escape_audit.py", block)

    def test_the_comparator_accepts_the_recorded_measurement(self) -> None:
        """Self-consistency: the expected block must validate against itself."""
        synthetic = {
            "package": self.doc["expect"]["package"],
            "escape": self.doc["expect"]["escape"],
            "net_completion": dict(self.doc["expect"]["net_completion"],
                                   escaped_but_net_open=[]),
        }
        self.assertEqual(escape_audit.compare(synthetic, self.doc), [])

    def test_the_comparator_reports_drift_rather_than_passing_it(self) -> None:
        """A gate that cannot fail is not a gate.

        Each mutation below is a regression someone could ship: one fewer ball
        escaping, a different ball stranded, an extra open item, a new error.
        """
        base = {
            "package": self.doc["expect"]["package"],
            "escape": json.loads(json.dumps(self.doc["expect"]["escape"])),
            "net_completion": dict(self.doc["expect"]["net_completion"],
                                   escaped_but_net_open=[]),
        }
        self.assertEqual(escape_audit.compare(base, self.doc), [])

        fewer = json.loads(json.dumps(base))
        fewer["escape"]["signal"]["escaped"] -= 1
        self.assertTrue(escape_audit.compare(fewer, self.doc),
                        "a lost escape passed the gate")

        swapped = json.loads(json.dumps(base))
        swapped["escape"]["all"]["stranded"] = \
            swapped["escape"]["all"]["stranded"][:-1] + ["A1"]
        self.assertTrue(escape_audit.compare(swapped, self.doc),
                        "a different stranded ball passed the gate")

        worse = json.loads(json.dumps(base))
        worse["net_completion"]["unconnected_items_board_wide"] += 1
        self.assertTrue(escape_audit.compare(worse, self.doc),
                        "an extra open connection passed the gate")

        broken = json.loads(json.dumps(base))
        broken["net_completion"]["errors_board_wide"] += 1
        self.assertTrue(escape_audit.compare(broken, self.doc),
                        "a new DRC error passed the gate")

    def test_reordering_a_stranded_list_is_not_drift(self) -> None:
        """Order is not a claim; only membership is."""
        shuffled = {
            "package": self.doc["expect"]["package"],
            "escape": json.loads(json.dumps(self.doc["expect"]["escape"])),
            "net_completion": dict(self.doc["expect"]["net_completion"],
                                   escaped_but_net_open=[]),
        }
        for group in ("all", "signal", "power"):
            shuffled["escape"][group]["stranded"] = \
                list(reversed(shuffled["escape"][group]["stranded"]))
        self.assertEqual(escape_audit.compare(shuffled, self.doc), [])

    def test_the_baseline_shows_the_router_did_the_work(self) -> None:
        """The delta is the evidence: 0/36 signal balls escaped before the run."""
        before = self.doc["baseline_placement_board"]["escape"]
        after = self.doc["expect"]["escape"]
        self.assertEqual(before["signal"]["escaped"], 0,
                         "the input board must be unrouted, or the delta means nothing")
        self.assertGreater(after["signal"]["escaped"], before["signal"]["escaped"])
        self.assertGreater(after["power"]["escaped"], before["power"]["escaped"])


class MathTest(unittest.TestCase):
    """The one non-obvious primitive, pinned directly."""

    def test_point_to_segment_uses_the_finite_segment(self) -> None:
        """Projecting onto an infinite line reports contact with copper that is not
        there -- a track pointing at a ball from 30 mm away would 'escape' it."""
        seg = {"x1": 0.0, "y1": 0.0, "x2": 1.0, "y2": 0.0}
        self.assertAlmostEqual(escape_audit.point_to_segment(0.5, 0.5, seg), 0.5)
        self.assertAlmostEqual(escape_audit.point_to_segment(5.0, 0.0, seg), 4.0)
        self.assertAlmostEqual(escape_audit.point_to_segment(-3.0, 4.0, seg), 5.0)

    def test_a_zero_length_segment_is_handled_as_a_point(self) -> None:
        """Freerouting emits them (a 0.009 mm stub is in the real measurement); a
        naive projection divides by zero."""
        seg = {"x1": 2.0, "y1": 2.0, "x2": 2.0, "y2": 2.0}
        self.assertAlmostEqual(escape_audit.point_to_segment(2.0, 3.0, seg), 1.0)

    def test_cluster_merges_coordinates_within_tolerance(self) -> None:
        """Row/column detection must survive a board's coordinate rounding without
        merging two genuinely different rows."""
        self.assertEqual(escape_audit.cluster([1.0, 1.001, 2.0], 0.05), [1.0, 2.0])
        self.assertEqual(escape_audit.cluster([1.0, 1.2, 1.4], 0.05), [1.0, 1.2, 1.4])


if __name__ == "__main__":
    unittest.main(verbosity=2)
