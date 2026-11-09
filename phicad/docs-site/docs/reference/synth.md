# Board synthesis

A design compiler: a small JSON design IR — parts, nets, and where things sit **relative to
each other** — is turned by deterministic code into a placed, routed, poured, silkscreened
KiCad project, with a gate after every stage. Coordinates are never model output, so they
stop being a source of model error.

The enabling fact is that **`ImportNetlist` works headless**: the handler
(`pcbnew/api/api_handler_pcb.cpp:1756`) has no `checkForHeadless` guard, and
`HEADLESS_PCB_CONTEXT` implements `ReadNetlistFromFile` / `MakeNetlistUpdater` /
`OnNetlistChanged` (`pcbnew/api/headless_pcb_context.cpp:141,171,177`). A netlist file plus
an empty board is therefore enough to make KiCad run the real `BOARD_NETLIST_UPDATER` —
footprints loaded from the libraries, references and values set, every pad attached to its
net, with no footprint s-expressions emitted on our side.

- `ir.py` — the IR and its gate: every pad of the **real** footprint is in exactly one net
  or explicitly `no_connect`, checked in milliseconds before a board exists. Problems are
  data (severity, location, message, fix hint), not prose.
- `place.py` — intent (`"near": "U1"`, `"group"`, `"edge"`) → coordinates; courtyard-legal
  by construction, outline shrink-wrapped to the result.
- `route.py` — grid A\* maze router plus ground stitching: every surface ground pad gets a
  via and the pour does the connecting, which removes ground from the maze entirely.
- `silk.py` — reference designator placement solved against the board's real obstacles
  (pad mask apertures, other silk, the board edge) instead of a hand-written offset table.
- `netlist.py` / `project.py` / `emit.py` — IR → `.net`; the `.kicad_pro` + empty board that
  `kicad-cli` provides no command for; edge cuts, pour and title spliced as text without
  reserializing the board KiCad just wrote.
- `session.py` — the only kipy-dependent module, so the IPC version traps live in one place.
  Everything else is stdlib-only and runs on any `python3`.

Stages and their gates: `validate` → `place` → `scaffold` → `schematic` → `erc` →
`sch-netlist` → `import` → `connectivity` → `position` → `outline` → `route` → `silk` →
`fill` → `drc`. Everything before `erc` takes about a second between them; DRC is the
authority but also the slow, late feedback that made board generation an hours-long loop.

`schematic` / `erc` / `sch-netlist` are what make the sheet and the board provably one
circuit: the emitted `.kicad_sch` is read back by KiCad's own netlist exporter and compared
to the design's connectivity **pad for pad**, the same comparison the board itself passes.

```bash
python3 phicad/synth/cli.py check design.json              # stdlib, ~1 s
phicad/bin/phicad py phicad/synth/cli.py build design.json -o out/
```

`build` needs the kipy venv (the `import`/`position` stages drive KiCad over IPC), which is
what `phicad py` provides; `check` is stdlib-only and runs on any `python3`. Running `build`
with the MCP venv's interpreter fails at the `import` stage with a named reason rather than a
traceback.

Verified end-to-end on `examples/sensor-node.json` (6 parts, 4 nets, 2-layer, poured):
14/14 stages, board 12.7 × 22.1 mm, 78 segments / 9 vias / 66.0 mm of track / 6 ground
stitches, ERC **0 errors** (1 `pin_to_pin` warning, a real design fact), DRC **0 errors,
0 warnings, 0 unconnected**. Full `build` ~20 s, and ~37 s for the 13-part
`regulated-breakout.json`; routing dominates. Two independent builds of one IR are
semantically identical (`kicad-cli pcb diff --exit-code-only` exits 0), which is what
"coordinates are never model output" buys.

Known limits, stated plainly in the module README: no
rip-up-and-reroute (grid A\*, not PNS — it will refuse boards a good router would finish),
a 0.1 mm router grid, `F.Cu`/`B.Cu` only regardless of declared layer count, shelf-packing
rather than floorplanning, and a macOS-only stock-footprint path. The agent-side contract is
`synth-designer`, which collapses the `sch-designer → pcb-placer → pcb-router → drc-fixer`
span for boards synth can handle and hands the partially-built board to those agents when a
gate fails in a way the IR cannot express.

Source of truth: [phicad/synth/README.md](https://github.com/couplet-physical-ai/PhiCad/blob/master/phicad/synth/README.md)
</content>
