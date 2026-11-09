# The application

PhiCad **is a desktop EDA application**: the full KiCad 10.99 suite — project manager, schematic
editor, PCB editor, 3D viewer, Gerber viewer — with an AI-native layer that lives *inside* it.
This page is the application's own manual: how to launch it, what is in it, how the AI layer
appears in the GUI, and how every GUI feature maps to a headless equivalent.

If you want to drive PhiCad *without* the GUI, start at
[Driving PhiCad from a shell or an agent](driving-phicad.md) instead — the two pages are
complements.

## Choose your route

<div class="grid cards" markdown>

- :material-rocket-launch: **New user**

    Start with [Getting started](getting-started.md) and the [visual product tour](product-tour.md)
    before choosing a desktop or headless workflow.

- :material-monitor: **Desktop user**

    Stay on this page: launch from the build tree, complete first-run setup, and open the editors
    and Agent View.

- :material-robot-outline: **Agent operator**

    Use [Driving PhiCad](driving-phicad.md) for copyable MCP, `phicad tool`, Python, IPC, and
    `kicad-cli` examples.

</div>

The tour labels **current build output** separately from **product direction**. Treat the latter
as a visual direction, not as a capability list for the current checkout.

## Launching

PhiCad runs from the build tree; there is no installer step:

```bash
phicad/bin/phicad build kicad          # only if the build tree is stale
KICAD_RUN_FROM_BUILD_DIR=1 build/mac-dev/kicad/PhiCad.app/Contents/MacOS/PhiCad
# or, equivalently:
phicad/bin/phicad run kicad
```

`KICAD_RUN_FROM_BUILD_DIR=1` makes the app resolve its support files from the build tree instead
of an installed bundle. Build instructions and the mandatory macOS CMake overrides are in
[Build](../reference/build.md).

!!! note "First run"
    A fresh config shows the **PhiCad Setup** wizard. Dev builds do not ship the upstream symbol /
    footprint library packs, so "Start with no libraries" is the honest choice — the AI layer
    brings its own parts (see [Parts pipeline](../reference/parts-pipeline.md)). The upstream
    update-check opt-in is hidden in this fork (updates arrive through git, not kicad.org), so
    the wizard no longer asks.

## The project manager

The launcher window is stock KiCad — Schematic Editor, Symbol Editor, PCB Editor, Footprint
Editor, Gerber Viewer, Image Converter, Calculator Tools, Drawing Sheet Editor, Plugin and
Content Manager — plus one PhiCad addition:

- **New AI Project** — the dashboard entry point added by the Agent View plugin. It takes a
  plain-language description and hands it to the synthesis pipeline
  ([Board synthesis](../reference/synth.md)): design IR → schematic + placed, routed,
  DRC-clean board, opened in the editors when it lands.

## Agent View — the AI pane

The heart of the application layer is **Agent View**, an API plugin (no C++ changes — it
survives upstream merges). A sparkle button at the right end of the PCB editor's top toolbar
opens a window showing the **live board as structured data** plus a chat pane grounded in that
data: an assistant reads layers, stackup, footprints, nets, routing state and extents over the
IPC API instead of squinting at screenshots.

### Install

Nothing to install: the plugin is **staged into `PhiCad.app` at build time**
(`phicad/plugins/stage_ai_view.cmake`), so every build carries its own Agent View and the
sparkle button is just there.

For plugin hacking, `phicad ai-view-install` places a per-user copy that **shadows** the stock
one inside the app bundle; edit and reload without rebuilding, and delete the copy (or re-run
the command) to fall back to the shipped plugin.

!!! warning "A symlink does not work (hacking path)"
    `API_PLUGIN_MANAGER::ReloadPlugins` walks the user plugins directory with
    `wxDir::Traverse`, which does not follow symlinked directories — a symlinked plugin is
    silently never discovered. Use `phicad ai-view-install`, which copies.

### What is in the window

| Tab / pane | What it gives you |
| --- | --- |
| Chat | a phi conversation grounded in the open board's structured context |
| PCB / Schematic / 3D views | rendered views of the live project, streamed as the design changes |
| BOM / Datasheets | the board's grouped BOM — qty, references, value, footprint, MPN, LCSC, datasheet, flags — with a preview pane |
| Build stages | `/design` and `/synth` runs stream their real pipeline stages instead of the window watching files appear |

### What it can do to the board

Chat is not read-only, and it is not unbounded either:

- **Whitelisted write commands** (`board_edits.py`) act on the open board over IPC —
  transactional, so a bad edit rolls back rather than half-landing.
- **`/design` and `/synth`** compile a *new* project from a description via the design-IR
  pipeline, with an automatic check/repair loop (`design_agent.py`).
