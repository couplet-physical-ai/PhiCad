# AI-native interfacing — driving PhiCad from agents and chat

How an AI agent creates, edits, analyzes, and fabricates KiCad designs against **this fork's
build** — no GUI required. Everything here is verified against the real `build/mac-dev` tree
(kicad-cli 10.99.0). See also: [architecture.md](architecture.md) §automation,
[testing.md](testing.md).

## The four channels, ranked

| Channel | What it's for | Write access | Version safety |
| --- | --- | --- | --- |
| **1. IPC API** (`phicad api-server` + kipy) | headless open-by-path, inspection and transactional editing | yes — transactional | fork-native |
| **2. kicad-cli** (`phicad run kicad-cli …`) | DRC/ERC, exports, diffs, renders, jobsets | no (converters aside) | fork-native |
| **3. MCP servers** (chat tools **and** `phicad tool` in a shell) | tool-call surface for agents (phi/Claude) and for scripts | via 1 & 2 underneath, plus its own schematic + git layers | see boundary below |
| **4. S-expression libs** (kicad-skip, kiutils) | direct file surgery, batch rewrites | yes — file level | version-sensitive |

Agent View is a client of these channels, not a fifth board API: its agent reaches the board
through MCP/IPC/CLI as usual. Each board project owns a Git repository with automatic stable-save
snapshots.

Legacy SWIG `pcbnew` python is **not** in this list: the fork does not build it, and it is the
one channel that can silently rewrite a 10.99-format file with a 10.0 serializer. Anything
SWIG-based stays on the stable app and stable-format files only.

## Channel 1 — the IPC API (the fork-native authoring path)

