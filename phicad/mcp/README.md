# phicad MCP server — the fork's own chat-tool surface

A single MCP server that exposes **this build tree's** KiCad capabilities as agent tools, grouped
into the sections below: DRC/ERC gates, renders and every fabrication export, semantic diffs, BOMs,
jobsets, board structure, a managed IPC (`api-server`) session with a full item-level editing
surface, the schematic read/write surface, and a per-project git journal that versions what the
tools do. CAD operations run against the configured fork build, defaulting to `build/mac-dev`; they
support `version 20260831` files that stable KiCad 10.0 refuses to open. The full MCP registry
also contains optional foreground panel tools and GUI-only session operations; it is not an
enforced background-only allowlist.

The live size of the surface is `phicad/bin/phicad-tool list --json | jq .tool_count`. This file
deliberately does not carry that number: it changed with every tool-adding commit and was wrong in
four of them. `test_registry.py` fails on any `N tools` claim in this file that disagrees with the
registry, so if you do write one it has to be true.

One surface, three front doors: an MCP client (chat), `phicad tool` / `phicad-tool` in a shell,
or a python caller through `registry.py`. All three invoke **the same functions** — the CLI reads
the live FastMCP registry and calls `tool.fn` in-process, so nothing is reimplemented and nothing
can drift.

Where it sits among the four channels: see [../ai-interfacing.md](../ai-interfacing.md). This is
Channel 3, implemented in-repo, on top of Channels 1 (IPC), 2 (kicad-cli) and 4 (kicad-tools).

## Why its own venv

`build/venv` (kipy + kicad-tools) pins **protobuf<6**, which is incompatible with the MCP stack.
So this server:

- lives in **`build/mcp-venv`** (fastmcp, mcp), and
- **never imports kipy in-process** — it shells out to `build/venv/bin/python` for kipy /
  kicad-tools work and to `kicad-cli` for everything else.

Do not merge the two venvs.

## Setup

```bash
phicad/bin/phicad venv            # creates build/venv with kipy
phicad/bin/regen-kipy-proto       # replaces kipy's stable-KiCad protobuf bindings
bash phicad/mcp/setup.sh          # creates build/mcp-venv, pip installs (proxy env stripped)
python3 phicad/mcp/doctor.py      # one-command health check (kicad-cli, venvs, kipy protos, mcp.json, orphans)
python3 phicad/mcp/register.py --build <build> --venv <venv> --mcp-venv <mcp-venv>  # writes the `phicad` key
build/mcp-venv/bin/python phicad/mcp/test_handshake.py --full [--autoroute]   # real end-to-end smoke test
```

Source and build routinely live in different worktrees. Every entry point honours the same
overrides — `PHICAD_REPO`, `PHICAD_BUILD`, `PHICAD_VENV`, `PHICAD_KICAD_CLI` (and
`PHICAD_MCP_VENV` for the doctor/setup) — and `doctor.py --json` reports where each path came
from. `doctor.py` exits non-zero on anything that would make a tool fail.

Requirements: python ≥ 3.10 (setup picks Homebrew's `python3.13`; macOS system python 3.9 is too
old — override with `PYTHON=/path/to/python3.x`), a built `kicad-cli`, and `build/venv`
(`phicad/bin/phicad venv`) for kipy / kicad-tools subprocesses.

### Regenerate kipy's fork bindings

Run **`phicad/bin/regen-kipy-proto` after `phicad venv` and after every `api/proto/` change**.
kipy 0.8 ships bindings generated from stable KiCad, so fork commands — most importantly
`OpenDocument` — cannot even be constructed by the packaged client. That client-side gap produced
the misleading server-looking error `no handler available for request of type
kiapi.common.commands.OpenDocument`.

Do not reproduce this by hand. The tool handles three non-obvious constraints:

1. `build/venv` pins `protobuf<6`, while Homebrew `protoc` 36 emits a runtime-7.x gencode check
   that protobuf 5.x refuses to import. The tool uses the runtime-matched compiler from
   `grpcio-tools` instead.
2. A parallel generated tree on `sys.path` fails with `duplicate file name
   common/types/enums.proto`: protobuf's default descriptor pool rejects re-registration. The
   generated files must **replace** kipy's installed `proto/` tree.
3. protoc emits absolute intra-tree imports; kipy's package layout requires relative imports.
   The tool rewrites them and adds compatibility aliases for **25** names still imported by
   kipy's Python layer: `TeardropType` / `TeardropSettings`, `RuleCheckerMarker`,
   `TeardropTarget`, `SaveDocument`, and 20 rules symbols moved into `board_rules.proto`.

It keeps `proto.bak-<timestamp>` next to the installed tree, then verifies the replacement by
importing kipy and round-tripping a fork-only `OpenDocument` message.

### Register with a client

**`phicad mcp register` is the path** — `--dry-run` first if you want to see the entry before it
lands. It writes only the `phicad` key of `~/.pi/agent/mcp.json` (backup kept, `chmod 600`), filled
in for the checkout you run it from:

```bash
phicad/bin/phicad mcp register --dry-run   # print the entry, change nothing
phicad/bin/phicad mcp register             # write it
phicad/bin/phicad mcp doctor               # confirm the client config the server will see
```

