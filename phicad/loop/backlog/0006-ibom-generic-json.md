---
id: 0006-ibom-generic-json
title: Interactive assembly BOM via InteractiveHtmlBom's generic JSON input (no SWIG)
status: done
priority: 60
tier: strong
scope:
  - phicad/bin/phicad
  - phicad/mcp/**
  - phicad/pylib/**
  - phicad/docs-site/docs/**
  - phicad/testing.md
depends_on:
  - 0001-linux-synth-evidence
requires:
  - $PHICAD_BUILD/pcbnew/_pcbnew.kiface
timeout_min: 60
max_attempts: 2
source: the OSS integration landscape review (unpublished proposal) ("Top recommendations", row 3)
branch: loop/0006-ibom-generic-json-20260922203258
---

## Definition of done

A new MCP tool `pcb_export_ibom` in `phicad/mcp/tools_cli.py` that produces InteractiveHtmlBom's
HTML for a board **without `import pcbnew`**: build ibom's *generic JSON* input
(`InteractiveHtmlBom/ecad/genericjson.py` documents the schema) from data the fork's own
`kicad-cli` can emit — `pcb export pos` (placements), the fork netlist/BOM export, board
outline/edges via `pcb export svg`/`stats` as available — then run
`generate_interactive_bom.py` on it as a **subprocess** (MIT tool, cloned or pip-installed under
`build/`, never vendored), returning the artifact path and a summary (component count, sides).

1. A pure-Python builder `phicad/pylib/ibom_json.py` with a stdlib test that feeds it captured
   `pcb export pos` CSV + a small netlist fixture and asserts the generic JSON shape (this is what
   `phicad check fast` runs). The end-to-end run belongs to the `full` gate.
2. The tool is registered in the MCP registry exactly like its siblings and
   `phicad/docs-site/docs/reference/mcp-tools-generated.md` is regenerated with the repo's
   generator (do not hand-edit).
3. `phicad/mcp/README.md` and the docs-site tools page describe the tool; `phicad/testing.md`
   lists the new tests.
4. Run it once on `phicad/synth/examples/sensor-node.json`'s generated board (from item
   0001's output directory if present, else regenerate) and quote the component count in the
   PR body.

## Facts

- `phicad/bin/phicad` is in scope only for the `run_gate` line that wires the new pylib test into `check fast`.

- The fork has no SWIG module (survey, "Summary" §1); everything must go through files,
  `kicad-cli`, or the IPC API.
- ibom's IPC-API backend is an open upstream PR (#555); do not depend on it.
- Keep the artifact under the project's `fab/` or the caller-supplied output directory,
  following the other `pcb_export_*` tools' conventions for `out_path`.
