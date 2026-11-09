#!/usr/bin/env python3
# This program source code file is part of KiCad, a free EDA software suite.
#
# Copyright (C) 2026 Exowatt, Inc.
#
# This program is free software: you can redistribute it and/or modify it
# under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
"""Focused fast regressions for schematic-parity no-connect classification."""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from drc_parity import classify_schematic_parity  # noqa: E402

BOARD = """(kicad_pcb
  (version 20240108)
  (net 0 "")
  (net 1 "BOARD_ONLY")
  (footprint "Test:Fixture"
    (property "Reference" "J8")
    (pad "1" thru_hole circle (uuid "00000000-0000-4000-8000-000000000001"))
    (pad "2" thru_hole circle
      (net 1 "BOARD_ONLY")
      (uuid "00000000-0000-4000-8000-000000000002"))
    (pad "3" thru_hole circle (uuid "00000000-0000-4000-8000-000000000003"))
    (pad "4" thru_hole circle
      (net 1 "BOARD_ONLY")
      (uuid "00000000-0000-4000-8000-000000000004"))
  )
)
"""

NETLIST = """(export
  (version "E")
  (nets
    (net
      (code "1")
      (name "/VCC")
      (node (ref "J8") (pin "1") (pinfunction "Pin_1") (pintype "passive")))
    (net
      (code "2")
      (name "unconnected-(J8-3:PC12-Pad3)")
      (node (ref "J8") (pin "3") (pinfunction "3:PC12_3")
        (pintype "passive+no_connect")))
  )
)
"""


def finding(description: str, uuid: str, item_description: str,
            finding_type: str = "net_conflict") -> dict:
    return {
        "description": description,
        "items": [{"description": item_description, "uuid": uuid,
                   "pos": {"x": 100.0, "y": 100.0}}],
        "severity": "warning",
        "type": finding_type,
    }


def classify(findings: list[dict], board: Path, netlist: Path) -> dict:
    report = {"schematic_parity": findings}
    before = json.dumps(report, sort_keys=True)
    result = classify_schematic_parity(report, board, netlist)
    assert json.dumps(report, sort_keys=True) == before, "classifier mutated the raw report"
    assert result["raw_items"] == findings
    assert all(item["raw"] == raw for item, raw in zip(result["items"], findings))
    return result


def check_contract(board: Path, netlist: Path) -> None:
    exact = finding(
        "Pad missing net given by schematic (unconnected-(J8-3:PC12-Pad3))",
        "00000000-0000-4000-8000-000000000003",
        "PTH pad 3 [<no net>] of J8",
    )
    result = classify([exact], board, netlist)
    assert result["status"] == "intentional_only"
    assert result["raw_count"] == 1
    assert result["ignored_count"] == 1
    assert result["effective_count"] == 0
    assert result["unresolved_count"] == 0
    assert result["items"][0]["classification"] == "intentional_no_connect"
    assert result["items"][0]["ignored"] is True
    assert result["items"][0]["evidence"]["board_pad"]["net"] == ""
    assert result["items"][0]["evidence"]["schematic_pin"]["no_connect"] is True

    real_missing = finding(
        "Pad missing net given by schematic (/VCC)",
        "00000000-0000-4000-8000-000000000001",
        "PTH pad 1 [<no net>] of J8",
    )
    board_only = finding(
        "No corresponding pin found in schematic",
        "00000000-0000-4000-8000-000000000004",
        "PTH pad 4 [BOARD_ONLY] of J8",
    )
    real_different = finding(
        "Pad net (BOARD_ONLY) doesn't match net given by schematic (/VCC)",
        "00000000-0000-4000-8000-000000000002",
        "PTH pad 2 [BOARD_ONLY] of J8",
    )
    result = classify([real_missing, board_only, real_different], board, netlist)
    assert result["status"] == "violations"
    assert result["raw_count"] == result["effective_count"] == 3
    assert result["ignored_count"] == 0
    assert [item["classification"] for item in result["items"]] == ["violation"] * 3
    assert all(item["ignored"] is False for item in result["items"])

    mixed = classify([exact, real_missing, board_only, real_different], board, netlist)
    assert mixed["status"] == "violations"
    assert mixed["raw_count"] == 4
    assert mixed["ignored_count"] == 1
    assert mixed["effective_count"] == 3
    assert mixed["by_classification"] == {
        "intentional_no_connect": 1, "violation": 3, "unresolved": 0,
    }


def check_fail_closed(board: Path, netlist: Path) -> None:
    netted_lookalike = finding(
        "Pad missing net given by schematic (unconnected-(J8-3:PC12-Pad3))",
        "00000000-0000-4000-8000-000000000004",
        "PTH pad 4 [BOARD_ONLY] of J8",
    )
    result = classify([netted_lookalike], board, netlist)
    assert result["status"] == "violations"
    assert result["ignored_count"] == 0
    assert result["items"][0]["reason"] == "candidate board pad is netted"

    exact_without_netlist = finding(
        "Pad missing net given by schematic (unconnected-(J8-3:PC12-Pad3))",
        "00000000-0000-4000-8000-000000000003",
        "PTH pad 3 [<no net>] of J8",
    )
    report = {"schematic_parity": [exact_without_netlist]}
    result = classify_schematic_parity(report, board, None)
    assert result["status"] == "uncertain"
    assert result["effective_count"] == 1
    assert result["unresolved_count"] == 1
    assert result["items"][0]["classification"] == "unresolved"
    assert result["items"][0]["ignored"] is False

    wrong_uuid = finding(
        "Pad missing net given by schematic (unconnected-(J8-3:PC12-Pad3))",
        "00000000-0000-4000-8000-000000009999",
        "PTH pad 3 [<no net>] of J8",
    )
    result = classify([wrong_uuid], board, netlist)
    assert result["status"] == "uncertain"
    assert result["ignored_count"] == 0

    wrong_type = finding(
        "Pad missing net given by schematic (unconnected-(J8-3:PC12-Pad3))",
        "00000000-0000-4000-8000-000000000003",
        "PTH pad 3 [<no net>] of J8",
        finding_type="some_future_conflict",
    )
    result = classify([wrong_type], board, netlist)
    assert result["status"] == "violations"
    assert result["ignored_count"] == 0

    malformed = classify_schematic_parity({"schematic_parity": {}}, board, netlist)
    assert malformed["status"] == "uncertain"
    assert malformed["unresolved_count"] == 1


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="drc-parity-") as tmp:
        root = Path(tmp)
        board = root / "fixture.kicad_pcb"
        netlist = root / "fixture.net"
        board.write_text(BOARD, encoding="utf-8")
        netlist.write_text(NETLIST, encoding="utf-8")
        check_contract(board, netlist)
        check_fail_closed(board, netlist)
    print("ok - schematic parity no-connect classifier is exact and fail-closed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