The shape it writes, for reference (`register.py` also adds `_comment`, `type`, `toolMode` and
`injectInstructions`; every path is absolute):

```json
{
  "phicad": {
    "command": "/ABSOLUTE/PATH/TO/PhiCad/build/mcp-venv/bin/python",
    "args": ["/ABSOLUTE/PATH/TO/PhiCad/phicad/mcp/server.py"],
    "env": {
      "PHICAD_REPO": "/ABSOLUTE/PATH/TO/PhiCad",
      "PHICAD_BUILD": "/ABSOLUTE/PATH/TO/PhiCad/build/mac-dev",
      "PHICAD_VENV": "/ABSOLUTE/PATH/TO/PhiCad/build/venv",
      "PHICAD_KICAD_CLI": "/ABSOLUTE/PATH/TO/PhiCad/build/mac-dev/kicad/PhiCad.app/Contents/MacOS/kicad-cli"
    }
  }
}
```

Every path must be absolute, and they must point at a worktree that has actually been built
(a source-only checkout has no `build/`, and every tool then fails with "kicad-cli not built"). **Put the `PHICAD_*` overrides in the entry's `env` block** (that is
what `register.py` writes): the MCP stdio client sanitises the child environment down to
`HOME/PATH/USER/SHELL/TERM/LOGNAME`, so shell exports never reach the server, and without them
it falls back to `<repo>/build/...` — which does not exist when the build tree is in another
worktree. The server sets `KICAD_RUN_FROM_BUILD_DIR=1` itself and strips the LLM proxy vars
from every child. Outputs land in `$TMPDIR/phicad-mcp-out/<tool>/`; override the root with
`PHICAD_MCP_OUT`.

## Tools — the section map

The surface is one flat namespace of plain functions, grouped into sections by the banner they
sit under (`phicad-tool list --section <key>`). This table is the map; the per-tool contract is
generated from the live registry into
[`phicad/docs-site/docs/reference/mcp-tools-generated.md`](../docs-site/docs/reference/mcp-tools-generated.md).

The table carries no per-section tool count on purpose — `phicad-tool list --section <key>` counts
them for you and cannot be stale. Every `--section` key below is checked against the live registry
by `test_registry.py` (the audit found a `repo-build-state` key here that never existed; the real
one is `host-integration-repo-state`).

| Section (`--section`) | Backing | What it is for |
| --- | --- | --- |
| `gates` | `kicad-cli pcb drc` / `sch erc --format json` | the pass/fail gate an agent must not skip |
| `visual-fabrication-exports` | `kicad-cli pcb/sch export …` | render, SVG, gerbers, STEP, BOM, stats, pos, PDF, netlist |
| `semantic-diffs` | `pcb diff` / `sch diff --format json` | verify a change instead of trusting it (exit 5 = differs) |
| `jobsets` | `kicad-cli jobset run` | the repeatable agent → fab handoff |
| `board-summary` | `build/venv` + `kicad_tools.PCB` | one structural read of a board |
| `ipc-api` | managed `kicad-cli api-server` | starting the one managed headless child |
| `headless-session-surface` | that child + kipy | open-by-path, live reads, footprint edits, save, stop, `kipy_exec` |
| `session` | same managed session, item-level API | the full live-editing surface: items, tracks, vias, zones, text, graphics, nets, layers, origin, selection, hit-test, refill, revert |
| `schematic` | `sch_worker.py` + `phicad/pylib/sexp_edit.py` | the schematic read AND write surface, byte-faithful |
| `git` | `phicad/pylib/project_history.py` | the per-project git journal: init, snapshot, log, diff, restore, undo, tag |
| `per-project-git-history` | the board project's own Git repository | explicit checkpoints, history, diffs and restores |
| `cli` | every remaining `kicad-cli` subcommand | libraries (fp/sym), gerber inspection, the long tail of exports, importers, upgrades, `kicad_cli` escape hatch |
| `autoroute` | `phicad/autoroute/*` + Freerouting | DSN out → route → SES back in, cleanup, BGA stitching |
| `verify` | `phicad/core/*` (pinned tscircuit) + this fork's `kicad-cli` | PhiCad Verify: build a `.tsx` design, then check the exported board independently — two verdicts, never merged; plus the manifest-bound cross-view launch contract (emits requests, opens nothing) |
| `parts-pipeline-layout-swarm` | `phicad/parts/`, `phicad/swarm/` | source exact parts, gate fab-readiness, partition/score/merge for parallel routing |
| `design-synthesis` | `phicad/synth/cli.py` | design IR → placed, routed, DRC-clean project; and re-gate an existing board |
| `host-integration-repo-state` | protocol metadata + `phicad status` | the host/model handshake, and which build tree, branch and venv the server is actually using |

Representative tools, with what they return:

