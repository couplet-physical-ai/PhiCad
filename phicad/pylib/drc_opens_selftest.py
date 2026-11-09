#!/usr/bin/env python3
# This program source code file is part of KiCad, a free EDA software suite.
#
# Copyright (C) 2026 Exowatt, Inc.
#
# This program is free software: you can redistribute it and/or modify it
# under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
"""Regression checks for deterministic DRC open classification."""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from drc_opens import classify_unconnected_report  # noqa: E402

FIXTURES = HERE / "testdata" / "drc_opens.json"


def check_fixtures() -> None:
    data = json.loads(FIXTURES.read_text(encoding="utf-8"))
    for case in data["cases"]:
        report = case["report"]
        before = json.dumps(report, sort_keys=True)
        result = classify_unconnected_report(report)
        expected = case["expected"]
        actual = {key: result[key] for key in expected}
        assert actual == expected, f"{case['name']}: {actual} != {expected}"
        assert json.dumps(report, sort_keys=True) == before, f"{case['name']}: input mutated"
        assert result["raw_items"] == report.get("unconnected_items", [])
        assert all(item["reason"] and item["evidence"] for item in result["items"])

        if case["name"] == "ambiguous":
            assert result["status"] == "uncertain"
            assert result["clean"] is False
            assert result["no_real_opens"] is False
        elif case["name"] == "zone-island":
            assert result["status"] == "zone_island_only"
            assert result["clean"] is False
            assert result["no_real_opens"] is True


def check_uuid_evidence() -> None:
    """Stable UUID/object type evidence wins even when descriptions are unhelpful."""
    board_text = """(kicad_pcb
  (segment (uuid \"00000007-0000-4000-8000-000000000001\"))
  (via (uuid \"00000007-0000-4000-8000-000000000002\"))
)\n"""
    report = {
        "unconnected_items": [{
            "type": "unconnected_items",
            "description": "Unconnected items",
            "severity": "error",
            "items": [
                {"uuid": "00000007-0000-4000-8000-000000000001",
                 "description": "Copper object A", "pos": {"x": 0, "y": 0}},
                {"uuid": "00000007-0000-4000-8000-000000000002",
                 "description": "Copper object B", "pos": {"x": 1, "y": 1}},
            ],
        }],
    }
    with tempfile.TemporaryDirectory(prefix="drc-opens-") as tmp:
        board = Path(tmp) / "uuid-fixture.kicad_pcb"
        board.write_text(board_text, encoding="utf-8")
        result = classify_unconnected_report(report, board)

    assert result["real_open_count"] == 1
    assert result["ambiguous_count"] == 0
    assert result["items"][0]["confidence"] == "high"
    assert result["evidence"]["board_uuid_count"] == 2


def main() -> int:
    check_fixtures()
    check_uuid_evidence()
    print("ok - DRC open classification fixtures and UUID evidence")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
