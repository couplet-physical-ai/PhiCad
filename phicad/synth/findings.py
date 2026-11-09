#
# This program source code file is part of PhiCad, a fork of KiCad.
#
# Copyright (C) 2026 Exowatt, Inc.
# Copyright The KiCad Developers, see AUTHORS.txt for contributors.
#
# SPDX-License-Identifier: GPL-2.0-or-later
#
"""A gate's findings as data, not as printed lines.

WHY THIS EXISTS
---------------
Every gate in the pipeline already knows exactly what is wrong: DRC has the two items that
short, the router knows which connection it abandoned, ERC has the pin.  All of that arrived
as a structured report and then got flattened into one summary string plus, at best, a list of
human sentences.  `FAIL drc — 2 errors` cannot be clicked, filtered, waived, or repaired; a
caller that wants to fix the board has to re-derive from scratch what the gate already knew.

That is the same failure the audit of this repo kept finding in different clothes: *the thing
that judges is not the thing that acts*.  A verdict that carries no evidence forces every
consumer to guess.

So a finding is a record:

    Finding(code="drc.shorting_items", severity=ERROR,
            message="Items shorting two nets (3V3 and VIN)",
            refs=["U1.2", "VIN"], where="F.Cu",
            at=(129.2, 110.8), fix="move the VIN track off U1 pad 2")

STABLE IDENTITY IS THE POINT
----------------------------
`Finding.id` is a digest of (code, refs, rounded position) -- deliberately *not* of the
message, which gets reworded, and not of a list index, which shifts the moment anything else
changes.  That is what lets a waiver stay granted across runs, and what lets a UI say "this is
the same violation you saw last time" rather than showing it as new.  Position is rounded to
1 um before hashing so that re-running a router that nudges a track by a nanometre does not
invent a new violation identity.

The rounding is also the limit of the guarantee: move the offending track a visible distance
and the identity legitimately changes, because it is a different violation in a different
place.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

# Severities, ordered worst-first so `sorted()` puts what matters at the top.
ERROR = "error"
WARNING = "warning"
INFO = "info"

_RANK = {ERROR: 0, WARNING: 1, INFO: 2}


@dataclass
class Finding:
    """One thing a gate found, with enough context to locate and fix it."""

    code: str                                   # "drc.shorting_items", "route.unrouted"
    severity: str                               # ERROR | WARNING | INFO
    message: str                                # human sentence, may be reworded freely
    refs: List[str] = field(default_factory=list)   # "U1.2", "GND", "R3"
    where: str = ""                             # layer, sheet, or file
    at: Optional[Tuple[float, float]] = None    # mm, board coordinates
    fix: str = ""                               # what to change, imperative
    waivable: bool = True

    @property
    def id(self) -> str:
        """Stable identity: same violation in the same place hashes the same.

        Excludes the message on purpose (wording changes are not new violations) and rounds
        the position to 1 um (a nanometre nudge from a re-run is not a new violation either).
        """
        pos = ""
        if self.at is not None:
            pos = f"{round(self.at[0], 3):.3f},{round(self.at[1], 3):.3f}"
        payload = "|".join([self.code, ";".join(sorted(self.refs)), self.where, pos])
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]

    def as_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "id": self.id,
            "code": self.code,
            "severity": self.severity,
            "message": self.message,
        }
        if self.refs:
            out["refs"] = list(self.refs)
        if self.where:
            out["where"] = self.where
        if self.at is not None:
            out["at"] = {"x_mm": self.at[0], "y_mm": self.at[1]}
        if self.fix:
            out["fix"] = self.fix
        if not self.waivable:
            out["waivable"] = False
        return out

    def one_line(self) -> str:
        """The form that goes under a gate's summary line, for a human reading the table."""
        head = f"{self.severity}: {self.message}"
        if self.refs:
            head += f"  [{', '.join(self.refs)}]"
        if self.at is not None:
            head += f"  @ ({self.at[0]:.2f}, {self.at[1]:.2f}) mm"
        return head


def sort_findings(findings: Sequence[Finding]) -> List[Finding]:
    """Worst first, then by code, then by identity so the order never depends on discovery."""
    return sorted(findings, key=lambda f: (_RANK.get(f.severity, 9), f.code, f.id))


def counts(findings: Sequence[Finding]) -> Dict[str, int]:
    out = {ERROR: 0, WARNING: 0, INFO: 0}
    for f in findings:
        if f.severity in out:
            out[f.severity] += 1
    return out


