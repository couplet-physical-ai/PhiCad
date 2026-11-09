#!/usr/bin/env python3
# This program source code file is part of KiCad, a free EDA CAD application.
#
# Copyright (C) 2026 Exowatt, Inc.
#
# This program is free software: you can redistribute it and/or modify it
# under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
"""Conservative classification of connectivity findings in a KiCad DRC report.

KiCad deliberately keeps ``unconnected_items`` small and structural: each finding has two
affected board objects, their stable UUIDs, and the objects' descriptions.  This module uses
those facts, rather than reference designators or net names, to distinguish an unrouted copper
edge from a zone/island finding.  The input report is never changed and every output finding
keeps a copy of its raw source object.

The classifier is intentionally not a geometry solver.  If the structured object kind cannot be
established from a board UUID or a KiCad item description, it returns ``ambiguous``.  A caller may
therefore gate on ``no_real_opens`` without turning a disagreement into a false clean result.
"""

from __future__ import annotations

import copy
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)

ROUTED_KINDS = frozenset(("pad", "track", "via"))
ZONE_VIOLATIONS = frozenset(("isolated_copper", "starved_thermal"))

# KiCad's descriptions are translated for interactive users.  These tokens are only a
# fallback when a board is not available; a UUID-resolved type always wins over text.
_DESCRIPTION_KINDS = (
    ("zone", re.compile(r"\b(?:zone|island|isolated\s+copper|rule\s+area|teardrop)\b",
                         re.IGNORECASE)),
    ("pad", re.compile(r"\bpad\b", re.IGNORECASE)),
    ("via", re.compile(r"\bvia\b", re.IGNORECASE)),
    ("track", re.compile(r"\b(?:track|arc|segment)\b", re.IGNORECASE)),
)

_BOARD_KINDS = {
    "pad": "pad",
    "segment": "track",
    "arc": "track",
    "via": "via",
    "zone": "zone",
}


def _normalise(value: Any) -> str:
    return str(value or "").strip().lower().replace("-", "_").replace(" ", "_")


def _uuid(value: Any) -> Optional[str]:
    text = str(value or "").strip().lower()
    return text if UUID_RE.match(text) else None


def _item_uuid(item: Dict[str, Any]) -> Optional[str]:
    for key in ("uuid", "kiid", "tstamp"):
        found = _uuid(item.get(key))
        if found:
            return found
    return None


def _explicit_kind(item: Dict[str, Any]) -> Optional[str]:
    """Read a structured item kind, accepting names used by current and future wrappers."""
    for key in ("kind", "item_type", "board_type", "object_type", "type"):
        value = _normalise(item.get(key))
        if not value:
            continue
        if value in ("pcb_pad_t", "pad"):
            return "pad"
        if value in ("pcb_trace_t", "pcb_arc_t", "track", "arc", "segment"):
            return "track"
        if value in ("pcb_via_t", "via"):
            return "via"
        if value in ("pcb_zone_t", "zone", "zone_layer", "island"):
            return "zone"
    return None


def _description_kind(description: Any) -> Optional[str]:
    text = str(description or "")
    for kind, pattern in _DESCRIPTION_KINDS:
        if pattern.search(text):
            return kind
    return None


def _board_item_kinds(board_path: Optional[Path]) -> Tuple[Dict[str, str], Optional[str]]:
    """Return ``uuid -> object kind`` from a board without using names or net labels."""
    if board_path is None:
        return {}, None

    try:
        try:
            from . import sexp  # type: ignore
        except ImportError:
            import sexp  # type: ignore

        document = sexp.parse_file(board_path)
        result: Dict[str, str] = {}

        def walk(node: Any) -> None:
            if not isinstance(node, list) or not node:
                return
            head = sexp.head(node)
            kind = _BOARD_KINDS.get(head or "")
            if kind:
                for child in node[1:]:
                    if not isinstance(child, list) or not child:
                        continue
                    if sexp.head(child) not in ("uuid", "tstamp"):
                        continue
                    value = _uuid(child[1] if len(child) > 1 else None)
                    if value:
                        result[value] = kind
                        break
            for child in node[1:]:
                if isinstance(child, list):
                    walk(child)

        walk(document)
        return result, None
    except Exception as exc:  # A description-based result is still useful without a board.
        return {}, f"board UUID lookup failed: {type(exc).__name__}: {exc}"


