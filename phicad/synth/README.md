# synth — design IR in, placed and routed board out

A design compiler. The input is ~60 lines of JSON that say what the parts are, what is
connected to what, and where things should sit **relative to each other**; the output is a
KiCad project with a schematic that passes ERC, every footprint placed, every net routed,
a ground pour, solved silkscreen, and a DRC report with nothing in it — with the schematic
and the board proven against each other pad for pad. Coordinates are never model output,
so they stop being a source of model error.

Verified end-to-end on macOS with `examples/sensor-node.json` (6 parts, 4 nets, 2-layer, poured):
**14/14 stages, ERC 0 errors / 1 warning, DRC 0 errors / 0 warnings / 0 unconnected**, board
12.7 × 22.1 mm — see §Verified results. The ERC warning is a real design fact, not noise:
`pin_to_pin`, the regulator's bidirectional pin strapped to a power rail. `stage_erc` prints
it rather than swallowing it (`PASS erc — 0 errors, 1 warnings (pin_to_pin)`); only errors
fail the stage.

The load-bearing discovery is that **`ImportNetlist` works headless**. The handler
(`pcbnew/api/api_handler_pcb.cpp:1756`) carries no `checkForHeadless` guard, and
`HEADLESS_PCB_CONTEXT` implements `ReadNetlistFromFile` / `MakeNetlistUpdater` /
`OnNetlistChanged` (`pcbnew/api/headless_pcb_context.cpp:141,171,177`). A netlist file plus
an empty board is therefore enough to make KiCad run the real `BOARD_NETLIST_UPDATER` —
footprints loaded from the libraries, references and values set, every pad attached to its
net. No footprint s-expressions on our side at all. This is the arrow `agents/README.md`'s
DAG draws from `sch-designer` to `pcb-placer` with nothing behind it.

## Why an IR beats emitting s-expressions

The alternative is in the repo: `demos/teensy-sensor-suite/generate_board.py`, 1667 lines
that write a `.kicad_pcb` by hand. Its comments are an honest log of what that costs.

```python
# -4.8, after two corrections.  -3.4 put C2's GND pad at the same y as J4's 3V3 tap
# (0.01 mm); -4.1 moved it only to 0.69 mm where 0.9 mm is needed for a 1.0 mm pad.
```

```python
# A constant (0 -2.2) put every refdes 2.2 mm above its own origin, which on this
# density lands on a neighbour's pads or on another refdes -- that was the whole
# silk_over_copper / silk_overlap warning population.
```

Almost none of that is design. It is constraint solving done one coordinate at a time,
in a model's head, with a ~25 s DRC round trip as the only oracle — and the same arithmetic
re-derived for every new board. The demo also needs a companion `check_routing.py` (647
lines) because four separate classes of clearance bug shipped past review.

The IR moves the boundary. "The decoupling cap belongs next to the pin it decouples" is one
line of intent; turning it into a coordinate that clears every courtyard is arithmetic, done
once, correctly, in `place.py`. The 400 lines of hand-tuned constants and the 17-entry
`REF_POS` table stop being written at all.

The second consequence is that the board is built by **the same code path a human's "Update
PCB from Schematic" uses**. Pad nets, footprint bodies and component UUIDs are KiCad's job,
not ours, so they cannot be subtly wrong in a way no checker looks for.

## The design IR

JSON is canonical (stdlib everywhere, and it matches `parts.json`, the parts pipeline's
ledger). YAML is accepted when PyYAML happens to be importable; it is never required.

`examples/sensor-node.json`, complete:

