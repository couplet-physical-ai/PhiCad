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
"""Focused tests for the provider-neutral automatic design repair loop."""

from __future__ import annotations

import json
import unittest
from dataclasses import dataclass

import design_agent


BASE_DESIGN = {
    "project": "sensor-node",
    "parts": {"J1": {"footprint": "Connector:Header"}},
    "nets": {"GND": ["J1.1"]},
}

INVALID_REPORT = {
    "ok": False,
    "project": "sensor-node",
    "parts": 1,
    "nets": 1,
    "no_connect": 0,
    "problems": [{
        "severity": "error",
        "where": "J1",
        "message": "pad 2 is in no net",
        "hint": "put J1.2 in one net or no_connect",
    }],
}

VALID_REPORT = {
    "ok": True,
    "project": "sensor-node",
    "parts": 1,
    "nets": 1,
    "no_connect": 1,
    "problems": [],
}


class FakeSession:
    def __init__(self, reports: list[object]) -> None:
        self.reports = list(reports)
        self.saved: list[dict] = []

    def save(self, design: dict) -> None:
        self.saved.append(json.loads(json.dumps(design)))

    def check_report(self) -> dict:
        if not self.reports:
            raise AssertionError("unexpected extra check")
        report = self.reports.pop(0)
        if isinstance(report, Exception):
            raise report
        return report


@dataclass
class FakeModel:
    replies: list[object]

    @property
    def capabilities(self) -> design_agent.ModelCapabilities:
        return design_agent.ModelCapabilities(
            provider="fake",
            model="future-model",
            structured_output=True,
            tool_calls=True,
            streaming=True,
            vision=True,
            background_tasks=True,
            session_continuity=True,
        )

    def __post_init__(self) -> None:
        self.prompts: list[str] = []

    def ask(self, message: str) -> str:
        self.prompts.append(message)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return str(reply)


def fenced(design: dict) -> str:
    return "Corrected design.\n```json\n" + json.dumps(design) + "\n```"


