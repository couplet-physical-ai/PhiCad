#!/usr/bin/env python3
# This program source code file is part of KiCad, a free EDA software suite.
#
# Copyright (C) 2026 Exowatt, Inc.
#
# This program is free software: you can redistribute it and/or modify it
# under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
"""Conservative classification of KiCad schematic-parity no-connect findings.

KiCad's JSON report is kept as the source of truth.  This module only decides whether one
very narrow finding is intentional: an unnetted board pad whose matching schematic pin is on
KiCad's generated ``unconnected-(...-PadN)`` net and is explicitly marked no-connect.  Every
other finding remains active.  Missing or contradictory evidence also remains active.
"""

from __future__ import annotations

import copy
import re
from pathlib import Path
from typing import Any

import sexp

_RULES_VERSION = "schematic-parity-v1"
_MISSING_NET_RE = re.compile(
    r"^Pad missing net given by schematic \((unconnected-\(.+-Pad[^()]+\))\)$"
)


def has_no_connect_candidate(report: dict[str, Any]) -> bool:
    """Whether a raw report contains a finding worth gathering no-connect evidence for."""
    findings = report.get("schematic_parity")
    if not isinstance(findings, list):
        return False
    return any(_candidate_net(item) is not None for item in findings)


def _candidate_net(finding: Any) -> str | None:
    if not isinstance(finding, dict) or finding.get("type") != "net_conflict":
        return None
    description = finding.get("description")
    if not isinstance(description, str):
        return None
    match = _MISSING_NET_RE.fullmatch(description)
    return match.group(1) if match else None


def _resolved_pad_net(pad: list, table: dict[str, str]) -> tuple[str | None, str | None]:
    """Return (net name, error); an empty name is a proven unnetted pad."""
    value = sexp.net_of(pad)
    if value is None:
        return "", None
    if getattr(value, "quoted", False):
        return str(value), None
    number = str(value)
    if number == "0":
        return "", None
    if number not in table:
        return None, f"board net id {number!r} is absent from the net table"
    return table[number], None


def _board_pad_evidence(board_path: Path | None) -> tuple[dict[str, list[dict[str, Any]]], str | None]:
    if board_path is None:
        return {}, "board evidence was not provided"
    try:
        doc = sexp.parse_file(board_path)
        if sexp.head(doc) != "kicad_pcb":
            return {}, f"{board_path.name} is not a kicad_pcb document"
        table = sexp.net_table(doc)
        pads: dict[str, list[dict[str, Any]]] = {}
        for footprint_head in ("footprint", "module"):
            for footprint in sexp.children(doc, footprint_head):
                reference = sexp.fp_property(footprint, "Reference")
                for pad in sexp.children(footprint, "pad"):
                    args = sexp.atoms(pad)
                    number = str(args[0]) if args else None
                    uuid = sexp.scalar(pad, "uuid") or sexp.scalar(pad, "tstamp")
                    if not uuid:
                        continue
                    net, net_error = _resolved_pad_net(pad, table)
                    pads.setdefault(uuid, []).append({
                        "uuid": uuid,
                        "reference": reference,
                        "pad": number,
                        "net": net,
                        "net_error": net_error,
                    })
        return pads, None
    except Exception as exc:
        return {}, f"could not parse board evidence: {exc}"


def _netlist_no_connect_evidence(
        netlist_path: Path | None,
) -> tuple[dict[tuple[str, str], list[dict[str, Any]]], str | None]:
    if netlist_path is None:
        return {}, "schematic netlist evidence was not available"
    try:
        doc = sexp.parse_file(netlist_path)
        if sexp.head(doc) != "export":
            return {}, f"{netlist_path.name} is not a KiCad exported netlist"
        nets = sexp.child(doc, "nets")
        if nets is None:
            return {}, "KiCad exported netlist has no nets block"
        records: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for net in sexp.children(nets, "net"):
            name = sexp.scalar(net, "name")
            nodes = sexp.children(net, "node")
            # A generated no-connect pseudo-net belongs to exactly one pin.  A multi-pin net,
            # however it is named, is real connectivity and must never be ignored.
            if (not name or not name.startswith("unconnected-(") or not name.endswith(")")
                    or len(nodes) != 1):
                continue
            node = nodes[0]
            reference = sexp.scalar(node, "ref")
            pin = sexp.scalar(node, "pin")
            pin_type = sexp.scalar(node, "pintype") or ""
            if not reference or pin is None:
                continue
            records.setdefault((reference, pin), []).append({
                "net": name,
                "pin_type": pin_type,
                "no_connect": "no_connect" in pin_type.split("+"),
            })
        return records, None
    except Exception as exc:
        return {}, f"could not parse schematic netlist evidence: {exc}"