```json
{
  "project": "sensor-node",
  "title": "I2C sensor node",
  "rev": "A",
  "company": "Example Co",

  "board": {
    "layers": 2, "outline": "auto", "margin": 2.0,
    "corner": { "chamfer": 2.0 }, "origin": [100, 100],
    "ground_net": "GND", "pour_ground": true
  },

  "rules": {
    "clearance": 0.2, "track_width": 0.25,
    "track_widths": { "3V3": 0.4, "GND": 0.4 }
  },

  "parts": {
    "J1": { "footprint": "Connector_PinHeader_2.54mm:PinHeader_1x04_P2.54mm_Vertical",
            "value": "I2C+PWR", "description": "host connector: GND / 3V3 / SDA / SCL" },
    "U1": { "footprint": "Package_LGA:Bosch_LGA-8_2.5x2.5mm_P0.65mm_ClockwisePinNumbering",
            "symbol": "Sensor:BME280",
            "value": "BME280", "mpn": "BME280", "lcsc": "C92489" },
    "C1": { "footprint": "Capacitor_SMD:C_0805_2012Metric", "value": "100nF" },
    "C2": { "footprint": "Capacitor_SMD:C_0805_2012Metric", "value": "10uF" },
    "R1": { "footprint": "Resistor_SMD:R_0805_2012Metric", "value": "4k7" },
    "R2": { "footprint": "Resistor_SMD:R_0805_2012Metric", "value": "4k7" }
  },

  "nets": {
    "GND": ["J1.1", "U1.1", "U1.5", "U1.7", "C1.2", "C2.2"],
    "3V3": ["J1.2", "U1.2", "U1.6", "U1.8", "C1.1", "C2.1", "R1.2", "R2.2"],
    "SDA": ["J1.3", "U1.3", "R1.1"],
    "SCL": ["J1.4", "U1.4", "R2.1"]
  },

  "no_connect": [],

  "placement": {
    "J1": { "edge": "west", "rot": 0 },
    "U1": { "group": "sensor" },
    "C1": { "near": "U1", "within": 2.5, "group": "sensor" },
    "C2": { "near": "J1", "within": 4.0 },
    "R1": { "near": "U1", "within": 4.0, "group": "sensor" },
    "R2": { "near": "U1", "within": 4.0, "group": "sensor" }
  }
}
```

That is the whole design. There is not a coordinate in it.

| Key | What it holds |
| --- | --- |
| `parts` | `REF -> { footprint, symbol, value, lcsc, mpn, datasheet, description, dnp, exclude_from_bom, fields }`. `symbol` is the schematic symbol (`"Sensor:BME280"`); passives and pin headers get safe defaults keyed off the footprint *library* name, but a package footprint (SOT-23, SOIC-8) determines nothing about the part on it, so package-generic parts must declare it — and `validate()` checks the symbol's pin numbers against the footprint's plated pads, because a mismatch means the sheet and the board would describe different circuits. A bare string is the footprint, which keeps a 30-part design readable. Unknown keys are copied through to the netlist and the board's footprint properties, so the parts ledger's provenance survives into the fab outputs. |
| `nets` | `NAME -> ["REF.PAD", …]`. Pads are strings, because KiCad pad names are (`"1"`, `"A2"`, `"MP"`). |
| `no_connect` | `["REF.PAD", …]` — pads deliberately left unconnected. Not optional: see the validate gate. |
| `placement` | `REF -> { at, rot, side, near, within, edge, group, lock }`. `at` wins when given; otherwise `near` / `edge` / `group` are resolved into coordinates. Parts sharing a `group`, or joined by `near`, are placed as a unit — moving a connector moves its decoupling with it. |
| `board` | `layers`, `thickness`, `outline` (`auto` \| `rect`), `width`/`height`, `margin`, `part_gap`, `corner.chamfer`, `origin`, `mounting_holes`, `ground_net`, `pour_ground`. |
| `rules` | `clearance`, `track_width`, `track_widths` (per net), `via_diameter`/`via_drill`, `via_diameter_min`/`via_drill_min`, `min_track_width`, `min_hole`, `edge_clearance`. Defaults are JLCPCB 2-layer safe. These are written into the `.kicad_pro`, so the rules declared in the IR are the rules DRC enforces — the two cannot drift apart. |

[`design-ir.schema.json`](design-ir.schema.json) is the versioned machine contract for this
shape. It lets model adapters request structured output and lets host integrations reject a
malformed envelope before the deeper footprint, symbol, pad, and connectivity validator runs.
The Python loader and `synth check` remain authoritative for semantic validity.

Boolean flags are strict: `board.pour_ground`, `placement.*.lock`, `parts.*.dnp`, and
`parts.*.exclude_from_bom` take literal `true` or `false`, not quoted strings or `0`/`1`.
The loader rejects a malformed value at its exact field path; treating the string `"false"`
as truthy could otherwise lock placement, change routing, or alter the BOM silently.

`board.part_gap` defaults to a computed value rather than a constant, and the reason is the
one place where placement and routing genuinely couple (`place.py:routing_gap`): a channel
must fit the **widest** track, not the nominal one, and on a poured board it must also fit a
stitching **via**, because a fine-pitch part's middle pads can only reach the pour through
the channel beside the part. The old flat 0.15 mm gap was courtyard-legal and unroutable,
which is the worst of both — it passes the placer's own check and then strands pads.