| Tool | Backing | Returns |
| --- | --- | --- |
| `pcb_drc(board_path, severity_all=True)` | `kicad-cli pcb drc --format json` | `summary.violations.total`, `by_severity`, `by_type` + examples, `summary.unconnected_items.real_open_count` / `zone_island_count` / `ambiguous_count`, `unconnected_classification`, raw items, `report_path` |
| `sch_erc(sch_path, severity_all=True)` | `kicad-cli sch erc --format json` | same shape (ERC nests per sheet — flattened), `sheet_count`, `report_path` |
| `pcb_diff(a, b)` / `sch_diff(a, b)` | `pcb/sch diff --format json` | `differs` / `identical`, `change_count`, `changes_by_kind`, `changes_by_type`, per-item before/after |
| `pcb_render` / `pcb_export_svg` / `pcb_export_gerbers` / `pcb_export_step` | `kicad-cli pcb …` | `png_path`+bytes; `svg_files[]`; `files[]`+`file_count`; `step_path`+`missing_models[]` |
| `pcb_export_drill(board_path, fmt='excellon', …)` | `pcb export drill` | `report.holes` / `report.tools` parsed out of the file it just wrote |
| `pcb_export_stackup` / `pcb_layers` | `pcb export stackup` / s-expression parse | material stackup; the layer table plus a ready-made `--layers` argument |
| `pcb_export_odb` / `pcb_export_ipc2581` / `pcb_export_ipcd356` / `pcb_export_gencad` | `pcb export …` | the fab/assembly/test handoffs beyond gerbers |
| `pcb_export_ibom(board_path, ibom_dir=None)` | `pcb export pos` + `ipcd356` + Edge.Cuts `svg` → [InteractiveHtmlBom](https://github.com/openscopeproject/InteractiveHtmlBom) as a subprocess | an interactive assembly BOM (one self-contained HTML) built through ibom's *generic JSON* input — no `import pcbnew` anywhere; `report` carries the component count, per-side counts, pads, nets and the `warnings` listing what this path cannot know. ibom is **not vendored**: point `ibom_dir`/`$PHICAD_IBOM` at a clone, or put one at `<build>/InteractiveHtmlBom`. The document is built by `phicad/pylib/ibom_json.py` (stdlib, gated by `check fast`) |
| `gerber_info` / `gerber_diff` / `gerber_convert_png` | GerbView kiface | read fab output back the way a fab house does |
| `fp_export_svg` / `fp_diff` / `sym_export_svg` / `sym_diff` / `fp_upgrade` / `sym_upgrade` | `kicad-cli fp/sym …` | library-level rendering, semantic diffs and format upgrades |
| `pcb_import` / `sch_import` / `pcb_upgrade` / `sch_upgrade` | `kicad-cli … import/upgrade` | foreign formats in; fork-format rewrites (`in_place=True` is the only way they touch your file) |
| `jobset_run(jobset_path, project_path)` | `kicad-cli jobset run` | `jobs[]`, `output_paths[]`, `jobs_succeeded`/`jobs_failed`, `failures[]` |
| `board_summary(board_path)` | `build/venv` + `kicad_tools.PCB` | size/area/layer & item counts, routing status, footprint list, net names |
| `sch_list_symbols` / `sch_get_symbol` / `sch_list_sheets` / `sch_find` / `sch_list_nets` | `sch_worker.py` (stdlib s-expression parse) | placed symbols with fields and positions, the sheet hierarchy, real pin-level connectivity |
| `sch_symbol_lib_list` / `sch_symbol_lib_get` | same | what is in a `.kicad_sym`, down to pins and properties |
| `sch_set_field` / `sch_set_fields_bulk` / `sch_place_symbol` / `sch_add_wire` / `sch_add_label` / `sch_add_no_connect` / `sch_delete_symbol` | `phicad/pylib/sexp_edit.py` | a NEW file by default (`out_path == sch_path` to edit in place), `changes[]`, and `loads: true` only after the fork's own reader re-parsed it |
| session tools (`session_open`, `session_board_query`, `session_list_items`, `session_hit_test`, …) | managed `api-server` + kipy | live board facts — geometry and connectivity, not pixels |
| session writers (`session_update_footprint`, `session_add_track/via/zone/text/graphic`, `session_batch_move`, `session_delete_*`, `session_set_net`, `session_refill_zones`, `session_revert`, `session_save`) | kipy `update_items` / commits | `applied` is true only when a fresh read-back matches, so the copy-semantics trap is a reported failure and never a silent no-op; nothing hits disk until `session_save()` |
| `project_init` / `project_snapshot` / `project_status` / `project_log` / `project_journal` | `phicad/pylib/project_history.py` | standalone repo or project-scoped enclosing repo: baseline commit, revisions with the actor and tool behind each, the action journal |
| `project_diff` / `project_restore` / `project_undo_last` / `project_tag` / `project_watch_once` | same + `pcb/sch diff` | `git diff --stat` **plus** the fork's semantic diff between two revisions; recovery, revert, milestones, and the save watcher |
| `pcb_export_dsn` / `pcb_import_ses` / `autoroute_freerouting` / `autoroute_cleanup` / `autoroute_stitch` | `phicad/autoroute/*` | the Specctra round trip, a real headless route with before/after unconnected + violation counts, debris removal, BGA fanout |
| `parts_fetch` / `parts_audit` | `phicad/parts/*` | exact parts (symbol + footprint + 3D) into a project library; the fab-readiness gate |
| `swarm_partition` / `swarm_score` / `swarm_merge` | `phicad/swarm/*` | disjoint net groups for parallel routers, one comparable float per candidate, lossless copper merge |
| `synth_check` / `synth_build` / `synth_verify` | `phicad/synth/cli.py` | design-IR validation, the full placed/routed/DRC'd build, and the gates re-run against an existing board |
| `kicad_cli_help` / `kicad_cli` | the fork's own binary | the escape hatch: any subcommand's real flags, and an arbitrary argv with the outputs it produced |
| `integration_capabilities()` | protocol metadata + in-repo JSON Schemas | host/model handshake: command and event transports, capability names, schema IDs/paths, approval requirement |
| `phicad_status()` | `phicad/bin/phicad status` | branch/head/upstream/build/venv/api-server, plus `extensions` (which `tools_*.py` modules are wired) |

`pcb_drc` keeps KiCad's `unconnected_items` byte-for-byte in `raw_unconnected_items` and adds a
separate `unconnected_classification` block. Each finding is classified as `real_open`,
`zone_island`, or `ambiguous` using board UUID object types first, structured item types second,
and KiCad item descriptions last. The block exposes `real_open_count`, `zone_island_count`,
`ambiguous_count`, per-item evidence/confidence/reason, and `no_real_opens`. `clean` is only true
for a report with no connectivity findings; a zone-only report is `zone_island_only`, and an
unknown endpoint is `uncertain`, never clean. Typed `isolated_copper` and `starved_thermal`
violations are retained as zone/island findings rather than being miscounted as routed opens.

Every tool: absolute paths only (relative inputs resolve against the repo root), a bounded
subprocess timeout, a structured dict result, never raises — failures come back as
`{"ok": false, "error": …}`. Every tool that writes returns `outputs` (files it created) and
`mutated` (pre-existing files it changed in place), which is what the git journal below keys on.

**Reads are free; writes are opt-in.** Every writer defaults to a NEW file under
`$TMPDIR/phicad-mcp-out/<tag>/`. Touching the caller's file requires saying so — `out_path` equal
to the input, `in_place=True`, or `apply=True` — and then the path appears in `mutated`. That is
the whole safety model: a tool cannot quietly rewrite a board you asked it to analyse.

### The per-project git journal

A standalone KiCad project gets its own git repository — never this source repo. When a board
project is inside an existing checkout, `project_init` reuses the nearest repository instead of
creating a nested `.git`; snapshots are scoped to the project subdirectory so unrelated monorepo
files are not staged. An existing checkout path without one unambiguous KiCad project is refused
with an explanation. `project_init` writes project metadata, commits a baseline, writes
`.phicad/journal.jsonl`, and installs the fork's **structural merge driver** (below). From then
on, every mutating
tool call is recorded automatically: `server.py` wraps each writer, works out which files on disk
it actually changed (from `mutated` / `outputs`, else the in-place input), appends a journal entry,
and commits a snapshot whose subject names the tool —
`phicad(mcp:sch_set_field): sch_set_field` — with the actor in the trailer. The result dict grows a
`project_history` block saying what happened:

```json
{"journaled": true, "committed": true, "commit": "051fc8f…", "short": "051fc8f",
 "changed_files": ["pic_programmer.kicad_sch"], "insertions": 9, "deletions": 0}
```

- **What gets committed**: only files inside a project that has been `project_init`-ed. Exports
  under `$TMPDIR/phicad-mcp-out/` are outside any project repo and are simply not journaled
  (`{"journaled": false, "reason": "no on-disk files reported"}`) — an analysis run leaves no
  commits behind.
- **What is journaled but NOT committed**: a failed call (`ok: false`) is journaled and never
  snapshotted, so history contains no commit for a change that did not happen.
- **The repo is safe to merge**: `project_init` writes a `.gitattributes` routing
  `*.kicad_pcb` / `*.kicad_sch` / `*.kicad_sym` / `*.kicad_mod` to `kicad-cli git-mergedriver`
  and sets the per-repo `merge.kicad-*` config — the same thing KiCad does for repositories
  created from the GUI. Git's default *text* merge on a 600 KB s-expression board does not
  merely conflict: it writes `<<<<<<<` into the file and KiCad then refuses to load it at all.
  With the driver, edits to different footprints merge clean keeping both, and edits to the
  same one still conflict — a human decides — but the board left behind stays loadable. Both
  halves are append-only, so a `merge=binary` opt-out or a `merge.tool=meld` preference
  survives. Reported as `merge_driver`; `installed: false` carries a `reason` (usually no built
  `kicad-cli`) and means merges fall back to text merge — worth checking before handing one
  project repo to a second agent.
- **The git tools journal themselves** rather than being wrapped, so `project_restore` records the
  rollback as its own `phicad(restore): …` commit instead of letting the watcher describe a
  rollback as an edit.
- **Opting out**: `PHICAD_TOOL_SNAPSHOT=0` in the environment disables the auto-commit; the
  journal entry is still written, so you keep the audit trail without the commits.
  `phicad-tool call --project-snapshot` sets it to `1` explicitly for one call. The default is on.
- **Nothing is ever rewritten.** `project_undo_last` uses `git revert`, and both it and
  `project_restore` are **dry-run by default** — pass `dry_run=False` to actually move bytes.
- `project_diff` is the reason this is more than `git log`: it returns `git diff --stat` **and**
  the fork's own semantic diff of the two revisions' boards/schematics (`exit_code: 5`,
  `differs: true`, the changed properties), because "9 insertions" is not a review and
  "R1 Value 10K → 12K" is. **`phicad project diff` does the same thing** — the semantic step lives
  in `project_history.attach_semantic()`, which shells out to the fork's `kicad-cli` (resolved from
  `PHICAD_KICAD_CLI`, else `PHICAD_BUILD`, else the build tree beside the checkout) so the stdlib-only
  module stays stdlib-only. With no built `kicad-cli` it degrades: the git stat and the extracted
  revisions still come back, with `semantic_skipped` naming the reason. `--no-semantic` turns it off.

