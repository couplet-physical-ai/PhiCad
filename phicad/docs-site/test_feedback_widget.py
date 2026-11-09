#!/usr/bin/env python3
#
# This program source code file is part of PhiCad, a fork of KiCad.
#
# Copyright (C) 2026 Exowatt, Inc.
# Copyright The KiCad Developers, see AUTHORS.txt for contributors.
#
# SPDX-License-Identifier: GPL-2.0-or-later
#
"""Stdlib checks for the privacy-safe documentation feedback widget."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import unittest


ROOT = Path(__file__).resolve().parent
JS_PATH = ROOT / "docs" / "assets" / "javascripts" / "feedback.js"
CSS_PATH = ROOT / "docs" / "assets" / "stylesheets" / "feedback.css"
GUIDE_PATH = ROOT / "docs" / "guides" / "feedback.md"


class FeedbackWidgetTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.javascript = JS_PATH.read_text(encoding="utf-8")
        cls.stylesheet = CSS_PATH.read_text(encoding="utf-8")
        cls.guide = GUIDE_PATH.read_text(encoding="utf-8")

    def test_global_control_and_accessible_dialog_are_present(self) -> None:
        for required in (
            'id = "phicad-feedback-control"',
            'id = "phicad-feedback-dialog"',
            'setAttribute("aria-haspopup", "dialog")',
            'setAttribute("aria-modal", "true")',
            'setAttribute("aria-labelledby", "phicad-feedback-title")',
            'event.key === "Escape"',
            'event.key !== "Tab"',
            'previouslyFocused',
            'Continue to GitHub',
        ):
            self.assertIn(required, self.javascript)

    def test_feedback_types_and_required_problem_fields_are_present(self) -> None:
        for required in (
            "Report a bug",
            "Request a feature",
            "Improve this page",
            'name="summary"',
            'name="product_version"',
            'name="surface"',
            'name="workflow"',
            'name="command_action"',
            'name="frequency"',
            'name="steps"',
            'name="expected"',
            'name="actual"',
            'name="error_excerpt"',
            'name="project_context"',
            'name="support_bundle_status"',
            'name="severity"',
            'name="problem"',
            'name="outcome"',
            'name="value"',
            'name="acceptance"',
            'name="improvement"',
            'name="page-url" readonly',
        ):
            self.assertIn(required, self.javascript)

        consent = re.search(
            r'<input type="checkbox" id="phicad-feedback-consent"[^>]*>',
            self.javascript,
        )
        self.assertIsNotNone(consent)
        self.assertNotIn("checked", consent.group(0))
        self.assertIn("after maintainer redaction", self.javascript.lower())
        self.assertIn("support bundles, prompts, transcripts", self.javascript.lower())

    def test_issue_url_is_private_repo_and_widget_has_no_background_transport(self) -> None:
        self.assertIn("https://github.com/couplet-physical-ai/PhiCad/issues/new", self.javascript)
        for pattern in (
            r"\bfetch\s*\(",
            r"\bXMLHttpRequest\b",
            r"\bsendBeacon\s*\(",
            r"document\.cookie",
            r"\blocalStorage\b",
            r"\bsessionStorage\b",
            r"\bWebSocket\s*\(",
        ):
            self.assertIsNone(re.search(pattern, self.javascript), pattern)

        self.assertIn("root.location.assign(buildIssueUrl", self.javascript)

    def test_safe_metadata_drops_query_and_fragment(self) -> None:
        node = shutil.which("node")
        if not node:
            self.skipTest("Node.js is not installed")

        script = r"""
const assert = require("node:assert/strict");
const feedback = require(process.argv[1]);
const metadata = {
    canonical_url: "https://docs.example.test/guides/feedback/?token=do-not-include#private",
    page_title: "Feedback guide",
    browser_user_agent: "Test browser",
    browser_language: "en-US",
    viewport: "1200x800",
    timezone: "America/Los_Angeles",
    captured_at_utc: "2026-09-17T12:00:00.000Z"
};
const url = new URL(feedback.buildIssueUrl("bug", {
    summary: "Button fails",
    product_version: "unreleased (469cac52c319)",
    surface: "Documentation",
    workflow: "Open the docs",
    command_action: "Select Feedback",
    frequency: "Every time",
    steps: "Click Feedback",
    expected: "A dialog opens",
    actual: "Nothing happens",
    error_excerpt: "Not provided",
    project_context: "Documentation only",
    support_bundle_status: "Not created",
    severity: "High",
    improvement_consent: false
}, metadata));
assert.equal(url.origin, "https://github.com");
assert.equal(url.pathname, "/couplet-physical-ai/PhiCad/issues/new");
assert.equal(url.searchParams.get("labels"), "feedback,needs-triage,bug");
const body = url.searchParams.get("body");
assert.match(body, /### Steps to reproduce/);
assert.match(body, /after maintainer redaction to evaluate or improve PhiCad: no/);
assert.match(body, /Captured at \(UTC\): 2026-09-17T12:00:00.000Z/);
assert.match(body, /https:\/\/docs\.example\.test\/guides\/feedback\//);
assert.doesNotMatch(body, /token=do-not-include/);
assert.doesNotMatch(body, /#private/);
"""
        completed = subprocess.run(
            [node, "-e", script, os.fspath(JS_PATH)],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(
            completed.returncode,
            0,
            msg=completed.stdout + completed.stderr,
        )

    def test_sensitive_markers_and_oversized_handoffs_are_refused(self) -> None:
        node = shutil.which("node")
        if not node:
            self.skipTest("Node.js is not installed")

        long_value = "x" * 4000
        script = "\n".join(
            (
                "const assert = require('node:assert/strict');",
                "const feedback = require(process.argv[1]);",
                "assert.equal(feedback.containsSensitiveMarker('Authorization: Bearer verysecretvalue'), true);",
                "assert.equal(feedback.containsSensitiveMarker('api_key=sk-examplecredentialvalue'), true);",
                "assert.equal(feedback.containsSensitiveMarker('the token guard rejected a request'), false);",
                "assert.throws(() => feedback.buildIssueUrl('bug', "
                "{summary: 'Authorization: Bearer verysecretvalue'}, {}), TypeError);",
                "assert.throws(() => feedback.buildIssueUrl('feature', "
                + json.dumps(
                    {
                        "problem": long_value,
                        "outcome": long_value,
                        "value": long_value,
                        "acceptance": long_value,
                    }
                )
                + ", {}), RangeError);",
            )
        )
        completed = subprocess.run(
            [node, "-e", script, os.fspath(JS_PATH)],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(completed.returncode, 0, msg=completed.stdout + completed.stderr)

    def test_visual_accessibility_requirements_are_encoded(self) -> None:
        for required in (
            ":focus-visible",
            "prefers-reduced-motion: reduce",
            "max-width: 42rem",
            "--phicad-feedback-focus: #005fcc",
            "--phicad-feedback-action: #b00000",
            "forced-colors: active",
        ):
            self.assertIn(required, self.stylesheet)

    def test_guide_documents_privacy_flow_and_support_attachment(self) -> None:
        for required in (
            "Continue to GitHub",
            "Submit new issue",
            "couplet-physical-ai/PhiCad",
            "cookies",
            "storage",
            "tokens",
            "prompts",
            "transcripts",
            "file contents",
            "environment values",
            "phicad support bundle",
            "attachments, support bundles, prompts, transcripts",
            "query strings and fragments are removed",
        ):
            self.assertIn(required, self.guide)


if __name__ == "__main__":
    unittest.main()