def _classify_one(
        finding: Any,
        board_pads: dict[str, list[dict[str, Any]]],
        board_error: str | None,
        schematic_pins: dict[tuple[str, str], list[dict[str, Any]]],
        netlist_error: str | None,
) -> dict[str, Any]:
    raw = copy.deepcopy(finding)
    base = {"raw": raw, "ignored": False}
    pseudo_net = _candidate_net(finding)
    if pseudo_net is None:
        return {
            **base,
            "classification": "violation",
            "reason": "not the exact no-connect missing-net finding",
            "evidence": {"raw_type": finding.get("type") if isinstance(finding, dict) else None},
        }

    if board_error:
        return {
            **base,
            "classification": "unresolved",
            "reason": "candidate could not be verified against the board",
            "evidence": {"schematic_net": pseudo_net, "error": board_error},
        }

    items = finding.get("items")
    if not isinstance(items, list) or len(items) != 1 or not isinstance(items[0], dict):
        return {
            **base,
            "classification": "unresolved",
            "reason": "candidate does not identify exactly one board item",
            "evidence": {"schematic_net": pseudo_net, "raw_item_count": len(items)
                         if isinstance(items, list) else None},
        }
    item_uuid = items[0].get("uuid")
    matches = board_pads.get(item_uuid, []) if isinstance(item_uuid, str) else []
    if len(matches) != 1:
        return {
            **base,
            "classification": "unresolved",
            "reason": "candidate UUID does not resolve to exactly one board pad",
            "evidence": {"schematic_net": pseudo_net, "item_uuid": item_uuid,
                         "board_pad_matches": len(matches)},
        }

    pad = matches[0]
    pad_evidence = {
        "uuid": item_uuid,
        "reference": pad["reference"],
        "pad": pad["pad"],
        "net": pad["net"],
    }
    if pad["net_error"]:
        return {
            **base,
            "classification": "unresolved",
            "reason": "candidate board pad net could not be resolved",
            "evidence": {"schematic_net": pseudo_net, "board_pad": pad_evidence,
                         "error": pad["net_error"]},
        }
    if pad["net"] != "":
        return {
            **base,
            "classification": "violation",
            "reason": "candidate board pad is netted",
            "evidence": {"schematic_net": pseudo_net, "board_pad": pad_evidence},
        }
    if not pad["reference"] or pad["pad"] is None:
        return {
            **base,
            "classification": "unresolved",
            "reason": "candidate board pad lacks reference or pad-number evidence",
            "evidence": {"schematic_net": pseudo_net, "board_pad": pad_evidence},
        }
    if netlist_error:
        return {
            **base,
            "classification": "unresolved",
            "reason": "candidate could not be verified as a schematic no-connect",
            "evidence": {"schematic_net": pseudo_net, "board_pad": pad_evidence,
                         "error": netlist_error},
        }

    pin_records = schematic_pins.get((pad["reference"], pad["pad"]), [])
    exact_records = [record for record in pin_records
                     if record["net"] == pseudo_net and record["no_connect"]]
    if len(exact_records) != 1:
        return {
            **base,
            "classification": "violation",
            "reason": "schematic does not prove one exact generated no-connect pseudo-net",
            "evidence": {"schematic_net": pseudo_net, "board_pad": pad_evidence,
                         "matching_pin_records": copy.deepcopy(pin_records)},
        }

    return {
        **base,
        "classification": "intentional_no_connect",
        "ignored": True,
        "reason": "exact schematic no-connect pseudo-net is paired with an unnetted board pad",
        "evidence": {"schematic_net": pseudo_net, "board_pad": pad_evidence,
                     "schematic_pin": copy.deepcopy(exact_records[0])},
    }


def classify_schematic_parity(
        report: dict[str, Any], board_path: Path | None = None,
        netlist_path: Path | None = None,
) -> dict[str, Any]:
    """Classify raw ``schematic_parity`` findings without mutating the report.

    ``netlist_path`` is a temporary KiCad s-expression netlist exported from the sibling
    schematic.  It is deliberately authoritative for no-connect status, including hierarchy
    and symbol transforms that a text-only schematic scan cannot safely reconstruct.
    """
    findings_value = report.get("schematic_parity")
    findings = findings_value if isinstance(findings_value, list) else []
    structure_error = None if isinstance(findings_value, list) else (
        "raw report has no schematic_parity list"
    )
    board_pads, board_error = _board_pad_evidence(board_path)
    schematic_pins, netlist_error = _netlist_no_connect_evidence(netlist_path)
    items = [
        _classify_one(finding, board_pads, board_error, schematic_pins, netlist_error)
        for finding in findings
    ]
    counts = {"intentional_no_connect": 0, "violation": 0, "unresolved": 0}
    for item in items:
        counts[item["classification"]] += 1
    ignored_count = counts["intentional_no_connect"]
    effective_count = len(findings) - ignored_count
    unresolved_count = counts["unresolved"] + (1 if structure_error else 0)
    if unresolved_count:
        status = "uncertain"
    elif effective_count:
        status = "violations"
    elif findings:
        status = "intentional_only"
    else:
        status = "clean"
    return {
        "rules_version": _RULES_VERSION,
        "status": status,
        "raw_count": len(findings),
        "ignored_count": ignored_count,
        "effective_count": effective_count,
        "unresolved_count": unresolved_count,
        "by_classification": counts,
        "raw_items": copy.deepcopy(findings),
        "items": items,
        "evidence": {
            "raw_structure_error": structure_error,
            "board": str(board_path) if board_path is not None else None,
            "board_error": board_error,
            "board_pad_uuid_count": len(board_pads),
            "schematic_netlist_available": netlist_path is not None,
            "schematic_netlist_error": netlist_error,
            "schematic_no_connect_pin_count": sum(len(records)
                                                   for records in schematic_pins.values()),
        },
    }