The same surface is a shell command — see `phicad project` in the CLI table below.

### The write surfaces, and which one to reach for

Three different mechanisms can change a design. They are not interchangeable:

| Surface | Mechanism | Use it when | Cost |
| --- | --- | --- | --- |
| `session_*` (sections `session` + `headless-session-surface`) | live IPC into a managed headless `api-server`, real commits and undo steps | you are editing a **board**: geometry, copper, zones, nets, layers — anything KiCad's own model should validate | needs the child process; nothing is on disk until `session_save()` |
| `sch_*` writers (section `schematic`) | s-expression span splicing (`phicad/pylib/sexp_edit.py`) | you are editing a **schematic**: fields, symbols, wires, labels | text-level, so the fork's reader is the only validator — every write is load-checked with `sch erc` |
| `pcb_import` / `*_upgrade` / `autoroute_*` / `swarm_merge` / `synth_build` | whole-file transforms | you are converting, routing or generating rather than editing | writes a new file unless you ask for in place |

The schematic side is text because there is no alternative: kipy 0.8 can open a `.kicad_sch` and
little else, and eeschema's reader lives inside a kiface DSO no Python can import. The splice
approach was chosen after measuring the round trip on `demos/pic_programmer`: kiutils **drops**
tokens it does not model (`do_not_autoplace` 641→0, `hide` 391→0, 229 kB → 178 kB), kicad-skip
keeps every token but reflows the whole file (16 830 diff lines), and `sexp_edit.py` is
byte-identical on a no-op load+save and produces a one-line diff for a one-field change. So a
schematic write is reviewable by `git diff`, which is what makes the journal above useful.

