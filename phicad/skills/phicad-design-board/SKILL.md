---
name: phicad-design-board
description: >-
  Design a printed circuit board from a description, using PhiCad's synth
  pipeline. Use when someone asks for a board, a PCB, a breakout, a carrier, a
  sensor node, "design me something that…", "make a PCB that…", or wants an
  existing generated design changed. Also use for "use PhiCad", "continue in
  Agent View", or "show me the board".
  Turns a circuit intent into a design IR, compiles it to a placed, routed,
  DRC-clean KiCad project, and reports what every gate said. Works from a terminal
  phi session, or any host
  with the phicad MCP server registered. Pairs with the synth-designer agent
  (which does the same job as a subagent) and with parts-scout when a part has
  to be sourced before it can be placed.
---

# Designing a board with PhiCad

You describe the circuit; deterministic code produces the board. Your job is the
**design** — which parts, what connects to what, what sits near what — never the
coordinates. The pipeline resolves geometry, and it is much better at it than you are.

## The one rule that matters

**Every pad of every part must be in exactly one net or explicitly in `no_connect`.**
This is the connectivity gate, it runs in milliseconds, and it is an error rather than a
warning. Count a footprint's pads before you write its nets.

## The loop

```bash
# validate — milliseconds, no board written. Run this after every edit.
phicad synth check design.json

# compile — scaffold, netlist, import, place, route, silkscreen, fill, DRC
phicad synth build design.json -o <project-dir>
```

Or the MCP tools `synth_check` / `synth_build`, which return the same report as JSON. Two
things about them, both easy to get wrong:

- **Write the IR to a file first.** Both tools take a path that must already exist; there is
  no inline-IR tool. In a worktree, `<worktree>/hw/<project>/design.json` is a good home.
- **Pass ABSOLUTE paths.** A relative path resolves against the *server's* repo root, not
  your working directory, so a relative path silently reads the wrong file or none.

If `phicad` is not on PATH, it is `<repo>/phicad/bin/phicad`. When the checkout holding the
repo is not the one holding the build tree — the normal layout — set `PHICAD_VENV` and
`PHICAD_KICAD_CLI`, or `PHICAD_REPO` for the MCP server.

## Show the board in Agent View

When the user wants to *see* the board rather than only read about it — "show me the board", or
any request to work on it without leaving the chat — serve the Agent View panel instead of telling
them to launch the PhiCad GUI:

```bash
phicad web-view <absolute-project-or-board-path> --open
```

Pass the path explicitly even though it is optional: with no path the command discovers a board
from its own working directory, which for an agent is not necessarily the board you mean.

Do not pin a model in the board project. Agent View inherits the live phi provider/model and
reasoning level, so future models work when they are available in phi's catalog.

The panel shows PCB/front/back/schematic/3D views, Git status, and a Snapshot button. For the
live Context tab, a
`phicad api-server <board>` must be running (or PhiCad open) — board renders work either way.

## The design IR

`phicad/synth/examples/sensor-node.json` is the worked example; read it before writing your
first design. `phicad/synth/README.md` has the full schema. The shape:

```json
{
  "project": "sensor-node",
  "board": {"layers": 2, "outline": "auto", "corner": {"chamfer": 2.0},
            "ground_net": "GND", "pour_ground": true},
  "rules": {"clearance": 0.2, "track_width": 0.25,
            "track_widths": {"3V3": 0.4, "GND": 0.4}},
  "parts": {"U1": {"footprint": "Package_LGA:Bosch_LGA-8_…", "value": "BME280",
                   "lcsc": "C92489"}},
  "nets":  {"GND": ["J1.1", "U1.1"], "SDA": ["J1.3", "U1.3", "R1.1"]},
  "no_connect": ["U1.4"],
  "placement": {"J1": {"edge": "west"}, "C1": {"near": "U1", "within": 2.5}}
}
```

- Pins are `"REF.PAD"` using the **footprint's** pad names, which are not always the chip's
  pin names. A wrong pad number fails `check` with the real list in the message.
- Footprints are `"Library:Name"` from the stock KiCad libraries. A wrong name fails with
  the closest real matches listed.
- **Do not choose coordinates.** State intent — `near`, `within`, `edge`, `group` — and let
  the placer resolve it. Use `"at": [x, y]` only when something is fixed by the outside
  world, like a connector aligned to an enclosure cutout.

## What the gates prove, and what they do not

Every stage reports its evidence, and a failure names the lever. The gates prove the
netlist was **built** correctly. They do not prove it is the **right circuit** — nothing in
the pipeline knows what a diode is, so a reversed LED or a swapped I2C pair passes all of
them. That judgement is yours, and it is the part of this job worth your attention.

Specifically worth checking by hand, because no gate will:

- **Polarity.** KiCad's `Device:LED` has pin 1 = K and pin 2 = A. Diodes, electrolytics and
  connectors have an orientation the netlist cannot express.
- **Pinout against the datasheet.** The footprint's pad numbering and the part's pin
  function are different things.
- **Strapping pins.** A sensor's address or interface-select pins have to be tied
  somewhere deliberate, not left to `no_connect`.
- **Part values.** `value` should be a part spec ("100nF 50V X7R 0603"), not a signal name.
  Carry `mpn` and `lcsc` when you know them, or the BOM is not orderable.

## When a gate fails

Fix the **design**, not the board. The pipeline is deterministic; editing its output is
work that the next build discards.

| Gate | What it means | The lever |
| --- | --- | --- |
| `validate` | a pad is unnetted, a footprint or pad name is wrong | the message names the fix |
| `place` | courtyards overlap | `board.part_gap`, or a `group`/`near` that packs better |
| `route` | a connection could not be made | usually placement, not routing: `board.part_gap`, a narrower `rules.track_widths` entry for that net, or a hint putting its ends nearer |
| `drc` | the authority | read `drc.json`; it names the items |

More room is **not** reliably better — widening spacing repacks the whole placement and can
make routing worse. Change one thing at a time and re-check.

## Sourcing

If a part has to be chosen rather than named, hand off to **parts-scout** first: it
researches an exact MPN and LCSC id into a `parts.json` ledger, and **parts-integrator**
fetches symbol, footprint and 3D model into the project. Then reference the fetched
footprint from the IR. Do not invent an LCSC id.

## Handing the result over

`build` reports the board, project, netlist and `drc.json` paths. A successful build also
retains `<project>.design.json` and initializes standalone Git history (or safely reuses an
existing enclosing repository) with a baseline
commit. Use `project_git_snapshot(project_path, message)` for named verified checkpoints;
Agent View autosnapshots stable manual or agent saves while it is open.

To open the project in a running PhiCad, the project manager must be the top window; otherwise
tell the user the path and let them open it.

PhiCad's own AI View pane accepts `/design save`, `/synth check` and `/synth build`, so a
design started in chat here can be continued inside the application, and vice versa. Both call
the same `phicad/synth/cli.py --json`; shared mode additionally uses this exact live phi
session, so conversation history and tool access do not drift.
