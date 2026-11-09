#!/usr/bin/env python3
# This program source code file is part of KiCad, a free EDA CAD application.
#
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
"""Audit a KiCad board for exact-parts readiness: 3D models + sourcing ids.

For every footprint on the board (read-only parse — nothing is written):

  - does it carry at least one 3D model binding, and does the path resolve?
    (${KIPRJMOD} -> board directory; ${KICAD*_3DMODEL_DIR} -> env or "global,
    unverified"; relative paths -> board directory)
  - does it carry a sourcing identity (LCSC / MPN / JLCPCB property)?

Footprints excluded from the BOM (fiducials, logos, mounting holes) and DNP
parts are exempt from the identity requirement; `--exempt GLOB` (repeatable)
exempts references entirely.

Handles BOTH stable-KiCad and fork (version 20260831) board formats via
phicad/pylib/sexp.py — stdlib only, runs under any python3.

Output: JSON report on stdout, human summary on stderr.
Exit 0 = board is fab-ready by these checks; 1 = gaps found.
    --require-id     escalate missing LCSC/MPN from warning to failure
    --parts ledger   also cross-check every board id appears in parts.json
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "pylib"))
import sexp  # noqa: E402

ID_PROPS = ("lcsc", "mpn", "manufacturer part number", "jlcpcb", "jlc")


def resolve_model(path_str: str, board_dir: Path) -> tuple[bool, str]:
    """-> (resolved_or_global, note)."""
    s = path_str
    if "${KIPRJMOD}" in s or "$(KIPRJMOD)" in s:
        p = Path(re.sub(r"\$[({]KIPRJMOD[)}]/?", "", s))
        p = board_dir / p
        return p.exists(), f"project-relative: {p}"
    m = re.match(r"\$[({]([A-Za-z0-9_]+)[)}]", s)
    if m:
        var = m.group(1)
        base = os.environ.get(var)
        if base:
            p = Path(s.replace("${%s}" % var, base).replace("$(%s)" % var, base))
            return p.exists(), f"env {var}: {p}"
        return True, f"global ({var}), unverified"
    p = Path(s)
    if not p.is_absolute():
        p = board_dir / p
    return p.exists(), str(p)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("board", type=Path, help=".kicad_pcb to audit")
    ap.add_argument("--parts", type=Path, help="parts.json ledger to cross-check")
    ap.add_argument("--require-id", action="store_true",
                    help="missing LCSC/MPN is a failure, not a warning")
    ap.add_argument("--exempt", action="append", default=[],
                    help="reference glob to exempt entirely (e.g. 'TP*', repeatable)")
    args = ap.parse_args()

    doc = sexp.parse_file(args.board)
    if sexp.head(doc) != "kicad_pcb":
        sys.exit(f"error: {args.board} is not a kicad_pcb document")
    board_dir = args.board.resolve().parent

    ledger_ids: set[str] = set()
    if args.parts:
        data = json.loads(args.parts.read_text())
        for entry in data.get("parts", []):
            for key in ("lcsc", "mpn"):
                if entry.get(key):
                    ledger_ids.add(str(entry[key]).lower())

    rows, failures, warnings = [], [], []
    for fp in sexp.children(doc, "footprint"):
        lib_id = next((str(a) for a in sexp.atoms(fp)), "?")
        ref = sexp.fp_property(fp, "Reference") or "?"
        attrs = sexp.fp_attrs(fp)
        exclude_bom = "exclude_from_bom" in attrs
        # pure board hardware (mounting holes, logos): excluded from BOM and pos
        # files -> not a sourced part, exempt from every parts check
        board_only = ("board_only" in attrs
                      or (exclude_bom and "exclude_from_pos_files" in attrs))
        dnp = "dnp" in attrs
        exempt = any(fnmatch.fnmatch(ref, g) for g in args.exempt)

        models = []
        models_ok = True
        for m in sexp.children(fp, "model"):
            margs = sexp.atoms(m)
            if not margs:
                continue
            mpath = str(margs[0])
            ok, note = resolve_model(mpath, board_dir)
            models.append({"path": mpath, "resolved": ok, "note": note})
            models_ok &= ok

        props = {}
        for key in ID_PROPS:
            val = sexp.fp_property(fp, key)
            if val:
                props[key] = val
        part_id = next(iter(props.values()), None)

        row = {"ref": ref, "lib_id": lib_id, "dnp": dnp,
               "exclude_from_bom": exclude_bom, "exempt": exempt,
               "models": models, "id": part_id}
        rows.append(row)
        if exempt or board_only:
            continue
        if not models:
            failures.append(f"{ref} ({lib_id}): no 3D model binding")
        elif not models_ok:
            bad = "; ".join(m["note"] for m in models if not m["resolved"])
            failures.append(f"{ref}: 3D model path does not resolve — {bad}")
        if not exclude_bom and not dnp and not part_id:
            msg = f"{ref} ({lib_id}): no LCSC/MPN property"
            (failures if args.require_id else warnings).append(msg)
        if part_id and ledger_ids and part_id.lower() not in ledger_ids:
            warnings.append(f"{ref}: id '{part_id}' not in parts ledger")

    total = len(rows)
    with_models = sum(1 for r in rows if r["models"])
    with_id = sum(1 for r in rows if r["id"])
    report = {
        "board": str(args.board),
        "footprints": total,
        "with_3d_model": with_models,
        "with_part_id": with_id,
        "failures": failures,
        "warnings": warnings,
        "rows": rows,
        "ok": not failures,
    }
    print(json.dumps(report, indent=2))
    print(f"audit: {total} footprints, {with_models} with 3D, {with_id} with id; "
          f"{len(failures)} failures, {len(warnings)} warnings",
          file=sys.stderr)
    for f in failures:
        print(f"  FAIL {f}", file=sys.stderr)
    for w in warnings[:20]:
        print(f"  warn {w}", file=sys.stderr)
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