## Same tools from a shell — `phicad-tool`

An agent in a terminal (or a CI job, or a human debugging one tool) has no MCP client. It does
not need one: `phicad/bin/phicad-tool` calls **the same python functions the server registers**,
so there is one implementation and no second behaviour to keep in sync.

```bash
phicad/bin/phicad-tool list                       # every tool, grouped by section (W = writes)
phicad/bin/phicad-tool list --section gates       # one section; --mutating for writers only
phicad/bin/phicad-tool list --json                # the whole registry, schemas included
phicad/bin/phicad-tool describe pcb_drc           # signature, JSON schema, full docstring
phicad/bin/phicad-tool call pcb_drc --arg board_path=/tmp/b.kicad_pcb
phicad/bin/phicad-tool call pcb_export_pos --arg board_path=/tmp/b.kicad_pcb \
                                           --arg fmt=csv --arg smd_only=yes
phicad/bin/phicad-tool call swarm_merge --json '{"base_board": "/tmp/b.kicad_pcb", …}'
echo '{"board_path": "/tmp/b.kicad_pcb"}' | phicad/bin/phicad-tool call pcb_drc --stdin
phicad/bin/phicad-tool docs -o                    # regenerate the generated reference page
phicad/bin/phicad-tool doctor | selftest | registry-test | serve
```

**Exit codes are the contract** — a shell script branches on them without parsing JSON:

| Code | Meaning |
| --- | --- |
| `0` | the tool ran and returned `ok: true` |
| `1` | the tool ran and returned `ok: false` (a real EDA failure: DRC missing, no venv, bad board) |
| `2` | usage error: unknown tool, unknown/missing parameter, uncoercible value, malformed `--json` |

Stdout is always the result dict as JSON (`--raw` for one compact line); stderr is empty on a
clean run, so anything there is a genuine problem.

`--arg key=value` values are coerced **by the tool's own JSON Schema**, not guessed:
`severity_all=false` reaches a `bool` as `False` (the string `"false"` would be truthy),
`passes=100` as `int`, `planes=GND,VCC` or `planes='["GND","VCC"]'` as `list[str]`,
`take='{"g0":"/tmp/c0.kicad_pcb"}'` as `dict`, and a `str` parameter is taken verbatim so a board
path that happens to look like JSON survives. `--dry-run` prints the coerced arguments without
calling. `--project-snapshot` sets `PHICAD_TOOL_SNAPSHOT=1` for the call, which the server-side
git journal hook uses to snapshot the project before a mutating tool touches it.

The script resolves `PHICAD_REPO` / `PHICAD_BUILD` / `PHICAD_VENV` / `PHICAD_KICAD_CLI` exactly
like `phicad/bin/phicad` (plus `PHICAD_MCP_VENV`, default `$REPO/build/mcp-venv`) and exports
them, so the driver, the server module and `phicad status` all report one build tree.

The same three surfaces are reachable through the main helper, which is what a human types:

