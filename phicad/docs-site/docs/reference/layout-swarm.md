# Layout swarm

Machinery for running many placement/routing agents on one board in parallel without
conflicts: N placers produce whole-board candidates scored best-of-N; routing is
partitioned into disjoint, spatially-coherent net groups routed in parallel on full board
copies and merged back losslessly, gated by the fork's own DRC.

- `partition_nets.py` — nets → N pad-balanced groups (`plan.json`); power nets to a
  `zones_bucket` (poured, not point-routed).
- `merge_routes.py` — pure text splice of each group's copper into the base; replace
  semantics, cross-format net-reference rewriting, copper-accounting hard check.
- `score_board.py` — candidate → one comparable float (unconnected ≫ DRC errors ≫
  warnings ≫ vias ≫ track length). **Name the project** (`--project p.kicad_pro`, or keep
  `<stem>.kicad_pro` beside the board): design rules live in the project and KiCad pairs one
  with a board only by filename stem, so a scratch-named candidate is otherwise judged by
  KiCad's *defaults* — one real board scored 0 DRC errors in its project and 13 copied out,
  36.2 against 13036.2 on the very number the swarm ranks by. Every report carries
  `rules_source`, and only scores sharing it are comparable.
- `selftest.py` — CI-safe invariants (parser/partition/merge, both board formats).

Concurrency rules: one agent = one file copy = one IPC socket; per-session git worktrees;
agents never touch git — the orchestrator owns branch/commit/push; merge is the only
writer of the final board.

Source of truth: [phicad/swarm/README.md](https://github.com/couplet-physical-ai/PhiCad/blob/master/phicad/swarm/README.md)
