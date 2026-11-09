# MCP tool reference (generated)

!!! warning "Generated file — do not edit by hand"
    This page is rendered from the live FastMCP registry by

    ```bash
    phicad/bin/phicad-tool docs > phicad/docs-site/docs/reference/mcp-tools-generated.md
    ```

    Every entry below is the tool's real signature, JSON Schema and docstring as the server
    registers it, so the page cannot drift from `phicad/mcp/server.py`. Edit the tool's
    docstring instead. Hand-written narrative belongs in
    [MCP tools](mcp-tools.md).

Every tool listed here is callable three ways with identical semantics:

| Surface | How |
| --- | --- |
| MCP client (chat) | `phicad` over stdio: `build/mcp-venv/bin/python phicad/mcp/server.py` |
| Shell / agent | `phicad/bin/phicad-tool call <tool> --arg key=value` |
| Python | `registry.collect(server)` -> `spec.fn(**kwargs)` |

`ok` in every result dict is the verdict: `true` means the tool did what it says. Tools marked
**writes** can change files that already existed or live session state; everything else either
reads, or writes a new artifact under the MCP output directory.


**127 tools** in 17 sections.

## Index

| Tool | Section | Writes | Summary |
| --- | --- | --- | --- |
| [`pcb_drc`](#pcb_drc) | Gates — DRC / ERC | no | Run design-rule check on a .kicad_pcb with the fork's kicad-cli. |
| [`sch_erc`](#sch_erc) | Gates — DRC / ERC | no | Run electrical-rule check on a .kicad_sch with the fork's kicad-cli. |
| [`pcb_render`](#pcb_render) | Visual + fabrication exports | no | Raytraced PNG render of a board (works headless). |
| [`pcb_export_svg`](#pcb_export_svg) | Visual + fabrication exports | no | Export board layers to SVG (multi-file by default). |
| [`pcb_export_gerbers`](#pcb_export_gerbers) | Visual + fabrication exports | no | Export Gerbers (and, by default, Excellon drill files) for fabrication. |
| [`pcb_export_step`](#pcb_export_step) | Visual + fabrication exports | no | Export a 3D STEP model of the board (OpenCascade). |
| [`sch_export_bom`](#sch_export_bom) | Visual + fabrication exports | no | Export a grouped BOM CSV from a schematic. |
| [`pcb_export_stats`](#pcb_export_stats) | Visual + fabrication exports | no | Board statistics as JSON (`kicad-cli pcb export stats --format json`). |
| [`pcb_export_pos`](#pcb_export_pos) | Visual + fabrication exports | no | Pick-and-place (component position) file for assembly. |
| [`pcb_export_pdf`](#pcb_export_pdf) | Visual + fabrication exports | no | Plot board layers into ONE multi-page PDF (one page per layer) for review. |
| [`sch_export_netlist`](#sch_export_netlist) | Visual + fabrication exports | no | Export a schematic netlist (`kicad-cli sch export netlist`). |
| [`sch_export_pdf`](#sch_export_pdf) | Visual + fabrication exports | no | Export the whole schematic hierarchy to a single PDF for review. |
| [`pcb_diff`](#pcb_diff) | Semantic diffs | no | Semantic diff of two .kicad_pcb files. |
| [`sch_diff`](#sch_diff) | Semantic diffs | no | Semantic diff of two .kicad_sch files. |
| [`jobset_run`](#jobset_run) | Jobsets — the repeatable agent -> fab handoff | no | Run a .kicad_jobset against a .kicad_pro project (so ${KIPRJMOD} resolves). |
| [`board_summary`](#board_summary) | Board summary | no | Structural summary of a board (footprints, tracks, vias, zones, nets) parsed with kicad-tools in the automation venv — no kicad-cli, no GUI, works on fork-format files. |
| [`api_server_start`](#api_server_start) | IPC API — ONE managed api-server child | no | Start the single managed `kicad-cli api-server` child — a HEADLESS PhiCad session. |
| [`session_open`](#session_open) | Headless SESSION surface | no | Open a document in the managed HEADLESS session over IPC (no GUI, no screen control). |
| [`session_documents`](#session_documents) | Headless SESSION surface | no | List what the managed headless session has open. |
| [`session_save`](#session_save) | Headless SESSION surface | no | Save the document open in the managed headless session (SaveDocument over IPC). |
| [`session_board_query`](#session_board_query) | Headless SESSION surface | no | Read structured facts from the board LIVE in the managed headless session. |
| [`session_close`](#session_close) | Headless SESSION surface | no | Close the document(s) open in the managed headless session, leaving the server running. |
| [`api_server_stop`](#api_server_stop) | Headless SESSION surface | no | Stop the managed api-server child (SIGTERM, then SIGKILL) and clean up its socket. |
| [`api_server_reap_orphans`](#api_server_reap_orphans) | Headless SESSION surface | no | Find and kill `kicad-cli api-server` processes that are orphaned AND unreachable. |
| [`api_server_status`](#api_server_status) | Headless SESSION surface | no | Report the managed api-server child: pid, socket, opened file, liveness. |
| [`kipy_exec`](#kipy_exec) | Headless SESSION surface | no | Run a kipy script in the automation venv against the managed api-server. |
| [`parts_fetch`](#parts_fetch) | Parts pipeline & layout swarm | no | Fetch exact parts (symbol + footprint + 3D model) into a project library. |
| [`parts_audit`](#parts_audit) | Parts pipeline & layout swarm | no | Fab-readiness gate: every footprint has a resolvable 3D model + LCSC/MPN id. |
| [`swarm_partition`](#swarm_partition) | Parts pipeline & layout swarm | no | Partition a board's nets into N disjoint groups for parallel routing agents. |
| [`swarm_score`](#swarm_score) | Parts pipeline & layout swarm | no | Score a board candidate (lower = better) for best-of-N selection. |
| [`swarm_merge`](#swarm_merge) | Parts pipeline & layout swarm | **yes** | Merge per-group copper from swarm candidate boards into the base board. |
| [`synth_check`](#synth_check) | Design synthesis (phicad/synth) | no | Validate a design IR without writing anything. |
| [`synth_build`](#synth_build) | Design synthesis (phicad/synth) | **yes** | Compile a design IR into a placed, routed, DRC-checked KiCad project. |
| [`synth_verify`](#synth_verify) | Design synthesis (phicad/synth) | no | Run the synth pipeline's gates (connectivity, outline, DRC) against an EXISTING board. |
| [`project_git_snapshot`](#project_git_snapshot) | Per-project Git history | no | Commit all current files in one board project's Git history scope. |
| [`integration_capabilities`](#integration_capabilities) | Host integration / repo state | no | Discover PhiCad's host/model protocol without guessing model or UI versions. |
| [`phicad_status`](#phicad_status) | Host integration / repo state | no | Parsed `phicad status`: branch, HEAD, build dir, upstream delta, venv, api-server. |
| [`pcb_export_dsn`](#pcb_export_dsn) | Autoroute | no | Export a board as a Specctra DSN design for an external autorouter. |
| [`pcb_import_ses`](#pcb_import_ses) | Autoroute | **yes** | Merge a Specctra session (.ses) back into a board: the return leg of the loop. |
| [`autoroute_freerouting`](#autoroute_freerouting) | Autoroute | **yes** | Route a placed board with Freerouting, headless: DSN out, router, SES back in. |
| [`autoroute_cleanup`](#autoroute_cleanup) | Autoroute | **yes** | Remove the debris an autorouter leaves behind: dangling vias/tracks, sub-min necks. |
| [`autoroute_stitch`](#autoroute_stitch) | Autoroute | **yes** | Drop plane-net BGA balls onto their planes: the dogbone fanout the router skipped. |
| [`fp_export_svg`](#fp_export_svg) | Cli | no | Render a .pretty footprint library (or one footprint in it) to SVG. |
| [`fp_upgrade`](#fp_upgrade) | Cli | no | Rewrite a .pretty footprint library in the current file format. |
| [`fp_diff`](#fp_diff) | Cli | no | Semantic diff of two .pretty footprint library directories. |
| [`sym_export_svg`](#sym_export_svg) | Cli | no | Render a .kicad_sym symbol library (or one symbol in it) to SVG. |
| [`sym_upgrade`](#sym_upgrade) | Cli | no | Rewrite a .kicad_sym symbol library in the current file format. |
| [`sym_diff`](#sym_diff) | Cli | no | Semantic diff of two .kicad_sym symbol libraries. |
| [`gerber_info`](#gerber_info) | Cli | no | Inspect one Gerber or Excellon file: type, item/aperture counts, bbox, copper area. |
| [`gerber_diff`](#gerber_diff) | Cli | no | Geometric diff of two Gerber/Excellon files: added/removed copper area. |
| [`gerber_convert_png`](#gerber_convert_png) | Cli | no | Rasterise one Gerber/Excellon file to PNG — a visual check on fab output. |
| [`pcb_export_drill`](#pcb_export_drill) | Cli | no | Drill files for fabrication (Excellon by default, Gerber X2 optionally). |
| [`pcb_export_ibom`](#pcb_export_ibom) | Cli | no | Interactive assembly BOM (InteractiveHtmlBom) for a board — one self-contained HTML file. |
| [`pcb_export_dxf`](#pcb_export_dxf) | Cli | no | DXF plot of a layer list — the format mechanical CAD actually reads. |
| [`pcb_export_gencad`](#pcb_export_gencad) | Cli | no | GenCAD 1.4 export — the format many older test/assembly houses ingest. |
| [`pcb_export_ipcd356`](#pcb_export_ipcd356) | Cli | no | IPC-D-356 netlist — the electrical-test (bed-of-nails / flying probe) handoff. |
| [`pcb_export_ipc2581`](#pcb_export_ipc2581) | Cli | no | IPC-2581 export — the single-file modern fab/assembly handoff (board + stackup + BOM). |
| [`pcb_export_odb`](#pcb_export_odb) | Cli | no | ODB++ export — the fab format most Asian board houses prefer. |
| [`pcb_export_png`](#pcb_export_png) | Cli | no | Flat 2D PNG plot of a layer list (NOT the 3D raytraced view — that is pcb_render). |
| [`pcb_export_ps`](#pcb_export_ps) | Cli | no | PostScript plot of a layer list — the classic 1:1 etch/photoplot output. |
| [`pcb_export_stackup`](#pcb_export_stackup) | Cli | no | Board stackup report — layer order, material, thickness, Er, loss tangent, finish. |
| [`pcb_export_3d`](#pcb_export_3d) | Cli | no | Every 3D export except plain STEP (that is server.py's pcb_export_step). |
| [`pcb_upgrade`](#pcb_upgrade) | Cli | **yes** | Rewrite a .kicad_pcb in the current board file format. |
| [`sch_upgrade`](#sch_upgrade) | Cli | no | Rewrite a .kicad_sch in the current schematic file format. |
| [`pcb_import`](#pcb_import) | Cli | **yes** | Convert a non-KiCad board file into a .kicad_pcb. |
| [`sch_import`](#sch_import) | Cli | **yes** | Convert a non-KiCad schematic file into a .kicad_sch. |
| [`sch_export_plot`](#sch_export_plot) | Cli | no | Plot a schematic to SVG / DXF / PS / PNG / HPGL — one file per page. |
| [`sch_export_pythonbom`](#sch_export_pythonbom) | Cli | no | Export the legacy BOM **XML** intermediate netlist (the `python-bom` format). |
| [`export_bom`](#export_bom) | Cli | no | Full-control BOM export — the same EXPORT_BOM_COMMAND behind `sch export bom` AND `pcb export bom`, dispatched on the input's extension. |
| [`pcb_layers`](#pcb_layers) | Cli | no | The board's layer table — parsed straight out of the .kicad_pcb, no kicad-cli, no API. |
| [`kicad_cli_help`](#kicad_cli_help) | Cli | no | The fork's own `--help` for any kicad-cli subcommand, parsed into a flag list. |
| [`kicad_cli`](#kicad_cli) | Cli | no | Escape hatch: run the fork's kicad-cli with an arbitrary argv and report what appeared. |
| [`project_init`](#project_init) | Git | no | Initialize history for a KiCad project without nesting inside an existing repo. |
| [`project_snapshot`](#project_snapshot) | Git | **yes** | Commit the project's current state as one auditable revision. |
| [`project_status`](#project_status) | Git | no | Report the project repository's branch, HEAD, pending changes and commit count. |
| [`project_log`](#project_log) | Git | no | List recent commits of the project with the actor and tool behind each one. |
| [`project_diff`](#project_diff) | Git | no | Diff two revisions of a project: git stat plus the fork's SEMANTIC board/schematic diff. |
| [`project_restore`](#project_restore) | Git | **yes** | Bring files back from an earlier revision — the "undo a bad edit" tool. |
| [`project_undo_last`](#project_undo_last) | Git | **yes** | Undo the most recent commit with `git revert` — history is preserved, not rewritten. |
| [`project_tag`](#project_tag) | Git | **yes** | Mark the project's current revision as a milestone (an annotated git tag). |
| [`project_journal`](#project_journal) | Git | no | Read the project's action journal: who did what to this board, in order. |
| [`project_watch_once`](#project_watch_once) | Git | no | One poll of the save watcher: commit the project once its files have stopped changing. |
| [`sch_list_symbols`](#sch_list_symbols) | Schematic | no | List the symbols placed on a schematic: refs, values, footprints, lib_ids, positions. |
| [`sch_get_symbol`](#sch_get_symbol) | Schematic | no | Everything about one placed symbol, by reference designator. |
| [`sch_annotate_check`](#sch_annotate_check) | Schematic | no | Find unannotated (R?, U?) and duplicated reference designators in a hierarchy. |
| [`sch_list_sheets`](#sch_list_sheets) | Schematic | no | Walk the sheet hierarchy from a root schematic: files, sheet pins, page numbers. |
| [`sch_find`](#sch_find) | Schematic | no | Search a schematic hierarchy across refs, lib_ids and every field name and value. |
| [`sch_list_nets`](#sch_list_nets) | Schematic | no | List the schematic's nets and the pins on them (the real connectivity, not geometry). |
| [`sch_symbol_lib_list`](#sch_symbol_lib_list) | Schematic | no | List every symbol in a .kicad_sym library: units, pin counts, prefix, footprint. |
| [`sch_symbol_lib_get`](#sch_symbol_lib_get) | Schematic | no | One symbol definition from a .kicad_sym: its pins and its properties. |
| [`sch_set_field`](#sch_set_field) | Schematic | **yes** | Set one field on one placed symbol (Value, Footprint, MPN, any custom field). |
| [`sch_set_fields_bulk`](#sch_set_fields_bulk) | Schematic | **yes** | Set many fields across many symbols in ONE pass over one schematic file. |
| [`sch_place_symbol`](#sch_place_symbol) | Schematic | **yes** | Place a symbol instance on a schematic sheet at (x_mm, y_mm). |
| [`sch_add_wire`](#sch_add_wire) | Schematic | **yes** | Add a wire (or `kind="bus"`) segment between two points, in millimetres. |
| [`sch_add_label`](#sch_add_label) | Schematic | **yes** | Add a net label: `kind` = local \| global \| hier. |
| [`sch_add_no_connect`](#sch_add_no_connect) | Schematic | **yes** | Mark a pin deliberately unconnected: the X flag that silences ERC for that pin. |
| [`sch_delete_symbol`](#sch_delete_symbol) | Schematic | **yes** | Delete every unit of one placed symbol from one schematic file. |
| [`session_list_items`](#session_list_items) | Session | no | List items of one kind on the board open in the managed headless session. |
| [`session_get_item`](#session_get_item) | Session | no | Everything about ONE item, addressed by the KIID that session_list_items returned. |
| [`session_get_nets`](#session_get_nets) | Session | no | Every net on the board with its net class, sorted by name. |
| [`session_get_stackup`](#session_get_stackup) | Session | no | The board's physical stackup plus its layer configuration. |
| [`session_bounding_box`](#session_bounding_box) | Session | no | KiCad's own bounding box, in mm, for the named items or for the whole board. |
| [`session_hit_test`](#session_hit_test) | Session | no | Ask KiCad what is under a point — the reliable way to find the item at a location. |
| [`session_selection`](#session_selection) | Session | no | Read or change the editor selection: action get \| set \| add \| clear. |
| [`session_title_block`](#session_title_block) | Session | no | Read the board's title block, or write it when `fields` is given. |
| [`session_update_footprint`](#session_update_footprint) | Session | **yes** | Change one footprint, addressed by reference designator: any subset of its properties. |
| [`session_batch_move`](#session_batch_move) | Session | **yes** | Move (and optionally rotate) many footprints to absolute positions in ONE undo step. |
| [`session_add_track`](#session_add_track) | Session | **yes** | Add one straight copper track segment to the open board. |
| [`session_add_via`](#session_add_via) | Session | **yes** | Add one via at a position, with a drill and a pad diameter in mm. |
| [`session_add_zone`](#session_add_zone) | Session | **yes** | Add a copper zone from a closed polygon of [x_mm, y_mm] points (>= 3). |
| [`session_add_text`](#session_add_text) | Session | **yes** | Add free text to the open board on any enabled layer. |
| [`session_add_graphic`](#session_add_graphic) | Session | **yes** | Add a graphic shape: kind = line \| rect \| circle \| arc \| poly. |
| [`session_delete_items`](#session_delete_items) | Session | **yes** | Delete the given items from the open board by KIID, in one undo step. |
| [`session_delete_by_filter`](#session_delete_by_filter) | Session | **yes** | Delete every item of a kind, optionally narrowed to a layer and/or net. |
| [`session_set_net`](#session_set_net) | Session | **yes** | Assign an existing net to the given items (tracks, vias, zones, pads, copper shapes). |
| [`session_set_enabled_layers`](#session_set_enabled_layers) | Session | **yes** | Set the board's enabled layer set. |
| [`session_set_origin`](#session_set_origin) | Session | **yes** | Move the grid origin (`which='grid'`) or the drill/place-file origin (`which='drill'`). |
| [`session_refill_zones`](#session_refill_zones) | Session | **yes** | Refill every zone on the board and report which ones actually ended up filled. |
| [`session_run_action`](#session_run_action) | Session | no | Run a named KiCad TOOL_ACTION in the session (escape hatch for anything unexposed). |
| [`session_revert`](#session_revert) | Session | **yes** | Discard every unsaved session change by reloading the board from its file on disk. |
| [`verify_capabilities`](#verify_capabilities) | Verify | no | What PhiCad Verify can do on THIS machine, with tool versions. |
| [`verify_check`](#verify_check) | Verify | no | Evaluate a tscircuit TSX design and return its diagnostics. |
| [`verify_build`](#verify_build) | Verify | no | Build a TSX design, then check the exported board with KiCad's DRC. |
| [`verify_circuit_json`](#verify_circuit_json) | Verify | no | Check a SUPPLIED Circuit JSON with the independent KiCad path. |
| [`verify_launch_targets`](#verify_launch_targets) | Verify | no | Which other product views a project can be handed to, and whether they resolve here. |
| [`verify_open_view`](#verify_open_view) | Verify | no | Emit a manifest-bound launch request so another view can open the SAME revision. |
| [`verify_manifest`](#verify_manifest) | Verify | no | Build a design and return the artifact manifest: files, digests and tool provenance. |

## Gates — DRC / ERC

### pcb_drc

```python
pcb_drc(board_path: str, severity_all: bool = True, schematic_parity: bool = False, refill_zones: bool = False, units: str = 'mm', all_track_errors: bool = False, save_board: bool = False, severity_error: bool = False, severity_warning: bool = False, severity_exclusions: bool = False, define_var: dict[str, str] | None = None) -> dict[str, Any]
```

`phicad/mcp/server.py:417`

Run design-rule check on a .kicad_pcb with the fork's kicad-cli.

Returns violation counts by severity and by rule type, plus the JSON report path.

`schematic_parity=True` additionally answers "does this board still match its schematic?"
-- the check to run after editing either side. It needs the sibling .kicad_sch, and the
`schematic_parity` summary key is present ONLY when it was requested, so an absent key is
never mistaken for "parity is clean". The raw KiCad findings and raw count remain intact.
A separate effective count ignores only an exact generated schematic no-connect pseudo-net
paired by UUID/reference/pad with an unnetted board pad; every other `net_conflict`, and any
candidate lacking authoritative board/netlist evidence, remains an active failure.

`refill_zones=True` refills zones first, which removes stale-fill false positives
(`save_board=True` then writes the refilled board back -- the only mutating combination).
`units` ('mm', 'in', 'mils') sets the report's coordinate units.

| Parameter | Type | Required | Default |
| --- | --- | --- | --- |
| `board_path` | `str` | yes | — |
| `severity_all` | `bool` | no | `true` |
| `schematic_parity` | `bool` | no | `false` |
| `refill_zones` | `bool` | no | `false` |
| `units` | `str` | no | `"mm"` |
| `all_track_errors` | `bool` | no | `false` |
| `save_board` | `bool` | no | `false` |
| `severity_error` | `bool` | no | `false` |
| `severity_warning` | `bool` | no | `false` |
| `severity_exclusions` | `bool` | no | `false` |
| `define_var` | `dict[str, str] \| None` | no | `null` |

=== "Shell"

    ```bash
    phicad/bin/phicad-tool call pcb_drc --arg board_path=<str>
    ```

### sch_erc

```python
sch_erc(sch_path: str, severity_all: bool = True) -> dict[str, Any]
```

`phicad/mcp/server.py:538`

Run electrical-rule check on a .kicad_sch with the fork's kicad-cli.

Returns violation counts by severity and by rule type, plus the JSON report path.

| Parameter | Type | Required | Default |
| --- | --- | --- | --- |
| `sch_path` | `str` | yes | — |
| `severity_all` | `bool` | no | `true` |

=== "Shell"

    ```bash
    phicad/bin/phicad-tool call sch_erc --arg sch_path=<str>
    ```

## Visual + fabrication exports

### pcb_render

```python
pcb_render(board_path: str, out_png: str | None = None, side: str = 'top', quality: str = 'basic') -> dict[str, Any]
```

`phicad/mcp/server.py:570`

Raytraced PNG render of a board (works headless). Returns the PNG path and size.

| Parameter | Type | Required | Default |
| --- | --- | --- | --- |
| `board_path` | `str` | yes | — |
| `out_png` | `str \| None` | no | `null` |
| `side` | `str` | no | `"top"` |
| `quality` | `str` | no | `"basic"` |

=== "Shell"

    ```bash
    phicad/bin/phicad-tool call pcb_render --arg board_path=<str>
    ```

### pcb_export_svg

```python
pcb_export_svg(board_path: str, layers: str = 'F.Cu,B.Cu,Edge.Cuts', out_dir: str | None = None, mode_multi: bool = True) -> dict[str, Any]
```

`phicad/mcp/server.py:590`

Export board layers to SVG (multi-file by default). `layers` is a comma list.

| Parameter | Type | Required | Default |
| --- | --- | --- | --- |
| `board_path` | `str` | yes | — |
| `layers` | `str` | no | `"F.Cu,B.Cu,Edge.Cuts"` |
| `out_dir` | `str \| None` | no | `null` |
| `mode_multi` | `bool` | no | `true` |

=== "Shell"

    ```bash
    phicad/bin/phicad-tool call pcb_export_svg --arg board_path=<str>
    ```

### pcb_export_gerbers

```python
pcb_export_gerbers(board_path: str, out_dir: str | None = None, with_drill: bool = True) -> dict[str, Any]
```

`phicad/mcp/server.py:612`

Export Gerbers (and, by default, Excellon drill files) for fabrication.

| Parameter | Type | Required | Default |
| --- | --- | --- | --- |
| `board_path` | `str` | yes | — |
| `out_dir` | `str \| None` | no | `null` |
| `with_drill` | `bool` | no | `true` |

=== "Shell"

    ```bash
    phicad/bin/phicad-tool call pcb_export_gerbers --arg board_path=<str>
    ```

### pcb_export_step

```python
pcb_export_step(board_path: str, out_step: str | None = None, subst_models: bool = True) -> dict[str, Any]
```

`phicad/mcp/server.py:637`

Export a 3D STEP model of the board (OpenCascade). Returns the .step path.

Note: kicad-cli exits 2 when some component 3D models are missing (a stock KiCad library
install provides them; the build tree alone does not) but still writes a valid STEP of the
board body. That is reported as ok=True with `missing_models` populated.

| Parameter | Type | Required | Default |
| --- | --- | --- | --- |
| `board_path` | `str` | yes | — |
| `out_step` | `str \| None` | no | `null` |
| `subst_models` | `bool` | no | `true` |

=== "Shell"

    ```bash
    phicad/bin/phicad-tool call pcb_export_step --arg board_path=<str>
    ```

### sch_export_bom

```python
sch_export_bom(sch_path: str, out_csv: str | None = None, fields: str = 'Reference,Value,Footprint,${QUANTITY},DNP') -> dict[str, Any]
```

`phicad/mcp/server.py:670`

Export a grouped BOM CSV from a schematic. Returns the CSV path and row count.

| Parameter | Type | Required | Default |
| --- | --- | --- | --- |
| `sch_path` | `str` | yes | — |
| `out_csv` | `str \| None` | no | `null` |
| `fields` | `str` | no | `"Reference,Value,Footprint,${QUANTITY},DNP"` |

=== "Shell"

    ```bash
    phicad/bin/phicad-tool call sch_export_bom --arg sch_path=<str>
    ```

### pcb_export_stats

```python
pcb_export_stats(board_path: str, subtract_holes: bool = False) -> dict[str, Any]
```

`phicad/mcp/server.py:693`

Board statistics as JSON (`kicad-cli pcb export stats --format json`).

Returns the parsed report under `stats`: board size/area, component and pad counts per
side and type, via counts, drill table, copper layers. Cheaper than board_summary when
only numbers are needed, and it comes from KiCad itself rather than an s-expression parser.

| Parameter | Type | Required | Default |
| --- | --- | --- | --- |
| `board_path` | `str` | yes | — |
| `subtract_holes` | `bool` | no | `false` |

=== "Shell"

    ```bash
    phicad/bin/phicad-tool call pcb_export_stats --arg board_path=<str>
    ```

### pcb_export_pos

```python
pcb_export_pos(board_path: str, out_file: str | None = None, fmt: str = 'csv', units: str = 'mm', side: str = 'both', smd_only: bool = False, exclude_dnp: bool = True) -> dict[str, Any]
```

`phicad/mcp/server.py:723`

Pick-and-place (component position) file for assembly.

`fmt` is ascii | csv | gerber; `side` is front | back | both (gerber supports one side
only). Returns the file path, row count and a preview of the first rows.

| Parameter | Type | Required | Default |
| --- | --- | --- | --- |
| `board_path` | `str` | yes | — |
| `out_file` | `str \| None` | no | `null` |
| `fmt` | `str` | no | `"csv"` |
| `units` | `str` | no | `"mm"` |
| `side` | `str` | no | `"both"` |
| `smd_only` | `bool` | no | `false` |
| `exclude_dnp` | `bool` | no | `true` |

=== "Shell"

    ```bash
    phicad/bin/phicad-tool call pcb_export_pos --arg board_path=<str>
    ```

### pcb_export_pdf

```python
pcb_export_pdf(board_path: str, layers: str = 'F.Cu,B.Cu,Edge.Cuts', out_pdf: str | None = None, include_title_block: bool = True, black_and_white: bool = False) -> dict[str, Any]
```

`phicad/mcp/server.py:768`

Plot board layers into ONE multi-page PDF (one page per layer) for review.

`layers` is a comma list of untranslated layer names. Returns the PDF path and size.

| Parameter | Type | Required | Default |
| --- | --- | --- | --- |
| `board_path` | `str` | yes | — |
| `layers` | `str` | no | `"F.Cu,B.Cu,Edge.Cuts"` |
| `out_pdf` | `str \| None` | no | `null` |
| `include_title_block` | `bool` | no | `true` |
| `black_and_white` | `bool` | no | `false` |

=== "Shell"

    ```bash
    phicad/bin/phicad-tool call pcb_export_pdf --arg board_path=<str>
    ```

### sch_export_netlist

```python
sch_export_netlist(sch_path: str, out_file: str | None = None, fmt: str = 'kicadsexpr') -> dict[str, Any]
```

`phicad/mcp/server.py:798`

Export a schematic netlist (`kicad-cli sch export netlist`).

`fmt` is one of kicadsexpr | kicadxml | cadstar | orcadpcb2 | spice | spicemodel | pads |
allegro. For kicadsexpr the result also carries `component_count` and `net_count` parsed
from the file, which is the input the synth pipeline and `ImportNetlist` consume.

| Parameter | Type | Required | Default |
| --- | --- | --- | --- |
| `sch_path` | `str` | yes | — |
| `out_file` | `str \| None` | no | `null` |
| `fmt` | `str` | no | `"kicadsexpr"` |

=== "Shell"

    ```bash
    phicad/bin/phicad-tool call sch_export_netlist --arg sch_path=<str>
    ```

### sch_export_pdf

```python
sch_export_pdf(sch_path: str, out_pdf: str | None = None, black_and_white: bool = False) -> dict[str, Any]
```

`phicad/mcp/server.py:834`

Export the whole schematic hierarchy to a single PDF for review.

| Parameter | Type | Required | Default |
| --- | --- | --- | --- |
| `sch_path` | `str` | yes | — |
| `out_pdf` | `str \| None` | no | `null` |
| `black_and_white` | `bool` | no | `false` |

=== "Shell"

    ```bash
    phicad/bin/phicad-tool call sch_export_pdf --arg sch_path=<str>
    ```

## Semantic diffs

*exit code 5 means "documents differ", not an error*

### pcb_diff

```python
pcb_diff(a: str, b: str) -> dict[str, Any]
```

`phicad/mcp/server.py:927`

Semantic diff of two .kicad_pcb files. `differs` is True when they are not identical
(kicad-cli exits 5 in that case — that is a normal result, not a failure).

| Parameter | Type | Required | Default |
| --- | --- | --- | --- |
| `a` | `str` | yes | — |
| `b` | `str` | yes | — |

=== "Shell"

    ```bash
    phicad/bin/phicad-tool call pcb_diff --arg a=<str> --arg b=<str>
    ```

### sch_diff

```python
sch_diff(a: str, b: str) -> dict[str, Any]
```

`phicad/mcp/server.py:934`

Semantic diff of two .kicad_sch files. `differs` is True when they are not identical
(kicad-cli exit code 5).

| Parameter | Type | Required | Default |
| --- | --- | --- | --- |
| `a` | `str` | yes | — |
| `b` | `str` | yes | — |

=== "Shell"

    ```bash
    phicad/bin/phicad-tool call sch_diff --arg a=<str> --arg b=<str>
    ```

## Jobsets — the repeatable agent -> fab handoff

### jobset_run

```python
jobset_run(jobset_path: str, project_path: str) -> dict[str, Any]
```

`phicad/mcp/server.py:946`

Run a .kicad_jobset against a .kicad_pro project (so ${KIPRJMOD} resolves).

| Parameter | Type | Required | Default |
| --- | --- | --- | --- |
| `jobset_path` | `str` | yes | — |
| `project_path` | `str` | yes | — |

=== "Shell"

    ```bash
    phicad/bin/phicad-tool call jobset_run --arg jobset_path=<str> --arg project_path=<str>
    ```

## Board summary

*s-expression read via the kipy/kicad-tools venv (subprocess only)*

### board_summary

```python
board_summary(board_path: str) -> dict[str, Any]
```

`phicad/mcp/server.py:1034`

Structural summary of a board (footprints, tracks, vias, zones, nets) parsed with
kicad-tools in the automation venv — no kicad-cli, no GUI, works on fork-format files.

| Parameter | Type | Required | Default |
| --- | --- | --- | --- |
| `board_path` | `str` | yes | — |

=== "Shell"

    ```bash
    phicad/bin/phicad-tool call board_summary --arg board_path=<str>
    ```

## IPC API — ONE managed api-server child

### api_server_start

```python
api_server_start(file_path: str | None = None, socket: str | None = None, wait_s: int = 20) -> dict[str, Any]
```

`phicad/mcp/server.py:1216`

Start the single managed `kicad-cli api-server` child — a HEADLESS PhiCad session.

`file_path` is OPTIONAL: with no file the server comes up empty and documents are opened on
demand with session_open() (that is the preferred flow — it keeps one long-lived session and
never touches the user's screen). Passing a file behaves exactly as before, opening it at
launch (verified non-destructive: the file is byte-identical afterwards).

Refuses to start a second one. `socket` is a PLAIN filesystem path (the server wraps it
into ipc:// itself); kipy clients get `ipc://<socket>` via kipy_exec.

| Parameter | Type | Required | Default |
| --- | --- | --- | --- |
| `file_path` | `str \| None` | no | `null` |
| `socket` | `str \| None` | no | `null` |
| `wait_s` | `int` | no | `20` |

=== "Shell"

    ```bash
    phicad/bin/phicad-tool call api_server_start
    ```

## Headless SESSION surface

*open/inspect/save documents over IPC in the managed session*

### session_open

```python
session_open(path: str, doc_type: str = 'auto', close_others: bool = True, copy_first: bool = False) -> dict[str, Any]
```

`phicad/mcp/server.py:1799`

Open a document in the managed HEADLESS session over IPC (no GUI, no screen control).

`doc_type` is auto-detected from the suffix (.kicad_pcb/.kicad_sch/.kicad_pro); the path is
always resolved to an absolute path (the server requires one).

`close_others=True` (default) sends CloseAllDocuments first: the server holds exactly ONE
project, and without it even re-opening the same path fails with 'already open'. It discards
unsaved in-session edits.

`copy_first=True` copies the document and its siblings (.kicad_pro/.kicad_prl/.kicad_dru) to
a scratch directory and opens THAT, so the user's file can never be rewritten.

Opening is non-destructive (verified byte-identical), but session_save() rewrites the file in
the fork's format (20260831) — see `rewrites_file` and `headless_limitations` in the result.
A .kicad_pro alone does NOT load a board: open the .kicad_pcb for session_board_query().

PRECONDITION: this needs the managed api-server — call api_server_start() (the file argument is optional) first; without it it returns ok=false with that hint.

| Parameter | Type | Required | Default |
| --- | --- | --- | --- |
| `path` | `str` | yes | — |
| `doc_type` | `str` | no | `"auto"` |
| `close_others` | `bool` | no | `true` |
| `copy_first` | `bool` | no | `false` |

=== "Shell"

    ```bash
    phicad/bin/phicad-tool call session_open --arg path=<str>
    ```

### session_documents

```python
session_documents() -> dict[str, Any]
```

`phicad/mcp/server.py:1880`

List what the managed headless session has open.

`documents` is this server's own bookkeeping and is the authoritative answer: the server only
registers the GetOpenDocuments handler while a document is actually open, so a live query on
an empty session fails with 'no handler available' rather than returning an empty list.
`live_documents` is the cross-check from the server when that query is answerable.

PRECONDITION: this needs the managed api-server — call api_server_start() (the file argument is optional) first; without it it returns ok=false with that hint.

=== "Shell"

    ```bash
    phicad/bin/phicad-tool call session_documents
    ```

### session_save

```python
session_save(basename: str | None = None) -> dict[str, Any]
```

`phicad/mcp/server.py:1914`

Save the document open in the managed headless session (SaveDocument over IPC).

DESTRUCTIVE: this rewrites the file in the fork's format (verified 20200628 -> 20260831) and
touches the sibling .kicad_pro. Only call it on a file the user consented to have rewritten
(or one opened with session_open(copy_first=True)).

`basename` must be the BASENAME of the already-open document — there is no save-as; a
different name is rejected with 'the requested document X is not open'. Defaults to the open
document's own name.

PRECONDITION: this needs a live headless session — call api_server_start() once, then session_open('<absolute>.kicad_pcb'), before this tool can do anything; without them it returns ok=false with a hint naming the missing step.

| Parameter | Type | Required | Default |
| --- | --- | --- | --- |
| `basename` | `str \| None` | no | `null` |

=== "Shell"

    ```bash
    phicad/bin/phicad-tool call session_save
    ```

### session_board_query

```python
session_board_query(what: str = 'all', limit: int = 50) -> dict[str, Any]
```

`phicad/mcp/server.py:1958`

Read structured facts from the board LIVE in the managed headless session.

This is the tool that replaces screenshotting the GUI: it returns real geometry and
connectivity instead of pixels. `what` is one of
all | counts (= summary) | footprints | tracks | nets | stackup | vias | title.
`all` = counts + footprints + tracks + nets + stackup + title; via/pad listings stay opt-in
so a dense board cannot blow up the response. `limit` caps every listing (clamped to 2000).

Read-only — it never mutates or saves. Coordinates are millimetres; layers are canonical
names ('F.Cu'), with the board's own name in parentheses when it differs. PCB only
(kipy has no schematic reader): use sch_erc / sch_export_bom for schematics.

PRECONDITION: this needs a live headless session — call api_server_start() once, then session_open('<absolute>.kicad_pcb'), before this tool can do anything; without them it returns ok=false with a hint naming the missing step.

| Parameter | Type | Required | Default |
| --- | --- | --- | --- |
| `what` | `str` | no | `"all"` |
| `limit` | `int` | no | `50` |

=== "Shell"

    ```bash
    phicad/bin/phicad-tool call session_board_query
    ```

### session_close

```python
session_close(all_documents: bool = True) -> dict[str, Any]
```

`phicad/mcp/server.py:1995`

Close the document(s) open in the managed headless session, leaving the server running.

`all_documents=True` (default) sends CloseAllDocuments, which works whatever is open —
including a project or schematic, whose specifier this server cannot read back. With False it
closes just the open board. UNSAVED IN-SESSION EDITS ARE DISCARDED; call session_save() first
if they matter. Use api_server_stop() to shut the server down too.

PRECONDITION: this needs a live headless session — call api_server_start() once, then session_open('<absolute>.kicad_pcb'), before this tool can do anything; without them it returns ok=false with a hint naming the missing step.

| Parameter | Type | Required | Default |
| --- | --- | --- | --- |
| `all_documents` | `bool` | no | `true` |

=== "Shell"

    ```bash
    phicad/bin/phicad-tool call session_close
    ```

### api_server_stop

```python
api_server_stop() -> dict[str, Any]
```

`phicad/mcp/server.py:2019`

Stop the managed api-server child (SIGTERM, then SIGKILL) and clean up its socket.

Also reaps a server recorded in the pidfile beside the socket, so a server started by an
earlier `phicad tool call` process can be stopped instead of orphaned.

=== "Shell"

    ```bash
    phicad/bin/phicad-tool call api_server_stop
    ```

### api_server_reap_orphans

```python
api_server_reap_orphans(dry_run: bool = True, include_reachable: bool = False) -> dict[str, Any]
```

`phicad/mcp/server.py:2091`

Find and kill `kicad-cli api-server` processes that are orphaned AND unreachable.

`api_server_stop` only reaps a server on ITS socket, so servers left on other sockets --
by a crashed host, a killed worktree session, or a build that spawned its own -- stay
stranded holding a board in memory, and `phicad mcp doctor` can only tell you to copy a
raw `kill`. This is that cleanup as a real operation.

Being orphaned (ppid 1) is NOT sufficient on its own: `phicad api-server` is normally
started in the background, its launcher shell exits, and the perfectly healthy server it
left behind is reparented to init. Killing on ppid alone therefore shoots down the live
server the web panel and `kipy` are talking to. So a process is only reaped when its
socket is also unreachable -- gone from disk, or refusing a connection -- which is exactly
the condition the doctor warns about ("holds its ipc:// socket and the next managed
session will collide").

`dry_run=True` (the default) reports what it would kill. `include_reachable=True` also
reaps orphans that still answer, for deliberately clearing every server on the machine.

| Parameter | Type | Required | Default |
| --- | --- | --- | --- |
| `dry_run` | `bool` | no | `true` |
| `include_reachable` | `bool` | no | `false` |

=== "Shell"

    ```bash
    phicad/bin/phicad-tool call api_server_reap_orphans
    ```

### api_server_status

```python
api_server_status() -> dict[str, Any]
```

`phicad/mcp/server.py:2189`

Report the managed api-server child: pid, socket, opened file, liveness.

When this process started no server, a live one recorded in the pidfile beside the socket
is reported as `adopted` -- otherwise a fresh CLI process claims nothing is running while
a server holds the socket.

=== "Shell"

    ```bash
    phicad/bin/phicad-tool call api_server_status
    ```

### kipy_exec

```python
kipy_exec(script_path: str, args: list[str] | None = None, timeout_s: int = 120) -> dict[str, Any]
```

`phicad/mcp/server.py:2222`

Run a kipy script in the automation venv against the managed api-server.

The script is executed by `build/venv/bin/python` with KICAD_API_SOCKET=ipc://<socket>,
so plain `KiCad()` connects. Requires api_server_start() first. Never call
`KiCad().check_version()` — kipy 0.8 is generated against 10.0.6 and raises against the
10.99 fork; ordinary commands work.

Headless traps (see HEADLESS_LIMITS): kipy getters return COPIES, so `item.position.x += n`
silently sends an unchanged item and the server answers ISC_OK — assign the whole object
back (`v = item.position; v.x += n; item.position = v`) and always read back to verify.
`KiCad().get_board()` raises 'no handler available ... GetOpenDocuments' when no document is
open, not just when the API is missing.

PRECONDITION: this needs the managed api-server — call api_server_start() (the file argument is optional) first; without it it returns ok=false with that hint.

| Parameter | Type | Required | Default |
| --- | --- | --- | --- |
| `script_path` | `str` | yes | — |
| `args` | `list[str] \| None` | no | `null` |
| `timeout_s` | `int` | no | `120` |

=== "Shell"

    ```bash
    phicad/bin/phicad-tool call kipy_exec --arg script_path=<str>
    ```

## Parts pipeline & layout swarm

*phicad/parts, phicad/swarm*

### parts_fetch

```python
parts_fetch(project_dir: str, lcsc_ids: list[str] | None = None, parts_json: str | None = None, lib: str = 'project') -> dict[str, Any]
```

`phicad/mcp/server.py:2326`

Fetch exact parts (symbol + footprint + 3D model) into a project library.

Wraps phicad/parts/fetch_part.py: verified LCSC ids -> <project>/lib/ assets with
${KIPRJMOD}-relative 3D bindings + lib-table registration. Either pass lcsc_ids
directly or parts_json (ledger mode: fetches rows with status approved/proposed and
promotes them to fetched). Needs network (LCSC/EasyEDA API).

| Parameter | Type | Required | Default |
| --- | --- | --- | --- |
| `project_dir` | `str` | yes | — |
| `lcsc_ids` | `list[str] \| None` | no | `null` |
| `parts_json` | `str \| None` | no | `null` |
| `lib` | `str` | no | `"project"` |

=== "Shell"

    ```bash
    phicad/bin/phicad-tool call parts_fetch --arg project_dir=<str>
    ```

### parts_audit

```python
parts_audit(board_path: str, parts_json: str | None = None, require_id: bool = False) -> dict[str, Any]
```

`phicad/mcp/server.py:2354`

Fab-readiness gate: every footprint has a resolvable 3D model + LCSC/MPN id.

Wraps phicad/parts/audit_parts.py. ok=false means gaps were found — read
report.failures / report.warnings. require_id escalates missing ids to failures.

| Parameter | Type | Required | Default |
| --- | --- | --- | --- |
| `board_path` | `str` | yes | — |
| `parts_json` | `str \| None` | no | `null` |
| `require_id` | `bool` | no | `false` |

=== "Shell"

    ```bash
    phicad/bin/phicad-tool call parts_audit --arg board_path=<str>
    ```

### swarm_partition

```python
swarm_partition(board_path: str, groups: int = 3, include_power: bool = False) -> dict[str, Any]
```

`phicad/mcp/server.py:2383`

Partition a board's nets into N disjoint groups for parallel routing agents.

Wraps phicad/swarm/partition_nets.py. Returns the plan (groups with nets/pads/bbox,
power nets in zones_bucket) and writes plan.json next to the board for merge use.

| Parameter | Type | Required | Default |
| --- | --- | --- | --- |
| `board_path` | `str` | yes | — |
| `groups` | `int` | no | `3` |
| `include_power` | `bool` | no | `false` |

=== "Shell"

    ```bash
    phicad/bin/phicad-tool call swarm_partition --arg board_path=<str>
    ```

### swarm_score

```python
swarm_score(board_path: str, project_path: str | None = None) -> dict[str, Any]
```

`phicad/mcp/server.py:2413`

Score a board candidate (lower = better) for best-of-N selection.

Wraps phicad/swarm/score_board.py: 10000*unconnected + 1000*drc_err + 10*warn
+ 2*vias + 0.1*track_mm, from the fork's DRC plus a phicad/pylib/sexp.py parse.

PASS project_path FOR SCRATCH-NAMED CANDIDATES. Design rules live in the
.kicad_pro and KiCad pairs one with a board only by filename stem, so a
candidate copied to cand-g0.kicad_pcb is otherwise judged by KiCad's DEFAULT
rules: one real board scored 0 DRC errors inside its project and 13 copied
out -- score 36.2 against 13036.2, on the number you are ranking by. The
reply's `rules_source` says which rules produced the numbers ('explicit',
'sibling', or 'kicad-defaults'), and only scores with the same
`rules_source` and project are comparable with each other.

| Parameter | Type | Required | Default |
| --- | --- | --- | --- |
| `board_path` | `str` | yes | — |
| `project_path` | `str \| None` | no | `null` |

=== "Shell"

    ```bash
    phicad/bin/phicad-tool call swarm_score --arg board_path=<str>
    ```

### swarm_merge

```python
swarm_merge(base_board: str, plan_json: str, take: dict[str, str] | None = None, strip: list[str] | None = None, out_board: str | None = None, run_drc: bool = True) -> dict[str, Any]
```

`phicad/mcp/server.py:2441` · **writes in place / changes state** ('swarm_merge' is a known writer)

Merge per-group copper from swarm candidate boards into the base board.

Wraps phicad/swarm/merge_routes.py (lossless text splice, replace semantics).
take maps group id -> candidate board path (e.g. {"g0": "/tmp/sw/cand-g0.kicad_pcb"});
strip removes groups' copper without replacement. Optionally DRC-gates the result.

| Parameter | Type | Required | Default |
| --- | --- | --- | --- |
| `base_board` | `str` | yes | — |
| `plan_json` | `str` | yes | — |
| `take` | `dict[str, str] \| None` | no | `null` |
| `strip` | `list[str] \| None` | no | `null` |
| `out_board` | `str \| None` | no | `null` |
| `run_drc` | `bool` | no | `true` |

=== "Shell"

    ```bash
    phicad/bin/phicad-tool call swarm_merge --arg base_board=<str> --arg plan_json=<str>
    ```

## Design synthesis (phicad/synth)

### synth_check

```python
synth_check(design_json: str) -> dict[str, Any]
```

`phicad/mcp/server.py:2551`

Validate a design IR without writing anything. Milliseconds, no board needed.

Wraps `phicad/synth/cli.py check`. Reports every problem at once with a fix hint each:
a pad in no net and not declared no-connect, a pad number the footprint does not have,
a pin claimed by two nets, an unresolvable footprint (with the closest real names).
Run this after every edit to a design — it is the cheap half of the DRC loop, and it
catches the class of hole that would otherwise surface as unconnected items twenty-five
seconds later.

| Parameter | Type | Required | Default |
| --- | --- | --- | --- |
| `design_json` | `str` | yes | — |

=== "Shell"

    ```bash
    phicad/bin/phicad-tool call synth_check --arg design_json=<str>
    ```

### synth_build

```python
synth_build(design_json: str, out_dir: str, force: bool = False, route: bool = True, drc: bool = True, grid_mm: float = 0.1) -> dict[str, Any]
```

`phicad/mcp/server.py:2568` · **writes in place / changes state** ('synth_build' is a known writer)

Compile a design IR into a placed, routed, DRC-checked KiCad project.

Wraps `phicad/synth/cli.py build`. Runs the whole pipeline with a gate after every
stage — validate, place, scaffold, import (KiCad's own BOARD_NETLIST_UPDATER over IPC),
position, connectivity, outline, route, silk, drc — and stops at the first failure
rather than handing the next stage a broken input. The report names every stage, its
verdict and its evidence, plus the paths of the board, project, netlist and drc.json.

`route=False` places without routing (fast, leaves the ratsnest); `drc=False` skips the
slow authoritative gate for an inner loop. `grid_mm` is the router's grid pitch: a
finer grid finds corridors a coarse one misses, at four times the cost per halving.

| Parameter | Type | Required | Default |
| --- | --- | --- | --- |
| `design_json` | `str` | yes | — |
| `out_dir` | `str` | yes | — |
| `force` | `bool` | no | `false` |
| `route` | `bool` | no | `true` |
| `drc` | `bool` | no | `true` |
| `grid_mm` | `float` | no | `0.1` |

=== "Shell"

    ```bash
    phicad/bin/phicad-tool call synth_build --arg design_json=<str> --arg out_dir=<str>
    ```

### synth_verify

```python
synth_verify(board_path: str) -> dict[str, Any]
```

`phicad/mcp/server.py:2606`

Run the synth pipeline's gates (connectivity, outline, DRC) against an EXISTING board.

Wraps `phicad/synth/cli.py verify`. Use it to re-check a board after manual or agent
edits with the same verdicts synth_build reports, without rebuilding anything.

| Parameter | Type | Required | Default |
| --- | --- | --- | --- |
| `board_path` | `str` | yes | — |

=== "Shell"

    ```bash
    phicad/bin/phicad-tool call synth_verify --arg board_path=<str>
    ```

## Per-project Git history

### project_git_snapshot

```python
project_git_snapshot(project_path: str, message: str = 'phicad: agent checkpoint') -> dict[str, Any]
```

`phicad/mcp/server.py:2634`

Commit all current files in one board project's Git history scope.

New/generated standalone projects are initialized automatically; projects inside an
existing repository reuse it with a project-relative path scope. AI View autosnapshots
stable manual or agent saves. Use this tool for an explicit design checkpoint before a
risky edit, after a verified milestone, or before handoff. An ambiguous checkout is
refused rather than staged wholesale.

| Parameter | Type | Required | Default |
| --- | --- | --- | --- |
| `project_path` | `str` | yes | — |
| `message` | `str` | no | `"phicad: agent checkpoint"` |

=== "Shell"

    ```bash
    phicad/bin/phicad-tool call project_git_snapshot --arg project_path=<str>
    ```

## Host integration / repo state

### integration_capabilities

```python
integration_capabilities() -> dict[str, Any]
```

`phicad/mcp/server.py:3023`

Discover PhiCad's host/model protocol without guessing model or UI versions.

Agent hosts and future model adapters use this as the handshake: MCP is the command plane,
the schemas describe design/event payloads, and the advertised transport list says
which workflow event transports are implemented today.

=== "Shell"

    ```bash
    phicad/bin/phicad-tool call integration_capabilities
    ```

### phicad_status

```python
phicad_status() -> dict[str, Any]
```

`phicad/mcp/server.py:3038`

Parsed `phicad status`: branch, HEAD, build dir, upstream delta, venv, api-server.

=== "Shell"

    ```bash
    phicad/bin/phicad-tool call phicad_status
    ```

## Autoroute

### pcb_export_dsn

```python
pcb_export_dsn(board_path: str, out_dsn: str | None = None) -> dict[str, Any]
```

`phicad/mcp/tools_autoroute.py:312`

Export a board as a Specctra DSN design for an external autorouter.

`kicad-cli pcb export dsn -o <out.dsn> <board>`. The DSN is what a router consumes:
structure/network/placement sections over the board's real copper layers and nets.
Read-only with respect to the board. Returns the .dsn path plus a section/count probe
(nets, pin references, placements) so a caller can see the design actually carried.

| Parameter | Type | Required | Default |
| --- | --- | --- | --- |
| `board_path` | `str` | yes | — |
| `out_dsn` | `str \| None` | no | `null` |

=== "Shell"

    ```bash
    phicad/bin/phicad-tool call pcb_export_dsn --arg board_path=<str>
    ```

### pcb_import_ses

```python
pcb_import_ses(board_path: str, ses_path: str, out_board: str | None = None) -> dict[str, Any]
```

`phicad/mcp/tools_autoroute.py:354` · **writes in place / changes state** (name starts with 'pcb_import')

Merge a Specctra session (.ses) back into a board: the return leg of the loop.

`kicad-cli pcb import-ses --ses <s.ses> [-o <out>] <board>`. A session is the COMPLETE
routing, not an addition: it replaces every unlocked track on the board. Writes a new
board under the server's out dir unless out_board names the input board explicitly, and
carries the project sidecars across so the result can be DRC'd against its own rules.

| Parameter | Type | Required | Default |
| --- | --- | --- | --- |
| `board_path` | `str` | yes | — |
| `ses_path` | `str` | yes | — |
| `out_board` | `str \| None` | no | `null` |

=== "Shell"

    ```bash
    phicad/bin/phicad-tool call pcb_import_ses --arg board_path=<str> --arg ses_path=<str>
    ```

### autoroute_freerouting

```python
autoroute_freerouting(board_path: str, out_board: str | None = None, passes: int = 100, threads: int | None = None, timeout_s: int = 3600, drc: bool = True, keep_dir: str | None = None, refill_zones: bool = True, cleanup: bool = False) -> dict[str, Any]
```

`phicad/mcp/tools_autoroute.py:402` · **writes in place / changes state** (name starts with 'autoroute_')

Route a placed board with Freerouting, headless: DSN out, router, SES back in.

Wraps phicad/autoroute/freeroute.py --json, which does the whole loop (export dsn ->
freerouting.jar -Djava.awt.headless=true -> import-ses) and carries the project
sidecars to the routed board. Writes <out>/autoroute/<stem>.routed.kicad_pcb unless
out_board says otherwise; pass out_board == board_path to route in place.

THE EXIT CODE IS NOT THE RESULT. Freerouting exits 0 on a partial route, so judge it by
the numbers this returns: `unconnected_before`/`unconnected_after`,
`violations_before`/`violations_after` and `new_violation_types` (drc=True, the fork's
own DRC before and after), and `router` -- Freerouting's own final score with its
unrouted/violations count. keep_dir keeps the .dsn, .ses and the router's full log so
any step can be re-run by hand.

Two post-import steps decide whether the board you get back passes DRC at all:
`refill_zones` (default true) refills the pours, because new copper leaves every zone
filled against the OLD copper -- worth 23 violations to 0 on a measured example board;
`cleanup` (default false, DESTRUCTIVE) then deletes the router's dangling vias/tracks
and widens sub-minimum necks, which is the last violation class standing after a refill.
With both, this returns a board the fork's own DRC calls clean.

Needs a JRE on PATH and freerouting.jar ($FREEROUTING_JAR, else ~/.kicad-mcp/).

| Parameter | Type | Required | Default |
| --- | --- | --- | --- |
| `board_path` | `str` | yes | — |
| `out_board` | `str \| None` | no | `null` |
| `passes` | `int` | no | `100` |
| `threads` | `int \| None` | no | `null` |
| `timeout_s` | `int` | no | `3600` |
| `drc` | `bool` | no | `true` |
| `keep_dir` | `str \| None` | no | `null` |
| `refill_zones` | `bool` | no | `true` |
| `cleanup` | `bool` | no | `false` |

=== "Shell"

    ```bash
    phicad/bin/phicad-tool call autoroute_freerouting --arg board_path=<str>
    ```

### autoroute_cleanup

```python
autoroute_cleanup(board_path: str, apply: bool = False, min_width_mm: float | None = None) -> dict[str, Any]
```

`phicad/mcp/tools_autoroute.py:519` · **writes in place / changes state** (name starts with 'autoroute_')

Remove the debris an autorouter leaves behind: dangling vias/tracks, sub-min necks.

Wraps phicad/autoroute/cleanup.py, which drives entirely off KiCad's own DRC report
(with zone refill -- a via whose job is to drop a pad onto a plane is connected BY the
fill, and against unfilled zones it reads as dangling). If DRC did not flag it, nothing
is touched.

apply=False (default) reports what it would do and writes nothing. apply=True works on
a COPY of the board -- cleanup.py can only edit in place -- and returns its path as
`output_board`; the caller's board is never modified. Iterates until the board stops
changing (removing a via orphans the track that reached it). min_width_mm overrides the
width necks are grown to; by default it is read out of the DRC message.

| Parameter | Type | Required | Default |
| --- | --- | --- | --- |
| `board_path` | `str` | yes | — |
| `apply` | `bool` | no | `false` |
| `min_width_mm` | `float \| None` | no | `null` |

=== "Shell"

    ```bash
    phicad/bin/phicad-tool call autoroute_cleanup --arg board_path=<str>
    ```

### autoroute_stitch

```python
autoroute_stitch(board_path: str, planes: list[str], apply: bool = False, pitch_mm: float = 0.8, via_mm: float = 0.4, drill_mm: float = 0.2, clearance_mm: float = 0.15, fanout_ref: str | None = None) -> dict[str, Any]
```

`phicad/mcp/tools_autoroute.py:604` · **writes in place / changes state** (name starts with 'autoroute_')

Drop plane-net BGA balls onto their planes: the dogbone fanout the router skipped.

Wraps phicad/autoroute/stitch.py. `planes` is a list of NET=LAYER strings, e.g.
["GND=In1.Cu", "3V3=In2.Cu"] -- one --plane per entry. Each ball gets a through via in
a free diagonal gap between four balls plus a short trace to it; a candidate site is
checked against the vias and pads already there and a ball is left alone when every
diagonal is taken (it never forces one in).

By default only pads DRC actually reports unconnected are touched. fanout_ref fans out
EVERY plane-net pad of that footprint instead -- use it BEFORE routing, since fanout
placed afterwards has to thread between tracks that already exist.

apply=False reports and writes nothing. apply=True works on a COPY (stitch.py can only
edit in place) and returns it as `output_board`; the caller's board is untouched.

| Parameter | Type | Required | Default |
| --- | --- | --- | --- |
| `board_path` | `str` | yes | — |
| `planes` | `list[str]` | yes | — |
| `apply` | `bool` | no | `false` |
| `pitch_mm` | `float` | no | `0.8` |
| `via_mm` | `float` | no | `0.4` |
| `drill_mm` | `float` | no | `0.2` |
| `clearance_mm` | `float` | no | `0.15` |
| `fanout_ref` | `str \| None` | no | `null` |

=== "Shell"

    ```bash
    phicad/bin/phicad-tool call autoroute_stitch --arg board_path=<str> --arg planes=<list[str]>
    ```

## Cli

### fp_export_svg

```python
fp_export_svg(lib_dir: str, out_dir: str | None = None, footprint: str | None = None, layers: str | None = None, theme: str | None = None, black_and_white: bool = False, sketch_pads_on_fab: bool = False, sketch_pad_numbers: bool = False, hide_dnp_on_fab: bool = False, sketch_dnp_on_fab: bool = False, crossout_dnp_on_fab: bool = False, define_vars: list[str] | None = None) -> dict[str, Any]
```

`phicad/mcp/tools_cli.py:461`

Render a .pretty footprint library (or one footprint in it) to SVG.

`kicad-cli fp export svg -o <dir> [--fp NAME] [-l LAYERS] [-t THEME] … <lib.pretty>`.
lib_dir must be a .pretty DIRECTORY, not a .kicad_mod file — the CLI enforces that. One
SVG per footprint lands in the output directory (default <out>/fp-svg/<lib>/), so
`report.svg_count` is the number of footprints rendered. Read-only on the library.
`layers` is a comma list of untranslated names (F.Cu,F.SilkS,F.Fab,…); omit it for the
footprint editor's default set. `define_vars` entries are KEY=VALUE text-variable
overrides passed as -D.

| Parameter | Type | Required | Default |
| --- | --- | --- | --- |
| `lib_dir` | `str` | yes | — |
| `out_dir` | `str \| None` | no | `null` |
| `footprint` | `str \| None` | no | `null` |
| `layers` | `str \| None` | no | `null` |
| `theme` | `str \| None` | no | `null` |
| `black_and_white` | `bool` | no | `false` |
| `sketch_pads_on_fab` | `bool` | no | `false` |
| `sketch_pad_numbers` | `bool` | no | `false` |
| `hide_dnp_on_fab` | `bool` | no | `false` |
| `sketch_dnp_on_fab` | `bool` | no | `false` |
| `crossout_dnp_on_fab` | `bool` | no | `false` |
| `define_vars` | `list[str] \| None` | no | `null` |

=== "Shell"

    ```bash
    phicad/bin/phicad-tool call fp_export_svg --arg lib_dir=<str>
    ```

### fp_upgrade

```python
fp_upgrade(lib_dir: str, out_dir: str | None = None, force: bool = False, in_place: bool = False) -> dict[str, Any]
```

`phicad/mcp/tools_cli.py:509`

Rewrite a .pretty footprint library in the current file format.

`kicad-cli fp upgrade -o <dir> [--force] <lib.pretty>`. Without --force only footprints
older than the current format are resaved, so an already-current library legitimately
reports nothing resaved (ok=True). By default a fresh copy of the library is written under
<out>/upgrade/<lib>.pretty and the input is untouched; in_place=True rewrites the given
library and lists every file it wrote under `mutated`. `report.resaved` names the files
whose mtime moved.

| Parameter | Type | Required | Default |
| --- | --- | --- | --- |
| `lib_dir` | `str` | yes | — |
| `out_dir` | `str \| None` | no | `null` |
| `force` | `bool` | no | `false` |
| `in_place` | `bool` | no | `false` |

=== "Shell"

    ```bash
    phicad/bin/phicad-tool call fp_upgrade --arg lib_dir=<str>
    ```

### fp_diff

```python
fp_diff(library_a: str, library_b: str, fmt: str = 'json', out_file: str | None = None) -> dict[str, Any]
```

`phicad/mcp/tools_cli.py:559`

Semantic diff of two .pretty footprint library directories.

`kicad-cli fp diff --format {json,text,png,svg} -o <path> <dirA> <dirB>`. Exit 5 means
"they differ" — a result, not a failure: read `differs`. json/text also print to stdout
and come back parsed (`report`) or raw (`text`); png/svg require a file, so one is created
under <out>/diff/ automatically.

| Parameter | Type | Required | Default |
| --- | --- | --- | --- |
| `library_a` | `str` | yes | — |
| `library_b` | `str` | yes | — |
| `fmt` | `str` | no | `"json"` |
| `out_file` | `str \| None` | no | `null` |

=== "Shell"

    ```bash
    phicad/bin/phicad-tool call fp_diff --arg library_a=<str> --arg library_b=<str>
    ```

### sym_export_svg

```python
sym_export_svg(lib_path: str, out_dir: str | None = None, symbol: str | None = None, theme: str | None = None, black_and_white: bool = False, include_hidden_pins: bool = False, include_hidden_fields: bool = False) -> dict[str, Any]
```

`phicad/mcp/tools_cli.py:576`

Render a .kicad_sym symbol library (or one symbol in it) to SVG.

`kicad-cli sym export svg -o <dir> [-s SYMBOL] [-t THEME] … <lib.kicad_sym>`. One SVG per
symbol unit lands in the output directory (default <out>/sym-svg/<lib>/), so
`report.svg_count` counts what was rendered — a multi-unit symbol yields several files.
Read-only on the library.

| Parameter | Type | Required | Default |
| --- | --- | --- | --- |
| `lib_path` | `str` | yes | — |
| `out_dir` | `str \| None` | no | `null` |
| `symbol` | `str \| None` | no | `null` |
| `theme` | `str \| None` | no | `null` |
| `black_and_white` | `bool` | no | `false` |
| `include_hidden_pins` | `bool` | no | `false` |
| `include_hidden_fields` | `bool` | no | `false` |

=== "Shell"

    ```bash
    phicad/bin/phicad-tool call sym_export_svg --arg lib_path=<str>
    ```

### sym_upgrade

```python
sym_upgrade(lib_path: str, out_path: str | None = None, force: bool = False, in_place: bool = False) -> dict[str, Any]
```

`phicad/mcp/tools_cli.py:613`

Rewrite a .kicad_sym symbol library in the current file format.

`kicad-cli sym upgrade -o <path> [--force] <lib.kicad_sym>`. Without --force an already
current library is left alone (ok=True, nothing changed). Defaults to writing a copy under
<out>/upgrade/; in_place=True rewrites the input and reports it in `mutated`. `report`
carries the byte size before/after and the symbol count in the result.

| Parameter | Type | Required | Default |
| --- | --- | --- | --- |
| `lib_path` | `str` | yes | — |
| `out_path` | `str \| None` | no | `null` |
| `force` | `bool` | no | `false` |
| `in_place` | `bool` | no | `false` |

=== "Shell"

    ```bash
    phicad/bin/phicad-tool call sym_upgrade --arg lib_path=<str>
    ```

### sym_diff

```python
sym_diff(file_a: str, file_b: str, fmt: str = 'json', out_file: str | None = None) -> dict[str, Any]
```

`phicad/mcp/tools_cli.py:665`

Semantic diff of two .kicad_sym symbol libraries.

`kicad-cli sym diff --format {json,text,png,svg} -o <path> <a> <b>`. Exit 5 means "they
differ" — read `differs`, not `ok`. json/text come back inline; png/svg are written under
<out>/diff/ because the CLI requires an output path for them.

| Parameter | Type | Required | Default |
| --- | --- | --- | --- |
| `file_a` | `str` | yes | — |
| `file_b` | `str` | yes | — |
| `fmt` | `str` | no | `"json"` |
| `out_file` | `str \| None` | no | `null` |

=== "Shell"

    ```bash
    phicad/bin/phicad-tool call sym_diff --arg file_a=<str> --arg file_b=<str>
    ```

### gerber_info

```python
gerber_info(file_path: str, units: str = 'mm', area: bool = True, strict: bool = False, fmt: str = 'json') -> dict[str, Any]
```

`phicad/mcp/tools_cli.py:756`

Inspect one Gerber or Excellon file: type, item/aperture counts, bbox, copper area.

`kicad-cli gerber info --format {json,text} --units {mm,inch,mils} [--area] [--strict]`.
Writes nothing — the report goes to stdout, so the parsed JSON comes back under `report`
and the human rendering under `text`. `--area` computes real copper area (worth the extra
work; on by default here). `--strict` turns parse warnings into a non-zero exit, which is
how you assert an export is clean rather than merely readable.

| Parameter | Type | Required | Default |
| --- | --- | --- | --- |
| `file_path` | `str` | yes | — |
| `units` | `str` | no | `"mm"` |
| `area` | `bool` | no | `true` |
| `strict` | `bool` | no | `false` |
| `fmt` | `str` | no | `"json"` |

=== "Shell"

    ```bash
    phicad/bin/phicad-tool call gerber_info --arg file_path=<str>
    ```

### gerber_diff

```python
gerber_diff(file_a: str, file_b: str, fmt: str = 'json', out_file: str | None = None, dpi: int = 300, tolerance_nm: int = 0, no_align: bool = False, transparent: bool = False, no_antialias: bool = False, strict: bool = False, exit_code_only: bool = False) -> dict[str, Any]
```

`phicad/mcp/tools_cli.py:803`

Geometric diff of two Gerber/Excellon files: added/removed copper area.

`kicad-cli gerber diff --format {png,text,json} [-o PATH] [--dpi N] [--tolerance NM]
[--no-align] [--transparent] [--no-antialias] [--strict] [--exit-code-only] <a> <b>`.
This is an AREA comparison, not a text diff: the two files are rasterised to polygon sets
and booleaned. Exit 5 means "they differ" (`differs`); exit 0 means the difference is below
1 µm². By default the two files' bounding boxes are origin-aligned first — pass
no_align=True when you are testing absolute placement, which alignment would hide.
png output is written under <out>/gerber-diff/; json/text also print to stdout.

| Parameter | Type | Required | Default |
| --- | --- | --- | --- |
| `file_a` | `str` | yes | — |
| `file_b` | `str` | yes | — |
| `fmt` | `str` | no | `"json"` |
| `out_file` | `str \| None` | no | `null` |
| `dpi` | `int` | no | `300` |
| `tolerance_nm` | `int` | no | `0` |
| `no_align` | `bool` | no | `false` |
| `transparent` | `bool` | no | `false` |
| `no_antialias` | `bool` | no | `false` |
| `strict` | `bool` | no | `false` |
| `exit_code_only` | `bool` | no | `false` |

=== "Shell"

    ```bash
    phicad/bin/phicad-tool call gerber_diff --arg file_a=<str> --arg file_b=<str>
    ```

### gerber_convert_png

```python
gerber_convert_png(file_path: str, out_png: str | None = None, dpi: int = 300, width_px: int = 0, height_px: int = 0, units: str = 'mm', origin_x: float = 0.0, origin_y: float = 0.0, window_width: float = 0.0, window_height: float = 0.0, foreground: str | None = None, background: str | None = None, transparent: bool = False, no_antialias: bool = False, strict: bool = False) -> dict[str, Any]
```

`phicad/mcp/tools_cli.py:873`

Rasterise one Gerber/Excellon file to PNG — a visual check on fab output.

`kicad-cli gerber convert png -o <png> [--dpi N | --width PX --height PX]
[--units mm|inch|mils --origin-x/--origin-y --window-width/--window-height]
[--foreground #RRGGBB --background #RRGGBB] [--transparent] [--no-antialias] [--strict]`.
width_px/height_px override DPI when non-zero. The viewport flags crop to a region:
window_width AND window_height must be given together (the CLI rejects one alone). Writes
<out>/gerber-png/<name>.png by default; `report.bytes` is the PNG size.

| Parameter | Type | Required | Default |
| --- | --- | --- | --- |
| `file_path` | `str` | yes | — |
| `out_png` | `str \| None` | no | `null` |
| `dpi` | `int` | no | `300` |
| `width_px` | `int` | no | `0` |
| `height_px` | `int` | no | `0` |
| `units` | `str` | no | `"mm"` |
| `origin_x` | `float` | no | `0.0` |
| `origin_y` | `float` | no | `0.0` |
| `window_width` | `float` | no | `0.0` |
| `window_height` | `float` | no | `0.0` |
| `foreground` | `str \| None` | no | `null` |
| `background` | `str \| None` | no | `null` |
| `transparent` | `bool` | no | `false` |
| `no_antialias` | `bool` | no | `false` |
| `strict` | `bool` | no | `false` |

=== "Shell"

    ```bash
    phicad/bin/phicad-tool call gerber_convert_png --arg file_path=<str>
    ```

### pcb_export_drill

```python
pcb_export_drill(board_path: str, out_dir: str | None = None, fmt: str = 'excellon', units: str = 'mm', drill_origin: str = 'absolute', zeros_format: str = 'decimal', oval_format: str = 'alternate', map_format: str = 'pdf', separate_th: bool = False, generate_map: bool = False, generate_report: bool = False, generate_tenting: bool = False, mirror_y: bool = False, minimal_header: bool = False, gerber_precision: int = 6) -> dict[str, Any]
```

`phicad/mcp/tools_cli.py:933`

Drill files for fabrication (Excellon by default, Gerber X2 optionally).

`kicad-cli pcb export drill -o <dir>/ --format {excellon,gerber} -u {mm,in}
--drill-origin {absolute,plot} --excellon-zeros-format {decimal,suppressleading,
suppresstrailing,keep} --excellon-oval-format {route,alternate} --map-format
{pdf,gerberx2,ps,dxf,svg} [--excellon-separate-th] [--generate-map] [--generate-report]
[--generate-tenting] [--excellon-mirror-y] [--excellon-min-header]
[--gerber-precision 5|6]`.

The output is a DIRECTORY (default <out>/drill/<board>/): one .drl for combined
PTH+NPTH, or -PTH/-NPTH pairs with separate_th=True, plus the map/report when asked.
`report.holes` and `report.tools` are counted out of the written files, per file and in
total — that is the number to compare against a board's via/pad-hole count.

| Parameter | Type | Required | Default |
| --- | --- | --- | --- |
| `board_path` | `str` | yes | — |
| `out_dir` | `str \| None` | no | `null` |
| `fmt` | `str` | no | `"excellon"` |
| `units` | `str` | no | `"mm"` |
| `drill_origin` | `str` | no | `"absolute"` |
| `zeros_format` | `str` | no | `"decimal"` |
| `oval_format` | `str` | no | `"alternate"` |
| `map_format` | `str` | no | `"pdf"` |
| `separate_th` | `bool` | no | `false` |
| `generate_map` | `bool` | no | `false` |
| `generate_report` | `bool` | no | `false` |
| `generate_tenting` | `bool` | no | `false` |
| `mirror_y` | `bool` | no | `false` |
| `minimal_header` | `bool` | no | `false` |
| `gerber_precision` | `int` | no | `6` |

=== "Shell"

    ```bash
    phicad/bin/phicad-tool call pcb_export_drill --arg board_path=<str>
    ```

### pcb_export_ibom

```python
pcb_export_ibom(board_path: str, out_path: str | None = None, ibom_dir: str | None = None, exclude_dnp: bool = True, dark_mode: bool = False, highlight_pin1: bool = False, keep_json: bool = True, extra_args: list[str] | None = None) -> dict[str, Any]
```

`phicad/mcp/tools_cli.py:1063`

Interactive assembly BOM (InteractiveHtmlBom) for a board — one self-contained HTML file.

No `import pcbnew` anywhere: three kicad-cli exports are translated into ibom's documented
*generic JSON* input by phicad/pylib/ibom_json.py, and `generate_interactive_bom.py` runs on
that JSON as a subprocess.

    pcb export pos --format csv --side both --units mm   placements -> components
    pcb export ipcd356                                   pads, nets (no schematic needed)
    pcb export svg -l Edge.Cuts --exclude-drawing-sheet   board outline

ibom is NOT vendored. It is looked for in `ibom_dir`, then $PHICAD_IBOM, then
<repo>/build/ and the build tree's parent under InteractiveHtmlBom/ibom/
interactive-html-bom; when none exists the tool fails with the clone command in `hint`.

The HTML lands at `out_path` (default <out>/ibom/<board>.html) and the generic JSON next to
it as <board>.ibom.json when keep_json is true — that file is the thing to inspect when the
render looks wrong. `report` carries the component count, per-side counts, pad and net
counts, and `warnings`: what this path CANNOT know (silkscreen and fabrication drawings,
tracks and zones, pad shapes finer than rect/circle) is listed there, not hidden.

| Parameter | Type | Required | Default |
| --- | --- | --- | --- |
| `board_path` | `str` | yes | — |
| `out_path` | `str \| None` | no | `null` |
| `ibom_dir` | `str \| None` | no | `null` |
| `exclude_dnp` | `bool` | no | `true` |
| `dark_mode` | `bool` | no | `false` |
| `highlight_pin1` | `bool` | no | `false` |
| `keep_json` | `bool` | no | `true` |
| `extra_args` | `list[str] \| None` | no | `null` |

=== "Shell"

    ```bash
    phicad/bin/phicad-tool call pcb_export_ibom --arg board_path=<str>
    ```

### pcb_export_dxf

```python
pcb_export_dxf(board_path: str, out_path: str | None = None, layers: str = 'F.Cu,B.Cu,Edge.Cuts', common_layers: str | None = None, mode: str = 'multi', output_units: str = 'mm', use_contours: bool = False, include_border_title: bool = False, exclude_refdes: bool = False, exclude_value: bool = False, subtract_soldermask: bool = False, use_drill_origin: bool = False, drill_shape: int = 2, scale: float = 1.0, check_zones: bool = False, sketch_pads_on_fab: bool = False, sketch_pad_numbers: bool = False, hide_dnp_on_fab: bool = False, sketch_dnp_on_fab: bool = False, crossout_dnp_on_fab: bool = False, drawing_sheet: str | None = None, variant: str | None = None, define_vars: list[str] | None = None) -> dict[str, Any]
```

`phicad/mcp/tools_cli.py:1199`

DXF plot of a layer list — the format mechanical CAD actually reads.

`kicad-cli pcb export dxf -o <path> -l LAYERS [--cl COMMON] --mode-multi|--mode-single
--ou {mm,in} [--uc] [--ibt] [--erd] [--ev] [--subtract-soldermask] [--udo]
[--drill-shape-opt 0|1|2] [--scale S] [--check-zones] [--sp/--spn/--hdnp/--sdnp/--cdnp]
[--drawing-sheet F] [--variant V] [-D K=V]`.

mode="multi" (the default here, and KiCad's future default) writes one DXF per layer into
a directory; mode="single" writes ONE file containing every layer in `layers`, and then
`common_layers` is ignored. Passing neither prints a deprecation warning, so this tool
always passes one. Output defaults to <out>/dxf/<board>/. `--uc` (use_contours) plots
graphics as outlines, which is what a mechanical importer usually wants.

| Parameter | Type | Required | Default |
| --- | --- | --- | --- |
| `board_path` | `str` | yes | — |
| `out_path` | `str \| None` | no | `null` |
| `layers` | `str` | no | `"F.Cu,B.Cu,Edge.Cuts"` |
| `common_layers` | `str \| None` | no | `null` |
| `mode` | `str` | no | `"multi"` |
| `output_units` | `str` | no | `"mm"` |
| `use_contours` | `bool` | no | `false` |
| `include_border_title` | `bool` | no | `false` |
| `exclude_refdes` | `bool` | no | `false` |
| `exclude_value` | `bool` | no | `false` |
| `subtract_soldermask` | `bool` | no | `false` |
| `use_drill_origin` | `bool` | no | `false` |
| `drill_shape` | `int` | no | `2` |
| `scale` | `float` | no | `1.0` |
| `check_zones` | `bool` | no | `false` |
| `sketch_pads_on_fab` | `bool` | no | `false` |
| `sketch_pad_numbers` | `bool` | no | `false` |
| `hide_dnp_on_fab` | `bool` | no | `false` |
| `sketch_dnp_on_fab` | `bool` | no | `false` |
| `crossout_dnp_on_fab` | `bool` | no | `false` |
| `drawing_sheet` | `str \| None` | no | `null` |
| `variant` | `str \| None` | no | `null` |
| `define_vars` | `list[str] \| None` | no | `null` |

=== "Shell"

    ```bash
    phicad/bin/phicad-tool call pcb_export_dxf --arg board_path=<str>
    ```

### pcb_export_gencad

```python
pcb_export_gencad(board_path: str, out_path: str | None = None, flip_bottom_pads: bool = False, unique_pins: bool = False, unique_footprints: bool = False, use_drill_origin: bool = False, store_origin_coord: bool = False, define_vars: list[str] | None = None) -> dict[str, Any]
```

`phicad/mcp/tools_cli.py:1283`

GenCAD 1.4 export — the format many older test/assembly houses ingest.

`kicad-cli pcb export gencad -o <file.cad> [-f|--flip-bottom-pads] [--unique-pins]
[--unique-footprints] [--use-drill-origin] [--store-origin-coord] [-D K=V]`. One text
file (default <out>/gencad/<board>.cad); `report` counts its $-delimited sections plus
the number of PADS/SHAPES/COMPONENTS/ROUTES entries, so you can see the board arrived.

| Parameter | Type | Required | Default |
| --- | --- | --- | --- |
| `board_path` | `str` | yes | — |
| `out_path` | `str \| None` | no | `null` |
| `flip_bottom_pads` | `bool` | no | `false` |
| `unique_pins` | `bool` | no | `false` |
| `unique_footprints` | `bool` | no | `false` |
| `use_drill_origin` | `bool` | no | `false` |
| `store_origin_coord` | `bool` | no | `false` |
| `define_vars` | `list[str] \| None` | no | `null` |

=== "Shell"

    ```bash
    phicad/bin/phicad-tool call pcb_export_gencad --arg board_path=<str>
    ```

### pcb_export_ipcd356

```python
pcb_export_ipcd356(board_path: str, out_path: str | None = None) -> dict[str, Any]
```

`phicad/mcp/tools_cli.py:1329`

IPC-D-356 netlist — the electrical-test (bed-of-nails / flying probe) handoff.

`kicad-cli pcb export ipcd356 -o <file.d356> <board>`. No options exist; the format is
fixed. `report.test_points` counts the 317/327 record lines and `report.nets` the distinct
net names in them, which is the number a test house cares about.

| Parameter | Type | Required | Default |
| --- | --- | --- | --- |
| `board_path` | `str` | yes | — |
| `out_path` | `str \| None` | no | `null` |

=== "Shell"

    ```bash
    phicad/bin/phicad-tool call pcb_export_ipcd356 --arg board_path=<str>
    ```

### pcb_export_ipc2581

```python
pcb_export_ipc2581(board_path: str, out_path: str | None = None, precision: int = 6, units: str = 'mm', version: str = 'C', compress: bool = False, mode: str | None = None, sections: str | None = None, net_names: str = 'include', ref_des: str = 'include', bom_rev: str | None = None, bom_col_int_id: str | None = None, bom_col_mfg: str | None = None, bom_col_mfg_pn: str | None = None, bom_col_dist: str | None = None, bom_col_dist_pn: str | None = None, drawing_sheet: str | None = None, variant: str | None = None, define_vars: list[str] | None = None) -> dict[str, Any]
```

`phicad/mcp/tools_cli.py:1357`

IPC-2581 export — the single-file modern fab/assembly handoff (board + stackup + BOM).

`kicad-cli pcb export ipc2581 -o <file.xml|.zip> [--precision N] [--units mm|in]
[--version B|C] [--compress] [--mode DATA_SET] [--sections KEYS]
[--net-names include|anonymize] [--ref-des include|omit] [--bom-rev R]
[--bom-col-int-id F --bom-col-mfg F --bom-col-mfg-pn F --bom-col-dist F
--bom-col-dist-pn F] [--drawing-sheet F] [--variant V] [-D K=V]`.

`mode` picks the IPC-2581 function mode data set (userdef, bom, stackup, fabrication,
assembly, test, stencil); leaving it unset keeps KiCad's pre-function-mode content.
net_names="anonymize" ships connectivity without leaking net names to a vendor.
compress=True writes a .zip instead of .xml. `report.bytes` plus the top-level XML element
counts show what landed.

| Parameter | Type | Required | Default |
| --- | --- | --- | --- |
| `board_path` | `str` | yes | — |
| `out_path` | `str \| None` | no | `null` |
| `precision` | `int` | no | `6` |
| `units` | `str` | no | `"mm"` |
| `version` | `str` | no | `"C"` |
| `compress` | `bool` | no | `false` |
| `mode` | `str \| None` | no | `null` |
| `sections` | `str \| None` | no | `null` |
| `net_names` | `str` | no | `"include"` |
| `ref_des` | `str` | no | `"include"` |
| `bom_rev` | `str \| None` | no | `null` |
| `bom_col_int_id` | `str \| None` | no | `null` |
| `bom_col_mfg` | `str \| None` | no | `null` |
| `bom_col_mfg_pn` | `str \| None` | no | `null` |
| `bom_col_dist` | `str \| None` | no | `null` |
| `bom_col_dist_pn` | `str \| None` | no | `null` |
| `drawing_sheet` | `str \| None` | no | `null` |
| `variant` | `str \| None` | no | `null` |
| `define_vars` | `list[str] \| None` | no | `null` |

=== "Shell"

    ```bash
    phicad/bin/phicad-tool call pcb_export_ipc2581 --arg board_path=<str>
    ```

### pcb_export_odb

```python
pcb_export_odb(board_path: str, out_path: str | None = None, precision: int = 2, units: str = 'mm', compression: str = 'zip', check_zones: bool = False, drawing_sheet: str | None = None, variant: str | None = None, define_vars: list[str] | None = None) -> dict[str, Any]
```

`phicad/mcp/tools_cli.py:1437`

ODB++ export — the fab format most Asian board houses prefer.

`kicad-cli pcb export odb -o <path> [--precision N] [--units mm|in]
[--compression none|zip|tgz] [--check-zones] [--drawing-sheet F] [--variant V] [-D K=V]`.
compression="none" writes a DIRECTORY tree (default <out>/odb/<board>-odb/), "zip"/"tgz"
a single archive. `report.bytes` measures either (a directory recursively) and
`report.entries` counts the tree's files, which is the cheap "did the whole set come out"
check. check_zones=True refills zones first — slower, but it stops a stale-fill export.

| Parameter | Type | Required | Default |
| --- | --- | --- | --- |
| `board_path` | `str` | yes | — |
| `out_path` | `str \| None` | no | `null` |
| `precision` | `int` | no | `2` |
| `units` | `str` | no | `"mm"` |
| `compression` | `str` | no | `"zip"` |
| `check_zones` | `bool` | no | `false` |
| `drawing_sheet` | `str \| None` | no | `null` |
| `variant` | `str \| None` | no | `null` |
| `define_vars` | `list[str] \| None` | no | `null` |

=== "Shell"

    ```bash
    phicad/bin/phicad-tool call pcb_export_odb --arg board_path=<str>
    ```

### pcb_export_png

```python
pcb_export_png(board_path: str, out_path: str | None = None, layers: str = 'F.Cu,B.Cu,Edge.Cuts', common_layers: str | None = None, dpi: int = 300, mirror: bool = False, negative: bool = False, black_and_white: bool = False, theme: str | None = None, include_border_title: bool = False, subtract_soldermask: bool = False, drill_shape: int = 2, scale: float = 1.0, no_antialias: bool = False, check_zones: bool = False, sketch_pads_on_fab: bool = False, sketch_pad_numbers: bool = False, hide_dnp_on_fab: bool = False, sketch_dnp_on_fab: bool = False, crossout_dnp_on_fab: bool = False, drawing_sheet: str | None = None, variant: str | None = None, define_vars: list[str] | None = None) -> dict[str, Any]
```

`phicad/mcp/tools_cli.py:1497`

Flat 2D PNG plot of a layer list (NOT the 3D raytraced view — that is pcb_render).

`kicad-cli pcb export png -o <dir> -l LAYERS [--cl COMMON] [--dpi N] [-m] [-n]
[--black-and-white] [-t THEME] [--ibt] [--subtract-soldermask] [--drill-shape-opt 0|1|2]
[--scale S] [--no-antialias] [--check-zones] [--sp/--spn/--hdnp/--sdnp/--cdnp]
[--drawing-sheet F] [--variant V] [-D K=V]`.

Output is a DIRECTORY (default <out>/png/<board>/): one PNG per layer in `layers`, with
every layer in `common_layers` drawn into each. This is the artwork-review render — use
mirror=True to look at bottom layers the right way round. `report.png_count` and
`report.bytes_total` say what came out.

| Parameter | Type | Required | Default |
| --- | --- | --- | --- |
| `board_path` | `str` | yes | — |
| `out_path` | `str \| None` | no | `null` |
| `layers` | `str` | no | `"F.Cu,B.Cu,Edge.Cuts"` |
| `common_layers` | `str \| None` | no | `null` |
| `dpi` | `int` | no | `300` |
| `mirror` | `bool` | no | `false` |
| `negative` | `bool` | no | `false` |
| `black_and_white` | `bool` | no | `false` |
| `theme` | `str \| None` | no | `null` |
| `include_border_title` | `bool` | no | `false` |
| `subtract_soldermask` | `bool` | no | `false` |
| `drill_shape` | `int` | no | `2` |
| `scale` | `float` | no | `1.0` |
| `no_antialias` | `bool` | no | `false` |
| `check_zones` | `bool` | no | `false` |
| `sketch_pads_on_fab` | `bool` | no | `false` |
| `sketch_pad_numbers` | `bool` | no | `false` |
| `hide_dnp_on_fab` | `bool` | no | `false` |
| `sketch_dnp_on_fab` | `bool` | no | `false` |
| `crossout_dnp_on_fab` | `bool` | no | `false` |
| `drawing_sheet` | `str \| None` | no | `null` |
| `variant` | `str \| None` | no | `null` |
| `define_vars` | `list[str] \| None` | no | `null` |

=== "Shell"

    ```bash
    phicad/bin/phicad-tool call pcb_export_png --arg board_path=<str>
    ```

### pcb_export_ps

```python
pcb_export_ps(board_path: str, out_path: str | None = None, layers: str = 'F.Cu,B.Cu,Edge.Cuts', common_layers: str | None = None, mode: str = 'multi', mirror: bool = False, negative: bool = False, black_and_white: bool = False, theme: str | None = None, include_border_title: bool = False, exclude_refdes: bool = False, exclude_value: bool = False, subtract_soldermask: bool = False, drill_shape: int = 2, scale: float = 1.0, force_a4: bool = False, track_width_correction: float = 0.0, x_scale_factor: float = 1.0, y_scale_factor: float = 1.0, check_zones: bool = False, sketch_pads_on_fab: bool = False, sketch_pad_numbers: bool = False, hide_dnp_on_fab: bool = False, sketch_dnp_on_fab: bool = False, crossout_dnp_on_fab: bool = False, drawing_sheet: str | None = None, variant: str | None = None, define_vars: list[str] | None = None) -> dict[str, Any]
```

`phicad/mcp/tools_cli.py:1564`

PostScript plot of a layer list — the classic 1:1 etch/photoplot output.

`kicad-cli pcb export ps -o <path> -l LAYERS [--cl COMMON] --mode-multi|--mode-single
[-m] [-n] [--black-and-white] [-t THEME] [--ibt] [--erd] [--ev]
[--subtract-soldermask] [--drill-shape-opt 0|1|2] [--scale S] [-A|--force-a4]
[-C TRACK_COR] [-X X_SCALE] [-Y Y_SCALE] [--check-zones] [--sp/--spn/--hdnp/--sdnp/--cdnp]
[--drawing-sheet F] [--variant V] [-D K=V]`.

mode="multi" (default) writes one .ps per layer into a directory; "single" writes one file
with every layer in `layers`. -C/-X/-Y exist for etching accuracy: track width correction
in mm and per-axis scale trim compensate for a specific printer. negative=True plots
inverted for direct photoresist exposure.

| Parameter | Type | Required | Default |
| --- | --- | --- | --- |
| `board_path` | `str` | yes | — |
| `out_path` | `str \| None` | no | `null` |
| `layers` | `str` | no | `"F.Cu,B.Cu,Edge.Cuts"` |
| `common_layers` | `str \| None` | no | `null` |
| `mode` | `str` | no | `"multi"` |
| `mirror` | `bool` | no | `false` |
| `negative` | `bool` | no | `false` |
| `black_and_white` | `bool` | no | `false` |
| `theme` | `str \| None` | no | `null` |
| `include_border_title` | `bool` | no | `false` |
| `exclude_refdes` | `bool` | no | `false` |
| `exclude_value` | `bool` | no | `false` |
| `subtract_soldermask` | `bool` | no | `false` |
| `drill_shape` | `int` | no | `2` |
| `scale` | `float` | no | `1.0` |
| `force_a4` | `bool` | no | `false` |
| `track_width_correction` | `float` | no | `0.0` |
| `x_scale_factor` | `float` | no | `1.0` |
| `y_scale_factor` | `float` | no | `1.0` |
| `check_zones` | `bool` | no | `false` |
| `sketch_pads_on_fab` | `bool` | no | `false` |
| `sketch_pad_numbers` | `bool` | no | `false` |
| `hide_dnp_on_fab` | `bool` | no | `false` |
| `sketch_dnp_on_fab` | `bool` | no | `false` |
| `crossout_dnp_on_fab` | `bool` | no | `false` |
| `drawing_sheet` | `str \| None` | no | `null` |
| `variant` | `str \| None` | no | `null` |
| `define_vars` | `list[str] \| None` | no | `null` |

=== "Shell"

    ```bash
    phicad/bin/phicad-tool call pcb_export_ps --arg board_path=<str>
    ```

### pcb_export_stackup

```python
pcb_export_stackup(board_path: str, out_path: str | None = None, fmt: str = 'json', units: str = 'mm', exclude_color: bool = False, exclude_material: bool = False, exclude_thickness: bool = False, exclude_epsilon_r: bool = False, exclude_loss_tangent: bool = False, exclude_finish: bool = False, exclude_board_options: bool = False) -> dict[str, Any]
```

`phicad/mcp/tools_cli.py:1652`

Board stackup report — layer order, material, thickness, Er, loss tangent, finish.

`kicad-cli pcb export stackup -o <file> --format {csv,json} --units {mm,in}
[--exclude-color] [--exclude-material] [--exclude-thickness] [--exclude-epsilon-r]
[--exclude-loss-tangent] [--exclude-finish] [--exclude-board-options]`. Written to
<out>/stackup/<board>.stackup.{json,csv}. `report.layer_count` (and, for json,
`report.layers` / `report.copper_layers`) is the number to check against the impedance
assumptions a design was routed under. The --exclude-* flags only affect CSV columns.

| Parameter | Type | Required | Default |
| --- | --- | --- | --- |
| `board_path` | `str` | yes | — |
| `out_path` | `str \| None` | no | `null` |
| `fmt` | `str` | no | `"json"` |
| `units` | `str` | no | `"mm"` |
| `exclude_color` | `bool` | no | `false` |
| `exclude_material` | `bool` | no | `false` |
| `exclude_thickness` | `bool` | no | `false` |
| `exclude_epsilon_r` | `bool` | no | `false` |
| `exclude_loss_tangent` | `bool` | no | `false` |
| `exclude_finish` | `bool` | no | `false` |
| `exclude_board_options` | `bool` | no | `false` |

=== "Shell"

    ```bash
    phicad/bin/phicad-tool call pcb_export_stackup --arg board_path=<str>
    ```

### pcb_export_3d

```python
pcb_export_3d(board_path: str, fmt: str = 'glb', out_path: str | None = None, board_only: bool = False, no_components: bool = False, no_board_body: bool = False, no_unspecified: bool = False, no_dnp: bool = False, include_tracks: bool = False, include_pads: bool = False, include_zones: bool = False, include_inner_copper: bool = False, include_silkscreen: bool = False, include_soldermask: bool = False, subst_models: bool = False, fuse_shapes: bool = False, fill_all_vias: bool = False, cut_vias_in_body: bool = False, no_extra_pad_thickness: bool = False, net_filter: str | None = None, component_filter: str | None = None, grid_origin: bool = False, drill_origin: bool = False, user_origin: str | None = None, min_distance: str = '0.01mm', no_optimize_step: bool = False, vrml_units: str = 'in', vrml_models_dir: str | None = None, vrml_models_relative: bool = False, variant: str | None = None, define_vars: list[str] | None = None) -> dict[str, Any]
```

`phicad/mcp/tools_cli.py:1702`

Every 3D export except plain STEP (that is server.py's pcb_export_step).

One tool over the whole `pcb export <fmt>` 3D family: fmt is glb, stl, ply, brep, xao,
stpz, u3d, 3dpdf, vrml — or "step" if you want this tool's fuller flag set.
`kicad-cli pcb export <fmt> -o <file> --force [--board-only] [--no-components]
[--no-board-body] [--no-unspecified] [--no-dnp] [--include-tracks/-pads/-zones/
-inner-copper/-silkscreen/-soldermask] [--subst-models] [--fuse-shapes] [--fill-all-vias]
[--cut-vias-in-body] [--no-extra-pad-thickness] [--net-filter W] [--component-filter W]
[--grid-origin|--drill-origin|--user-origin 25.4x25.4mm] [--min-distance 0.01mm]
[--no-optimize-step] [--variant V] [-D K=V]`.

--force is always passed, so an existing file is overwritten rather than failing the run.
The geometry flags are OCC-only: fmt="vrml" ignores them and takes --units
{mm,m,in,tenths} / --models-dir / --models-relative instead (mapped from vrml_*).
STEP-family (step/stpz) additionally honours no_optimize_step. These exports are the
slowest in the CLI — mesh formats on a dense board can take minutes; timeout is 20 min.

| Parameter | Type | Required | Default |
| --- | --- | --- | --- |
| `board_path` | `str` | yes | — |
| `fmt` | `str` | no | `"glb"` |
| `out_path` | `str \| None` | no | `null` |
| `board_only` | `bool` | no | `false` |
| `no_components` | `bool` | no | `false` |
| `no_board_body` | `bool` | no | `false` |
| `no_unspecified` | `bool` | no | `false` |
| `no_dnp` | `bool` | no | `false` |
| `include_tracks` | `bool` | no | `false` |
| `include_pads` | `bool` | no | `false` |
| `include_zones` | `bool` | no | `false` |
| `include_inner_copper` | `bool` | no | `false` |
| `include_silkscreen` | `bool` | no | `false` |
| `include_soldermask` | `bool` | no | `false` |
| `subst_models` | `bool` | no | `false` |
| `fuse_shapes` | `bool` | no | `false` |
| `fill_all_vias` | `bool` | no | `false` |
| `cut_vias_in_body` | `bool` | no | `false` |
| `no_extra_pad_thickness` | `bool` | no | `false` |
| `net_filter` | `str \| None` | no | `null` |
| `component_filter` | `str \| None` | no | `null` |
| `grid_origin` | `bool` | no | `false` |
| `drill_origin` | `bool` | no | `false` |
| `user_origin` | `str \| None` | no | `null` |
| `min_distance` | `str` | no | `"0.01mm"` |
| `no_optimize_step` | `bool` | no | `false` |
| `vrml_units` | `str` | no | `"in"` |
| `vrml_models_dir` | `str \| None` | no | `null` |
| `vrml_models_relative` | `bool` | no | `false` |
| `variant` | `str \| None` | no | `null` |
| `define_vars` | `list[str] \| None` | no | `null` |

=== "Shell"

    ```bash
    phicad/bin/phicad-tool call pcb_export_3d --arg board_path=<str>
    ```

### pcb_upgrade

```python
pcb_upgrade(board_path: str, out_path: str | None = None, in_place: bool = False, force: bool = False) -> dict[str, Any]
```

`phicad/mcp/tools_cli.py:1885` · **writes in place / changes state** (name starts with 'pcb_upgrade')

Rewrite a .kicad_pcb in the current board file format.

`kicad-cli pcb upgrade [--force] <board>` — the CLI has no -o, it rewrites the file it is
handed. So by default this copies the board (plus any .kicad_pro/.kicad_dru/.kicad_prl
sidecar, which the loader wants) to <out>/upgrade/ and upgrades the COPY; the original is
never touched. `in_place=True` upgrades `board_path` itself and lists it under `mutated`
(the server's git journal snapshots that). Without --force an already-current board is
left alone and still reports ok=True — check `report.version_before` /
`report.version_after` / `report.version_changed` for what actually happened.

| Parameter | Type | Required | Default |
| --- | --- | --- | --- |
| `board_path` | `str` | yes | — |
| `out_path` | `str \| None` | no | `null` |
| `in_place` | `bool` | no | `false` |
| `force` | `bool` | no | `false` |

=== "Shell"

    ```bash
    phicad/bin/phicad-tool call pcb_upgrade --arg board_path=<str>
    ```

### sch_upgrade

```python
sch_upgrade(sch_path: str, out_path: str | None = None, in_place: bool = False, force: bool = False) -> dict[str, Any]
```

`phicad/mcp/tools_cli.py:1900`

Rewrite a .kicad_sch in the current schematic file format.

`kicad-cli sch upgrade [--force] <sch>`. Same in-place-only CLI as pcb_upgrade, so the
same safety: a copy under <out>/upgrade/ by default, `in_place=True` to rewrite the input
(reported in `mutated`). NOTE this upgrades ONE sheet file; a hierarchical design has a
.kicad_sch per sheet and each must be upgraded (the sub-sheets are not followed here).
`report.version_changed` tells you whether the format stamp moved.

| Parameter | Type | Required | Default |
| --- | --- | --- | --- |
| `sch_path` | `str` | yes | — |
| `out_path` | `str \| None` | no | `null` |
| `in_place` | `bool` | no | `false` |
| `force` | `bool` | no | `false` |

=== "Shell"

    ```bash
    phicad/bin/phicad-tool call sch_upgrade --arg sch_path=<str>
    ```

### pcb_import

```python
pcb_import(source_path: str, out_board: str | None = None, fmt: str = 'auto', report_format: str = 'none', report_file: str | None = None, layer_map: str | None = None) -> dict[str, Any]
```

`phicad/mcp/tools_cli.py:2011` · **writes in place / changes state** (name starts with 'pcb_import')

Convert a non-KiCad board file into a .kicad_pcb.

`kicad-cli pcb import -o <out.kicad_pcb> --format {auto,pads,altium,eagle,cadstar,
fabmaster,pcad,solidworks} [--report-format {none,json,text}] [--report-file F]
[--layer-map F.json] <input>`. `auto` sniffs the file by extension/content, which is
right nearly always; name the format when a vendor reuses an extension. `layer_map` is a
JSON file mapping source layer names to KiCad layer names — unmapped layers fall back to
the importer's best guess, and `report_format="json"` gets you the condensed verdict:
`report.statistics` (footprints/tracks/vias/zones imported), `report.layers_mapped`,
`report.layers_unmapped` (source layers whose geometry had nowhere to go) and
errors/warnings counts; the untruncated report stays on disk at `report_file`.

SIDE EFFECT worth knowing: the board importers also write a footprint cache library
`<source>-import-fps.pretty` plus an `fp-lib-table` NEXT TO THE OUTPUT, so give each
import its own directory — re-importing into a directory that already has that cache
logs `File exists` errors even though the board itself is written. Everything that
appeared is in `outputs`. Writes <out>/import/<name>.kicad_pcb by default and never
modifies the source. ok means the .kicad_pcb exists and is non-empty.

This is the single-file converter. `kicad-cli import` (whole-project, board+schematic at
once) and `pcb import-ses` (Specctra routing session, see pcb_import_ses) are separate.

| Parameter | Type | Required | Default |
| --- | --- | --- | --- |
| `source_path` | `str` | yes | — |
| `out_board` | `str \| None` | no | `null` |
| `fmt` | `str` | no | `"auto"` |
| `report_format` | `str` | no | `"none"` |
| `report_file` | `str \| None` | no | `null` |
| `layer_map` | `str \| None` | no | `null` |

=== "Shell"

    ```bash
    phicad/bin/phicad-tool call pcb_import --arg source_path=<str>
    ```

### sch_import

```python
sch_import(source_path: str, out_sch: str | None = None, fmt: str = 'auto', report_format: str = 'none', report_file: str | None = None) -> dict[str, Any]
```

`phicad/mcp/tools_cli.py:2040` · **writes in place / changes state** (name contains '_import')

Convert a non-KiCad schematic file into a .kicad_sch.

`kicad-cli sch import -o <out.kicad_sch> --format {auto,altium,eagle,cadstar,easyeda,
easyedapro,ltspice,pads,diptrace,pcad,orcad} [--report-format {none,json,text}]
[--report-file F] <input>`. No --layer-map (that is board-only). A hierarchical source
design produces the root sheet PLUS one `<source>_N.kicad_sch` per sub-sheet beside it
(a 7-sheet DipTrace file gives 1 root + 6 sub-sheets), so read `report.files_written` /
`outputs` rather than assuming a single file, and point each import at an empty directory.
Writes under <out>/import/ by default; the source is read-only. Symbol libraries the
import needs land next to the output as the importer decides.

| Parameter | Type | Required | Default |
| --- | --- | --- | --- |
| `source_path` | `str` | yes | — |
| `out_sch` | `str \| None` | no | `null` |
| `fmt` | `str` | no | `"auto"` |
| `report_format` | `str` | no | `"none"` |
| `report_file` | `str \| None` | no | `null` |

=== "Shell"

    ```bash
    phicad/bin/phicad-tool call sch_import --arg source_path=<str>
    ```

### sch_export_plot

```python
sch_export_plot(sch_path: str, fmt: str = 'svg', out_dir: str | None = None, black_and_white: bool = False, pages: str | None = None, exclude_drawing_sheet: bool = False, theme: str | None = None, no_background_color: bool = False, drawing_sheet: str | None = None, default_font: str | None = None, draw_hop_over: bool = False, dpi: int = 300, no_antialias: bool = False, variant: str | None = None, define_vars: list[str] | None = None) -> dict[str, Any]
```

`phicad/mcp/tools_cli.py:2067`

Plot a schematic to SVG / DXF / PS / PNG / HPGL — one file per page.

`kicad-cli sch export <fmt> -o <dir> [-b] [-e] [-t THEME] [-n] [--pages 1,3]
[--drawing-sheet F] [--default-font NAME] [--draw-hop-over] [--variant V] [-D K=V]`
plus `--dpi/--no-antialias` for png only, and `-n/--no-background-color` for svg/ps/png
(dxf and hpgl reject it). Output is a directory (default <out>/sch-plot/<sheet>/) because
a multi-page schematic yields several files; `report.file_count` and `report.files` are
the answer to "did every page come out". `pages` is a comma list of page numbers, blank
for all. Read-only on the schematic. For PDF use sch_export_pdf (server.py); HPGL is
accepted by the schematic side (unlike pcb export hpgl) but its --pen-size/--origin flags
are documented as having no effect.

| Parameter | Type | Required | Default |
| --- | --- | --- | --- |
| `sch_path` | `str` | yes | — |
| `fmt` | `str` | no | `"svg"` |
| `out_dir` | `str \| None` | no | `null` |
| `black_and_white` | `bool` | no | `false` |
| `pages` | `str \| None` | no | `null` |
| `exclude_drawing_sheet` | `bool` | no | `false` |
| `theme` | `str \| None` | no | `null` |
| `no_background_color` | `bool` | no | `false` |
| `drawing_sheet` | `str \| None` | no | `null` |
| `default_font` | `str \| None` | no | `null` |
| `draw_hop_over` | `bool` | no | `false` |
| `dpi` | `int` | no | `300` |
| `no_antialias` | `bool` | no | `false` |
| `variant` | `str \| None` | no | `null` |
| `define_vars` | `list[str] \| None` | no | `null` |

=== "Shell"

    ```bash
    phicad/bin/phicad-tool call sch_export_plot --arg sch_path=<str>
    ```

### sch_export_pythonbom

```python
sch_export_pythonbom(sch_path: str, out_path: str | None = None) -> dict[str, Any]
```

`phicad/mcp/tools_cli.py:2137`

Export the legacy BOM **XML** intermediate netlist (the `python-bom` format).

`kicad-cli sch export python-bom -o <file> <sch>`. This is the XML that the classic
eeschema "BOM script" plugins (bom2grouped_csv.py and friends) consume: a <export> tree
with <components>, <libparts>, <nets>. It is NOT the modern grouped CSV — use
sch_export_bom (server.py) or export_bom for that. Useful when an existing Python BOM
generator has to be driven from an agent, or when you want per-component library and pin
data in one file. Written to <out>/bom/<sheet>.python-bom.xml by default;
`report.component_count` / `report.libpart_count` / `report.net_count` come from a cheap
tag count, so they are exact but shallow.

| Parameter | Type | Required | Default |
| --- | --- | --- | --- |
| `sch_path` | `str` | yes | — |
| `out_path` | `str \| None` | no | `null` |

=== "Shell"

    ```bash
    phicad/bin/phicad-tool call sch_export_pythonbom --arg sch_path=<str>
    ```

### export_bom

```python
export_bom(input_path: str, out_path: str | None = None, fields: str = 'Reference,Value,Footprint,${QUANTITY},DNP', labels: str | None = None, group_by: str | None = None, sort_field: str | None = None, sort_asc: bool = True, filter_string: str | None = None, filter_scope: str = 'reference', exclude_dnp: bool = False, preset: str | None = None, format_preset: str | None = None, field_delimiter: str | None = None, string_delimiter: str | None = None, ref_delimiter: str | None = None, ref_range_delimiter: str | None = None, keep_tabs: bool = False, keep_line_breaks: bool = False, include_byte_order_mark: bool = False, variant: str | None = None) -> dict[str, Any]
```

`phicad/mcp/tools_cli.py:2173`

Full-control BOM export — the same EXPORT_BOM_COMMAND behind `sch export bom` AND
`pcb export bom`, dispatched on the input's extension.

kicad/kicad_cli.cpp registers ONE class twice: `EXPORT_BOM_COMMAND{KIWAY::FACE_SCH}` as
`sch export bom` and `EXPORT_BOM_COMMAND{KIWAY::FACE_PCB}` as `pcb export bom`. The flag
set is identical (command_export_bom.cpp); only the reader differs — the schematic one
walks the netlist, the board one walks footprints. So this tool takes a .kicad_sch or a
.kicad_pcb and picks the matching subcommand, rather than pretending there is a separate
top-level `kicad-cli export bom` (there is not; `kicad-cli export` does not exist).

Beyond server.py's sch_export_bom (which only exposes --fields) this reaches every knob:
--labels, --group-by, --sort-field, --filter/--filter-scope
{reference,visible,all}, --exclude-dnp, --preset/--format-preset (named presets stored in
the project), the four delimiters, --keep-tabs/--keep-line-breaks,
--include-byte-order-mark and --variant. Generated fields (QUANTITY, ITEM_NUMBER, DNP,
EXCLUDE_FROM_BOM, EXCLUDE_FROM_BOARD, EXCLUDE_FROM_SIM) work with or without ${}.

`sort_asc=False` is REFUSED with an explanation instead of being sent to the CLI:
--sort-asc is declared with a bool default but no .flag(), so handing it ANY value token
crashes the command (std::bad_any_cast, exit 255 — verified on this fork). Sorting is
always ascending; reverse the rows downstream if you need the other order.
Written to <out>/bom/<stem>.bom.csv unless out_path says otherwise;
`report.row_count`/`header`/`preview` come from the file itself.

| Parameter | Type | Required | Default |
| --- | --- | --- | --- |
| `input_path` | `str` | yes | — |
| `out_path` | `str \| None` | no | `null` |
| `fields` | `str` | no | `"Reference,Value,Footprint,${QUANTITY},DNP"` |
| `labels` | `str \| None` | no | `null` |
| `group_by` | `str \| None` | no | `null` |
| `sort_field` | `str \| None` | no | `null` |
| `sort_asc` | `bool` | no | `true` |
| `filter_string` | `str \| None` | no | `null` |
| `filter_scope` | `str` | no | `"reference"` |
| `exclude_dnp` | `bool` | no | `false` |
| `preset` | `str \| None` | no | `null` |
| `format_preset` | `str \| None` | no | `null` |
| `field_delimiter` | `str \| None` | no | `null` |
| `string_delimiter` | `str \| None` | no | `null` |
| `ref_delimiter` | `str \| None` | no | `null` |
| `ref_range_delimiter` | `str \| None` | no | `null` |
| `keep_tabs` | `bool` | no | `false` |
| `keep_line_breaks` | `bool` | no | `false` |
| `include_byte_order_mark` | `bool` | no | `false` |
| `variant` | `str \| None` | no | `null` |

=== "Shell"

    ```bash
    phicad/bin/phicad-tool call export_bom --arg input_path=<str>
    ```

### pcb_layers

```python
pcb_layers(board_path: str) -> dict[str, Any]
```

`phicad/mcp/tools_cli.py:2349`

The board's layer table — parsed straight out of the .kicad_pcb, no kicad-cli, no API.

Every plotting tool here (`pcb_export_svg/png/ps/dxf/pdf/gerbers`) takes a `layers`
string of CANONICAL names, and getting one wrong is the most common way those calls fail.
This reads the `(layers …)` block from the file with the stdlib s-expression parser
(phicad/pylib/sexp.py, with a bracket-scan + regex fallback) and returns the table in
board order:

    layers        [{index, canonical, user_name, type}]  — `canonical` is what -l wants
    copper_count  number of *.Cu layers enabled on this board (the "layer count" a fab
                  quotes), and `copper` lists them in stack order
    by_type       {signal: n, user: n, …}
    renamed       the layers whose user_name differs from the canonical name

Costs milliseconds, needs no build, and works on a board the editor has open. ok=False
only if the file cannot be read or has no layer table.

| Parameter | Type | Required | Default |
| --- | --- | --- | --- |
| `board_path` | `str` | yes | — |

=== "Shell"

    ```bash
    phicad/bin/phicad-tool call pcb_layers --arg board_path=<str>
    ```

### kicad_cli_help

```python
kicad_cli_help(command: str | list[str] = '') -> dict[str, Any]
```

`phicad/mcp/tools_cli.py:2471`

The fork's own `--help` for any kicad-cli subcommand, parsed into a flag list.

Accepts `"pcb export gerbers"` or `["pcb","export","gerbers"]`; empty means the top-level
command list. Returns `help_text` verbatim plus:

    usage         the Usage: line
    flags         [{names, flag, help, default}] — every optional argument
    positionals   [{name, help}]
    subcommands   [{name, help}] when the command is a group (e.g. `pcb export`)
    is_group      True when it only dispatches to subcommands
    unknown       True when argparse fell back to the PARENT's help, i.e. the last token
                  is not a real subcommand (kicad-cli exits 0 either way, so this flag is
                  the only way to tell)
    removed       subcommands under this one that the fork still PARSES and then refuses,
                  with what to use instead — `pcb export hpgl` is gone in 10.0 (use
                  pcb_export_ps / pcb_export_dxf / pcb_export_svg) yet still shows up in
                  --help, which is exactly the trap this field exists to close

This is the ground truth for flags: the typed tools here were written against
kicad/cli/command_*.cpp, but a fork change lands in --help first. Use it before reaching
for `kicad_cli()`, and cross-check anything surprising in a typed tool's docstring.

| Parameter | Type | Required | Default |
| --- | --- | --- | --- |
| `command` | `str \| list[str]` | no | `""` |

=== "Shell"

    ```bash
    phicad/bin/phicad-tool call kicad_cli_help
    ```

### kicad_cli

```python
kicad_cli(args: list[str], timeout_s: int = 300, cwd: str | None = None) -> dict[str, Any]
```

`phicad/mcp/tools_cli.py:2524`

Escape hatch: run the fork's kicad-cli with an arbitrary argv and report what appeared.

`args` is the argv AFTER the binary, e.g.
`["pcb","export","gerbers","-o","/tmp/g/","b.kicad_pcb"]`.
Use this only for something no typed tool covers (a brand-new fork flag, an odd flag
combination, `version --format json`); the typed tools give better validation, defaults,
output bookkeeping and parsed reports. Discover flags with kicad_cli_help first.

Behaviour and guard rails:
  * `api-server` is REFUSED — the IPC server is lifecycle-managed by api_server_start /
    api_server_stop, and a stray one here would leak a process holding a socket.
  * empty argv is refused (that would just print help; call kicad_cli_help).
  * cwd defaults to <out>/cli/ so a command that writes beside its input cannot litter the
    repo; pass cwd explicitly when the command must run next to a project.
  * files that newly appear in cwd and in the directory of every `-o/--output` value are
    detected and returned in `outputs` (+ `output_bytes`); nothing is assumed about naming.
    A value ending in `/` is treated as a directory and created; otherwise only its parent
    is created, because for several subcommands (`pcb export svg` in its default
    mode-single, `pcb export pdf`) `-o` is a FILE and a directory in the way is an error.
  * `mutated` is left empty: this tool cannot know which pre-existing file the command
    rewrote, so treat an in-place command run here as unjournaled.
  * ok mirrors exit code 0. stdout is returned in full (capped by the runner) because for
    the escape hatch the text IS the result.

| Parameter | Type | Required | Default |
| --- | --- | --- | --- |
| `args` | `list[str]` | yes | — |
| `timeout_s` | `int` | no | `300` |
| `cwd` | `str \| None` | no | `null` |

=== "Shell"

    ```bash
    phicad/bin/phicad-tool call kicad_cli --arg args=<list[str]>
    ```

## Git

### project_init

```python
project_init(path: str, message: str | None = None) -> dict[str, Any]
```

`phicad/mcp/tools_git.py:282`

Initialize history for a KiCad project without nesting inside an existing repo.

A standalone project gets a repository at its root; a project below an existing
repository reuses that repository and scopes its baseline commit to this project.
`path` is the project directory or any file inside it. Writes `.phicad/project.json`,
appends PhiCad's block to `.gitignore` (KiCad lock/backup files, the host-bridge
descriptor and the journal stay untracked) and commits everything else as
`phicad(init): <message>`. Safe to call twice: an existing repository is left alone and
only genuinely new files are committed.

Also installs the fork's STRUCTURAL merge driver — `.gitattributes` routing
`*.kicad_pcb`/`_sch`/`_sym`/`_mod` to `kicad-cli git-mergedriver`, plus the per-repo
`merge.kicad-*` config — because git's default text merge writes `<<<<<<<` into a board
and the result will not load at all. Both halves are append-only: a glob or config key
someone already chose is never overwritten. Reported as `merge_driver`, whose
`installed: false` (with a `reason`) means merges fall back to text merge, so check it
before handing one project repo to a second agent.

REFUSED, deliberately: a directory containing `phicad/`, anything inside the PhiCad
source checkout, $HOME, and an existing repository path that does not identify one KiCad
project — `ok: false` with the reason. Never creates a nested repository silently.

| Parameter | Type | Required | Default |
| --- | --- | --- | --- |
| `path` | `str` | yes | — |
| `message` | `str \| None` | no | `null` |

=== "Shell"

    ```bash
    phicad/bin/phicad-tool call project_init --arg path=<str>
    ```

### project_snapshot

```python
project_snapshot(path: str, message: str, actor: str = 'agent', files: list[str] | None = None) -> dict[str, Any]
```

`phicad/mcp/tools_git.py:336` · **writes in place / changes state** (name starts with 'project_snapshot')

Commit the project's current state as one auditable revision.

The subject is `phicad(<actor>): <message>` and the body carries `Phicad-Actor:`,
`Phicad-Tool:` and `Phicad-Files:` trailers, so `git log` alone says who changed a board
and why. `files` limits the commit to those paths (anything outside the project repo is
dropped) — without it every change in the project is staged.

`ok: true, committed: false` with a `reason` when there was nothing to commit; that is a
normal answer, not an error. Read `committed`, `changed_files`, `insertions`,
`deletions`. Journals the action to `.phicad/journal.jsonl` either way.

| Parameter | Type | Required | Default |
| --- | --- | --- | --- |
| `path` | `str` | yes | — |
| `message` | `str` | yes | — |
| `actor` | `str` | no | `"agent"` |
| `files` | `list[str] \| None` | no | `null` |

=== "Shell"

    ```bash
    phicad/bin/phicad-tool call project_snapshot --arg path=<str> --arg message=<str>
    ```

### project_status

```python
project_status(path: str) -> dict[str, Any]
```

`phicad/mcp/tools_git.py:372`

Report the project repository's branch, HEAD, pending changes and commit count.

Read-only. `clean` is False when the working tree has uncommitted changes; `pending`
lists them with their porcelain state (`M`, `??`, …). Also returns the committer identity
the project will use and how many journal entries have been recorded. An uninitialised
project is `ok: false` with the hint to call project_init.

| Parameter | Type | Required | Default |
| --- | --- | --- | --- |
| `path` | `str` | yes | — |

=== "Shell"

    ```bash
    phicad/bin/phicad-tool call project_status --arg path=<str>
    ```

### project_log

```python
project_log(path: str, limit: int = 20, file: str | None = None) -> dict[str, Any]
```

`phicad/mcp/tools_git.py:393`

List recent commits of the project with the actor and tool behind each one.

Read-only. Each entry is `{sha, short, ts, iso, subject, author, actor, tool, files}`,
newest first; `actor`/`tool` come from the `Phicad-*` trailers a PhiCad snapshot writes
(they are None for commits made by hand or by `git`). `file` restricts the log to the
history of one file (renames followed).

| Parameter | Type | Required | Default |
| --- | --- | --- | --- |
| `path` | `str` | yes | — |
| `limit` | `int` | no | `20` |
| `file` | `str \| None` | no | `null` |

=== "Shell"

    ```bash
    phicad/bin/phicad-tool call project_log --arg path=<str>
    ```

### project_diff

```python
project_diff(path: str, rev_a: str = 'HEAD~1', rev_b: str = 'HEAD', semantic: bool = True) -> dict[str, Any]
```

`phicad/mcp/tools_git.py:411`

Diff two revisions of a project: git stat plus the fork's SEMANTIC board/schematic diff.

Read-only with respect to the project. `git diff --numstat/--stat` gives the file-level
picture; then, with `semantic=True`, every changed `.kicad_pcb` / `.kicad_sch` is
extracted from BOTH revisions to temp files and compared with `kicad-cli {pcb,sch} diff
--format json` — which answers in objects ("footprint R12 moved", "track added") instead
of s-expression line noise. Pass `rev_b="WORKTREE"` to diff committed history against
uncommitted edits.

Per document you get `differs`, `change_count`, `changes_by_kind`, `changes_by_type`,
the first 20 changes and `report_path` for the full JSON. `differs` comes from
kicad-cli's exit code 5, which means "documents differ" and is a SUCCESS. `semantic` is
silently skipped (with `semantic_skipped`) when kicad-cli is not built.

| Parameter | Type | Required | Default |
| --- | --- | --- | --- |
| `path` | `str` | yes | — |
| `rev_a` | `str` | no | `"HEAD~1"` |
| `rev_b` | `str` | no | `"HEAD"` |
| `semantic` | `bool` | no | `true` |

=== "Shell"

    ```bash
    phicad/bin/phicad-tool call project_diff --arg path=<str>
    ```

### project_restore

```python
project_restore(path: str, rev: str, files: list[str] | None = None, dry_run: bool = True) -> dict[str, Any]
```

`phicad/mcp/tools_git.py:468` · **writes in place / changes state** (name starts with 'project_restore')

Bring files back from an earlier revision — the "undo a bad edit" tool.

`dry_run=True` (the DEFAULT) writes nothing and only reports `would_change`. With
`dry_run=False` the files are checked out from `rev` AND committed as
`phicad(restore): …` with a `Phicad-Source-Rev:` trailer, so the recovery is itself part
of the audit trail (a bare checkout would leave the project dirty and let the autosnapshot
watcher describe a rollback as an edit). History is never rewritten. `files` limits the
restore; without it the whole project is restored to that revision.

| Parameter | Type | Required | Default |
| --- | --- | --- | --- |
| `path` | `str` | yes | — |
| `rev` | `str` | yes | — |
| `files` | `list[str] \| None` | no | `null` |
| `dry_run` | `bool` | no | `true` |

=== "Shell"

    ```bash
    phicad/bin/phicad-tool call project_restore --arg path=<str> --arg rev=<str>
    ```

### project_undo_last

```python
project_undo_last(path: str, dry_run: bool = True) -> dict[str, Any]
```

`phicad/mcp/tools_git.py:504` · **writes in place / changes state** (name starts with 'project_undo')

Undo the most recent commit with `git revert` — history is preserved, not rewritten.

A revert adds a commit that inverts HEAD, so the undone state stays reachable and the
undo can itself be undone. `dry_run=True` (the DEFAULT) reports the HEAD subject and the
files that would be put back. A dirty working tree is REFUSED rather than folded into the
revert, because that would attribute the designer's unsaved edits to the undo — snapshot
first. Refused on a repository whose HEAD is the initial commit.

| Parameter | Type | Required | Default |
| --- | --- | --- | --- |
| `path` | `str` | yes | — |
| `dry_run` | `bool` | no | `true` |

=== "Shell"

    ```bash
    phicad/bin/phicad-tool call project_undo_last --arg path=<str>
    ```

### project_tag

```python
project_tag(path: str, name: str, message: str = '') -> dict[str, Any]
```

`phicad/mcp/tools_git.py:535` · **writes in place / changes state** (name starts with 'project_tag')

Mark the project's current revision as a milestone (an annotated git tag).

For the states worth naming: `drc-clean-v1`, `sent-to-fab-2026-03`, `rev-b`. The tag
points at HEAD, so snapshot first if there are pending changes — a milestone that does
not include the last save is worse than no milestone. An existing tag name is refused
rather than moved (`ok: false`), because moving a tag rewrites what a colleague already
fetched.

| Parameter | Type | Required | Default |
| --- | --- | --- | --- |
| `path` | `str` | yes | — |
| `name` | `str` | yes | — |
| `message` | `str` | no | `""` |

=== "Shell"

    ```bash
    phicad/bin/phicad-tool call project_tag --arg path=<str> --arg name=<str>
    ```

### project_journal

```python
project_journal(path: str, limit: int = 50) -> dict[str, Any]
```

`phicad/mcp/tools_git.py:565`

Read the project's action journal: who did what to this board, in order.

Read-only. Every mutating PhiCad tool appends one JSON line to `.phicad/journal.jsonl`
with `{ts, actor, action, args, files, commit, result}` — the commit sha ties an entry to
the revision it produced, and argument values whose key looks like a token/secret/key are
stored as `***`. The journal is gitignored (it is written on every action; tracking it
would make the autosnapshot watcher commit its own noise), so this is the only way to
read it back. Returns the LAST `limit` entries.

| Parameter | Type | Required | Default |
| --- | --- | --- | --- |
| `path` | `str` | yes | — |
| `limit` | `int` | no | `50` |

=== "Shell"

    ```bash
    phicad/bin/phicad-tool call project_journal --arg path=<str>
    ```

### project_watch_once

```python
project_watch_once(path: str, stable_seconds: float = 2.0) -> dict[str, Any]
```

`phicad/mcp/tools_git.py:588`

One poll of the save watcher: commit the project once its files have stopped changing.

Designed to be called from a loop (the ai-view plugin does exactly this). A KiCad save
rewrites several files in quick succession, so this commits only after the porcelain
status has been IDENTICAL for `stable_seconds` — otherwise it returns
`committed: false` with a `reason` (`changes still arriving`, `waiting for the save to
settle`, `working tree is clean`) and the caller polls again. The stability window is
per-call-site state, so the first call on a freshly dirty tree never commits;
that is intentional, not a miss.

Commits as `phicad(editor): autosnapshot <UTC timestamp>`.

| Parameter | Type | Required | Default |
| --- | --- | --- | --- |
| `path` | `str` | yes | — |
| `stable_seconds` | `float` | no | `2.0` |

=== "Shell"

    ```bash
    phicad/bin/phicad-tool call project_watch_once --arg path=<str>
    ```

## Schematic

### sch_list_symbols

```python
sch_list_symbols(sch_path: str, sheet: str | None = None) -> dict[str, Any]
```

`phicad/mcp/tools_schematic.py:327`

List the symbols placed on a schematic: refs, values, footprints, lib_ids, positions.

Read-only.  Each row carries ref, value, footprint, lib_id, at {x_mm, y_mm, rotation},
unit, body_style, uuid and the in_bom/on_board/dnp/exclude_from_sim flags.

`sheet` selects the scope: None = THIS FILE ONLY (a .kicad_sch is one sheet), "*" = the
whole hierarchy reachable from it, or a Sheetname / sheet filename for one child sheet.
Refs starting with '#' are power/flag pseudo-symbols (`power_symbol_count`), not parts —
they carry no footprint and never reach the board.

A multi-unit part appears once PER UNIT with the same ref, so `symbol_count` is normally
larger than `unique_refs`.

| Parameter | Type | Required | Default |
| --- | --- | --- | --- |
| `sch_path` | `str` | yes | — |
| `sheet` | `str \| None` | no | `null` |

=== "Shell"

    ```bash
    phicad/bin/phicad-tool call sch_list_symbols --arg sch_path=<str>
    ```

### sch_get_symbol

```python
sch_get_symbol(sch_path: str, ref: str, sheet: str | None = None) -> dict[str, Any]
```

`phicad/mcp/tools_schematic.py:349`

Everything about one placed symbol, by reference designator.

Read-only.  Searches the whole hierarchy by default (`sheet` narrows it as in
sch_list_symbols) and returns one entry per unit: all fields (`fields`), the non-stock
ones separately (`custom_fields`), the pin-number -> pin-uuid map, the `(instances …)`
blocks that netlisting actually reads, and the cached library definition with its pins
(number/name/type/position) when the schematic carries one.

`definition_cached: false` means the schematic references a lib_id it has no cached
definition for — the symbol would draw as a placeholder in eeschema.

| Parameter | Type | Required | Default |
| --- | --- | --- | --- |
| `sch_path` | `str` | yes | — |
| `ref` | `str` | yes | — |
| `sheet` | `str \| None` | no | `null` |

=== "Shell"

    ```bash
    phicad/bin/phicad-tool call sch_get_symbol --arg sch_path=<str> --arg ref=<str>
    ```

### sch_annotate_check

```python
sch_annotate_check(sch_path: str, sheet: str | None = None) -> dict[str, Any]
```

`phicad/mcp/tools_schematic.py:372`

Find unannotated (R?, U?) and duplicated reference designators in a hierarchy.

Read-only; walks the whole hierarchy by default.  Returns `unannotated` (any ref holding
a '?'), `duplicates` (the SAME ref AND unit used twice — a multi-unit part legitimately
repeats its ref with different units, and that is not flagged), `missing_reference`
(a symbol with no Reference at all) and `field_instance_mismatch`.

That last one is the trap this tool exists for: a refdes is stored twice, in the drawn
`(property "Reference" …)` and in `(instances … (reference …))`, and only the instance
reaches the netlist and the board.  A mismatch means the canvas and the netlist disagree.
`annotated` is true only when unannotated, duplicates and missing are all empty.

| Parameter | Type | Required | Default |
| --- | --- | --- | --- |
| `sch_path` | `str` | yes | — |
| `sheet` | `str \| None` | no | `null` |

=== "Shell"

    ```bash
    phicad/bin/phicad-tool call sch_annotate_check --arg sch_path=<str>
    ```

### sch_list_sheets

```python
sch_list_sheets(sch_path: str) -> dict[str, Any]
```

`phicad/mcp/tools_schematic.py:394`

Walk the sheet hierarchy from a root schematic: files, sheet pins, page numbers.

Read-only, cycle-safe, depth-capped at 16.  `tree` is the nested structure (each sheet
with its Sheetname, Sheetfile, resolved path, position, size, hierarchical pins and page
numbers); `files` is the flat list with a per-file symbol count; `missing_sheet_files`
names children whose .kicad_sch is not on disk (a broken project, and the reason a sheet
can silently contribute no parts). `is_flat` is true for a single-sheet design.

| Parameter | Type | Required | Default |
| --- | --- | --- | --- |
| `sch_path` | `str` | yes | — |

=== "Shell"

    ```bash
    phicad/bin/phicad-tool call sch_list_sheets --arg sch_path=<str>
    ```

### sch_find

```python
sch_find(sch_path: str, query: str, regex: bool = False, sheet: str | None = None, limit: int = 200) -> dict[str, Any]
```

`phicad/mcp/tools_schematic.py:411`

Search a schematic hierarchy across refs, lib_ids and every field name and value.

Read-only, case-insensitive substring match by default; `regex=True` treats `query` as a
Python regular expression.  Each hit reports `matched_in` (e.g. ["ref"],
["field:Value"], ["field_name:MPN"]) so it is obvious WHY it matched, plus the symbol's
full field dict.  This is the tool for "which parts have no MPN", "who uses this
footprint", "find every 0603".

| Parameter | Type | Required | Default |
| --- | --- | --- | --- |
| `sch_path` | `str` | yes | — |
| `query` | `str` | yes | — |
| `regex` | `bool` | no | `false` |
| `sheet` | `str \| None` | no | `null` |
| `limit` | `int` | no | `200` |

=== "Shell"

    ```bash
    phicad/bin/phicad-tool call sch_find --arg sch_path=<str> --arg query=<str>
    ```

### sch_list_nets

```python
sch_list_nets(sch_path: str, out_netlist: str | None = None) -> dict[str, Any]
```

`phicad/mcp/tools_schematic.py:433`

List the schematic's nets and the pins on them (the real connectivity, not geometry).

Runs `kicad-cli sch export netlist --format kicadsexpr` — the fork's own netlister, which
resolves wires, labels, hierarchical pins and buses — and parses the result: every net
with its `pins` (ref, pin number, pin function, pin type), the component list, and
`single_pin_nets`, which is the fastest signal that something is not actually wired.

Read-only with respect to the schematic; the netlist file itself is written under
<out>/netlist/ unless out_netlist says otherwise.  Needs kicad-cli.

| Parameter | Type | Required | Default |
| --- | --- | --- | --- |
| `sch_path` | `str` | yes | — |
| `out_netlist` | `str \| None` | no | `null` |

=== "Shell"

    ```bash
    phicad/bin/phicad-tool call sch_list_nets --arg sch_path=<str>
    ```

### sch_symbol_lib_list

```python
sch_symbol_lib_list(sym_path: str) -> dict[str, Any]
```

`phicad/mcp/tools_schematic.py:468`

List every symbol in a .kicad_sym library: units, pin counts, prefix, footprint.

Read-only.  Each row carries name, `extends` (an alias inherits its parent's pins and
graphics, so its own pin_count is 0), is_power, unit_count, pin_count, the Reference
prefix, Value, Footprint, Datasheet, Description, ki_keywords and ki_fp_filters.
Use sch_symbol_lib_get for the pin table of one symbol.

| Parameter | Type | Required | Default |
| --- | --- | --- | --- |
| `sym_path` | `str` | yes | — |

=== "Shell"

    ```bash
    phicad/bin/phicad-tool call sch_symbol_lib_list --arg sym_path=<str>
    ```

### sch_symbol_lib_get

```python
sch_symbol_lib_get(sym_path: str, name: str) -> dict[str, Any]
```

`phicad/mcp/tools_schematic.py:484`

One symbol definition from a .kicad_sym: its pins and its properties.

Read-only.  `pins` lists every pin with number, name, electrical type (input, output,
power_in, tri_state, …), graphic style, position {x_mm, y_mm, rotation}, length and unit;
`units` maps the `<name>_<unit>_<body>` sub-symbols (unit 0 = shared across all units,
which is where a multi-unit part keeps VCC/GND); `properties` is the raw property dict.
On a miss, `did_you_mean` lists names containing the query.

| Parameter | Type | Required | Default |
| --- | --- | --- | --- |
| `sym_path` | `str` | yes | — |
| `name` | `str` | yes | — |

=== "Shell"

    ```bash
    phicad/bin/phicad-tool call sch_symbol_lib_get --arg sym_path=<str> --arg name=<str>
    ```

### sch_set_field

```python
sch_set_field(sch_path: str, ref: str, field: str, value: str, out_path: str | None = None, verify: bool = True) -> dict[str, Any]
```

`phicad/mcp/tools_schematic.py:508` · **writes in place / changes state** (name starts with 'sch_set')

Set one field on one placed symbol (Value, Footprint, MPN, any custom field).

An existing field is rewritten in place; a field the symbol does not have is CREATED
hidden at the symbol's own position (which is what eeschema does for a BOM field). A
multi-unit part carries a copy of the field on every unit and all of them are updated.
Setting "Reference" also rewrites the `(instances … (reference …))` blocks, because those
are what the netlist and the board read — updating only one of the two is how a
schematic ends up annotated on screen and unannotated in the netlist.

Writes <out>/schematic/<stem>.edited.kicad_sch unless out_path says otherwise; pass
out_path == sch_path to edit in place (then the file appears in `mutated`).  Untouched
text is preserved byte-for-byte, so `changes` and a one-line diff agree.  With
verify=True the result is parsed back by `kicad-cli sch erc` and reported as
`loads: true/false`.

| Parameter | Type | Required | Default |
| --- | --- | --- | --- |
| `sch_path` | `str` | yes | — |
| `ref` | `str` | yes | — |
| `field` | `str` | yes | — |
| `value` | `str` | yes | — |
| `out_path` | `str \| None` | no | `null` |
| `verify` | `bool` | no | `true` |

=== "Shell"

    ```bash
    phicad/bin/phicad-tool call sch_set_field --arg sch_path=<str> --arg ref=<str> --arg field=<str> --arg value=<str>
    ```

### sch_set_fields_bulk

```python
sch_set_fields_bulk(sch_path: str, updates: list[dict[str, str]], out_path: str | None = None, verify: bool = True) -> dict[str, Any]
```

`phicad/mcp/tools_schematic.py:529` · **writes in place / changes state** (name starts with 'sch_set')

Set many fields across many symbols in ONE pass over one schematic file.

`updates` is a list of {"ref": …, "field": …, "value": …}.  All edits are applied to a
single parse and written once, so the BOM-stamping case (MPN + Manufacturer + LCSC on
fifty parts) costs one file rewrite instead of fifty, and two updates targeting the same
(ref, field) are rejected rather than silently resolved.

Refs are matched in THIS FILE only — a hierarchical child is a separate .kicad_sch (call
sch_list_sheets, then this tool per file).  Unknown refs fail the whole call with the
known refs listed; nothing is written.  Same output/verify rules as sch_set_field.

| Parameter | Type | Required | Default |
| --- | --- | --- | --- |
| `sch_path` | `str` | yes | — |
| `updates` | `list[dict[str, str]]` | yes | — |
| `out_path` | `str \| None` | no | `null` |
| `verify` | `bool` | no | `true` |

=== "Shell"

    ```bash
    phicad/bin/phicad-tool call sch_set_fields_bulk --arg sch_path=<str> --arg updates=<list[dict[str, str]]>
    ```

### sch_place_symbol

```python
sch_place_symbol(sch_path: str, lib_id: str, ref: str, value: str = '', x_mm: float = 0.0, y_mm: float = 0.0, rotation: float = 0.0, unit: int = 1, footprint: str | None = None, from_lib: str | None = None, out_path: str | None = None, verify: bool = True) -> dict[str, Any]
```

`phicad/mcp/tools_schematic.py:568` · **writes in place / changes state** (name starts with 'sch_place')

Place a symbol instance on a schematic sheet at (x_mm, y_mm).

`lib_id` is LIBRARY:SYMBOL.  If the schematic already caches that definition in its
`(lib_symbols …)` block the placement just references it; otherwise pass `from_lib` = a
.kicad_sym file and the definition is COPIED into the cache (a placement whose definition
is missing loads as a broken symbol).  Aliases (`extends`) are refused rather than
half-flattened.  Refuses a refdes already used in the file, and rotation other than
0/90/180/270.

The placement gets a fresh uuid, a uuid per pin, Reference/Value/Footprint/Datasheet/
Description fields (defaults taken from the definition) and an `(instances …)` block
copied from a symbol already in the file — which is the only way to get the /root/sheet
path right in a CHILD sheet, since a child does not know who instantiated it.

THE SYMBOL IS WIRED TO NOTHING.  Its pins are unconnected until sch_add_wire /
sch_add_label reaches them, and ERC will say so.  For a multi-unit part only `unit` is
placed (`multi_unit_note` says how many there are).  Same output/verify rules as
sch_set_field.

| Parameter | Type | Required | Default |
| --- | --- | --- | --- |
| `sch_path` | `str` | yes | — |
| `lib_id` | `str` | yes | — |
| `ref` | `str` | yes | — |
| `value` | `str` | no | `""` |
| `x_mm` | `float` | no | `0.0` |
| `y_mm` | `float` | no | `0.0` |
| `rotation` | `float` | no | `0.0` |
| `unit` | `int` | no | `1` |
| `footprint` | `str \| None` | no | `null` |
| `from_lib` | `str \| None` | no | `null` |
| `out_path` | `str \| None` | no | `null` |
| `verify` | `bool` | no | `true` |

=== "Shell"

    ```bash
    phicad/bin/phicad-tool call sch_place_symbol --arg sch_path=<str> --arg lib_id=<str> --arg ref=<str>
    ```

### sch_add_wire

```python
sch_add_wire(sch_path: str, x1_mm: float, y1_mm: float, x2_mm: float, y2_mm: float, kind: str = 'wire', style: str = 'solid', out_path: str | None = None, verify: bool = True) -> dict[str, Any]
```

`phicad/mcp/tools_schematic.py:617` · **writes in place / changes state** (name starts with 'sch_add')

Add a wire (or `kind="bus"`) segment between two points, in millimetres.

A wire connects ONLY where an endpoint lands exactly on a pin, a label anchor or another
wire's endpoint (or a junction); wires that merely cross are not connected, and KiCad's
grid is 1.27 mm for a reason — off-grid endpoints look connected and are not.  Verify
with sch_list_nets, not with the picture.

`style` is one of default/solid/dash/dash_dot/dash_dot_dot/dot.  Zero-length segments are
refused.  Returns the new element's uuid.  Same output/verify rules as sch_set_field.

| Parameter | Type | Required | Default |
| --- | --- | --- | --- |
| `sch_path` | `str` | yes | — |
| `x1_mm` | `float` | yes | — |
| `y1_mm` | `float` | yes | — |
| `x2_mm` | `float` | yes | — |
| `y2_mm` | `float` | yes | — |
| `kind` | `str` | no | `"wire"` |
| `style` | `str` | no | `"solid"` |
| `out_path` | `str \| None` | no | `null` |
| `verify` | `bool` | no | `true` |

=== "Shell"

    ```bash
    phicad/bin/phicad-tool call sch_add_wire --arg sch_path=<str> --arg x1_mm=<float> --arg y1_mm=<float> --arg x2_mm=<float> --arg y2_mm=<float>
    ```

### sch_add_label

```python
sch_add_label(sch_path: str, text: str, x_mm: float, y_mm: float, kind: str = 'local', rotation: float = 0.0, shape: str | None = None, size_mm: float = 1.27, out_path: str | None = None, verify: bool = True) -> dict[str, Any]
```

`phicad/mcp/tools_schematic.py:640` · **writes in place / changes state** (name starts with 'sch_add')

Add a net label: `kind` = local | global | hier.

A local label names a net inside one sheet; a global label joins nets across the whole
hierarchy by name; a hierarchical label is the sheet-side end of a sheet pin and must be
matched by a pin of the same name on the parent's `(sheet …)` block, or the connection
simply does not exist.  Global and hierarchical labels take a `shape`
(input/output/bidirectional/tri_state/passive, default input); a local label must not.

A label names a net only where it TOUCHES a wire or pin — a label in empty space is an
ERC 'unconnected' item, not a net.  rotation must be 0/90/180/270.  Same output/verify
rules as sch_set_field.

| Parameter | Type | Required | Default |
| --- | --- | --- | --- |
| `sch_path` | `str` | yes | — |
| `text` | `str` | yes | — |
| `x_mm` | `float` | yes | — |
| `y_mm` | `float` | yes | — |
| `kind` | `str` | no | `"local"` |
| `rotation` | `float` | no | `0.0` |
| `shape` | `str \| None` | no | `null` |
| `size_mm` | `float` | no | `1.27` |
| `out_path` | `str \| None` | no | `null` |
| `verify` | `bool` | no | `true` |

=== "Shell"

    ```bash
    phicad/bin/phicad-tool call sch_add_label --arg sch_path=<str> --arg text=<str> --arg x_mm=<float> --arg y_mm=<float>
    ```

### sch_add_no_connect

```python
sch_add_no_connect(sch_path: str, x_mm: float, y_mm: float, out_path: str | None = None, verify: bool = True) -> dict[str, Any]
```

`phicad/mcp/tools_schematic.py:670` · **writes in place / changes state** (name starts with 'sch_add')

Mark a pin deliberately unconnected: the X flag that silences ERC for that pin.

The flag must sit EXACTLY on the pin's connection point (pin position + length along its
rotation); a millimetre off and ERC reports both the pin and the flag as unconnected.
Get the pin geometry from sch_get_symbol's `definition.pins` plus the symbol's own `at`.
Refuses a duplicate at the same coordinates.  Same output/verify rules as sch_set_field.

| Parameter | Type | Required | Default |
| --- | --- | --- | --- |
| `sch_path` | `str` | yes | — |
| `x_mm` | `float` | yes | — |
| `y_mm` | `float` | yes | — |
| `out_path` | `str \| None` | no | `null` |
| `verify` | `bool` | no | `true` |

=== "Shell"

    ```bash
    phicad/bin/phicad-tool call sch_add_no_connect --arg sch_path=<str> --arg x_mm=<float> --arg y_mm=<float>
    ```

### sch_delete_symbol

```python
sch_delete_symbol(sch_path: str, ref: str, out_path: str | None = None, verify: bool = True) -> dict[str, Any]
```

`phicad/mcp/tools_schematic.py:689` · **writes in place / changes state** (name starts with 'sch_delete')

Delete every unit of one placed symbol from one schematic file.

Only the `(symbol …)` blocks go.  The wires, junctions, labels and no-connects that
touched it are geometry, not children of the symbol, and are LEFT IN PLACE — they become
dangling stubs that ERC will report, which is deliberate: silently deleting a net's
wiring because a part was removed is how a schematic quietly changes meaning.  The
`(lib_symbols …)` cache entry is kept too, exactly as eeschema keeps it.

Refuses a ref this file does not contain (listing what it does contain).  Same
output/verify rules as sch_set_field.

| Parameter | Type | Required | Default |
| --- | --- | --- | --- |
| `sch_path` | `str` | yes | — |
| `ref` | `str` | yes | — |
| `out_path` | `str \| None` | no | `null` |
| `verify` | `bool` | no | `true` |

=== "Shell"

    ```bash
    phicad/bin/phicad-tool call sch_delete_symbol --arg sch_path=<str> --arg ref=<str>
    ```

## Session

### session_list_items

```python
session_list_items(kind: str, layer: str | None = None, net: str | None = None, limit: int = 100) -> dict[str, Any]
```

`phicad/mcp/tools_session.py:320`

List items of one kind on the board open in the managed headless session.

`kind` is one of footprint, pad, track, via, zone, shape, text, dimension, group.
`layer` and `net` narrow the result (both are validated against the board: a typo comes
back with the list of enabled layers / known net names, not an empty result).  Positions,
widths and lengths are millimetres.  Every row carries an `id` (KIID) — that is what the
write tools, session_get_item and session_delete_items address items by.

`ok` means the board was readable; `matched` is the filtered count, `total_on_board` the
unfiltered one, and `truncated` says whether `limit` cut the list short.

PRECONDITION: this needs a live headless session — call api_server_start() once, then session_open('<absolute>.kicad_pcb'), before this tool can do anything; without them it returns ok=false with a hint naming the missing step.

| Parameter | Type | Required | Default |
| --- | --- | --- | --- |
| `kind` | `str` | yes | — |
| `layer` | `str \| None` | no | `null` |
| `net` | `str \| None` | no | `null` |
| `limit` | `int` | no | `100` |

=== "Shell"

    ```bash
    phicad/bin/phicad-tool call session_list_items --arg kind=<str>
    ```

### session_get_item

```python
session_get_item(item_id: str) -> dict[str, Any]
```

`phicad/mcp/tools_session.py:341`

Everything about ONE item, addressed by the KIID that session_list_items returned.

Detailed by design: a footprint answers with its pad list (number, net, position) and its
custom fields, a zone with its full outline, a text item with size/thickness/rotation.
Fails with the id echoed back when nothing on the board has it.

PRECONDITION: this needs a live headless session — call api_server_start() once, then session_open('<absolute>.kicad_pcb'), before this tool can do anything; without them it returns ok=false with a hint naming the missing step.

| Parameter | Type | Required | Default |
| --- | --- | --- | --- |
| `item_id` | `str` | yes | — |

=== "Shell"

    ```bash
    phicad/bin/phicad-tool call session_get_item --arg item_id=<str>
    ```

### session_get_nets

```python
session_get_nets(with_items: bool = False) -> dict[str, Any]
```

`phicad/mcp/tools_session.py:354`

Every net on the board with its net class, sorted by name.

`with_items=True` adds `item_count` and `items_by_kind` per net, which costs one IPC round
trip per net — fine for a hundred nets, slow for thousands.

PRECONDITION: this needs a live headless session — call api_server_start() once, then session_open('<absolute>.kicad_pcb'), before this tool can do anything; without them it returns ok=false with a hint naming the missing step.

| Parameter | Type | Required | Default |
| --- | --- | --- | --- |
| `with_items` | `bool` | no | `false` |

=== "Shell"

    ```bash
    phicad/bin/phicad-tool call session_get_nets
    ```

### session_get_stackup

```python
session_get_stackup() -> dict[str, Any]
```

`phicad/mcp/tools_session.py:363`

The board's physical stackup plus its layer configuration.

Returns `enabled_layers` (the canonical names every other session tool accepts),
`renamed_layers` when the user gave a layer their own name, `copper_layer_count`, the
per-layer stackup rows (type, thickness_mm, material) and the summed `board_thickness_mm`.

PRECONDITION: this needs a live headless session — call api_server_start() once, then session_open('<absolute>.kicad_pcb'), before this tool can do anything; without them it returns ok=false with a hint naming the missing step.

=== "Shell"

    ```bash
    phicad/bin/phicad-tool call session_get_stackup
    ```

### session_bounding_box

```python
session_bounding_box(item_ids: list[str] | None = None) -> dict[str, Any]
```

`phicad/mcp/tools_session.py:373`

KiCad's own bounding box, in mm, for the named items or for the whole board.

With no `item_ids` every item on the board is measured and the union is returned — which
is how you find where a board actually sits before placing anything near it.  With ids,
each item's own box is reported too.  Note the union covers ALL layers, including
documentation graphics far outside the board outline.

PRECONDITION: this needs a live headless session — call api_server_start() once, then session_open('<absolute>.kicad_pcb'), before this tool can do anything; without them it returns ok=false with a hint naming the missing step.

| Parameter | Type | Required | Default |
| --- | --- | --- | --- |
| `item_ids` | `list[str] \| None` | no | `null` |

=== "Shell"

    ```bash
    phicad/bin/phicad-tool call session_bounding_box
    ```

### session_hit_test

```python
session_hit_test(x_mm: float, y_mm: float, layer: str | None = None, tolerance_mm: float = 0.0) -> dict[str, Any]
```

`phicad/mcp/tools_session.py:387`

Ask KiCad what is under a point — the reliable way to find the item at a location.

Every candidate item is hit-tested by KiCad itself (no geometry is reimplemented here),
which costs one IPC round trip per candidate: pass `layer` to cut the candidate set down,
or expect a whole-board probe to take seconds.  `tolerance_mm` grows the probe.

PRECONDITION: this needs a live headless session — call api_server_start() once, then session_open('<absolute>.kicad_pcb'), before this tool can do anything; without them it returns ok=false with a hint naming the missing step.

| Parameter | Type | Required | Default |
| --- | --- | --- | --- |
| `x_mm` | `float` | yes | — |
| `y_mm` | `float` | yes | — |
| `layer` | `str \| None` | no | `null` |
| `tolerance_mm` | `float` | no | `0.0` |

=== "Shell"

    ```bash
    phicad/bin/phicad-tool call session_hit_test --arg x_mm=<float> --arg y_mm=<float>
    ```

### session_selection

```python
session_selection(action: str = 'get', item_ids: list[str] | None = None) -> dict[str, Any]
```

`phicad/mcp/tools_session.py:399`

Read or change the editor selection: action get | set | add | clear.

Selection belongs to an editor FRAME, so on a headless `kicad-cli api-server` KiCad
refuses all four actions and this returns ok=false with `headless: true` — that is a
property of the session, not a bug.  It works when the socket points at a running PhiCad
GUI.  Selection is never written to disk.

PRECONDITION: this needs a live headless session — call api_server_start() once, then session_open('<absolute>.kicad_pcb'), before this tool can do anything; without them it returns ok=false with a hint naming the missing step.

| Parameter | Type | Required | Default |
| --- | --- | --- | --- |
| `action` | `str` | no | `"get"` |
| `item_ids` | `list[str] \| None` | no | `null` |

=== "Shell"

    ```bash
    phicad/bin/phicad-tool call session_selection
    ```

### session_title_block

```python
session_title_block(fields: dict[str, str] | None = None) -> dict[str, Any]
```

`phicad/mcp/tools_session.py:419`

Read the board's title block, or write it when `fields` is given.

Settable keys: title, date, revision, company, and `comments` as {"1": "text", ...}.
Unknown keys are refused with the valid list.  A write is verified by reading the title
block back and is reported through `applied`; it is session state until session_save().

PRECONDITION: this needs a live headless session — call api_server_start() once, then session_open('<absolute>.kicad_pcb'), before this tool can do anything; without them it returns ok=false with a hint naming the missing step.

| Parameter | Type | Required | Default |
| --- | --- | --- | --- |
| `fields` | `dict[str, str] \| None` | no | `null` |

=== "Shell"

    ```bash
    phicad/bin/phicad-tool call session_title_block
    ```

### session_update_footprint

```python
session_update_footprint(ref: str, x_mm: float | None = None, y_mm: float | None = None, rot_deg: float | None = None, side: str | None = None, locked: bool | None = None, value: str | None = None, reference: str | None = None, fields: dict[str, str] | None = None, relative: bool = False) -> dict[str, Any]
```

`phicad/mcp/tools_session.py:459` · **writes in place / changes state** (name starts with 'session_update')

Change one footprint, addressed by reference designator: any subset of its properties.

ABSOLUTE BY DEFAULT: `x_mm`/`y_mm` are where the footprint origin ENDS UP and `rot_deg` is
the orientation it ends up at.  With `relative=True` those three become DELTAS added to the
footprint's current placement (read back first, so the arithmetic is KiCad's numbers, not
yours) — "move it 2 mm left" is relative=True with x_mm=-2, "move it to x=50" is the
default.  `relative` affects ONLY x_mm/y_mm/rot_deg; a relative move larger than 500 mm is
refused as a units mistake.  The resolved absolute target is reported as `resolved_mm` /
`resolved_deg` next to the `delta` you asked for.

The rest: `side` = front|back (applied through KiCad's own FlipItems, so children mirror
properly — a side change is a SEPARATE transaction and therefore a second undo step),
`locked`, `value`, `reference` (the refdes itself), and `fields` for existing custom fields
only.

Property writes land in one commit; the whole thing is then re-read through a fresh board
handle and `applied` reflects that read-back, with `unapplied` naming anything that did
not stick.  Session state only: `saved` is false until session_save().

PRECONDITION: this needs a live headless session — call api_server_start() once, then session_open('<absolute>.kicad_pcb'), before this tool can do anything; without them it returns ok=false with a hint naming the missing step.

| Parameter | Type | Required | Default |
| --- | --- | --- | --- |
| `ref` | `str` | yes | — |
| `x_mm` | `float \| None` | no | `null` |
| `y_mm` | `float \| None` | no | `null` |
| `rot_deg` | `float \| None` | no | `null` |
| `side` | `str \| None` | no | `null` |
| `locked` | `bool \| None` | no | `null` |
| `value` | `str \| None` | no | `null` |
| `reference` | `str \| None` | no | `null` |
| `fields` | `dict[str, str] \| None` | no | `null` |
| `relative` | `bool` | no | `false` |

=== "Shell"

    ```bash
    phicad/bin/phicad-tool call session_update_footprint --arg ref=<str>
    ```

### session_batch_move

```python
session_batch_move(moves: list[dict[str, Any]]) -> dict[str, Any]
```

`phicad/mcp/tools_session.py:543` · **writes in place / changes state** (name starts with 'session_batch')

Move (and optionally rotate) many footprints to absolute positions in ONE undo step.

`moves` is [{"ref": "R1", "x_mm": 10.0, "y_mm": 20.0, "rot_deg": 90.0}, ...] — x_mm, y_mm
and rot_deg are each optional per row, so a row can rotate in place.  The whole batch is
one commit: it is pushed only if KiCad accepted it, dropped otherwise, so the board is
never left half-placed.  Every row is verified against a fresh read and reported with its
own `applied`; `unapplied_refs` names the failures.

PRECONDITION: this needs a live headless session — call api_server_start() once, then session_open('<absolute>.kicad_pcb'), before this tool can do anything; without them it returns ok=false with a hint naming the missing step.

| Parameter | Type | Required | Default |
| --- | --- | --- | --- |
| `moves` | `list[dict[str, Any]]` | yes | — |

=== "Shell"

    ```bash
    phicad/bin/phicad-tool call session_batch_move --arg moves=<list[dict[str, Any]]>
    ```

### session_add_track

```python
session_add_track(start_mm: list[float], end_mm: list[float], layer: str, width_mm: float, net: str | None = None) -> dict[str, Any]
```

`phicad/mcp/tools_session.py:570` · **writes in place / changes state** (name starts with 'session_add')

Add one straight copper track segment to the open board.

`layer` must be an enabled copper layer (session_get_stackup lists them; a bad name comes
back with the valid list).  `net` is an EXISTING net name — a track with no net is legal
and routes nothing, so pass the net you mean.  Zero-length segments are refused.

Returns the KIID KiCad assigned in `created_ids`, plus the item read back off a fresh
board in `created`.  Session state: `saved` is false until session_save().

PRECONDITION: this needs a live headless session — call api_server_start() once, then session_open('<absolute>.kicad_pcb'), before this tool can do anything; without them it returns ok=false with a hint naming the missing step.

| Parameter | Type | Required | Default |
| --- | --- | --- | --- |
| `start_mm` | `list[float]` | yes | — |
| `end_mm` | `list[float]` | yes | — |
| `layer` | `str` | yes | — |
| `width_mm` | `float` | yes | — |
| `net` | `str \| None` | no | `null` |

=== "Shell"

    ```bash
    phicad/bin/phicad-tool call session_add_track --arg start_mm=<list[float]> --arg end_mm=<list[float]> --arg layer=<str> --arg width_mm=<float>
    ```

### session_add_via

```python
session_add_via(at_mm: list[float], drill_mm: float, size_mm: float, net: str | None = None, start_layer: str = 'F.Cu', end_layer: str = 'B.Cu') -> dict[str, Any]
```

`phicad/mcp/tools_session.py:595` · **writes in place / changes state** (name starts with 'session_add')

Add one via at a position, with a drill and a pad diameter in mm.

`size_mm` is the copper pad diameter and must exceed `drill_mm`.  `start_layer`/`end_layer`
are the copper span the barrel drills (the default F.Cu..B.Cu is a through via); both must
be enabled on the board.  `net` is an existing net name.

Returns the new KIID plus the via read back from a fresh board (drill, diameter, span).
Writes to the SESSION only (`outputs: []`, `mutated: []`, `saved: false`); session_save()
is what writes the board file.

PRECONDITION: this needs a live headless session — call api_server_start() once, then session_open('<absolute>.kicad_pcb'), before this tool can do anything; without them it returns ok=false with a hint naming the missing step.

| Parameter | Type | Required | Default |
| --- | --- | --- | --- |
| `at_mm` | `list[float]` | yes | — |
| `drill_mm` | `float` | yes | — |
| `size_mm` | `float` | yes | — |
| `net` | `str \| None` | no | `null` |
| `start_layer` | `str` | no | `"F.Cu"` |
| `end_layer` | `str` | no | `"B.Cu"` |

=== "Shell"

    ```bash
    phicad/bin/phicad-tool call session_add_via --arg at_mm=<list[float]> --arg drill_mm=<float> --arg size_mm=<float>
    ```

### session_add_zone

```python
session_add_zone(polygon_mm: list[list[float]], layer: str, net: str | None = None, name: str | None = None, priority: int = 0) -> dict[str, Any]
```

`phicad/mcp/tools_session.py:623` · **writes in place / changes state** (name starts with 'session_add')

Add a copper zone from a closed polygon of [x_mm, y_mm] points (>= 3).

The polygon is closed for you.  `net` is the net the copper pours for — a zone with no net
is a bare copper area that connects nothing.  `priority` decides which zone wins where two
overlap (higher wins).

The zone is created UNFILLED: it contributes no connectivity until session_refill_zones().
Returns the new KIID and the zone read back from a fresh board.

PRECONDITION: this needs a live headless session — call api_server_start() once, then session_open('<absolute>.kicad_pcb'), before this tool can do anything; without them it returns ok=false with a hint naming the missing step.

| Parameter | Type | Required | Default |
| --- | --- | --- | --- |
| `polygon_mm` | `list[list[float]]` | yes | — |
| `layer` | `str` | yes | — |
| `net` | `str \| None` | no | `null` |
| `name` | `str \| None` | no | `null` |
| `priority` | `int` | no | `0` |

=== "Shell"

    ```bash
    phicad/bin/phicad-tool call session_add_zone --arg polygon_mm=<list[list[float]]> --arg layer=<str>
    ```

### session_add_text

```python
session_add_text(text: str, at_mm: list[float], layer: str, size_mm: float = 1.0, thickness_mm: float | None = None, rotation_deg: float = 0.0) -> dict[str, Any]
```

`phicad/mcp/tools_session.py:649` · **writes in place / changes state** (name starts with 'session_add')

Add free text to the open board on any enabled layer.

`size_mm` is the glyph height/width; `thickness_mm` defaults to 15% of it, which is
KiCad's own stroke-font ratio.  `rotation_deg` is counter-clockwise.  Text on a copper
layer is copper and will be checked by DRC like any other copper.

Returns the new KIID plus the text read back from a fresh board.  Writes to the SESSION
only (`outputs: []`, `mutated: []`, `saved: false`); session_save() writes the board file.

PRECONDITION: this needs a live headless session — call api_server_start() once, then session_open('<absolute>.kicad_pcb'), before this tool can do anything; without them it returns ok=false with a hint naming the missing step.

| Parameter | Type | Required | Default |
| --- | --- | --- | --- |
| `text` | `str` | yes | — |
| `at_mm` | `list[float]` | yes | — |
| `layer` | `str` | yes | — |
| `size_mm` | `float` | no | `1.0` |
| `thickness_mm` | `float \| None` | no | `null` |
| `rotation_deg` | `float` | no | `0.0` |

=== "Shell"

    ```bash
    phicad/bin/phicad-tool call session_add_text --arg text=<str> --arg at_mm=<list[float]> --arg layer=<str>
    ```

### session_add_graphic

```python
session_add_graphic(kind: str, layer: str, width_mm: float, points_mm: list[list[float]] | None = None, center_mm: list[float] | None = None, radius_mm: float | None = None, start_mm: list[float] | None = None, end_mm: list[float] | None = None) -> dict[str, Any]
```

`phicad/mcp/tools_session.py:680` · **writes in place / changes state** (name starts with 'session_add')

Add a graphic shape: kind = line | rect | circle | arc | poly.

Which arguments matter depends on `kind`:
    line    start_mm + end_mm
    rect    start_mm + end_mm (opposite corners; normalised for you)
    circle  center_mm + radius_mm
    arc     start_mm + end_mm + center_mm (the short way round; KiCad stores start/mid/end)
    poly    points_mm, at least 3 points, closed for you
`width_mm` is the stroke width.  On Edge.Cuts these shapes ARE the board outline; on a
copper layer they are copper.

Returns the new KIID plus the shape read back from a fresh board.  Writes to the SESSION
only (`outputs: []`, `mutated: []`, `saved: false`); session_save() writes the board file.

PRECONDITION: this needs a live headless session — call api_server_start() once, then session_open('<absolute>.kicad_pcb'), before this tool can do anything; without them it returns ok=false with a hint naming the missing step.

| Parameter | Type | Required | Default |
| --- | --- | --- | --- |
| `kind` | `str` | yes | — |
| `layer` | `str` | yes | — |
| `width_mm` | `float` | yes | — |
| `points_mm` | `list[list[float]] \| None` | no | `null` |
| `center_mm` | `list[float] \| None` | no | `null` |
| `radius_mm` | `float \| None` | no | `null` |
| `start_mm` | `list[float] \| None` | no | `null` |
| `end_mm` | `list[float] \| None` | no | `null` |

=== "Shell"

    ```bash
    phicad/bin/phicad-tool call session_add_graphic --arg kind=<str> --arg layer=<str> --arg width_mm=<float>
    ```

### session_delete_items

```python
session_delete_items(item_ids: list[str]) -> dict[str, Any]
```

`phicad/mcp/tools_session.py:749` · **writes in place / changes state** (name starts with 'session_delete')

Delete the given items from the open board by KIID, in one undo step.

Ids that do not exist are reported in `missing_ids` rather than silently dropped, and the
delete is verified by proving the ids are gone from a fresh read (`still_present` names any
item KiCad refused to remove).  Session state until session_save().

PRECONDITION: this needs a live headless session — call api_server_start() once, then session_open('<absolute>.kicad_pcb'), before this tool can do anything; without them it returns ok=false with a hint naming the missing step.

| Parameter | Type | Required | Default |
| --- | --- | --- | --- |
| `item_ids` | `list[str]` | yes | — |

=== "Shell"

    ```bash
    phicad/bin/phicad-tool call session_delete_items --arg item_ids=<list[str]>
    ```

### session_delete_by_filter

```python
session_delete_by_filter(kind: str, layer: str | None = None, net: str | None = None, dry_run: bool = True) -> dict[str, Any]
```

`phicad/mcp/tools_session.py:762` · **writes in place / changes state** (name starts with 'session_delete')

Delete every item of a kind, optionally narrowed to a layer and/or net.

DRY RUN BY DEFAULT: with `dry_run=True` nothing is touched and the answer is
`would_delete` plus a sample of up to 50 items, because "delete every track on F.Cu" is
one keystroke from erasing a board's routing.  Pass `dry_run=False` to actually apply it;
the deletion is one commit and is verified against a fresh read.

PRECONDITION: this needs a live headless session — call api_server_start() once, then session_open('<absolute>.kicad_pcb'), before this tool can do anything; without them it returns ok=false with a hint naming the missing step.

| Parameter | Type | Required | Default |
| --- | --- | --- | --- |
| `kind` | `str` | yes | — |
| `layer` | `str \| None` | no | `null` |
| `net` | `str \| None` | no | `null` |
| `dry_run` | `bool` | no | `true` |

=== "Shell"

    ```bash
    phicad/bin/phicad-tool call session_delete_by_filter --arg kind=<str>
    ```

### session_set_net

```python
session_set_net(item_ids: list[str], net_name: str) -> dict[str, Any]
```

`phicad/mcp/tools_session.py:777` · **writes in place / changes state** (name starts with 'session_set')

Assign an existing net to the given items (tracks, vias, zones, pads, copper shapes).

`net_name` must already exist on the board — this does not create nets (an unknown name
comes back with the known list).  Pass "" to clear an item's net.  Item kinds that cannot
carry a net are reported in `skipped` instead of failing the call.  One commit, verified
by re-reading each item's net from a fresh board.

PRECONDITION: this needs a live headless session — call api_server_start() once, then session_open('<absolute>.kicad_pcb'), before this tool can do anything; without them it returns ok=false with a hint naming the missing step.

| Parameter | Type | Required | Default |
| --- | --- | --- | --- |
| `item_ids` | `list[str]` | yes | — |
| `net_name` | `str` | yes | — |

=== "Shell"

    ```bash
    phicad/bin/phicad-tool call session_set_net --arg item_ids=<list[str]> --arg net_name=<str>
    ```

### session_set_enabled_layers

```python
session_set_enabled_layers(layer_names: list[str]) -> dict[str, Any]
```

`phicad/mcp/tools_session.py:792` · **writes in place / changes state** (name starts with 'session_set')

Set the board's enabled layer set. DESTRUCTIVE and NOT UNDOABLE through this API.

Pass the complete list of layers that should be enabled, canonical names ("F.Cu", "In1.Cu",
"Edge.Cuts", ...).  The copper layer count is derived from the copper names in the list and
must be even and >= 2.  ANY CONTENT ON A LAYER YOU LEAVE OUT IS DELETED — KiCad also keeps
a few layers enabled regardless, which the result reports as `extra_enabled`.

Verified by reading the enabled set back; `missing_requested` names anything KiCad refused.
Writes to the SESSION only (`outputs: []`, `mutated: []`, `saved: false`) — but the deleted
content is gone from the session immediately; session_save() writes the board file.

PRECONDITION: this needs a live headless session — call api_server_start() once, then session_open('<absolute>.kicad_pcb'), before this tool can do anything; without them it returns ok=false with a hint naming the missing step.

| Parameter | Type | Required | Default |
| --- | --- | --- | --- |
| `layer_names` | `list[str]` | yes | — |

=== "Shell"

    ```bash
    phicad/bin/phicad-tool call session_set_enabled_layers --arg layer_names=<list[str]>
    ```

### session_set_origin

```python
session_set_origin(x_mm: float, y_mm: float, which: str = 'grid') -> dict[str, Any]
```

`phicad/mcp/tools_session.py:810` · **writes in place / changes state** (name starts with 'session_set')

Move the grid origin (`which='grid'`) or the drill/place-file origin (`which='drill'`).

The drill origin is what exported drill and placement files are measured from, so changing
it changes every subsequent fabrication export.  Verified by reading the origin back.
Writes to the SESSION only (`outputs: []`, `mutated: []`, `saved: false`); session_save()
writes the board file.

PRECONDITION: this needs a live headless session — call api_server_start() once, then session_open('<absolute>.kicad_pcb'), before this tool can do anything; without them it returns ok=false with a hint naming the missing step.

| Parameter | Type | Required | Default |
| --- | --- | --- | --- |
| `x_mm` | `float` | yes | — |
| `y_mm` | `float` | yes | — |
| `which` | `str` | no | `"grid"` |

=== "Shell"

    ```bash
    phicad/bin/phicad-tool call session_set_origin --arg x_mm=<float> --arg y_mm=<float>
    ```

### session_refill_zones

```python
session_refill_zones() -> dict[str, Any]
```

`phicad/mcp/tools_session.py:824` · **writes in place / changes state** (name starts with 'session_refill')

Refill every zone on the board and report which ones actually ended up filled.

Blocking and genuinely slow on a dense board (minutes), which is why this has its own
long timeout.  A zone that stays unfilled usually has no net, no island big enough to
keep, or sits outside the board outline — the per-zone `filled` flags are the evidence.
The new fills are written to the SESSION only (`outputs: []`, `mutated: []`,
`saved: false`); session_save() writes them into the board file.

PRECONDITION: this needs a live headless session — call api_server_start() once, then session_open('<absolute>.kicad_pcb'), before this tool can do anything; without them it returns ok=false with a hint naming the missing step.

=== "Shell"

    ```bash
    phicad/bin/phicad-tool call session_refill_zones
    ```

### session_run_action

```python
session_run_action(action_name: str) -> dict[str, Any]
```

`phicad/mcp/tools_session.py:836`

Run a named KiCad TOOL_ACTION in the session (escape hatch for anything unexposed).

UNSTABLE by KiCad's own warning: action names are not API and change between versions.
A headless `kicad-cli api-server` has no editor frame, so it refuses RunAction outright and
this returns ok=false with `headless: true`; it works against a running PhiCad GUI.  Even
then, RAS_OK only means the action was dispatched — re-read the board to see if it did
anything.

PRECONDITION: this needs a live headless session — call api_server_start() once, then session_open('<absolute>.kicad_pcb'), before this tool can do anything; without them it returns ok=false with a hint naming the missing step.

| Parameter | Type | Required | Default |
| --- | --- | --- | --- |
| `action_name` | `str` | yes | — |

=== "Shell"

    ```bash
    phicad/bin/phicad-tool call session_run_action --arg action_name=<str>
    ```

### session_revert

```python
session_revert() -> dict[str, Any]
```

`phicad/mcp/tools_session.py:851` · **writes in place / changes state** (name starts with 'session_revert')

Discard every unsaved session change by reloading the board from its file on disk.

NOT RECOVERABLE: unsaved edits are gone.  A headless api-server refuses RevertDocument
(it needs an editor frame), so this returns ok=false with `headless: true` and the
workaround it names — session_close() then session_open() on the same path reloads the
file, which is what revert would have done.

PRECONDITION: this needs a live headless session — call api_server_start() once, then session_open('<absolute>.kicad_pcb'), before this tool can do anything; without them it returns ok=false with a hint naming the missing step.

=== "Shell"

    ```bash
    phicad/bin/phicad-tool call session_revert
    ```

## Verify

### verify_capabilities

```python
verify_capabilities() -> dict[str, Any]
```

`phicad/mcp/tools_verify.py:169`

What PhiCad Verify can do on THIS machine, with tool versions. No side effects.

Call this first. Every field is a checked fact rather than a claim: whether the pinned
tscircuit engine is installed, whether this fork's kicad-cli was found and at what
version, and the locked dependency versions that will produce any artifact.

PhiCad Verify contains no model runtime, no provider UI and no credentials -- phi
is the only AI harness. The KiCad step is an independent CAD check, not a
fabrication-readiness verdict.

=== "Shell"

    ```bash
    phicad/bin/phicad-tool call verify_capabilities
    ```

### verify_check

```python
verify_check(source_path: str, timeout_s: int = 300) -> dict[str, Any]
```

`phicad/mcp/tools_verify.py:185`

Evaluate a tscircuit TSX design and return its diagnostics. No CAD check.

This is the fast inner loop for an agent: it runs the pinned engine over `source_path`
and returns the design census plus tscircuit's own typed diagnostics, translated into
PhiCad findings with stable ids.

A finding's `id` is derived from its code, refs and rounded position -- not its wording --
so the same problem keeps the same id across runs and a waiver stays granted.

| Parameter | Type | Required | Default |
| --- | --- | --- | --- |
| `source_path` | `str` | yes | — |
| `timeout_s` | `int` | no | `300` |

=== "Shell"

    ```bash
    phicad/bin/phicad-tool call verify_check --arg source_path=<str>
    ```

### verify_build

```python
verify_build(source_path: str, timeout_s: int = 300) -> dict[str, Any]
```

`phicad/mcp/tools_verify.py:205`

Build a TSX design, then check the exported board with KiCad's DRC.

Returns TWO independent result sets that are never merged:

  `engine`       -- the tscircuit build and its own diagnostics
  `verification` -- this fork's kicad-cli reading the exported .kicad_pcb

The engine reporting success is not evidence the board is sound; that is the whole reason
the second check exists and is reported separately. `verification.skipped` is true when
kicad-cli is absent, and is NEVER a pass -- a checker that could not run is not a checker
that was satisfied.

The KiCad step evaluates the board against the design rules recorded in the exported file.
It is an independent CAD check, not a fabrication-readiness verdict; no fabricator's
process or acceptance criteria are consulted. It is also not a manufacturing export -- that
is `kicad-cli pcb export` in the same native PhiCad application.

| Parameter | Type | Required | Default |
| --- | --- | --- | --- |
| `source_path` | `str` | yes | — |
| `timeout_s` | `int` | no | `300` |

=== "Shell"

    ```bash
    phicad/bin/phicad-tool call verify_build --arg source_path=<str>
    ```

### verify_circuit_json

```python
verify_circuit_json(circuit_json_path: str, output_dir: str, result_path: str = '', timeout_s: int = 300) -> dict[str, Any]
```

`phicad/mcp/tools_verify.py:233`

Check a SUPPLIED Circuit JSON with the independent KiCad path. No TSX re-evaluation.

This is the Platform service operation. Use it when you already hold the Circuit JSON and
want it checked -- as opposed to `verify_build`, which evaluates a `.tsx` source first.

The difference matters and is not cosmetic. Re-evaluating a source is not guaranteed to
reproduce the artifact you are holding: the engine mints random element ids and orders
diagnostics by whichever supplier lookup finished first. Checking a board the caller never
saw, then reporting it as the answer to their question, is a confident wrong answer. So this
operation exports **the document you pass** and never reads a TSX file (`source_hash` is
always null).

The caller's FILENAME is not part of the contract: any regular `.json` path works. (The
pinned engine detects Circuit JSON by basename, so the service stages a private copy inside
`output_dir` and removes it before enumerating artifacts. `circuit_json_hash` is always the
hash of the original bytes you supplied, never of the copy.)

Result shape (service contract v1, stable): `artifacts` is a list of paths relative to
`output_dir`; `artifact_hashes` is keyed by those same relative paths; `tool_versions` holds
exact scalar version strings only (`tscircuit`, `@tscircuit/eval`, `circuit-json`,
`kicad-cli`, `bun`, `service-contract`) for a machine-verifiable version lock, while
resolved binary paths live in `tool_paths` -- a path is not a version. `result_path` may
live inside `output_dir` and is never listed as an artifact of the run that produced it.

`output_dir` must be FRESH. A directory that already holds artifacts is refused
(`verify.output_dir_not_fresh`) rather than mixed in, because a stale file in the result
would make the artifact manifest a lie. Nothing is written outside it, and every artifact
path in the result is relative to it with a sha256 in `artifact_hashes`.

Diagnostic ORIGINS are preserved, never merged, and there are EXACTLY TWO: `kicad` for what
this checker independently produced, and `tscircuit` for a diagnostic found inside the
supplied document and relayed unchanged. The enum is CLOSED -- any other value, including the
retired `openpcb` lane, fails closed as `verify.invalid_origin`, because accepting a value is
itself a contract and a consumer must not be handed an origin this service can never justify.
Origins are assigned here, never read from the document, so an element carrying its own
`origin` cannot inject one. `ok` reflects the independent KiCad check only.

The KiCad step is an independent CAD check against the design rules recorded in the exported
board -- not a fabrication-readiness verdict.

| Parameter | Type | Required | Default |
| --- | --- | --- | --- |
| `circuit_json_path` | `str` | yes | — |
| `output_dir` | `str` | yes | — |
| `result_path` | `str` | no | `""` |
| `timeout_s` | `int` | no | `300` |

=== "Shell"

    ```bash
    phicad/bin/phicad-tool call verify_circuit_json --arg circuit_json_path=<str> --arg output_dir=<str>
    ```

### verify_launch_targets

```python
verify_launch_targets() -> dict[str, Any]
```

`phicad/mcp/tools_verify.py:291`

Which other product views a project can be handed to, and whether they resolve here.

Returns the versioned launch contract and one entry per view: `studio` (PhiCad
Studio), `source` (the authored tscircuit code), `phi` (a phi session) and `docs`
(the platform documentation).

`resolvable` is a checked fact about THIS machine, not an aspiration. A view with no
installed application and no configured platform launcher reports `resolvable: false` with
a reason, because a deep link that silently fails is worse than one that admits it cannot
resolve. Configure with PHICAD_STUDIO, PHICAD_PLATFORM_LAUNCHER and PHICAD_PLATFORM_DOCS.

=== "Shell"

    ```bash
    phicad/bin/phicad-tool call verify_launch_targets
    ```

### verify_open_view

```python
verify_open_view(source_path: str, view: str = 'source', timeout_s: int = 300) -> dict[str, Any]
```

`phicad/mcp/tools_verify.py:308`

Emit a manifest-bound launch request so another view can open the SAME revision.

`view` is one of `studio`, `source`, `phi`, `docs`.

This EMITS a request and opens nothing. PhiCad Verify does not own the other applications
and must not mutate them: the PhiCad Platform launcher decides whether and how to honour a
request. No duplicate desktop shell is involved, and no application is launched from here.

The request carries project identity, manifest identity, the expected content hash and the
desired view -- and **never credentials** (`carriesCredentials: false`, asserted by a test
over the serialised payload). A receiver is expected to reject a stale or mismatched hash;
Verify never reconciles revisions and never does last-writer-wins, because silently picking
a revision for someone is how two tools come to disagree about what the board is.

The returned `target.resolvable` tells you whether the requested view actually exists on
this machine. A request for an unresolvable view is still returned, so a caller can show
the intended navigation honestly rather than a dead link.

| Parameter | Type | Required | Default |
| --- | --- | --- | --- |
| `source_path` | `str` | yes | — |
| `view` | `str` | no | `"source"` |
| `timeout_s` | `int` | no | `300` |

=== "Shell"

    ```bash
    phicad/bin/phicad-tool call verify_open_view --arg source_path=<str>
    ```

### verify_manifest

```python
verify_manifest(source_path: str, timeout_s: int = 300) -> dict[str, Any]
```

`phicad/mcp/tools_verify.py:341`

Build a design and return the artifact manifest: files, digests and tool provenance.

Use this when the answer has to be auditable. Each artifact carries its size, a raw
sha256, and a content digest computed with the engine's generated element ids removed --
tscircuit mints a random nanoid per diagnostic, so two builds of one unchanged source
differ byte-for-byte while describing the identical design.

`manifestId` is deterministic for a given source and toolchain: it is derived from the
source digest, the pinned tool versions and the sorted artifact content digests, and
excludes paths, timestamps and durations. Two runs of the same design agree; any real
change to the design or the toolchain changes it.

`boundary` records that Circuit JSON is a versioned build/interchange artifact rather than
authored source -- it is not lossless and is not intended to be round-tripped back to TSX.

| Parameter | Type | Required | Default |
| --- | --- | --- | --- |
| `source_path` | `str` | yes | — |
| `timeout_s` | `int` | no | `300` |

=== "Shell"

    ```bash
    phicad/bin/phicad-tool call verify_manifest --arg source_path=<str>
    ```