| Command | Does | Notes |
| --- | --- | --- |
| `phicad tool list \| describe \| call \| docs` | the whole MCP surface from a shell | a shorthand for the same `phicad_tool.py` driver — identical semantics and exit codes |
| `phicad project init \| status \| snapshot \| log \| diff \| restore \| undo \| tag \| journal \| watch \| remote \| push` | the per-project git journal | stdlib-only (`phicad/pylib/project_history.py`), so it works with no venv at all; `--json` on any subcommand, `watch --loop` is the save watcher, and `diff` runs the fork's semantic `pcb/sch diff` as a subprocess (`--no-semantic` to skip, `--kicad-cli` to point it somewhere) |
| `phicad mcp setup \| register \| doctor \| serve \| selftest \| docs` | server lifecycle | `doctor` is the health check, `register` writes only the `phicad` key of the client config |

`phicad project` takes the project directory **or any file in it** as its first positional
argument (default: the current directory), so `phicad project log board.kicad_pcb` works from
inside a project. Full walkthrough, side by side with the chat and `kicad-cli` ways of doing the
same three tasks: [Driving PhiCad from a shell or an agent](../docs-site/docs/guides/driving-phicad.md).

### The registry

`phicad/mcp/registry.py` is the single description of the surface, derived from the live FastMCP
instance (`await mcp.get_tools()` → `tool.parameters` for the schema, `tool.fn` for the
callable). `collect(server_module)` returns a `ToolSpec` per tool with its name, section (the
banner comment it sits under in `server.py`, or its `tools_*.py` module), signature, parameter
schema, docstring and a `mutates` verdict; `render_markdown` / `render_json` produce the docs
page and `list --json`. Four consumers, one list — the CLI, the docs, the self-test and any
python caller.

`mutates` is name- and docstring-based (no execution): `session_*` writers, `autoroute_*`,
`*_import*`, `swarm_merge`, `synth_build`, `project_snapshot/restore/undo/tag`, `sch_set/add/
delete/place`, `pcb_upgrade/import`, or a docstring saying "in place". `describe` prints the rule
that fired, so a surprising verdict traces to one line of `registry.py`.

### The three tests, and what each one proves

All three are plain asserts with a summary line and exit 0/1 — no pytest (the MCP venv has no test
framework and must not grow one for this). Run them with `build/mcp-venv/bin/python`.

| Test | Proves | Summary line |
| --- | --- | --- |
| `test_registry.py` | the SHAPE of the surface is legal: docstring present with a real summary, every parameter and the return fully type-hinted, no duplicate names, the published schema covering every parameter, every **mutating** tool's docstring saying where its bytes go, every tool under a real section — plus the CLI end to end (`list --json` parses, `describe`, a real `call`, all three exit codes, docs rendering), a staleness check that fails if the generated page was not regenerated, and the DOC-CONSISTENCY gates below | `all <N> checks passed` |
| `test_tools.py [--quick\|--full]` | the TOOLS work: imports `server.py` in-process, asserts every extension module configured, then runs a representative tool per section against /tmp copies of `demos/` and checks the parsed numbers (DRC violation count, drill holes, layer table, symbol list, semantic diff). Also the integration proof: a throwaway project is `project_init`-ed, mutated through a real tool, and the snapshot commit + journal entry are read back out of **real git** | `all <N> checks passed across <M> stages [full]` |
| `test_handshake.py [--full] [--autoroute] [--sections]` | the SERVER and its transport: spawns `server.py` as an MCP stdio subprocess and calls tools as a client does, forwarding the `PHICAD_*` env block explicitly (the stdio client strips shell environment). `--sections` lists every check and which flag enables it | `all checks passed — <N>/<M> sections` |

Three of `test_registry.py`'s checks gate **this file and the other three hand-written docs**
(`phicad/ai-interfacing.md`, `docs-site/docs/reference/mcp-tools.md`,
`docs-site/docs/guides/driving-phicad.md`), because every one of them had drifted from the code:

- **no `### tool_name(` heading** in `mcp-tools.md` — per-tool prose belongs on the generated page,
  which has a byte-exact staleness gate; the hand-written page has none and rotted;
- **every `--section <key>` token in this README is a real section key** — `repo-build-state` was
  documented here and errors when you run it;
- **every `N tools` / `all N` claim in those four files matches the live registry count** — so the
  honest options are "no number" or "the right number", and nothing in between survives CI.

`phicad-tool registry-test` / `selftest` and `phicad mcp selftest` are the front doors for the
first and third; `test_tools.py` is run directly. A new tool is expected to pass `test_registry.py`
and to gain a section assertion in `test_tools.py`.

### Semantics worth knowing

- **`pcb_diff` / `sch_diff` exit 5 means "documents differ"** — a normal result, not an error.
  `ok` is true for both 0 and 5; read `differs` / `identical`.
- **Schematic parity keeps raw and effective counts separate.** With
  `pcb_drc(schematic_parity=True)`, `raw_schematic_parity`, the on-disk KiCad report, and
  `summary.raw_total_violations` retain every finding. `summary.effective_total_violations`
  ignores only `intentional_no_connect`: the exact `Pad missing net given by schematic
  (unconnected-(...-PadN))` shape, one UUID-resolved unnetted board pad, and one matching
  no-connect pin in a temporary KiCad-exported sibling-schematic netlist. A generic
  `net_conflict`, real missing/board-only/differing net, netted lookalike, malformed report, or
  missing evidence remains active; unresolved candidates produce `gate_status: uncertain`.
