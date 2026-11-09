#!/usr/bin/env python3
#
# This program source code file is part of KiCad, a free EDA CAD application.
#
# Copyright (C) 2026 Exowatt, Inc.
#
# This program is free software: you can redistribute it and/or modify it
# under the terms of the GNU General Public License as published by the
# Free Software Foundation, either version 2 or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
#
"""Hermetic regression test for the protobuf-JSON stackup report parser.

The fixture is the current ``kiapi.board.BoardStackup`` JSON shape: layer display names are
``userName`` and layer kinds are enum values such as ``BSLT_COPPER``.  It deliberately omits
optional/detail fields that the parser does not need, matching protobuf JSON's omission of unset
fields.  No kicad-cli, venv, network, or board file is needed.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

MCP_DIR = Path(__file__).resolve().parent
FIXTURE = MCP_DIR / "fixtures" / "stackup-protobuf-json.json"

if str(MCP_DIR) not in sys.path:
    sys.path.insert(0, str(MCP_DIR))

from tools_cli import _stackup_json_report, _stackup_report  # noqa: E402

EXPECTED_NAMES = [
    "F.Cu", "In1.Cu", "In2.Cu", "B.Cu", "F.Mask", "B.Mask",
    "F.Silkscreen", "B.Silkscreen", "F.Paste",
]
EXPECTED_COPPER = ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"]


def test_current_schema() -> None:
    """Names come from userName, not the stale name key, in source order."""
    data = json.loads(FIXTURE.read_text())
    report = _stackup_report(FIXTURE, "json")

    assert isinstance(data, dict)
    assert isinstance(data.get("layers"), list)
    assert report == {
        "layer_count": 9,
        "layers": EXPECTED_NAMES,
        "copper_layers": 4,
    }

    copper = [name for name in report["layers"] if name in EXPECTED_COPPER]
    assert copper == EXPECTED_COPPER


def test_unset_optional_fields_are_ignored() -> None:
    """Unset protobuf detail fields do not prevent the layer summary."""
    data = json.loads(FIXTURE.read_text())
    for layer in data["layers"]:
        assert "name" not in layer
        assert "color" not in layer
        assert "details" not in layer
        assert "thickness" not in layer

    report = _stackup_json_report(data)
    assert report["layers"] == EXPECTED_NAMES
    assert report["copper_layers"] == len(EXPECTED_COPPER)


def test_required_summary_fields_stay_validated() -> None:
    """Malformed layer containers and summary fields fail closed."""
    invalid: list[dict[str, Any]] = [
        {"layers": "not a list"},
        {"layers": [None]},
        {"layers": [{"userName": "F.Cu"}]},
        {"layers": [{"userName": 7, "type": "BSLT_COPPER"}]},
        {"layers": [{"userName": "F.Cu", "type": 7}]},
    ]
    for data in invalid:
        assert "parse_error" in _stackup_json_report(data)


def main() -> int:
    tests = [
        test_current_schema,
        test_unset_optional_fields_are_ignored,
        test_required_summary_fields_stay_validated,
    ]
    for test in tests:
        test()
    print(f"stackup JSON regression: {len(tests)} tests passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
