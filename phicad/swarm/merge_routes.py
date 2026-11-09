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
"""Merge per-net-group copper from swarm candidate boards into a base board.

Each router agent in a swarm routes ONLY its assigned net group (see
partition_nets.py) on its own full copy of the board. This tool then builds
the merged board: for every `--take GROUP=CANDIDATE`, the group's copper
(segments, vias, arcs) is taken from that candidate and spliced into the base.

Pure text splicing on the s-expression source — footprints, zones, setup,
everything else stays byte-identical to the base. `--take` has REPLACE
semantics: the group's existing copper in the base is stripped first, so
re-merging and re-routing are idempotent. Handles BOTH net-reference styles
— stable KiCad's numeric `(net 42)` (with a top-level net table) and this
fork's name-only `(net "GND")` (version 20260831, no net table) — and
remaps between them when base and candidate styles/numbering differ; a
candidate net name missing from the base is a hard error (routers must not
invent nets).

Zones are never merged (pour them on the merged board: zones refill via IPC,
or route power in the zones_bucket single-agent). If the base contains
`(group …)` blocks, stripping warns — dangling member uuids are possible.

Usage:
    merge_routes.py base.kicad_pcb --plan plan.json \
        --take g0=candA.kicad_pcb --take g1=candB.kicad_pcb -o merged.kicad_pcb
    merge_routes.py base.kicad_pcb --plan plan.json --strip g0 -o bare.kicad_pcb

Gate the output like any other board change:
    kicad-cli pcb drc --format json --exit-code-violations merged.kicad_pcb

Stdlib only — runs under any python3.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

COPPER_HEADS = {"segment", "via", "arc"}


def scan_top_blocks(text: str) -> list[tuple[int, int, str]]:
    """All depth-1 blocks inside (kicad_pcb …) as (start, end_exclusive, head)."""
    blocks = []
    depth = 0
    in_str = False
    esc = False
    start = -1
    head = ""
    for i, ch in enumerate(text):
        if esc:
            esc = False
            continue
        if ch == "\\":
            esc = True
            continue
        if ch == '"':
            in_str = not in_str
            continue
        if in_str:
            continue
        if ch == "(":
            depth += 1
            if depth == 2:
                start = i
                m = re.match(r"\(\s*([A-Za-z0-9_]+)", text[i:i + 40])
                head = m.group(1) if m else ""
        elif ch == ")":
            if depth == 2 and start >= 0:
                blocks.append((start, i + 1, head))
                start = -1
            depth -= 1
    if depth != 0 or in_str:
        sys.exit("error: unbalanced s-expression source (parse aborted)")
    return blocks


def net_table(text: str, blocks) -> dict[int, str]:
    """(net N "NAME") declarations -> {number: name}. Empty for fork files."""
    table = {}
    for s, e, head in blocks:
        if head != "net":
            continue
        m = re.match(r'\(\s*net\s+(\d+)\s+"((?:[^"\\]|\\.)*)"\s*\)', text[s:e], re.S)
        if m:
            table[int(m.group(1))] = m.group(2)
    return table


_NET_NUM_RE = re.compile(r"\(\s*net\s+(\d+)\s*\)")
_NET_NAME_RE = re.compile(r'\(\s*net\s+"((?:[^"\\]|\\.)*)"\s*\)')


def block_net_name(text: str, s: int, e: int,
                   table: dict[int, str]) -> str | None:
    """Net NAME of a copper block — numeric refs resolved through the table."""
    m = _NET_NAME_RE.search(text[s:e])
    if m:
        return m.group(1)
    m = _NET_NUM_RE.search(text[s:e])
    if m:
        return table.get(int(m.group(1)))
    return None


def strip_leading_ws(text: str, start: int) -> int:
    """Extend a block's span left over its own indentation/newline."""
    i = start
    while i > 0 and text[i - 1] in " \t":
        i -= 1
    if i > 0 and text[i - 1] == "\n":
        i -= 1
    return i


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("base", type=Path)
    ap.add_argument("--plan", required=True, type=Path,
                    help="plan.json from partition_nets.py")
    ap.add_argument("--take", action="append", default=[], metavar="GROUP=BOARD",
                    help="take GROUP's copper from candidate BOARD (repeatable)")
    ap.add_argument("--strip", action="append", default=[], metavar="GROUP",
                    help="remove GROUP's copper from the base (repeatable)")
    ap.add_argument("-o", "--out", required=True, type=Path)
    args = ap.parse_args()

    plan = json.loads(args.plan.read_text())
    group_nets = {g["id"]: set(g["nets"]) for g in plan["groups"]}
    group_nets["zones"] = set(plan.get("zones_bucket", []))

    takes: list[tuple[str, Path]] = []
    for spec in args.take:
        gid, _, path = spec.partition("=")
        if gid not in group_nets:
            sys.exit(f"error: unknown group '{gid}' (plan has {sorted(group_nets)})")
        takes.append((gid, Path(path)))
    for gid in args.strip:
        if gid not in group_nets:
            sys.exit(f"error: unknown group '{gid}' (plan has {sorted(group_nets)})")
    if not takes and not args.strip:
        sys.exit("error: nothing to do (--take and/or --strip)")

    base_text = args.base.read_text()
    base_blocks = scan_top_blocks(base_text)
    base_nets = net_table(base_text, base_blocks)          # {} on fork files
    base_name2num = {v: k for k, v in base_nets.items()}
    base_is_numeric = bool(base_nets)
    # Known net names in the base: the table (stable) or every name-style
    # reference anywhere in the document (fork — pads carry them all).
    base_known_names = set(base_name2num) if base_is_numeric else \
        set(_NET_NAME_RE.findall(base_text))

    if any(h == "group" for _, _, h in base_blocks) and (args.strip or takes):
        print("warning: base contains (group …) blocks; stripping copper may leave "
              "dangling member uuids — verify in the GUI", file=sys.stderr)

    # 1) strip: explicit --strip groups + every --take group (replace semantics)
    strip_names: set[str] = set()
    for gid in args.strip:
        strip_names |= group_nets[gid]
    for gid, _ in takes:
        strip_names |= group_nets[gid]
    removed = 0
    out_parts: list[str] = []
    pos = 0
    for s, e, head in base_blocks:
        if head in COPPER_HEADS and \
                block_net_name(base_text, s, e, base_nets) in strip_names:
            ws = strip_leading_ws(base_text, s)
            out_parts.append(base_text[pos:ws])
            pos = e
            removed += 1
    out_parts.append(base_text[pos:])
    merged = "".join(out_parts)

    # 2) take: extract each group's copper from its candidate, remap net numbers
    inserted_chunks: list[str] = []
    added = 0
    for gid, cand_path in takes:
        cand_text = cand_path.read_text()
        cand_blocks = scan_top_blocks(cand_text)
        cand_nets = net_table(cand_text, cand_blocks)
        wanted = group_nets[gid]
        missing = [n for n in wanted if n not in base_known_names]
        if missing:
            sys.exit(f"error: group {gid} nets not in the base board: {missing[:5]} "
                     "(routers must not create nets)")
        n_before = added
        for s, e, head in cand_blocks:
            if head not in COPPER_HEADS:
                continue
            name = block_net_name(cand_text, s, e, cand_nets)
            if name not in wanted:
                continue
            chunk = cand_text[s:e]
            # Rewrite the net reference into the BASE's style.
            esc = name.replace("\\", r"\\").replace('"', r"\"")
            new_ref = f"(net {base_name2num[name]})" if base_is_numeric \
                else f'(net "{esc}")'
            chunk = _NET_NAME_RE.sub(lambda _: new_ref, chunk)
            chunk = _NET_NUM_RE.sub(lambda _: new_ref, chunk)
            inserted_chunks.append(chunk)
            added += 1
        print(f"take {gid}: {added - n_before} copper items from {cand_path.name}",
              file=sys.stderr)

    if inserted_chunks:
        tail = merged.rstrip()
        if not tail.endswith(")"):
            sys.exit("error: base does not end with ')'")
        body = "".join(f"\n\t{c}" for c in inserted_chunks)
        merged = tail[:-1].rstrip() + body + "\n)\n"

    # sanity: output must still scan clean, and copper accounting must add up
    final_blocks = scan_top_blocks(merged)
    base_copper = sum(1 for _, _, h in base_blocks if h in COPPER_HEADS)
    final_copper = sum(1 for _, _, h in final_blocks if h in COPPER_HEADS)
    if final_copper != base_copper - removed + added:
        sys.exit(f"error: copper accounting mismatch ({base_copper} - {removed} "
                 f"+ {added} != {final_copper}) — output NOT written")

    args.out.write_text(merged)
    print(f"merged -> {args.out}: {removed} copper items stripped, {added} added, "
          f"{final_copper} total", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