- **`pcb_export_step` exits 2 when component 3D models are missing** (the build tree has no
  `${KICAD10_3DMODEL_DIR}` library) yet still writes a valid board-body STEP. Reported as
  `ok: true` with `missing_model_count`.
- **`jobset_run` needs the `.kicad_pro`**, not the board, so `${KIPRJMOD}` resolves; run it from
  the project dir (the server does). `exit_code == 0` is not the whole story — the parsed
  `jobs_succeeded` / `jobs_failed` tally is authoritative. Jobset DRC `severity` is a bitmask,
  not an enum ordinal; the starter uses `52` (`error | warning | exclusion`). Treat a generated
  `drc.json` whose `included_severities` omits `error` as a misconfigured gate, even if it reports
  zero findings. Starter jobset:
  [examples/fab_pack.kicad_jobset](examples/fab_pack.kicad_jobset) (DRC JSON + gerbers + drill → `fab/`).
- **Board projects own their Git repositories.** Generated projects retain the design IR and
  start with a baseline commit. Agent View autosnapshots stable saves; `project_git_snapshot`
  names explicit verified checkpoints. Runtime bridge state and KiCad lock/backups are ignored.
- **One managed api-server at a time.** The server's session tools own one background,
  document-less `kicad-cli api-server`, then open boards/schematics by absolute path with
  `OpenDocument`; no GUI, window focus or screenshots are involved. `--socket` gets a *plain
  path* (the server wraps it in `ipc://`) while kipy gets `KICAD_API_SOCKET=ipc://<path>`. The
  socket appears before readiness, so startup polls `Ping`; the child is killed on MCP exit.
- **Never call `KiCad().check_version()`** in a session script: kipy 0.8's version guard still
  says KiCad 10.0.6 and raises `FutureVersionError` against the 10.99 fork. Regenerating the
  protobuf files adds the fork messages; it does not change that guard. Plain commands work.
- **No DRC/ERC over IPC** — that is why the gates are CLI-backed.
- **Copy the whole project before any session that may save.** Opening alone is byte-identical,
  but `SaveDocument` / `board.save()` rewrites the board as `version 20260831` and also rewrites
  its sibling `.kicad_pro` (`pcbnew/api/headless_pcb_context.cpp:94-113`); stable KiCad 10.0 then
  refuses the board. A QA fixture was silently
  dirtied this way during development.
- **Read writes back before trusting `ISC_OK`.** Tracks use `Modify()` + `CopyFrom()`; footprints
  and groups instead use a commit `Remove()` + `Add()` because replacing child geometry in place
  leaves a live editor view's caches stale (`pcbnew/api/api_handler_board.cpp:439-465`). Current
  headless verification persisted both a track-width update and a footprint move, but the special
  replacement path deserves an immediate re-read plus `pcb diff` before touching source files.

### GUI attach versus a managed headless session

Enabling `api.enable_server` in the GUI creates `/tmp/kicad/api.sock`. **In this fork the GUI can
also open documents on request**: the project manager installs an `OpenDocument` handler, so a
client can ask the running app to load a board or schematic and then talk to the editor's own
handlers. Upstream (and this fork before that change) left the callback unset, so `OpenDocument`
returned `AS_UNIMPLEMENTED` (`common/api/api_handler_common.cpp`) and the GUI socket was limited
to `Ping`, version and netclasses.

Which one to use:

| | GUI socket (`/tmp/kicad/api.sock`) | managed headless `kicad-cli api-server` |
| --- | --- | --- |
| Opens documents | yes (fork-only) | yes |
| Sees what the user has on screen | yes — same document, live | no, separate process |
| Disturbs the user | no focus stealing, but it *does* switch their project | none |
| Blocked by unsaved changes | yes, refuses with `AS_BUSY` (see below) | n/a |
| `RunAction` | GUI-only commands work in an editor | registered but rejected as GUI-only (`pcbnew/api/api_handler_board.cpp:500-516`) |

Two GUI-specific caveats:

- **A cold open takes ~2-3.5 s**, which exceeds kipy's 2000 ms default. Use
  `KiCad(timeout_ms=30000)` or the call will time out on a perfectly healthy server.
- **Switching projects is refused while any editor has unsaved changes** (`AS_BUSY`, naming the
  editor). This is deliberate: closing a modified editor raises a modal "save your changes?"
  dialog, and because API requests are dispatched on the main thread that modal's nested event
  loop would block the whole server — including `Ping` — until a human clicked it. Save or revert,
  then retry. A board with no sibling `.kicad_pro` is likewise refused rather than having a
  project file fabricated in the user's directory.

Prefer a managed headless session for unattended work (no user-visible side effects, no modal
risk); attach to the GUI when the point is to act on the document the user is actually looking at.

## Layout