- **Project history** (`project_history.py`) keeps standalone projects in their own Git
  repository and safely scopes projects inside existing checkouts to the nearest repository,
  with a baseline and stable-save autosnapshots, so every AI edit is a commit you can diff and
  revert —
  the same journal the headless tools write
  ([the git journal workflow](driving-phicad.md#the-git-journal-workflow)). The journal ships a
  KiCad-aware merge driver, so merging two lines of work cannot write conflict markers into a
  board file.

### The interpreter it runs on

The plugin's manifest uses runtime type `exec`, deliberately: the `python` runtime would build a
fresh venv and pip-install a stock `kipy` from the network, but the fork needs the
**regenerated** bindings in the repo's `build/venv` (`phicad/bin/regen-kipy-proto`).
`ai_view.sh` walks up from its own real location to find `build/venv/bin/python` and honours
`$PHICAD_AI_PYTHON` as an override. That interpreter needs both `tkinter` and `kipy`:

```bash
build/venv/bin/python -c 'import tkinter, kipy'   # phicad bootstrap installs both
```

## Designing a board inside the application

The end-to-end loop, entirely in the GUI:

1. **Describe** — *New AI Project* from the dashboard, or `/design` in Agent View.
2. **Synthesize** — the design IR is compiled: parts resolved from the parts pipeline,
   schematic emitted, board placed and routed; stages stream into the pane as they run.
3. **Gate** — DRC/ERC run as part of the pipeline; the result opens in the editors only after
   the gates pass ([Board synthesis](../reference/synth.md) documents every gate).
4. **Iterate** — chat edits (whitelisted, transactional) or normal hand edits in the editors;
   every stable save is autosnapshotted into the project's history repo.
5. **Route densely** — boards beyond the built-in router's honesty threshold go to the
   Freerouting loop: [Autoroute](../reference/autoroute.md).
6. **Ship** — fabrication packs via jobsets, from chat/MCP or the CLI
   ([Driving PhiCad](driving-phicad.md#3-export-a-fabrication-pack)).

A complete worked example lives in the repo: `phicad/demos/teensy-sensor-suite/` — generation,
routing check, and plots, with its own README.

## GUI ↔ headless map

Everything the application does has a headless twin — same functions, different front door:

| In the application | Headless |
| --- | --- |
| Agent View chat, board context | `phicad tool` / MCP `board_summary`, IPC API |
| New AI Project, `/design`, `/synth` | `phicad synth check` / `phicad synth build` |
| DRC dialog | `pcb_drc` tool / `kicad-cli pcb drc --format json` |
| Autoroute from the design loop | `phicad autoroute board.kicad_pcb --drc` |
| BOM / Datasheets tab | BOM jobs and the [parts pipeline](../reference/parts-pipeline.md) |
| Plot / fab outputs | jobsets (`phicad/mcp/examples/fab_pack.kicad_jobset`) |
| Project history autosnapshots | the git journal (`init` / edit hooks / `snapshot`) |

The mapping is exact because the GUI layer calls the same registry the MCP server and
`phicad tool` expose — nothing is reimplemented, so nothing drifts
([how that works](driving-phicad.md)).

## Support and feedback

For a visual orientation, see the [PhiCad product tour](product-tour.md). Prepare a bug, feature,
or page-improvement draft through the [feedback guide](feedback.md); maintainers use the
[feedback administration reference](../reference/feedback-admin.md).

When a report needs bounded diagnostics, generate and review a local archive before sharing it:

```bash
phicad support bundle [--project PATH] [--output PATH] [--json]
```

The default bundle excludes credentials, prompts, transcripts, board/schematic contents, arbitrary
environment values, and raw logs. Supplying `--project` adds only names, sizes, and SHA-256 hashes.

## Troubleshooting

| Symptom | Cause / fix |
| --- | --- |
| No sparkle button | stale build (the plugin stages at build time — rebuild `kicad`), or a broken per-user hacking copy shadowing the stock one — remove it or re-run `phicad ai-view-install` |
| Agent View opens then dies | `build/venv/bin/python` missing `tkinter` or `kipy` — `phicad bootstrap`, then the import check above |
| Chat pane connects to nothing | the plugin inherits `KICAD_API_SOCKET` / `KICAD_API_TOKEN` from the app — launch PhiCad itself, not the script by hand |
| `/design` never places parts | parts pipeline not primed — see [Parts pipeline](../reference/parts-pipeline.md) |
| Autoroute "starts a GUI" or hangs | running Freerouting by hand without `-Djava.awt.headless=true` — use `phicad autoroute`, which always sets it |
| Setup wizard reappears | config home is per build-dir instance under `KICAD_RUN_FROM_BUILD_DIR=1`; that is deliberate isolation |
| Editors warn about missing libraries | expected on a library-less first run; AI-generated projects carry their own symbols/footprints |
| Where do bug reports go? | **Help → Report Bug** files against the fork's tracker (`couplet-physical-ai/PhiCad` issues), never upstream — the version block it pre-fills already carries the `-phicad` suffix |

## Where the application layer lives

| Piece | Path |
| --- | --- |
| Agent View plugin (all of it) | `phicad/plugins/ai-view/` |
| Synthesis pipeline | `phicad/synth/` |
| Autoroute driver | `phicad/autoroute/` |
| Parts pipeline | `phicad/parts/` |
| MCP server (chat tools) | `phicad/mcp/` |
| Worked demo | `phicad/demos/teensy-sensor-suite/` |
| The wrapper that ties it together | `phicad/bin/phicad` |

Everything above is inside `phicad/` by design — the fork's golden rule — so upstream KiCad
merges never collide with the application layer ([Architecture](../reference/architecture.md)).

## Next steps

<div class="grid cards" markdown>

- :material-rocket-launch: **[Build a first board](getting-started.md)**

    Run the smallest real synthesis, then read the gate evidence and known limitations.

- :material-console: **[Drive PhiCad headlessly](driving-phicad.md)**

    Use the same capabilities from an agent, shell, Python script, or MCP client.

- :material-map: **[Take the visual tour](product-tour.md)**

    Compare reproducible current output with explicitly labeled product direction.

</div>
