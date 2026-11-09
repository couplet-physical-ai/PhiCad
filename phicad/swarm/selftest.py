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
"""Self-test for the swarm pipeline (sexp parser, partition, merge).

Synthesizes one tiny board in BOTH net-reference styles (stable numeric table
and fork name-only), then asserts:

  1. sexp parses both and net_of/net_table resolve identically;
  2. partition produces identical group content for both;
  3. strip -> re-merge round-trips the copper exactly (normalized-set compare)
     within each style AND across styles (numeric candidate into fork base,
     fork candidate into numeric base, with reference rewriting).

Stdlib only, no build tree, no network — safe for CI. Exit 0 = pass.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "pylib"))
import sexp  # noqa: E402

FORK = """(kicad_pcb
\t(version 20260831)
\t(generator "pcbnew")
\t(layers (0 "F.Cu" signal) (2 "B.Cu" signal) (44 "Edge.Cuts" user))
\t(setup (pad_to_mask_clearance 0))
\t(footprint "T:R2"
\t\t(layer "F.Cu")
\t\t(at 20 20 90)
\t\t(property "Reference" "R1" (at 0 0 0) (layer "F.SilkS"))
\t\t(attr smd)
\t\t(pad "1" smd rect (at -1 0) (size 1 1) (layers "F.Cu") (net "A"))
\t\t(pad "2" smd rect (at 1 0) (size 1 1) (layers "F.Cu") (net "B"))
\t)
\t(footprint "T:R2"
\t\t(layer "F.Cu")
\t\t(at 30 20)
\t\t(property "Reference" "R2" (at 0 0 0) (layer "F.SilkS"))
\t\t(attr smd)
\t\t(pad "1" smd rect (at -1 0) (size 1 1) (layers "F.Cu") (net "A"))
\t\t(pad "2" smd rect (at 1 0) (size 1 1) (layers "F.Cu") (net "GND"))
\t)
\t(footprint "T:R2"
\t\t(layer "F.Cu")
\t\t(at 40 20)
\t\t(property "Reference" "R3" (at 0 0 0) (layer "F.SilkS"))
\t\t(attr smd)
\t\t(pad "1" smd rect (at -1 0) (size 1 1) (layers "F.Cu") (net "B"))
\t\t(pad "2" smd rect (at 1 0) (size 1 1) (layers "F.Cu") (net "GND"))
\t)
\t(segment (start 21 20) (end 29 20) (width 0.25) (layer "F.Cu") (net "A") (uuid "s1"))
\t(segment (start 31 20) (end 39 20) (width 0.25) (layer "F.Cu") (net "B") (uuid "s2"))
\t(via (at 35 22) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net "GND") (uuid "v1"))
)
"""


def make_numeric(fork_text: str) -> str:
    """Rewrite the fork fixture into stable style: net table + numeric refs."""
    names = sorted(set(re.findall(r'\(net\s+"([^"]+)"\)', fork_text)))
    num = {n: i + 1 for i, n in enumerate(names)}
    out = re.sub(r'\(net\s+"([^"]+)"\)',
                 lambda m: f"(net {num[m.group(1)]})", fork_text)
    decls = "".join(f'\n\t(net {i} "{n}")'
                    for n, i in sorted(num.items(), key=lambda kv: kv[1]))
    anchor = out.index("\n\t(footprint")   # net table before the first footprint
    return out[:anchor] + decls + out[anchor:]


def copper_set(path: Path) -> list[str]:
    sys.path.insert(0, str(HERE))
    from merge_routes import scan_top_blocks, COPPER_HEADS
    text = path.read_text()
    # normalize whitespace AND net-reference style for cross-style compares
    items = []
    for s, e, h in scan_top_blocks(text):
        if h in COPPER_HEADS:
            chunk = re.sub(r"\s+", " ", text[s:e])
            chunk = re.sub(r'\(net [^)]*\)', "(net X)", chunk)
            items.append(chunk)
    return sorted(items)


def run(tool: str, *argv: str) -> None:
    proc = subprocess.run([sys.executable, str(HERE / tool), *argv],
                          capture_output=True, text=True)
    if proc.returncode != 0:
        sys.exit(f"FAIL: {tool} {' '.join(argv)}\n{proc.stdout}{proc.stderr}")


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="swarm-selftest-"))
    fork = tmp / "fork.kicad_pcb"
    numeric = tmp / "numeric.kicad_pcb"
    fork.write_text(FORK)
    numeric.write_text(make_numeric(FORK))

    # 1. parser equivalence
    for path, style in ((fork, "fork"), (numeric, "numeric")):
        doc = sexp.parse_file(path)
        table = sexp.net_table(doc)
        pads = []
        for fp in sexp.children(doc, "footprint"):
            for pad in sexp.children(fp, "pad"):
                ref = sexp.net_of(pad)
                name = str(ref) if ref.quoted else table.get(str(ref))
                pads.append(name)
        assert pads == ["A", "B", "A", "GND", "B", "GND"], f"{style}: {pads}"
    assert sexp.net_table(sexp.parse_file(fork)) == {}
    assert len(sexp.net_table(sexp.parse_file(numeric))) == 3
    print("ok 1 - parser resolves nets identically in both styles")

    # 1b. the accessors must REFUSE a bare atom rather than answer zero.
    # parse_file() returns the root node, so `parse_file(p)[0]` hands the head
    # atom to children(); iterating a str yields characters, so every walk used
    # to come back empty and a gate built that way passed while counting
    # nothing.  This is the swarm's own read path, so it is checked here.
    doc = sexp.parse_file(fork)
    assert len(sexp.children(doc, "footprint")) == 3, "fixture changed"
    for name, call in (("children", lambda: sexp.children(doc[0], "footprint")),
                       ("child", lambda: sexp.child(doc[0], "version")),
                       ("atoms", lambda: sexp.atoms(doc[0]))):
        try:
            call()
        except TypeError:
            pass
        else:
            sys.exit("FAIL: sexp.%s() silently accepted a bare atom" % name)
    print("ok 1b - accessors reject a bare atom instead of counting zero")

    # 2. partition equivalence (GND must be zone-bucketed, A/B routable)
    plans = {}
    for path in (fork, numeric):
        plan_p = path.with_suffix(".plan.json")
        run("partition_nets.py", str(path), "--groups", "2", "-o", str(plan_p))
        plans[path] = json.loads(plan_p.read_text())
    for plan in plans.values():
        assert plan["zones_bucket"] == ["GND"], plan["zones_bucket"]
        got = sorted(n for g in plan["groups"] for n in g["nets"])
        assert got == ["A", "B"], got
    print("ok 2 - partition groups identical in both styles")

    # 3. round-trips, same-style and cross-style
    plan_fork = str(fork.with_suffix(".plan.json"))
    plan_num = str(numeric.with_suffix(".plan.json"))
    cases = [
        ("fork->fork", fork, plan_fork, fork),
        ("num->num", numeric, plan_num, numeric),
        ("fork cand -> num base", numeric, plan_num, fork),
        ("num cand -> fork base", fork, plan_fork, numeric),
    ]
    for label, base, plan, cand in cases:
        merged = tmp / "merged.kicad_pcb"
        take = [f"g{i}={cand}" for i in range(2)]
        args = [str(base), "--plan", plan, "-o", str(merged)]
        for t in take:
            args += ["--take", t]
        run("merge_routes.py", *args)
        assert copper_set(merged) == copper_set(base), f"{label}: copper differs"
        # style of inserted refs must match the base
        flat = re.sub(r"\s+", " ", merged.read_text())
        if base is numeric:
            assert re.search(r"\(net \d+\) \(uuid", flat), f"{label}: refs not numeric"
        else:
            assert re.search(r'\(net "[AB]"\) \(uuid', flat), f"{label}: refs not names"
        print(f"ok 3 - {label}: lossless, base-style refs")

    # 4. score_board's project resolution: design rules live in the project, and
    # KiCad pairs one with a board only by filename stem, so a scratch-named
    # candidate is otherwise judged against KiCad's DEFAULT rules (measured on a
    # real board: 0 violations in-project, 13 copied out).  No CLI needed here --
    # this is the resolution/staging contract, which is where the bug was.
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import score_board

    proj = tmp / "design.kicad_pro"
    proj.write_text("{}")
    (tmp / "design.kicad_pcb").write_text(FORK)
    cand = tmp / "cand-g0.kicad_pcb"
    cand.write_text(FORK)

    got, source = score_board.resolve_project(tmp / "design.kicad_pcb", None)
    assert (got, source) == (proj, "sibling"), (got, source)
    got, source = score_board.resolve_project(cand, None)
    assert (got, source) == (None, "kicad-defaults"), (got, source)
    got, source = score_board.resolve_project(cand, proj)
    assert (got, source) == (proj, "explicit"), (got, source)

    # staging must hand DRC a board whose stem matches the project, without
    # touching the caller's files
    with score_board.staged_board(cand, proj) as staged:
        assert staged.stem == proj.stem, staged
        assert staged.parent != cand.parent, "staged into the candidate's own dir"
        assert (staged.parent / proj.name).is_file(), "project not staged beside it"
        assert staged.read_text() == FORK, "staged board differs from the candidate"
    assert cand.is_file() and (tmp / "design.kicad_pcb").is_file(), "originals disturbed"
    # an in-project board must NOT be copied anywhere
    with score_board.staged_board(tmp / "design.kicad_pcb", proj) as staged:
        assert staged == tmp / "design.kicad_pcb", staged
    print("ok 4 - score_board pairs a candidate with the project's real rules")

    print("selftest: all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