```
phicad/mcp/
├── server.py                      # the MCP server: the base tools, the extension wiring, the git journal hook
├── tools_cli.py                   # every remaining kicad-cli subcommand (libraries, gerber, the long-tail exports)
├── tools_session.py               # the live item-level editing surface over the managed IPC session
├── tools_schematic.py             # schematic + symbol-library reads AND writes (via sch_worker.py)
├── sch_worker.py                  # stdlib s-expression worker; one process, one JSON object per call
├── tools_git.py                   # the per-project git journal tools (via phicad/pylib/project_history.py)
├── tools_autoroute.py             # Freerouting / cleanup / stitch / DSN-SES tools
├── doctor.py                      # stdlib health check: paths, kicad-cli, venvs, kipy protos, mcp.json, orphans
├── register.py                    # writes only the `phicad` key of ~/.pi/agent/mcp.json (backup + chmod 600)
├── registry.py                    # the shared tool registry: name/section/schema/doc/mutates per tool
├── phicad_tool.py                 # the CLI: list / describe / call / docs / selftest / doctor / serve
├── requirements.txt               # pinned fastmcp
├── setup.sh                       # build/mcp-venv + proxy-safe pip install
├── test_registry.py               # registry + CLI contract gate (docstrings, hints, schemas, exit codes)
├── test_tools.py                  # in-process end-to-end: one real tool per section + the git-journal proof
├── test_handshake.py              # real stdio-client smoke test (--full = exports + IPC, --sections lists checks)
└── examples/                       # fabrication jobset + managed-session walkthroughs
```

Each `tools_*.py` exposes `configure(ctx) -> {"ok": …}` and a `TOOLS` tuple; `server.py` imports
them, registers each function, and records the outcome in `phicad_status().extensions`, so a module
that fails to configure degrades that section instead of taking the server down. Two stdlib-only
modules live outside this directory because non-MCP callers need them: `phicad/pylib/sexp_edit.py`
(the byte-faithful s-expression splicer) and `phicad/pylib/project_history.py` (the journal, which
is also `phicad project`).

## Verified — run the suites, do not read a number here

This section used to paste the check counts, violation counts and hole counts from one particular
run. Every one of them was a number describing the *previous* commit's surface within a week, so
they are gone on purpose. The suites are cheap; run them and read the summary line:

```bash
export TMPDIR=/tmp/phicad-mine/tmp PHICAD_MCP_OUT=/tmp/phicad-mine/out   # isolate: fixed paths
mkdir -p "$TMPDIR" "$PHICAD_MCP_OUT"
build/mcp-venv/bin/python phicad/mcp/test_registry.py        # or: phicad tool registry-test
build/mcp-venv/bin/python phicad/mcp/test_tools.py --full
build/mcp-venv/bin/python phicad/mcp/test_handshake.py --full  # or: phicad mcp selftest --full
phicad/bin/phicad check                                     # all of the above, as CI runs it
```

The **assertions** are the durable record, and they live in the suites rather than in prose: every
number the old block quoted (`pcb_drc` violations by severity and type, `pcb_export_drill` holes and
tools, the `pcb_layers` table, `sch_list_symbols` counts per sheet and across the hierarchy,
`swarm_score`, `pcb_export_dsn` nets/placements, `session_board_query` footprints and nets over IPC)
is a real `check(...)` in `test_tools.py` / `test_handshake.py` against copies of `demos/microwave`
and `demos/pic_programmer` under `$TMPDIR`. If one changes, the suite fails with the new value —
which is exactly what a paragraph here could not do.

What those suites prove qualitatively, and which is worth knowing before you trust the surface:

- **the shape of the registry is legal and the generated reference is current** — including the
  doc-consistency gates above, so this README cannot name a `--section` key that does not exist and
  cannot claim a tool count the registry disagrees with (`test_registry.py`);
- **one real tool per section runs against real boards** and its parsed numbers are asserted, with
  all extension modules configured (`test_tools.py --full`);
- **the git-journal integration end to end**: `project_init` → one baseline commit → an in-place
  `sch_set_field` → the hook journaled it (`actor: mcp:sch_set_field`) and committed exactly one
  snapshot whose subject names the tool, with real `git` agreeing with `project_status` /
  `project_log` on HEAD, the commit count and the journal length; `project_diff HEAD~1 HEAD`
  returning both the stat and a semantic `sch diff` (`exit 5`, `differs`); and
  `project_restore --apply` bringing the pre-edit bytes back **and committing the rollback**;
- **the stdio transport itself** — the catalogue after `initialize`, the gates, a ~34 kB schematic
  result document (which is what caught a truncating hop in the worker plumbing), the fabrication
  exports, a jobset run, and the IPC lifecycle from document-less start through `OpenDocument` to
  stop with no GUI launched (`test_handshake.py --full`; `--sections` lists every check and the flag
  that enables it).

## Background-only execution contract

Use the owned `api_server_start` child, never the user's editor socket. CAD gates, renders,
exports, typed item edits and headless Freerouting are the supported paths. Each concurrent
worker needs its own board copy, MCP process/managed session and unique socket; a server owns
one managed child and that child holds one project at a time. Save and independently re-read
changes; do not mistake a successful IPC call for evidence that a copied object changed.

Do not call session selection, `session_run_action`,
or `session_revert` in a background workflow. GUI-only failures must remain unsupported;
ignore hints suggesting attachment to a desktop editor. A deliberate close/reopen of an owned
session can discard unsaved changes; saved checkpoints supply artifact rollback.

`kipy_exec` and `kicad_cli` are developer escape hatches, not constrained product tools. The
draft background profile (an unpublished design proposal) proposes
an enforced allowlist. It is not implemented by these documentation changes. Return generated
artifacts and reports through the host rather than opening applications or browsers.