class DesignValidationWorkflowTest(unittest.TestCase):
    def test_invalid_design_is_repaired_and_rechecked_without_user_relay(self) -> None:
        corrected = dict(BASE_DESIGN, no_connect=["J1.2"])
        session = FakeSession([INVALID_REPORT, VALID_REPORT])
        model = FakeModel([fenced(corrected)])
        events: list[design_agent.WorkflowEvent] = []
        workflow = design_agent.DesignValidationWorkflow(session, model, max_repairs=3)

        result = workflow.validate(BASE_DESIGN, events.append)

        self.assertTrue(result.ok)
        self.assertEqual(result.design, corrected)
        self.assertEqual(result.repairs, 1)
        self.assertEqual(result.as_dict()["design_digest"],
                         design_agent.design_digest(corrected))
        self.assertEqual(session.saved, [BASE_DESIGN, corrected])
        self.assertEqual(
            [event.kind for event in events],
            ["checking", "validation_failed", "repair_started",
             "design_updated", "checking", "validated"],
        )
        self.assertEqual([event.sequence for event in events], list(range(1, 7)))
        self.assertEqual(len({event.session_id for event in events}), 1)
        self.assertIn("pad 2 is in no net", model.prompts[0])
        self.assertIn("do not ask the user to copy, paste, or diagnose", model.prompts[0])
        self.assertIn(json.dumps(BASE_DESIGN, indent=2, sort_keys=True), model.prompts[0])

    def test_non_design_model_reply_is_retried_automatically(self) -> None:
        corrected = dict(BASE_DESIGN, no_connect=["J1.2"])
        session = FakeSession([INVALID_REPORT, VALID_REPORT])
        model = FakeModel(["I need more information.", fenced(corrected)])
        events: list[design_agent.WorkflowEvent] = []
        workflow = design_agent.DesignValidationWorkflow(session, model, max_repairs=3)

        result = workflow.validate(BASE_DESIGN, events.append)

        self.assertTrue(result.ok)
        self.assertEqual(result.repairs, 2)
        self.assertIn("repair_rejected", [event.kind for event in events])
        self.assertIn("did not contain a complete design", model.prompts[1])

    def test_unchanged_repair_stops_at_bound_and_is_retryable_by_host(self) -> None:
        session = FakeSession([INVALID_REPORT])
        model = FakeModel([fenced(BASE_DESIGN)])
        events: list[design_agent.WorkflowEvent] = []
        workflow = design_agent.DesignValidationWorkflow(session, model, max_repairs=1)

        result = workflow.validate(BASE_DESIGN, events.append)

        self.assertFalse(result.ok)
        self.assertEqual(result.repairs, 1)
        self.assertIn("stopped after 1 attempt", result.failure)
        self.assertEqual(events[-1].kind, "repair_exhausted")

    def test_build_feedback_repairs_before_running_preflight_again(self) -> None:
        corrected = dict(BASE_DESIGN, rules={"clearance": 0.25})
        session = FakeSession([VALID_REPORT])
        model = FakeModel([fenced(corrected)])
        events: list[design_agent.WorkflowEvent] = []
        workflow = design_agent.DesignValidationWorkflow(session, model, max_repairs=2)
        build_feedback = {
            "ok": False,
            "source": "synth-build",
            "problems": [{"severity": "error", "where": "drc", "message": "clearance"}],
        }

        result = workflow.validate(BASE_DESIGN, events.append,
                                   initial_feedback=build_feedback)

        self.assertTrue(result.ok)
        self.assertEqual(result.design, corrected)
        self.assertEqual(events[0].kind, "feedback_received")
        self.assertEqual(events[1].kind, "repair_started")
        self.assertIn("synth-build", model.prompts[0])

    def test_checker_runtime_failure_is_not_sent_to_the_model(self) -> None:
        session = FakeSession([design_agent.DesignError("no venv")])
        model = FakeModel([])
        events: list[design_agent.WorkflowEvent] = []
        workflow = design_agent.DesignValidationWorkflow(session, model, max_repairs=3)

        result = workflow.validate(BASE_DESIGN, events.append)

        self.assertFalse(result.ok)
        self.assertIn("checker could not run", result.failure)
        self.assertEqual(model.prompts, [])
        self.assertEqual(events[-1].kind, "check_error")

    def test_invalid_design_without_model_reports_unavailable(self) -> None:
        session = FakeSession([INVALID_REPORT])
        events: list[design_agent.WorkflowEvent] = []
        workflow = design_agent.DesignValidationWorkflow(session, None, max_repairs=3)

        result = workflow.validate(BASE_DESIGN, events.append)

        self.assertFalse(result.ok)
        self.assertIn("model adapter", result.failure)
        self.assertEqual(events[-1].kind, "repair_unavailable")

    def test_design_digest_is_canonical_across_key_order(self) -> None:
        reordered = {
            "nets": BASE_DESIGN["nets"],
            "parts": BASE_DESIGN["parts"],
            "project": BASE_DESIGN["project"],
        }
        self.assertEqual(design_agent.design_digest(BASE_DESIGN),
                         design_agent.design_digest(reordered))

    def test_capabilities_and_events_are_json_serializable_for_host_bridges(self) -> None:
        model = FakeModel([])
        workflow = design_agent.DesignValidationWorkflow(FakeSession([]), model)
        event = design_agent.WorkflowEvent(
            kind="checking",
            message="checking",
            data={"project": "sensor-node"},
            session_id="00000000-0000-0000-0000-000000000001",
            sequence=1,
        )

        encoded = json.dumps({
            "capabilities": workflow.capabilities,
            "event": event.as_dict(),
        })

        self.assertIn("phicad.design-workflow.event/v1", encoded)
        self.assertIn('"vision": true', encoded)

    def test_event_messages_use_real_plurals(self) -> None:
        """Regression: live runs printed "1 blocking problem(s)" to the user."""
        one = dict(INVALID_REPORT)
        session = FakeSession([one, VALID_REPORT])
        model = FakeModel([fenced(dict(BASE_DESIGN, no_connect=["J1.2"]))])
        events: list[design_agent.WorkflowEvent] = []

        design_agent.DesignValidationWorkflow(session, model, 3).validate(
            BASE_DESIGN, events.append)

        messages = [event.message for event in events]
        self.assertIn("Synth found 1 blocking problem.", messages)
        self.assertIn("Design passed after 1 automatic repair.", messages)
        for message in messages:
            self.assertNotIn("(s)", message)


class FailedGateParsingTest(unittest.TestCase):
    """`_failed_gate` names the gate in the model's `where`, from a real build log."""

    def setUp(self) -> None:
        try:
            import new_project_gui
        except Exception as exc:                                  # pragma: no cover
            self.skipTest(f"Tk GUI module unavailable: {exc}")
        self.gui = new_project_gui

    def test_last_failing_gate_is_extracted_from_a_real_log(self) -> None:
        log = ("PASS  validate  —  3 parts, 4 nets\n"
               "PASS  outline  —  5 items: edge cuts, ground pour\n"
               "FAIL  route  —  13 segments, 1 vias, 1 UNROUTED\n"
               "----------------------------------------------------------------\n"
               "10/11 stages passed\n")
        self.assertEqual(self.gui._failed_gate(log), "route")

    def test_no_failing_gate_is_not_guessed(self) -> None:
        self.assertEqual(self.gui._failed_gate("PASS  validate  —  ok"), "")
        self.assertEqual(self.gui._failed_gate(""), "")