## The pipeline

Each stage ends in a gate, and a failed gate stops the pipeline rather than handing the next
stage a broken input.

| Stage | What the gate proves | Cost |
| --- | --- | --- |
| `validate` | Every pad of every part's **real** footprint is in exactly one net or declared no-connect; every footprint, ref and pad exists. | ms |
| `place` | Zero **courtyard** overlaps (what `courtyards_overlap` actually tests — packing against the pad box passes a private check and fails the real one), and an outline shrink-wrapped to the result. | ms |
| `scaffold` | `.kicad_pro` + an openable empty `.kicad_pcb` + the `.net`, written from the design's own rules. | ms |
| `schematic` | A `.kicad_sch` the `.net`'s `source` field has been naming all along: symbols on the connection grid, a global label per pin, no-connect markers, power flags. | ms |
| `erc` | `kicad-cli sch erc`: 0 errors (warnings surfaced, not swallowed). | ~1 s |
| `sch-netlist` | KiCad's own exporter reads the schematic back; the result equals the design's connectivity **pad for pad** — the same comparison the board passes, so sheet and board provably describe one circuit. | ~1 s |
| `import` | KiCad's own `BOARD_NETLIST_UPDATER` reports 0 errors — footprints loaded, refs and values set, pads netted. | ~1 s |
| `position` | Every move read back from a fresh board and compared; `ISC_OK` is returned for edits the board discarded, so a successful call is not a changed board. | ~1 s |
| `connectivity` | The board's nets equal the design's nets **pad for pad**. | ms |
| `outline` | Edge cuts and the ground pour spliced in without reserializing the board KiCad just wrote. | ms |
| `route` | Every connection routed, or the failing connection named with a reason. | ~1 s |
| `silk` | Every reference designator clears the pads' mask apertures, other silk, and the board edge. | ms–s |
| `drc` | `kicad-cli pcb drc --severity-all --refill-zones`: the authority. | ~10 s |

The first seven run in about a second between them, which is the point. DRC is the authority
but it is also the slow, late feedback that made board generation an hours-long loop;
catching a design hole before a board exists costs nothing.