The fork serves a protobuf API over NNG (`ipc://` unix socket): `kicad/cli/command_api_server.cpp`,
`common/api/api_server.cpp`, protos in `api/proto/`. The official client is
[kicad-python](https://pypi.org/project/kicad-python/) (`import kipy`).

kipy 0.8's packaged protobuf bindings describe stable KiCad and cannot construct newer fork
commands, above all `OpenDocument`. Prepare the client after creating/recreating the venv and
after every proto change:

```bash
phicad venv
phicad/bin/regen-kipy-proto      # replaces installed kipy/proto, keeps a timestamped backup, verifies
```

The generator uses `grpcio-tools`' runtime-matched protoc (Homebrew protoc 36 emits a runtime-7.x
check that the venv's protobuf 5.x rejects), replaces rather than shadows kipy's tree (a parallel
tree collides in protobuf's default descriptor pool), fixes protoc's absolute imports, and adds
25 compatibility aliases needed by kipy's Python layer. Without it the client-side binding gap
looked like a server failure: `no handler available for request of type
kiapi.common.commands.OpenDocument`.

Start the server with no document, then open copied boards or schematics by absolute path over
IPC. This is a background model session: no GUI, window focus or screenshots.

```bash
cp -R /path/to/project /tmp/phicad-agent-project   # copy the whole project, not only the board
phicad api-server &                 # document-less, socket ipc:///tmp/phicad-api.sock
phicad py open_and_inspect.py       # venv python, KICAD_API_SOCKET preset
```

`open_and_inspect.py` can issue the fork command directly (kipy 0.8 has no high-level wrapper for
it yet):

```python
from kipy import KiCad
from kipy.proto.common.commands.project_commands_pb2 import OpenDocument, OpenDocumentResponse
from kipy.proto.common.types import DocumentType

k = KiCad()
request = OpenDocument(type=DocumentType.DOCTYPE_PCB,
                       path="/tmp/phicad-agent-project/board.kicad_pcb")
k._client.send(request, OpenDocumentResponse)
b = k.get_board()
print(len(b.get_footprints()), len(b.get_tracks()), len(b.get_nets()), len(b.get_stackup().layers))
```

Verified in one document-less session against a copy of `demos/microwave`: 4 footprints, 0 tracks,
1 net and 9 stackup layers — whatever the board holds, read back over IPC. (Numbers are the board's,
not a contract: on a routed board the same script prints hundreds. `demos/pic_programmer` is the
case to know about — `get_footprints()` raises `NotImplementedError` there, see the drift note
below.) Preloading a path at server launch also remains supported; runnable example:
[examples/ipc_quickstart.py](examples/ipc_quickstart.py).

Socket contract (both halves verified):

- server `--socket` takes a **plain path** — `common/api/api_server.cpp:113` wraps it into
  `ipc://…` itself at `:159`. Passing the URL form creates a literal `ipc:/` directory.
- kipy reads `KICAD_API_SOCKET` **verbatim** and wants the `ipc://` URL
  (`kipy/kicad.py:_default_socket_path`). `phicad api-server`/`phicad py` translate.

Minimal write session (ran against a copy of `demos/microwave` — the text landed in the saved
file):

```python
from kipy import KiCad
from kipy.board_types import BoardText
from kipy.geometry import Vector2
from kipy.proto.board import board_types_pb2

k = KiCad()                      # connects via KICAD_API_SOCKET
b = k.get_board()
commit = b.begin_commit()
t = BoardText()
t.value = "PHICAD-AI-SMOKE"
t.layer = board_types_pb2.BoardLayer.Value('BL_F_SilkS')   # MUST set a real layer
t.position = Vector2.from_xy_mm(50, 50)   # ASSIGN a Vector2 (see gotchas)
b.create_items(t)
b.push_commit(commit, "add text")
b.save()
```

Gotchas (all hit for real — fuller catalogue in the wave's IPC report):

- items with layer 0 (`BL_UNKNOWN`) are accepted by `create_items` but **silently dropped at
  save** — always set a layer;
- **`t.position.x_nm = …` is a silent no-op**: kipy wrapper `.position` getters return a *copy*
  (and expose `.x`/`.y`, not `.x_nm`), so mutating it never reaches the item — assign a whole
  `Vector2` (`t.position = Vector2.from_xy_mm(50, 50)`);
- `board.get_items()` needs an iterable of types, not `None`;
- kipy 0.8's `import kipy.schematic` is broken on import (self-inconsistent packaging) — the
  board API is the usable kipy surface; use Channel 2 for schematic facts;
- **copy the entire project before a session that may save.** Opening alone is byte-identical,
  but `SaveDocument` / `board.save()` upgrades the board (`version 20260831`,
  `generator_version "10.99"`) **and rewrites its sibling `.kicad_pro`**. Stable KiCad 10.0 will
  refuse the board afterward; this silently dirtied a QA fixture during development;
- kipy 0.8's version guard still targets KiCad **10.0.6** (`kipy/kicad_api_version.py`), even
  after regenerating its protobuf files. `check_version()` raises `FutureVersionError` against
  the 10.99 fork, but plain `KiCad()` + commands work (verified: ping/read/write/save all fine);
  don't call `check_version()`;
- **`update_items` on a footprint `ImportNetlist` created in the same session destroys its
  3D models.** Measured on a generated board: `(model …)` count 11 before the move, 0 after,
  with every other node — pads, properties, graphics, attrs — byte-identical. DRC does not
  look at 3D, so the board passes every gate and the 3D viewer shows bare pads on an empty
  board. The obvious generalisation is wrong and worth knowing: the same call on a board
  **opened from disk** preserves them (11 -> 11, single item or batched), so `/move` in the
  AI View pane and anything else editing an existing board is unaffected. Re-opening the
  document mid-session to dodge it is refused (`a document of this type is already open`).
  If you must place footprints that a netlist import just created, edit the saved file's
  `(transform …)` instead — `synth/emit.py:set_positions()`.
- **read every mutation back before trusting `ISC_OK`.** Tracks take the ordinary `Modify()` +
  `CopyFrom()` path; footprints and groups take commit `Remove()` + `Add()` because replacing
  their children in place leaves live-view geometry caches stale
  (`pcbnew/api/api_handler_board.cpp:439-465`). Current headless verification persisted a track
  width change and a footprint move, but replacement-type writes still need an immediate
  re-read and the Channel 2 diff gate;
- concrete drift case: the fork emits `kiapi.board.types.ReferencePoint` items inside footprint
  definitions; kipy 0.8 has no mapping, so **`get_footprints()` raises `NotImplementedError`
  on boards whose footprints carry them** (hit live on `demos/pic_programmer` from the pcbnew
  GUI). Reads that don't unwrap footprint *definitions* (`get_items` on tracks/pads/text) and
  all writes still work;
- **no DRC/ERC over IPC** — the proto surface only has marker *types*; run `kicad-cli pcb drc`;
- **export jobs live in `kiapi.board.jobs` / `kiapi.schematic.jobs`**, not `.commands` — wrong
  package looks like "unsupported" (`AS_UNHANDLED`). `AS_OK` ≠ job success: check
  `RunJobResponse.status` too. SVG/PDF/DXF jobs need explicit `plot_settings.layers`;
- some commands are GUI-only: `GetSelection`, visible/active layer get/set →
  `AS_UNIMPLEMENTED "not available in headless mode"`;
- server startup: the socket exists **before** the server is ready — poll `Ping` until it stops
  returning `AS_NOT_READY` (~1 s small boards). Reuse ONE connection; rapid reconnects wedge
  NNG req/rep state (upstream QA says the same in `qa/tests/api/api_e2e_utils.h`);
- `--socket` collisions: a stale socket left by a dead server is recovered automatically (the
  server takes an `api.lock` flock next to it and unlinks the orphan —
  `common/api/api_server.cpp:121-159`); if the path is held by a **live** server it silently falls
  back to `api-<pid>.sock` in the same dir. One socket path per server.

### GUI attach vs a managed headless session

Both can open documents in this fork — pick the one that matches the intent.

The GUI project manager serves `/tmp/kicad/api.sock` when `api.enable_server` is true, and this
fork installs an `OpenDocument` handler on it (`kicad/kicad.cpp`), so a client can ask the
**running app** to load a board or schematic; the editor that opens then registers the
document-specific handlers, so `get_board()` and friends work against what the user sees.
Upstream leaves that callback unset, in which case `OpenDocument` returns `AS_UNIMPLEMENTED`
(`common/api/api_handler_common.cpp`) and only `kicad-cli api-server`
(`kicad/cli/command_api_server.cpp:443-445`) can open documents.

Use the **GUI socket** when the task is about the document already in front of the user. Use a
**managed headless server** for unattended work: it has no user-visible side effects and cannot
trip the modal hazard below.

Three things to know before driving the GUI:

- **Cold open takes ~2-3.5 s** — longer than kipy's 2000 ms default. Construct the client as
  `KiCad(timeout_ms=30000)`.
- **Project switches are refused (`AS_BUSY`) while any editor has unsaved changes**, with an
  error naming the editor. Closing a modified editor raises a "save your changes?" modal, and
  since API requests are dispatched on the main thread, that modal's nested event loop would
  block the entire server — `Ping` included — until someone clicked it. Save or revert and retry.
- **A board with no sibling `.kicad_pro` is refused**, rather than fabricating a project file in
  the user's directory as a side effect of an innocuous-looking request.

`RunAction` remains headless-hostile: after a board opens in a headless server its board handler
registers the command but rejects it as GUI-only
(`pcbnew/api/api_handler_board.cpp:500-516`); in an attached GUI editor it runs.

## Channel 2 — kicad-cli: analysis, exports, diffs, jobsets

Full verified cookbook incl. real outputs: the jobsets/kicad-cli report (summarized here).

- **Gates**: `pcb drc --format json`, `sch erc --format json` — machine-readable
  (`schemas.kicad.org/drc.v1.json`); exit 0 even with violations unless
  `--exit-code-violations`. The `phicad` `pcb_drc` wrapper preserves raw
  `unconnected_items` and also reports deterministic `real_open_count`,
  `zone_island_count`, and `ambiguous_count` with per-item UUID/type evidence. With
  `schematic_parity=True`, raw parity findings and `raw_total_violations` remain unchanged;
  `effective_total_violations` ignores only an exact KiCad-generated schematic no-connect
  pseudo-net whose UUID resolves to an unnetted board pad. The sibling schematic is exported
  to a temporary netlist to prove the pin's no-connect type; missing or contradictory evidence,
  and every other `net_conflict`, stays active. The wrapper returns `gate_status: uncertain`
  rather than claiming clean when evidence cannot be identified; `no_real_opens` is the only
  safe routing-open gate.
- **Semantic diffs** (agent-verifiable change review): `pcb diff` / `sch diff` / `fp diff`
  `--format json|text|png|svg`; differing docs exit **5**. JSON carries item kind, stable UUID,
  bbox, changed properties. Metadata-only edits are ignored by design.
- **Visual for chat**: `pcb render` (real PNG raytrace, works headless on macOS),
  `pcb export svg --mode-multi -l F.Cu,B.Cu,Edge.Cuts`, `sch export pdf/svg`.
- **Fabrication**: `pcb export gerbers/drill/pos/step/glb/stats/stackup`, BOM via
  `sch export bom --fields 'Reference,Value,Footprint,${QUANTITY},DNP'`.
- **Jobsets** (`.kicad_jobset`, JSON, `meta.version:1`): one file describing a whole
  fabrication pack (DRC + gerbers + drill + STEP → folder/zip destination), run with
  `kicad-cli jobset run --file pack.kicad_jobset project.kicad_pro`. Run against the
  **project** so `${KIPRJMOD}` resolves. DRC `severity` is a bitmask: the starter pack uses
  `52` (`error | warning | exclusion`), and a report whose `included_severities` omits `error`
  is a configuration failure rather than a clean board. This is the repeatable agent→fab
  handoff boundary.
- `version --format json` is NOT supported (plain/commit/about only).

**In a shell, prefer `phicad tool` over raw `kicad-cli`.** Every subcommand above is wrapped as a
tool that parses the output into numbers (`pcb_export_drill` → hole/tool counts, `jobset_run` → a
per-job tally, `pcb_drc` → violations by severity and type), defaults to writing a NEW file, and
reports `outputs` / `mutated`. `phicad tool call kicad_cli --arg args=…` and
`phicad tool call kicad_cli_help --arg command=…` are the escape hatches when you want the raw
binary or its real flag list (`phicad tool describe kicad_cli` for the exact parameter names — they
come from the registry, so `describe` is never wrong about them). Full mapping, side by side:
[docs-site/docs/guides/driving-phicad.md](docs-site/docs/guides/driving-phicad.md).

## Channel 3 — MCP servers (the chat-tool surface)

Three servers are registered in `~/.pi/agent/mcp.json`:

| Server | Repo | Backing | Role |
| --- | --- | --- | --- |
| `phicad` | [`mcp/`](mcp/) in this repo | **fork** CLI + managed headless IPC + its own schematic and git layers | the whole fork surface, sectioned as [`mcp/README.md`](mcp/README.md) maps it (`phicad tool list --json \| jq .tool_count` for the live size): gates, every fabrication export, semantic diffs, jobsets, board structure, a live item-level board-editing session, the schematic read/write surface, the per-project git journal, autoroute, parts, swarm, synth; `doctor.py` checks it and `register.py` writes its registration |
| `kicad-analysis` | `~/tools/kicad-mcp` (branch `phicad-fork-support`) | **fork** kicad-cli + file parsing | 16 read-only tools: DRC, thumbnails, BOM, netlists, circuit patterns |
| `kicad` | `~/tools/KiCAD-MCP-Server` | **stable** KiCad 10.0 SWIG + CLI + (partial) IPC | 229 authoring tools for stable-format files |

The in-repo `phicad` server's session tools own a document-less background
`kicad-cli api-server`, issue `OpenDocument` over IPC and run kipy in `build/venv`; they never
bring PhiCad to the foreground, and its separate `build/mcp-venv` must not import kipy because
the MCP stack and kipy require incompatible protobuf versions. Use `doctor.py` for the health
check and `register.py` to write the `phicad` entry; put `PHICAD_REPO`, `PHICAD_BUILD`,
`PHICAD_VENV`, and `PHICAD_KICAD_CLI` in the client's MCP `env` block because the stdio client
strips shell environment variables and source/build worktrees may differ (full setup and
boundaries: [mcp/README.md](mcp/README.md)).

**One surface, three front doors — chat is not the only one.** `mcp/registry.py` derives the whole
catalogue from the live FastMCP instance (name, section, JSON Schema, docstring, a `mutates`
verdict) and `phicad tool` calls `tool.fn` **in-process**, so a terminal agent, a CI job and a chat
client run the identical function. Nothing is reimplemented per front door, and the generated
reference cannot drift from the code.

```bash
phicad tool list [--section S] [--mutating] [--json]   # discover; W marks writers
phicad tool describe pcb_drc                           # signature, schema, docstring
phicad tool call pcb_drc --arg board_path=/abs/b.kicad_pcb   # exit 0=ok:true 1=ok:false 2=usage
phicad mcp doctor | register | serve | selftest | docs  # server lifecycle
```

**Every mutating tool versions itself.** A standalone KiCad project gets its own git repository;
a board project inside an existing checkout reuses the nearest repository and scopes snapshots to
that project subdirectory (`phicad/pylib/project_history.py`, stdlib-only). An ambiguous checkout
path or the PhiCad source tree is refused rather than initialized. After `phicad project init`,
`server.py` wraps
each writer: it works out which files changed, appends `<project>/.phicad/journal.jsonl`, and
commits a snapshot whose subject names the tool (`phicad(mcp:sch_set_field): sch_set_field`),
returning a `project_history` block. Exports under `$TMPDIR` are outside any project repo and are
not journaled; a failed call is journaled but never committed; `PHICAD_TOOL_SNAPSHOT=0` keeps the
journal and drops the auto-commit. The same surface is `phicad project
init|status|snapshot|log|diff|restore|undo|tag|journal|watch`, where `diff` returns `git diff
--stat` **plus** the fork's semantic `pcb/sch diff` between two revisions — the reason this beats a
plain `git log` for reviewing a board change. `restore` and `undo` are dry-run by default and never
rewrite history (`undo` is `git revert`).

`init` also installs the fork's **structural merge driver** (`.gitattributes` → `kicad-cli
git-mergedriver`, plus the per-repo `merge.kicad-*` config), because git's default text merge
writes `<<<<<<<` into a board and the result will not load at all. This is what makes it safe for
two agents to work on branches of one project repo: different footprints merge clean keeping both,
the same footprint still conflicts for a human, and either way the board stays loadable. It is
reported as `merge_driver` — `installed: false` (with a `reason`, usually an unbuilt `kicad-cli`)
means you are back on text merge, so check it before handing a repo to a second agent.

The schematic section mostly has no Channel 1 or 2 equivalent: kipy 0.8 can open a `.kicad_sch`
and little else, so the `schematic` section reads and writes it as text through
`phicad/pylib/sexp_edit.py` — chosen over kiutils (drops unmodelled tokens) and kicad-skip (reflows
the file) because a no-op load+save is byte-identical and a one-field change is a one-line diff.
Every write is load-checked with the fork's own `sch erc` before it reports `ok`.

The exception is **reading from a running schematic editor**: the fork's IPC serves
`GetSchematicNetlist` and `GetSchematicHierarchy`, and `phicad/pylib/sch_api.py` calls them from
Python without a forked kipy — it loads a descriptor set compiled from `api/proto` (written by
`phicad venv` to `build/venv/phicad-api.desc`), builds the fork-only messages dynamically in a
private descriptor pool, and sends them through kipy's own transport. That is how the AI View
launched from eeschema shows live nets and the sheet tree, and the pattern generalizes to any
fork-only command PyPI kipy has not caught up with.

Walkthrough of the same three tasks done all three ways, plus the journal workflow:
[docs-site/docs/guides/driving-phicad.md](docs-site/docs/guides/driving-phicad.md).

UI/model hosts should negotiate the versioned boundary instead of importing
Tk or MCP implementation details. `phicad integration capabilities` (also exposed as the
`integration_capabilities` MCP tool) returns the design-IR, host-action, and workflow-event
schemas plus the model capability names. The architecture and remaining loopback transport
work are in [integration/README.md](integration/README.md).

`kicad-analysis` runs **fork-native**: its env block points `KICAD_APP_PATH`/`KICAD_CLI_PATH`/
`PATH` at `build/mac-dev/.../PhiCad.app` with `KICAD_RUN_FROM_BUILD_DIR=1`. The branch
`phicad-fork-support` (in a personal fork of kicad-mcp) makes the server honor client env — the
stock repo hardcoded `/Applications/KiCad/KiCad.app` and let `.env` clobber client env.
Verified: its DRC tool found the fork CLI and returned 24 violations on a 10.99-format board
that stable 10.0.6 *refuses to open*.

### The version boundary (the one rule that prevents data loss)

> **Stable-KiCad tools never touch fork-format files.** The fork saves `version 20260831`;
> stable 10.0.6 rejects those files ("created with a more recent version") — rejection is safe,
> but any file stable *can* still load, it will rewrite with the 10.0 serializer on save,
> dropping fork-only constructs. Authoring on fork files goes through Channel 1 (IPC) or
> Channel 4 with round-trip care, never through the stable SWIG server.

## Channel 4 — s-expression libraries (in `phicad venv`)

Ranked by the ecosystem survey (10 packages evaluated against real fork files):

- **kicad-tools** (0.20.0) — the strongest headless editor, built for AI agents. Verified against
  a **true fork-format file** (`version 20260831`): parse OK, targeted edit
  (`update_footprint_reference`) OK, round-trip reopens in the fork's DRC. Caveat: the round-trip
  perturbs board settings (DRC severity overrides + netclass hash changed in `pcb diff`) — not
  byte-preserving, so always gate with the diff recipe below. Ships CLI tools too
  (`kicad-pcb-query`, `kicad-nets`).
- **kicad-skip** (0.2.5) — exploratory/REPL-friendly *schematic* editing; preserves unknown
  tokens. Its **PCB path is broken on 10.x files** (`ParsedValue.fp_text` AttributeError) —
  schematics only.
- **kiutils** (1.4.8) — dataclass round-trip parser; parses 10.x files but reserializes the whole
  document (±20% size change) and its "create new" default is KiCad-6-era. Reads: fine.
- Evaluated and **rejected**: `kicad-sch-api` (drops `lib_symbols` on write), pip `kicad-mcp`
  (dependency-broken with MCP 2.x), `kibot` on native macOS (needs SWIG pcbnew; use in Linux CI
  only). `pcbdraw render` works but the fork's own `kicad-cli pcb render` covers it. `skidl` and
  `atopile`: greenfield netlist/code-first tools, not file editors.

All of Channel 4 is version-sensitive on 10.99 files: parse before trusting, `pcb diff` after
writing (exit 5 + JSON tells you exactly what changed — use it as the write-verification gate).

## Recipes

**Which model does what — and what it cost**: agent model tiers are validated and
retiered against the live LLM gateway with `phicad models [list|check|retarget]`;
every AI turn records the model that actually answered plus provider-reported
tokens/cost, rolled up per model by `phicad telemetry`. The whole story — including
the one-command path onto a future GPT-6 line — is docs-site → "Model tiers &
GPT-6 readiness" (`docs-site/docs/reference/model-tiers.md`).

**Exact parts with 3D models** (BOM-driven design): the parts pipeline in
[parts/README.md](parts/README.md) turns verified LCSC ids into project-local
symbol + footprint + STEP/WRL assets (`fetch_part.py`) and gates fab-readiness
(`audit_parts.py`: every footprint has a resolvable 3D model + LCSC/MPN id).
Agents: `parts-scout` (research → ledger), `parts-integrator` (ledger → libs).

**A whole board from a design IR**: `synth/README.md` compiles ~60 lines of JSON (parts,
nets, relative placement intent) into a placed, routed, poured, DRC-clean project, gated
stage by stage. It works because `ImportNetlist` carries no `checkForHeadless` guard
(`pcbnew/api/api_handler_pcb.cpp:1756`), so a netlist plus an empty board makes KiCad run
the real `BOARD_NETLIST_UPDATER` headless — no footprint s-expressions on our side.
Verified on `synth/examples/sensor-node.json`: 14/14 stages, ERC 0 errors, DRC 0 errors /
0 warnings / 0 unconnected, and the emitted schematic proven equal to the board's
connectivity pad for pad. Agent: `synth-designer`.

```bash
python3 phicad/synth/cli.py check design.json                       # stdlib, ~1 s
phicad py phicad/synth/cli.py build design.json -o out/             # ~20 s incl. DRC
```

**Agent review loop for any board change** (works today, all fork-native):

```bash
work=$(mktemp -d)
cp -R /path/to/project "$work/project"    # saves rewrite both .kicad_pcb and .kicad_pro
# … open/edit $work/project/board.kicad_pcb via IPC or edit it with an s-expression library …
phicad run kicad-cli pcb diff --format json /path/to/project/board.kicad_pcb "$work/project/board.kicad_pcb"  # exit 5 = changed
phicad run kicad-cli pcb drc --format json --exit-code-violations "$work/project/board.kicad_pcb"
phicad run kicad-cli pcb render -o review.png "$work/project/board.kicad_pcb"   # visual for chat
```

**Parallel layout (swarm)**: best-of-N placement scoring and partition→route→merge
for running several placer/router agents on one board without conflicts — tools and
concurrency rules in [swarm/README.md](swarm/README.md) (verified lossless on
`demos/interf_u`: strip→re-merge → `pcb diff` exit 0).

**One-command fabrication pack**: write a `.kicad_jobset` (DRC json + gerbers + drill + STEP →
`${KIPRJMOD}/fab`), commit it next to the project, `kicad-cli jobset run` it in CI or from chat.

## IPC command surface (summary)

From the proto catalogue (`api/proto/`), verified live: session (`Ping`, `GetVersion`,
`OpenDocument`/`Save`/`CloseAllDocuments`), items CRUD (`CreateItems`/`GetItems`/`UpdateItems`/
`DeleteItems` + `BeginCommit`/`EndCommit`), board reads (nets, stackup, layers, design rules,
netclasses), board writes (design rules, custom `.kicad_dru` text, origin, flip, zones refill,
`ImportNetlist`), schematic (`GetSchematicHierarchy`, `GetSchematicNetlist`), and export **jobs**
(gerbers/drill/pos/step-glb/svg/pdf/stats… in `kiapi.board.jobs`; svg/pdf/netlist/BOM in
`kiapi.schematic.jobs`). Working C++ client reference: `qa/tests/api/api_e2e_utils.h`; e2e suite:
`phicad test '^qa_api$'`.

## Reference reports

Deep-dive reports from the survey wave that produced this doc (kept out of the repo; regenerate
as needed): IPC command catalogue + envelope protocol, kipy-vs-fork compatibility, MCP server
audits (both servers, file:line), kicad-cli/jobset cookbook with real outputs, and the
pip-ecosystem survey (10 packages scored).