class RepairHistoryTest(unittest.TestCase):
    """A later repair attempt must not look like the first one to the model."""

    def test_rejected_attempts_are_named_in_the_next_prompt(self) -> None:
        # Two failing checks, then a pass: the third prompt must list attempts 1 and 2.
        second_failure = {
            "ok": False,
            "problems": [{"severity": "error", "where": "R2",
                          "message": "pad 2 is not connected to any net"}],
        }
        # Each reply must differ from the last design, or the loop rejects it as an
        # unchanged repair before it ever asks again.
        first_try = json.loads(json.dumps(BASE_DESIGN))
        first_try["nets"] = {"SDA": ["J1.3", "R1.1"]}
        second_try = json.loads(json.dumps(BASE_DESIGN))
        second_try["nets"] = {"SDA": ["J1.3", "R1.1"], "SCL": ["J1.4", "R2.1"]}

        session = FakeSession([INVALID_REPORT, second_failure, VALID_REPORT])
        model = FakeModel([fenced(first_try), fenced(second_try)])
        workflow = design_agent.DesignValidationWorkflow(session, model, max_repairs=4)

        outcome = workflow.validate(BASE_DESIGN)
        self.assertTrue(outcome.ok, outcome.failure)
        self.assertEqual(outcome.repairs, 2)
        self.assertEqual(len(model.prompts), 2)

        # The first repair has nothing to look back on.
        self.assertNotIn("REJECTED", model.prompts[0])

        # The second must carry attempt 1's rejection, quoting the gate's own words.
        second = model.prompts[1]
        self.assertIn("REJECTED", second)
        self.assertIn("attempt 1:", second)
        self.assertIn("J1", second)

    def test_summary_prefers_failed_build_stages_over_problem_text(self) -> None:
        summary = design_agent._rejection_summary(
            {"ok": False, "failed": ["route", "drc"], "problems": [{"message": "ignored"}]})
        self.assertIn("route", summary)
        self.assertIn("drc", summary)
        self.assertNotIn("ignored", summary)

    def test_summary_counts_the_problems_it_does_not_show(self) -> None:
        report = {"ok": False, "problems": [
            {"where": "A", "message": "first"},
            {"where": "B", "message": "second"},
            {"where": "C", "message": "third"},
            {"where": "D", "message": "fourth"},
        ]}
        summary = design_agent._rejection_summary(report)
        self.assertIn("first", summary)
        self.assertIn("2 more problems", summary)

    def test_summary_never_raises_on_a_shapeless_report(self) -> None:
        for report in ({}, {"problems": []}, {"problems": ["not-a-mapping"]},
                       {"failed": []}):
            self.assertIsInstance(design_agent._rejection_summary(report), str)

    def test_summary_is_bounded_however_big_the_gate_output_is(self) -> None:
        """One line per attempt x 8 attempts: an unbounded line is a prompt-size bug."""
        worst = [
            {"ok": False, "problems": [{"where": "J1", "message": "x" * 5000}]},
            {"ok": False, "failed": [f"stage{i}" for i in range(40)]},
            {"ok": False, "problems": [{"where": "R" * 900, "message": "y" * 900}]},
        ]
        for report in worst:
            summary = design_agent._rejection_summary(report)
            self.assertLessEqual(len(summary), design_agent.SUMMARY_LIMIT,
                                 f"unbounded summary: {len(summary)} chars")

    def test_summary_is_a_single_line(self) -> None:
        """History is one line per attempt; an embedded newline breaks that listing."""
        summary = design_agent._rejection_summary(
            {"ok": False, "problems": [{"where": "J1", "message": "a\nb\r\nc\td"}]})
        self.assertNotIn("\n", summary)
        self.assertNotIn("\r", summary)
        self.assertIn("a b c d", summary)

    def test_extra_failed_stages_are_counted_not_dropped(self) -> None:
        summary = design_agent._rejection_summary(
            {"ok": False, "failed": ["route", "drc", "erc", "place", "emit"]})
        self.assertIn("route", summary)
        self.assertIn("2 more stages", summary)