def _entry_items(entry: Dict[str, Any]) -> List[Dict[str, Any]]:
    items = entry.get("items")
    return [item for item in items if isinstance(item, dict)] if isinstance(items, list) else []


def _candidate_entries(report: Dict[str, Any]) -> List[Tuple[str, Dict[str, Any]]]:
    """Keep the raw unconnected list and explicit zone connectivity findings separate."""
    entries: List[Tuple[str, Dict[str, Any]]] = []
    raw = report.get("unconnected_items")
    if isinstance(raw, list):
        entries.extend(("unconnected_items", item) for item in raw if isinstance(item, dict))

    # KiCad reports an isolated filled-zone island in `violations`, not in the ratsnest list.
    # Include these typed findings so a caller cannot count them as routed opens by accident.
    violations = report.get("violations")
    if isinstance(violations, list):
        for item in violations:
            if isinstance(item, dict) and _normalise(item.get("type")) in ZONE_VIOLATIONS:
                entries.append(("violations", item))
    return entries


def _classify_entry(source: str, entry: Dict[str, Any], board_kinds: Dict[str, str]) -> Dict[str, Any]:
    entry_type = _normalise(entry.get("type"))
    items = _entry_items(entry)
    kinds: List[str] = []
    uuids: List[str] = []
    evidence: List[str] = []
    kind_sources: List[str] = []
    conflict = False

    for item in items:
        item_uuid = _item_uuid(item)
        if item_uuid:
            uuids.append(item_uuid)

        board_kind = board_kinds.get(item_uuid) if item_uuid else None
        explicit_kind = _explicit_kind(item)
        description_kind = _description_kind(item.get("description"))
        if board_kind and explicit_kind and board_kind != explicit_kind:
            conflict = True
            evidence.append(f"UUID {item_uuid} resolves to {board_kind}, but structured type says "
                            f"{explicit_kind}")
        kind = board_kind or explicit_kind or description_kind
        if kind:
            kinds.append(kind)
        if board_kind:
            kind_sources.append("board UUID")
            evidence.append(f"UUID {item_uuid} resolves to board object {board_kind}")
        elif explicit_kind:
            kind_sources.append("structured type")
            evidence.append(f"structured item type identifies {explicit_kind}")
        elif description_kind:
            kind_sources.append("description")
            evidence.append(f"KiCad item description identifies {description_kind}")
        elif item_uuid:
            evidence.append(f"UUID {item_uuid} is present but its object type is unavailable")
        else:
            evidence.append("item has no usable UUID or recognised object type")

    if entry_type in ZONE_VIOLATIONS:
        return {
            "classification": "zone_island",
            "confidence": "high",
            "reason": f"KiCad DRC type {entry_type!r} is a filled-zone isolation finding, "
                       "not a routed endpoint edge",
            "evidence": evidence + [f"DRC type is {entry_type}"],
            "item_types": kinds,
            "uuids": uuids,
            "source": source,
            "raw": copy.deepcopy(entry),
        }

    if "zone" in kinds:
        confidence = "high" if any(source_name in ("board UUID", "structured type")
                                   for source_name in kind_sources) else "medium"
        return {
            "classification": "zone_island",
            "confidence": confidence,
            "reason": "a zone/island object is an endpoint; this is not a pad/track/via routed edge",
            "evidence": evidence,
            "item_types": kinds,
            "uuids": uuids,
            "source": source,
            "raw": copy.deepcopy(entry),
        }

    if len(items) == 2 and len(kinds) == 2 and all(kind in ROUTED_KINDS for kind in kinds) and not conflict:
        confidence = "high" if all(source_name in ("board UUID", "structured type")
                                   for source_name in kind_sources) else "medium"
        return {
            "classification": "real_open",
            "confidence": confidence,
            "reason": "the two affected objects are routed copper endpoints (pad, track, or via)",
            "evidence": evidence,
            "item_types": kinds,
            "uuids": uuids,
            "source": source,
            "raw": copy.deepcopy(entry),
        }

    reason = "the finding does not expose exactly two compatible routed endpoints"
    if conflict:
        reason += "; structured object evidence conflicts"
    return {
        "classification": "ambiguous",
        "confidence": "low",
        "reason": reason,
        "evidence": evidence,
        "item_types": kinds,
        "uuids": uuids,
        "source": source,
        "raw": copy.deepcopy(entry),
    }