def summarise(findings: Sequence[Finding]) -> str:
    """"2 errors, 1 warning" -- and "no problems" when there is genuinely nothing."""
    c = counts(findings)
    parts = [f"{n} {name}{'s' if n != 1 else ''}"
             for name, n in ((ERROR, c[ERROR]), (WARNING, c[WARNING]), (INFO, c[INFO])) if n]
    return ", ".join(parts) if parts else "no problems"


# --------------------------------------------------------------------------- adapters
# Each adapter turns one tool's real report into findings.  They live here rather than in the
# stages so that the mapping from a tool's vocabulary to ours is in one reviewable place.

def from_drc_report(report: Dict[str, Any]) -> List[Finding]:
    """KiCad's DRC JSON -> findings, keeping the geometry that produced each violation.

    `kicad-cli pcb drc` exits 0 even when it finds errors, so the report is the only truth
    here; the exit code is not consulted anywhere in this function on purpose.
    """
    out: List[Finding] = []
    for v in report.get("violations", []):
        items = v.get("items", []) or []
        refs = [i.get("description", "") for i in items if i.get("description")]
        at: Optional[Tuple[float, float]] = None
        for i in items:
            pos = i.get("pos") or {}
            if "x" in pos and "y" in pos:
                at = (float(pos["x"]), float(pos["y"]))
                break
        out.append(Finding(
            code=f"drc.{v.get('type', 'unknown')}",
            severity=v.get("severity", ERROR),
            message=v.get("description", "") or v.get("type", "DRC violation"),
            refs=refs[:4],
            where=_layer_of(items),
            at=at,
            fix=_DRC_FIX.get(v.get("type", ""), ""),
        ))
    for u in report.get("unconnected_items", []):
        items = u.get("items", []) or []
        refs = [i.get("description", "") for i in items if i.get("description")]
        out.append(Finding(
            code="drc.unconnected",
            severity=u.get("severity", ERROR),
            message=u.get("description", "Missing connection"),
            refs=refs[:4],
            fix="route the connection, or declare it no-connect if it is deliberate",
        ))
    return sort_findings(out)


def _layer_of(items: Sequence[Dict[str, Any]]) -> str:
    for i in items:
        text = i.get("description", "")
        for layer in ("F.Cu", "B.Cu", "In1.Cu", "In2.Cu", "F.SilkS", "B.SilkS", "Edge.Cuts"):
            if layer in text:
                return layer
    return ""


# Fixes are advice, so they are only written where the advice is actually specific.
_DRC_FIX = {
    "shorting_items": "move one of the two items apart, or put them on the same net",
    "solder_mask_bridge": "increase the mask expansion, or move the items apart",
    "clearance": "widen the gap, or lower the clearance for this net class",
    "track_width": "widen the track, or lower the minimum for this net class",
    "hole_clearance": "move the hole, or reduce its diameter",
    "courtyards_overlap": "move the footprints apart",
    "silk_over_copper": "move the silkscreen text off the pad",
}


def from_erc_report(report: Dict[str, Any]) -> List[Finding]:
    """Eeschema's ERC JSON -> findings."""
    out: List[Finding] = []
    for sheet in report.get("sheets", []) or []:
        where = sheet.get("path", "") or sheet.get("uuid", "")
        for v in sheet.get("violations", []) or []:
            items = v.get("items", []) or []
            out.append(Finding(
                code=f"erc.{v.get('type', 'unknown')}",
                severity=v.get("severity", WARNING),
                message=v.get("description", "") or v.get("type", "ERC violation"),
                refs=[i.get("description", "") for i in items if i.get("description")][:4],
                where=where,
            ))
    return sort_findings(out)


def from_problems(problems: Sequence[Any]) -> List[Finding]:
    """The design IR's own `Problem` list -> findings.

    `Problem` carries `severity`, `where` and `message`, and its `where` is already a path
    into the design ("parts.U1", "nets.3V3"), which is the most useful ref we have.
    """
    out: List[Finding] = []
    for p in problems:
        sev = getattr(p, "severity", WARNING)
        where = str(getattr(p, "where", ""))
        out.append(Finding(
            code=f"ir.{where.split('.')[0] or 'design'}",
            severity=ERROR if sev in (ERROR, "error") else WARNING,
            message=str(getattr(p, "message", p)),
            refs=[where] if where else [],
            fix=str(getattr(p, "hint", "") or ""),
        ))
    return sort_findings(out)


def to_json(findings: Sequence[Finding], indent: int = 2) -> str:
    return json.dumps([f.as_dict() for f in sort_findings(findings)], indent=indent)