class CodexSafeSchemaTests(unittest.TestCase):
    """The derived-schema transform: widen-only, and honest about what cannot fit."""

    def _derive(self, schema: dict):
        import tempfile
        from pathlib import Path

        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump(schema, f)
        self.addCleanup(lambda: Path(f.name).unlink(missing_ok=True))
        out = design_agent._codex_safe_schema(Path(f.name))
        return json.loads(Path(out).read_text()) if out else None

    def test_fitting_schema_is_transformed(self) -> None:
        derived = self._derive({
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "width": {"oneOf": [{"type": "number"}, {"type": "string"}]},
            },
            "required": ["name"],
        })
        assert derived is not None
        # every property required; the optional one became nullable
        self.assertEqual(derived["required"], ["name", "width"])
        self.assertIn({"type": "null"}, derived["properties"]["width"]["anyOf"])
        self.assertIs(derived["additionalProperties"], False)
        # oneOf became anyOf inside the wrapped schema
        self.assertNotIn("oneOf", json.dumps(derived))

    def test_open_key_map_declines(self) -> None:
        # The strict validator has no spelling for a map (probed live); the
        # derivation must say so rather than emit a schema that fails per turn.
        derived = self._derive({
            "type": "object",
            "properties": {"m": {"type": "object",
                                  "additionalProperties": {"type": "string"}}},
            "required": ["m"],
        })
        self.assertIsNone(derived)

    def test_real_design_ir_declines_today(self) -> None:
        # parts/nets are open-key maps -- the whole contract is unfit.  The day
        # this test FAILS is the day binding turns on by itself; delete it then.
        import design_commands

        root = design_commands.repo_root()
        if root is None:
            self.skipTest("not running from a checkout")
        schema = root / "phicad" / "synth" / "design-ir.schema.json"
        self.assertIsNone(design_agent._codex_safe_schema(schema))

    def test_rejected_constraint_keywords_are_dropped(self) -> None:
        derived = self._derive({
            "type": "object",
            "properties": {"tags": {"type": "array", "items": {"type": "string"},
                                     "uniqueItems": True}},
            "required": ["tags"],
            "propertyNames": {"pattern": "^[a-z]+$"},
        })
        assert derived is not None
        blob = json.dumps(derived)
        self.assertNotIn("uniqueItems", blob)
        self.assertNotIn("propertyNames", blob)


class DesignSessionWorkdirTests(unittest.TestCase):
    def test_caller_supplied_workdir_is_created(self) -> None:
        # The default workdir exists because mkdtemp made it; a caller-supplied
        # one used to be trusted blindly, and the first save() then failed two
        # steps later as "Synth checker could not run: FileNotFoundError".
        import tempfile
        from pathlib import Path

        import design_commands

        workdir = Path(tempfile.mkdtemp()) / "not" / "yet" / "made"
        session = design_commands.DesignSession(workdir=workdir)
        path = session.save({"project": "x", "parts": {}, "nets": {}})
        self.assertTrue(path.is_file())
        self.assertEqual(json.loads(path.read_text())["project"], "x")


class ExtractDesignRobustnessTests(unittest.TestCase):
    """Replies as real backends produce them, not as we wish they did."""

    def test_prose_preamble_before_naked_json(self) -> None:
        import design_commands

        reply = ("I’ll check the design IR contract for the required fields.\n\n"
                 '{"project": "x", "parts": {"C1": "10uF"}, "nets": {"GND": []}}')
        design = design_commands.extract_design(reply)
        assert design is not None
        self.assertEqual(design["project"], "x")

    def test_fenced_block_still_wins_over_naked_json(self) -> None:
        import design_commands

        reply = ('{"project": "naked", "parts": {}, "nets": {}}\n'
                 '```json\n{"project": "fenced", "parts": {}, "nets": {}}\n```')
        design = design_commands.extract_design(reply)
        assert design is not None
        self.assertEqual(design["project"], "fenced")

    def test_braces_inside_strings_do_not_confuse_the_scan(self) -> None:
        import design_commands

        reply = ('note: "{ not a block }" follows\n'
                 '{"project": "y", "title": "a {weird} title", '
                 '"parts": {"R1": "1k"}, "nets": {"N": []}}')
        design = design_commands.extract_design(reply)
        assert design is not None
        self.assertEqual(design["title"], "a {weird} title")

    def test_null_valued_optionals_are_stripped(self) -> None:
        import design_commands

        reply = ('{"project": "z", "title": null, '
                 '"parts": {"C1": {"part": "10uF", "footprint": null}}, '
                 '"nets": {"GND": []}}')
        design = design_commands.extract_design(reply)
        assert design is not None
        self.assertNotIn("title", design)
        self.assertNotIn("footprint", design["parts"]["C1"])


if __name__ == "__main__":
    unittest.main()
