# Layout swarm — parallel placement & routing without conflicts

Machinery for running **many layout agents on one board at once**: N candidate
placements scored best-of-N, and routing split across parallel router agents that
each own a disjoint net group, merged back losslessly. Every step is gated by the
fork's own oracles (`pcb drc`, `pcb diff`), and the concurrency rules below make
agent collisions structurally impossible rather than merely unlikely.

Verified end-to-end on `demos/interf_u` (108 routable nets, 815 copper items):
partition → strip → re-merge reproduced the original board **exactly**
(`pcb diff` exit 0; score identical to 0.1) — see §Verification.

## Tools (all stdlib — any python3; no venv needed)

| Tool | Job |
| --- | --- |
| `partition_nets.py` | nets → N disjoint, spatially-coherent, pad-balanced groups (`plan.json`); power nets → `zones_bucket` |
| `merge_routes.py` | splice each group's copper from its candidate into the base (replace semantics, net-style remap, copper accounting check) |
| `score_board.py` | candidate → one float: `10000·unconnected + 1000·err + 10·warn + 2·vias + 0.1·track_mm` (lower wins; DRC via the fork CLI) |

All three parse boards with `phicad/pylib/sexp.py` and handle **both** board formats:
stable KiCad (numeric net table, `(net 42)` references) and this fork's
`version 20260831` (no net table, name-only `(net "GND")` references — the format
that hard-crashes kiutils 1.4.8). `merge_routes.py` also merges ACROSS the two
styles, rewriting candidate net references into the base's style — needed because
a router saving over IPC upgrades its candidate to fork format even when the base
was stable.

## The two swarm patterns

### 1. Placement: best-of-N (candidates are whole boards)

Placement is global optimization — divide-and-conquer misparallelizes it. Instead:
N `pcb-placer` agents each produce a **complete candidate** from the same input
(different strategies: connection-length-greedy, functional-cluster-first,
edge-connector-anchored…), the orchestrator scores all and keeps the winner.

```
cp base.kicad_pcb cand-{a,b,c}.kicad_pcb          # one copy per agent
# design rules live in the PROJECT, and KiCad only finds <board stem>.kicad_pro:
# name it in every score call, or a scratch-named candidate is judged against
# KiCad's DEFAULT rules (measured: 0 violations in-project vs 13 copied out).
# subagent tasks: [placer×3, each on its own copy, own socket]
python3 phicad/swarm/score_board.py cand-a.kicad_pcb --project base.kicad_pro   # … b, c
# keep argmin(score); it becomes the routing base
```

### 2. Routing: partition → parallel route → merge (candidates are per-group)

```
python3 phicad/swarm/partition_nets.py placed.kicad_pcb --groups 3 -o plan.json
cp placed.kicad_pcb cand-g{0,1,2}.kicad_pcb        # full copy per agent
# subagent tasks: router-g0 routes ONLY plan.groups[0].nets on cand-g0, etc.
python3 phicad/swarm/merge_routes.py placed.kicad_pcb --plan plan.json \
    --take g0=cand-g0.kicad_pcb --take g1=cand-g1.kicad_pcb \
    --take g2=cand-g2.kicad_pcb -o merged.kicad_pcb
# the gate — inside the project, so the design's own rules apply, and refilling
# because any copper change stales the pour (18 phantom violations without it):
cp placed.kicad_pro merged.kicad_pro 2>/dev/null || true
kicad-cli pcb drc --format json --severity-all --refill-zones \
    --exit-code-violations merged.kicad_pcb
```

- Each agent sees the whole board (all obstacles) but **may only add/modify copper
  on its own nets** — pcb-router.md §Swarm mode is the agent-side contract.
- `--take` strips the group's old copper first (replace semantics): re-routing one
  failed group and re-merging is idempotent; other groups' copper is untouched.
- Copper on nets outside the group is *ignored at merge* — an agent that strays
  wastes its own time but cannot corrupt the result.