Two stage-ordering facts are load-bearing rather than stylistic. Routing runs **after** the
outline (the router needs the board edge to know where it may not go) and **before** the
silkscreen (a label's legal slots depend on what copper is already there). And DRC passes
`--refill-zones`: without it the pour is unfilled when connectivity is computed, and since
every surface ground pad reaches the pour through a stitching via, an unfilled zone makes
every one of them read as unconnected — the gate would fail a correctly routed board. The
DRC stage then classifies each reported finding from board UUIDs, structured object types, and
KiCad descriptions: its detail reports `real opens`, `zone/island artifacts`, and any
`ambiguous` findings. A zone-only result is not called clean; an ambiguous result fails closed.
Only `no_real_opens` permits the routed-open part of the gate to pass.

### Modules

| Module | Job |
| --- | --- |
| `ir.py` | The IR, and `Design.validate()` — the "every pad is netted or explicitly no-connect" gate, checked against the real footprint before a board exists. Problems are data (`severity`, `where`, `message`, `hint`), so a CLI prints them and an agent acts on them without parsing prose. |
| `library.py` | Footprints as data: pads, courtyards, re-emittable body, parsed once with `phicad/pylib/sexp.py` instead of the four regexes the demo used. |
| `symbols.py` | The same job for schematic symbols: pins, prefix, verbatim body for `lib_symbols` embedding. Derived symbols (`(extends …)`, half the stock set) are always flattened, because embedded unflattened they silently contribute zero pins to the netlist. |
| `netlist.py` | IR → KiCad `.net`, format version `E`, deterministic `uuid5` component ids. |
| `schematic.py` | IR → `.kicad_sch` at schematic version 20260830 (a different counter from the board's — the board's version makes the file unloadable). Connectivity is one global label per pin, placed exactly on the pin's connection point; no wires. Deterministic, so the file is honestly diffable — unlike the `.kicad_pcb`. |
| `project.py` | `.kicad_pro` + an empty openable board from the design's rules — the scaffolding nothing in `kicad-cli` provides. |
| `place.py` | Intent → coordinates: cluster, shelf-pack, legalize, shrink-wrap. |
| `route.py` | Grid A\* maze router plus ground stitching. |
| `../pylib/drc_opens.py` | Conservative DRC connectivity classification; preserves raw findings and fails closed on ambiguity. |
| `silk.py` | Reference designator placement solved against the board (courtyard-anchored candidates, scored), instead of a hand-written offset table. |
| `emit.py` | Edge cuts, pour, refdes moves and title spliced as text — nothing here reserializes a board KiCad wrote. |
| `session.py` | The one kipy-dependent module; drives `ImportNetlist` and placement over IPC, and encodes the traps. |
| `cli.py` | `check` \| `build` \| `verify`, with a gate after every stage. |

Everything except `session.py` is stdlib-only and runs on any `python3`. Keeping kipy in
exactly one module is deliberate: the IPC client is the part with version traps, and a trap
is easier to fix in one place than in five.

## Running it

`check` needs nothing but a `python3` — it is the fast inner loop:

```bash
python3 phicad/synth/cli.py check phicad/synth/examples/sensor-node.json
```

```
sensor-node: 6 parts, 4 nets, 0 no-connect
no problems
```

Failures are addressed to whoever wrote the design, with the real alternatives listed
(this is the output for a design with a dropped pin, a typo'd footprint and a pad that does
not exist):

```
error: nets.SCL: U1 (Package_LGA:Bosch_LGA-8_2.5x2.5mm_P0.65mm_ClockwisePinNumbering) has no pad '9'
    hint: its pads are: 1, 2, 3, 4, 5, 6, 7, 8
error: parts.R1: footprint 'Resistor_SMD:R_0805_2012Metrc' is not in any library
    hint: did you mean: Resistor_SMD:R_0805_2012Metric, Resistor_SMD:R_0805_2012Metric_Pad1.20x1.40mm_HandSolder
error: parts.U1: pad 5 is in no net and is not declared no-connect
    hint: add "U1.5" to a net, or list it in "no_connect" if it is deliberately unconnected (a mechanical pin, a spare header position)
```

`build` needs the automation venv, because the import/position stages go over IPC:

```bash
phicad/bin/phicad venv                       # once per checkout
phicad/bin/phicad synth build phicad/synth/examples/sensor-node.json -o /tmp/sn
```

`phicad synth` runs both subcommands under the venv — `check` would run on any python3,
but offering two interpreters lets a design validate here and fail to build there, which
is a worse failure than one command with one requirement. It also unsets
`KICAD_API_SOCKET`/`KICAD_API_TOKEN`, so a build opens its own document-less server
instead of reaching into an editor session you have open.

A finished `build` also gives the output directory a Git history scope, with the design IR
retained beside it, via the same `prepare_generated_project` hook the MCP `synth_build`
tool and New Project use. Standalone output gets a repository at its root; output inside an
existing checkout reuses that repository and scopes snapshots to the output directory, so
Agent View's Git status and Snapshot button work without silently nesting a repo or committing
unrelated files. It is best-effort: a build whose gates all passed is
never failed because git was unavailable, and the reason lands in the `project_history` field
of the `--json` report instead (a build that fails at `validate` writes no directory, and says
so).

The venv is resolved as `$SRC/build/venv`; point `PHICAD_VENV` at an existing one when
working from a second checkout or a worktree. The fork's `kicad-cli` is found via
`PHICAD_KICAD_CLI`, then the build tree next to the checkout —
`/Applications/KiCad.app` is **never** consulted, because this fork writes `version
20260831` and a stable KiCad 10.0 binary that can open such a file would rewrite it with the
10.0 serializer and drop fork constructs.

Useful flags: `--no-apply` stops after the netlist (no board touched), `--no-route` and
`--no-drc` cut the slow stages for a fast inner loop, `--grid` sets the router pitch,
`--force` overwrites an existing project, `--socket` sets the IPC socket path (default
`/tmp/phicad-synth.sock` — give concurrent runs distinct paths, per the swarm concurrency
rules), and `--json` prints one machine-readable report instead of the stage table.
`verify` re-runs the DRC gate against a board that already exists.

`--json` is the integration surface: the ai-view pane's `/design` and `/synth` commands and
the MCP tools all shell out to this CLI rather than reimplementing the pipeline, so the
pane, the tools and the command line cannot drift.

A failed stage carries the same detail a human sees, not just its one-line summary: `items`
lists the individual diagnostics (which connection did not route, which nets differ) and
`hint` says what to change. Those fields exist because the automatic repair loop feeds a
failed gate straight back to a model — `"1 UNROUTED"` names no net and suggests no fix, so a
summary alone is not machine-actionable. An unexpected failure still yields one JSON object
with `ok: false` and an `error` field rather than empty stdout, and the traceback goes to
stderr.

`build` starts and stops its own `kicad-cli api-server`. It never attaches to a running GUI:
pointing `KICAD_API_SOCKET` at the app's socket is a choice the caller makes explicitly,
because a headless save would rewrite a board the user has open.

## Verified results

Full macOS `build` on `examples/sensor-node.json`, against the fork build (`kicad-cli 10.99.0`):

```
PASS  validate      6 parts, 4 nets
PASS  place         12.7 x 22.1 mm (281 mm^2), no courtyard overlaps
PASS  scaffold      sensor-node.kicad_pcb, sensor-node.kicad_pro, sensor-node.net
PASS  schematic     sensor-node.kicad_sch: 6 symbols, 22 labels
PASS  erc           0 errors, 1 warnings (pin_to_pin)
PASS  sch-netlist   4 nets match the design pad for pad
PASS  import        6 footprints added, 0 errors, 0 warnings
PASS  position      6 placed
PASS  connectivity  4 nets match the design pad for pad
PASS  outline       9 items: edge cuts, ground pour
PASS  route         78 segments, 9 vias, 66.0 mm, 6 ground stitches, 2 necked
PASS  silk          6 references placed, title
PASS  fill          1 zone(s), 1 filled region(s)
PASS  drc           0 errors, 0 warnings, 0 unconnected

14/14 stages passed
```

A 2026-09-22 Linux run did **not** reproduce that result. It stopped at footprint validation
(`0/1 stages passed`) because the loader searched the example-local libraries and the macOS-only
stock-footprint path, neither of which held the required libraries on the host. Consequently ERC,
parity, routing, fill, and DRC did not run. The machine-readable
[Linux synth attestation](../build-attestations/linux-synth-sensor-node.json) preserves the exact
command, output, timing, environment, and unavailable later counts; this is evidence of the
current Linux limitation, not an end-to-end pass.

And on `examples/regulated-breakout.json` — 13 parts, 6 nets, an LDO, a status LED, two
mounting holes and connectors pinned to opposite edges — which exists to show the pipeline
is not fitted to the first example:

```
PASS  place         23.5 x 35.5 mm (836 mm^2), no courtyard overlaps
PASS  schematic     regulated-breakout.kicad_sch: 13 symbols, 36 labels
PASS  erc           0 errors, 1 warnings (pin_to_pin)
PASS  sch-netlist   6 nets match the design pad for pad
PASS  import        13 footprints added, 0 errors, 0 warnings
PASS  connectivity  6 nets match the design pad for pad
PASS  route         133 segments, 21 vias, 170.6 mm, 10 ground stitches, 2 necked
PASS  silk          13 references placed, title
PASS  fill          1 zone(s), 1 filled region(s)
PASS  drc           0 errors, 0 warnings, 0 unconnected

14/14 stages passed
```

That design carries `board.part_gap: 1.2` rather than the 1.0 mm the rules imply. At the
automatic value one ground pad in the middle of the LGA could not reach the pour, and the
CLI said so and named the lever; 0.2 mm more channel routed it. Widening the automatic
value instead was measured and rejected — it repacks every placement, and the arrangement
that came out failed on *both* examples. Routing success here is sensitive to the exact
arrangement rather than monotone in the spacing, which is why the design asks for room
when it needs room.

Both boards now come out with **no** DRC warnings. The `hole_to_hole` pairs the earlier
recorded runs showed — vias the maze router placed on layer changes, which nothing kept apart
because same-net cells are passable by design — are gone, and the routes are shorter on both
examples (66.0 mm vs 68.5, 170.6 mm vs 184.5) with fewer vias on the 13-part board. Every
number in the two blocks above was re-measured on the current tree; both runs are
reproducible, and two independent builds of one IR are byte-comparable via
`kicad-cli pcb diff --exit-code-only` (exit 0).

Timings on this machine: `check` ~0.95 s. A full `build` is ~20 s for the 6-part example and
~37 s for the 13-part one; routing dominates and scales with the copper already on the board,
because every segment laid becomes an obstacle the following nets are stamped against.

The two `necked` notes are the router reporting where it fell back from a net's declared
width, which is a design fact worth seeing rather than a silent optimization:

```
note: 3V3: U1.8 -> U1.2 at 0.15 mm (nominal 0.4 mm would not fit)
note: GND: U1.7 -> pour at 0.15 mm (nominal 0.4 mm would not fit)
```

Silkscreen was measured separately by the module that solves it: 9 silkscreen warnings → 0
on this example, and 0 on a 14-part design at rot 0/90/180/270 and on a 120-part board, with
solve costs of 14 ms, 21 ms and 1.1 s.

**Determinism, precisely.** The `.net` is byte-identical across runs (component ids are
`uuid5` of a fixed namespace plus project and reference), and so is the geometry: three
consecutive builds produced identical copper — 244 segment/via coordinate lines — and
identical footprint positions. The `.kicad_pcb` itself is **not** byte-identical, because
KiCad assigns fresh item UUIDs on every import and writes the footprint blocks in its own
order (verified: normalising UUIDs leaves two boards with the same set of lines in a
different order). Treat a regenerated board as semantically comparable, not diffable; use
`kicad-cli pcb diff` rather than `git diff` to ask whether anything really changed.

## What it does not do yet

- **Schematic layout is a grid, not routing.** The generated `.kicad_sch` is real —
  every symbol drawn, ERC-gated, and its exported netlist provably equal to the design's
  connectivity — but symbols sit in columns by kind with a global label per pin, no
  drawn wires. It reads as a pinout reference, not as signal flow; a hand-drawn sheet is
  still prettier.
- **The router does not rip up or shove.** It is grid A\*, not PNS: copper already committed
  is a permanent obstacle for every later net, so net ordering is chosen up front (escapes
  reserved per pad most-constrained-first; ground stitched last) and there is no mechanism
  to undo a bad early choice. It will refuse boards a good router would finish. That trade
  is deliberate — a refusal is recoverable, a clearance violation found at the fab is not.
- **Grid resolution is a real limit.** The default pitch is 0.1 mm, with a 0.05 mm retry as
  the last attempt. A corridor narrower than the grid can resolve is invisible to the
  router even when it is geometrically legal, and it can be missed purely by where the
  cells happen to land.
- **Two copper layers only.** `route.py` routes on `F.Cu` and `B.Cu` regardless of
  `board.layers`; declaring 4 gets a 4-layer stackup in the project file and a 2-layer
  route. Inner layers are never used.
- **Placement is shelf-packing, not floorplanning.** Clusters are packed into rows, tallest
  first, against a target aspect ratio, then legalized by a separation sweep. It produces
  courtyard-legal, routable boards; it does not reason about signal flow, thermals, return
  paths, or connector ergonomics, and it will not beat `pcb-placer` on a board where those
  matter.
- **Ground is assumed poured.** The strategy that removes ground from the maze entirely —
  a stitching via per surface pad, the pour doing the connecting — is what makes dense
  boards route at all here. `pour_ground: false` is accepted and leaves ground to the maze
  router with no special handling, which is a much weaker path and is not what the verified
  results exercise.

## Stock footprint lookup

Project-local `.pretty` roots beside the design (including `lib/`) are searched first.
`PHICAD_FOOTPRINT_DIR` selects one explicit stock root containing `Library.pretty` directories;
it replaces platform defaults and supports paths containing spaces. A missing override reports
the searched paths and does not silently fall back to another stock installation.

Without the override, Linux searches `/usr/local/share/kicad/footprints` followed by
`/usr/share/kicad/footprints`. macOS retains
`~/Library/Application Support/kicad/stock/footprints`. Libraries must be supplied separately;
path discovery does not install them. The corresponding existing symbol override is
`PHICAD_SYMBOL_DIR`.

## Where it sits

`synth` collapses the `sch-designer → pcb-placer → pcb-router → drc-fixer` span for boards
it can handle. It does not replace those agents: when a gate fails in a way the IR cannot
express, the fallback is the existing per-stage agents on the board synth already produced.
The agent-side contract is [`../agents/synth-designer.md`](../agents/synth-designer.md).

In the app, the ai-view pane exposes the same pipeline as `/design` and `/synth`. The model
answers in the IR and never writes a file or starts a build; `/design save` is the moment a
human agreed, and the compile follows. That review step is only meaningful because the IR is
small — a generated board arriving as a thousand lines of s-expressions is not something
anyone reads, and sixty lines of parts and nets is.
</content>
</invoke>