def classify_unconnected_report(report: Dict[str, Any],
                                board_path: Optional[Path] = None) -> Dict[str, Any]:
    """Classify connectivity findings while retaining every raw report object.

    ``real_open_count`` and ``zone_island_count`` count findings, not endpoints.  The former
    includes pad-pad, pad-track, track-via, and other fully identified routed-copper pairs.
    ``ambiguous_count`` is deliberately not folded into either count.  ``no_real_opens`` is safe
    to use as a gate only when it is true; ``status`` is ``uncertain`` whenever ambiguity exists.
    ``clean`` means the report contained no connectivity findings at all, and is never true for a
    zone-only or ambiguous report.
    """
    if not isinstance(report, dict):
        report = {}
    path = Path(board_path) if board_path is not None else None
    board_kinds, board_error = _board_item_kinds(path)
    findings = [_classify_entry(source, entry, board_kinds)
                for source, entry in _candidate_entries(report)]

    real_open_count = sum(item["classification"] == "real_open" for item in findings)
    zone_island_count = sum(item["classification"] == "zone_island" for item in findings)
    ambiguous_count = sum(item["classification"] == "ambiguous" for item in findings)
    raw_unconnected = report.get("unconnected_items")
    raw_unconnected_count = len(raw_unconnected) if isinstance(raw_unconnected, list) else 0
    zone_violation_count = len(findings) - raw_unconnected_count

    if ambiguous_count:
        status = "uncertain"
    elif real_open_count:
        status = "open"
    elif zone_island_count:
        status = "zone_island_only"
    else:
        status = "clean"

    confidence = "high"
    if any(item["confidence"] == "low" for item in findings):
        confidence = "low"
    elif any(item["confidence"] == "medium" for item in findings):
        confidence = "medium"

    evidence: Dict[str, Any] = {
        "rules_version": "drc-opens-v1",
        "object_kinds": "board UUID, structured item type, then KiCad item description",
        "board_uuid_lookup": bool(path),
        "board_uuid_count": len(board_kinds),
        "raw_items_preserved": True,
        "sources": {
            "unconnected_items": raw_unconnected_count,
            "typed_zone_violations": zone_violation_count,
        },
    }
    if board_error:
        evidence["board_uuid_lookup_error"] = board_error

    return {
        "status": status,
        "clean": status == "clean",
        "no_real_opens": real_open_count == 0 and ambiguous_count == 0,
        "confidence": confidence,
        "raw_count": len(findings),
        "unconnected_item_count": raw_unconnected_count,
        "typed_zone_violation_count": zone_violation_count,
        "real_open_count": real_open_count,
        "zone_island_count": zone_island_count,
        "ambiguous_count": ambiguous_count,
        "evidence": evidence,
        "items": findings,
        # This is intentionally a separate copy rather than an annotated replacement.  A caller
        # can compare it byte-for-byte with report["unconnected_items"] after JSON encoding.
        "raw_items": copy.deepcopy(raw_unconnected if isinstance(raw_unconnected, list) else []),
        "raw_zone_violations": [copy.deepcopy(item["raw"]) for item in findings
                                if item["source"] == "violations"],
    }