- Power nets (`zones_bucket`) are not point-routed: pour zones on the merged board
  (IPC `refill zones`, or a final single-agent pass), because they span every
  region and would collide with all groups.
- Cross-group DRC clearance violations at region seams are possible: the merged
  DRC is the gate, and seam violations go to **drc-fixer** (single agent) — not
  back to the swarm.

Orchestrator dispatch (phi subagent tool, from the repo root):

```json
{"tasks": [
  {"name": "route-g0", "agent": "pcb-router",
   "task": "Route ONLY the nets in plan.json group g0 on /tmp/sw/cand-g0.kicad_pcb. Swarm mode: socket /tmp/sw/api-g0.sock. Nets: <paste plan.groups[0].nets>"},
  {"name": "route-g1", "agent": "pcb-router", "task": "… g1 … api-g1.sock …"},
  {"name": "route-g2", "agent": "pcb-router", "task": "… g2 … api-g2.sock …"}
]}
```

## Concurrency rules (what makes it collision-free)

1. **One agent, one file.** Every agent works on its own `cp` of the board. Nobody
   edits a shared board file, ever.
2. **One agent, one socket.** Parallel `phicad api-server` instances need distinct
   `--socket /tmp/<run>/api-<agent>.sock` paths. A stale socket from a dead server
   is auto-recovered (flock + unlink); a *live* collision silently falls back to
   `api-<pid>.sock` — which kipy's env won't point at. Unique paths avoid the
   whole class.
3. **One session, one worktree.** Concurrent phi sessions on this repo use separate
   git worktrees (`git worktree add`), each with its own `build/` (venv, build tree
   are per-worktree and gitignored). Never run two sessions in one worktree.
4. **Agents never touch git.** The orchestrator owns branch, commits, and pushes;
   candidates live under `/tmp/<run>/` or a gitignored scratch dir, and only the
   merged, gated board is committed.
5. **Merge is the only writer of the final board**, and it refuses to write on
   copper-accounting mismatch, unbalanced source, or nets missing from the base.

## Scoring weights

`score_board.py` weights encode the priority order: connectivity ≫ correctness ≫
manufacturing cost (vias) ≫ wirelength. Unconnected items at 10000 make an
unrouted board strictly worse than any routed one; 2/via vs 0.1/mm means the
scorer prefers 1 via saved over 20 mm of extra track. Change the constants in
one place (`WEIGHTS`) if a board has different priorities (e.g. RF: vias much
more expensive).

## Verification (run for real against the fork build)

On `demos/interf_u` (KiCad-9-format, 731 segments + 84 vias, DRC baseline
3 errors / 25 warnings / 0 unconnected):

- `partition_nets.py --groups 3`: 108 routable nets → 82/82/81 pads per group,
  bboxes tiling the board's long axis; GND+VCC → zones_bucket.
- `--strip g0 --strip g1 --strip g2`: 650 copper items removed; DRC on the bare
  board: **137 unconnected** (score 1 373 344 — correctly catastrophic).
- Re-merge of all three groups from the original as candidates: 650 items back,
  815 total, **`pcb diff` exit 0 vs the original** (semantically identical) and
  score restored to the baseline 3928.1 (0 unconnected).

On the fork-format `teensy-sensor-suite` demo (`version 20260831`, name-only
net refs, 106 segments + 35 vias):

- partition: 8 routable nets → 2 groups (14/10 pads); 3V3+GND → zones_bucket.
- strip-all → re-merge: 106 out, 106 back; the normalized copper-block SET of
  the re-merged board equals the original exactly.
- cross-style merges (synthesized numeric twin of the same board): fork-format
  candidate → numeric base rewrites refs to `(net N)`; numeric candidate →
  fork base rewrites to `(net "NAME")` — both directions verified.

The round-trip proves the splice math; a real swarm differs only in *where the
candidate copper comes from*.
