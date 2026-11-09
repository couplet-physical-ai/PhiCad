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
"""Focused regression test for the synth MCP gate verdict.

This test extracts only ``_synth`` so it can run without FastMCP or the build venv.  The
subprocess is replaced with a fake ``_run`` that returns the same shape as a synth CLI
invocation, including exit code 1 and its JSON report.
"""

from __future__ import annotations

import ast
import json
import time
from pathlib import Path
from typing import Any, Callable

SERVER = Path(__file__).with_name("server.py")


def load_synth() -> tuple[Callable[..., dict[str, Any]], dict[str, Any]]:
    """Load the production helper with only its stdlib globals and injectable dependencies."""
    tree = ast.parse(SERVER.read_text(encoding="utf-8"), filename=str(SERVER))
    function = next(node for node in tree.body
                    if isinstance(node, ast.FunctionDef) and node.name == "_synth")
    module = ast.Module(body=[function], type_ignores=[])
    ast.fix_missing_locations(module)
    namespace: dict[str, Any] = {
        "Any": Any,
        "VENV_PY": Path("/bin/sh"),
        "SYNTH_CLI": Path("/bin/sh"),
        "KICAD_CLI": Path("/bin/sh"),
        "_cli_env": lambda: {},
        "_telemetry": None,
        "json": json,
        "time": time,
    }
    exec(compile(module, str(SERVER), "exec"), namespace)
    return namespace["_synth"], namespace


def run_fake(stdout: str, exit_code: int = 1) -> dict[str, Any]:
    """Run _synth against a fake failed CLI result."""
    synth, namespace = load_synth()

    def fake_run(*args: Any, **kwargs: Any) -> dict[str, Any]:
        assert kwargs["ok_codes"] == (0, 1)
        return {
            "ok": True,
            "exit_code": exit_code,
            "stdout": stdout,
            "stderr": "gate diagnostics",
        }

    namespace["_run"] = fake_run
    return synth(["check", "/tmp/design.json"])


def test_synth_gate_verdict() -> None:
    """A parsed failed gate must be a failed MCP result, not merely failed telemetry."""
    failed_report = {
        "ok": False,
        "failed": ["validate"],
        "stages": [{"stage": "validate", "ok": False}],
    }
    result = run_fake(json.dumps(failed_report))
    assert result["ok"] is False
    assert result["exit_code"] == 1
    assert result["report"] == failed_report

    passed_report = {"ok": True, "failed": [], "stages": []}
    assert run_fake(json.dumps(passed_report))["ok"] is True

    for report, message in (
        (["not", "an", "object"], "not an object"),
        ({"stages": []}, "missing"),
        ({"ok": "false"}, "boolean"),
    ):
        malformed = run_fake(json.dumps(report))
        assert malformed["ok"] is False
        assert message in malformed["error"]
        assert malformed["report"] == report

    invalid = run_fake("not-json")
    assert invalid["ok"] is False
    assert "valid JSON" in invalid["error"]
    assert invalid["stdout"] == "not-json"


if __name__ == "__main__":
    test_synth_gate_verdict()
    print("synth verdict regression: passed")
