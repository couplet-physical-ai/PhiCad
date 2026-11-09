# MCP tools

The `phicad` MCP server is the fork-native chat-tool surface for PhiCad. It runs the build-tree
`kicad-cli`, the automation virtual environment, and one managed headless IPC session. Every tool
returns a structured dictionary; subprocess failures come back as `ok: false` rather than raised to
the MCP client.

This page is the **narrative**: setup, registration, path resolution, the three write surfaces, the
exit-code contract, and the behaviours worth knowing before you call anything.

!!! tip "Per-tool signatures live on the generated page"
    **[MCP tool reference (generated)](mcp-tools-generated.md)** is the per-tool contract — every
    tool's real signature, JSON Schema and docstring, rendered from the live FastMCP registry by
    `phicad/bin/phicad-tool docs -o` and gated byte-for-byte by `phicad/mcp/test_registry.py`.

    Per-tool prose is deliberately **not** duplicated here. It rotted the last time it was: this
    page hand-documented a fraction of the surface, four tool-adding commits never touched it, and
    three "Added in this release" headings survived weeks past their release. The generated page
    cannot drift, so it is the one that describes tools. `test_registry.py` fails if a
    `### tool_name(` heading reappears below.

To find a tool, ask the registry rather than a document:

```bash
phicad tool list                          # every tool, grouped by section (W = writes)
phicad tool list --section gates           # one section
phicad tool list --mutating                # only tools that can change existing files
phicad tool describe pcb_drc               # signature, JSON schema, full docstring, mutate verdict
phicad tool list --json | jq '.tool_count' # the live size of the surface
```

## Setup and registration

The setup and registration helpers are:

- [`phicad/mcp/setup.sh`](https://github.com/couplet-physical-ai/PhiCad/blob/master/phicad/mcp/setup.sh)
  creates or refreshes `build/mcp-venv` and installs the MCP dependencies (`phicad mcp setup`).
  Keep this separate from `build/venv`, which contains kipy and pins an incompatible protobuf
  version.
- [`phicad/mcp/doctor.py`](https://github.com/couplet-physical-ai/PhiCad/blob/master/phicad/mcp/doctor.py)
  is the one-command health check. Run `phicad mcp doctor [--json]` (or
  `python3 phicad/mcp/doctor.py [--json]`) to check `kicad-cli`, both virtual environments, the
  fork-regenerated kipy protos, `mcp.json` registration, and orphaned `api-server` processes.
- [`phicad/mcp/register.py`](https://github.com/couplet-physical-ai/PhiCad/blob/master/phicad/mcp/register.py)
  rewrites only the `phicad` key in `~/.pi/agent/mcp.json`. **`phicad mcp register [--dry-run]` is
  the documented path** — it fills in the build, venv and MCP-venv paths for the checkout you run
  it from, so nothing is hand-written.

```bash
phicad mcp setup                # build/mcp-venv (fastmcp) — separate from build/venv (kipy)
phicad mcp register --dry-run    # show the `phicad` entry it would write, change nothing
phicad mcp register             # write it (backup kept, chmod 600)
phicad mcp doctor               # paths, kicad-cli, venvs, kipy protos, client config, orphans
phicad mcp selftest --full      # a real stdio client against this build tree
phicad mcp docs                 # regenerate the generated tool reference
```

### Path resolution

Every entry point resolves the same four overrides, in this order: the environment, then paths
derived from the checkout the server module lives in.

| Variable | Meaning |
| --- | --- |
| `PHICAD_REPO` | Repository containing `phicad/` and the MCP server. |
| `PHICAD_BUILD` | Build directory containing the PhiCad app and `kicad-cli`. |
| `PHICAD_VENV` | Automation venv containing kipy and kicad-tools. |
| `PHICAD_KICAD_CLI` | Full path to the fork's `kicad-cli`; overrides the path derived from `PHICAD_BUILD`. |
| `PHICAD_MCP_VENV` | The MCP venv (`doctor.py` / `setup.sh`); default `$PHICAD_REPO/build/mcp-venv`. |
| `PHICAD_MCP_OUT` | Root for tool outputs; default `$TMPDIR/phicad-mcp-out/<tool>/`. |

Rules that apply to every tool:

- **Absolute paths are preferred; relative inputs resolve against the repository root**, not
  against the client's working directory (an MCP client rarely has one an agent can predict).
- **Writers default to a NEW file** under `$TMPDIR/phicad-mcp-out/<tag>/`.
- **`PHICAD_*` overrides must go in the MCP client's `env` block**, not only in the shell that
  starts the client. The MCP stdio client starts the server with a restricted environment
  containing only `HOME`, `PATH`, `USER`, `SHELL`, `TERM` and `LOGNAME`, so shell exports are
  stripped. Without them the server falls back to `<repo>/build/…`, which does not exist when the
  build tree lives in another worktree.

A registration has the following shape — but let `phicad mcp register` write it:

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

## One surface, three front doors

The same functions are reachable from an MCP client, from a shell, and from Python, because the
shell front end reads the live FastMCP registry and calls `tool.fn` **in-process**:

| Front door | How |
| --- | --- |
| MCP client (chat) | `phicad` over stdio: `build/mcp-venv/bin/python phicad/mcp/server.py` |
| Shell / CI / terminal agent | `phicad tool call <tool> --arg key=value` (or `phicad/bin/phicad-tool`) |
| Python | `registry.collect(server_module)` → `spec.fn(**kwargs)` |

**Exit codes are the contract** for the shell door, so a script branches without parsing JSON:

| Code | Meaning |
| --- | --- |
| `0` | the tool ran and returned `ok: true` |
| `1` | the tool ran and returned `ok: false` (a real EDA failure: missing board, no venv, DRC unavailable) |
| `2` | usage error: unknown tool, unknown/missing parameter, uncoercible value, malformed `--json` |

Stdout is always the result dict as JSON (`--raw` for one compact line); stderr is empty on a clean
run, so anything there is a genuine problem. `--arg key=value` values are coerced by the tool's own
JSON Schema (`severity_all=false` reaches a `bool` as `False`), `--json '{…}'` takes nested
arguments, and `--dry-run` prints the coerced arguments without calling.

A side-by-side walkthrough of the same three tasks through all three doors — and through raw
`kicad-cli` — is in
[Driving PhiCad from a shell or an agent](../guides/driving-phicad.md).

## The three write surfaces, and which one to reach for

Three different mechanisms can change a design. They are not interchangeable:

| Surface | Mechanism | Use it when | Cost |
| --- | --- | --- | --- |
| `session_*` | live IPC into a managed headless `api-server`, real commits and undo steps | you are editing a **board**: geometry, copper, zones, nets, layers — anything KiCad's own model should validate | needs the child process; nothing is on disk until `session_save()` |
| `sch_*` writers | s-expression span splicing (`phicad/pylib/sexp_edit.py`) | you are editing a **schematic**: fields, symbols, wires, labels | text-level, so the fork's reader is the only validator — every write is load-checked |
| `pcb_import` / `*_upgrade` / `autoroute_*` / `swarm_merge` / `synth_build` | whole-file transforms | you are converting, routing or generating rather than editing | writes a new file unless you ask for in place |

The schematic side is text because there is no alternative: kipy 0.8 can open a `.kicad_sch` and
little else, and eeschema's reader lives inside a kiface DSO no Python can import. The splice
approach was measured on `demos/pic_programmer`: kiutils **drops** tokens it does not model,
kicad-skip keeps every token but reflows the whole file, and `sexp_edit.py` is byte-identical on a
no-op load+save and produces a one-line diff for a one-field change — so a schematic write is
reviewable with `git diff`.

**Reads are free; writes are opt-in.** Every writer defaults to a NEW file under
`$TMPDIR/phicad-mcp-out/<tag>/`. Touching the caller's file requires saying so — `out_path` equal
to the input, `in_place=True`, or `apply=True` — and then the path appears in `mutated`. That is
the whole safety model: a tool cannot quietly rewrite a board you asked it to analyse. Every tool
that writes also returns `outputs` (files it created), which is what the git journal keys on.

## Shared Agent View and the per-project git journal

A standalone KiCad project gets its own git repository — never the PhiCad source repo. When a
board directory is already inside a Git checkout, `project_init` (or `phicad project init`)
reuses the nearest repository and scopes snapshots to that project subdirectory; it never
silently creates a nested `.git`. A checkout path without one unambiguous KiCad project, or a
path inside the PhiCad source tree, is refused with an explanation. After initialization, every
mutating tool call is journaled to
`.phicad/journal.jsonl` and committed with a subject naming the tool
(`phicad(mcp:sch_set_field): sch_set_field`), and the result dict grows a `project_history` block.
Exports under `$TMPDIR/phicad-mcp-out/` are outside any project repo and are not journaled; a
failed call is journaled but never committed; `PHICAD_TOOL_SNAPSHOT=0` keeps the journal and drops
the auto-commit.

`project_init` also makes the repository **safe to merge**. Git's default text merge on a 600 KB
s-expression board does not merely conflict: it writes `<<<<<<<` *into the file*, and KiCad then
refuses to load it at all (measured: `Failed to load board: ... Got ''symbol'' ... line 136`). So
init writes a `.gitattributes` routing `*.kicad_pcb` / `*.kicad_sch` / `*.kicad_sym` /
`*.kicad_mod` to the fork's structural driver (`kicad-cli git-mergedriver`) and sets the per-repo
`merge.kicad-*` / `mergetool.kicad` config — the same thing KiCad does for repositories created
from the GUI, so a project is configured identically however it was made. With it, edits to
different footprints merge **clean keeping both**, and edits to the *same* footprint still
conflict — git asks a human, correctly — but the board left in the worktree remains loadable.

Both halves are append-only: a glob or config key that is already set is never overwritten (a
`merge=binary` opt-out, or a `merge.tool=meld` preference, survives). The two halves are
independent by design — `.gitattributes` is committed and travels with the project, while the
`merge.*` config is per-clone and cannot — so a clone only needs the config half, and a machine
with no built `kicad-cli` still gets the attributes. `project_init` reports `merge_driver`;
`installed: false` carries a `reason` and means merges fall back to text merge, which is worth
checking before handing one project repo to a second agent.

`project_diff` — and `phicad project diff`, which is the same answer from a shell — returns
`git diff --stat` **plus** the fork's own semantic `pcb`/`sch` diff of the two revisions, because
"9 insertions" is not a review and "R1 Value 10K → 12K" is. `project_restore` and
`project_undo_last` are dry-run by default and never rewrite history (`undo` is `git revert`).

The history tools (`project_git_snapshot`, …) are documented on the
[generated page](mcp-tools-generated.md).

## Semantics worth knowing

These behaviours are intentional and apply across the tools:

- **Semantic diffs exit 5 when documents differ.** `pcb_diff`, `sch_diff` and the semantic half of
  `project_diff` treat `kicad-cli` exit 5 as a successful comparison; read `differs`, `identical`
  and the change fields, not the exit code.
- **`pcb_export_step` may exit 2 when component 3D models are missing** (the build tree has no
  `${KICAD10_3DMODEL_DIR}` library) yet still writes a valid board-body STEP. It is reported as
  `ok: true` with `missing_model_count`.
- **`pcb_export_ibom` needs an InteractiveHtmlBom checkout, and never vendors one.** The
  interactive assembly BOM is produced without `import pcbnew`: `pcb export pos` (placements),
  `pcb export ipcd356` (pads and nets, straight from the board — no schematic needed) and an
  Edge.Cuts SVG plot are translated by `phicad/pylib/ibom_json.py` into ibom's documented
  *generic JSON* input, and `generate_interactive_bom.py` runs on that file as a subprocess.
  ibom (MIT) is looked for in the `ibom_dir` argument, then `$PHICAD_IBOM`, then
  `<repo>/build/` and the build tree's parent under `InteractiveHtmlBom` / `ibom` /
  `interactive-html-bom`; with none present the call fails with the clone command in `hint`.
  Because no `kicad-cli` export hands over silkscreen or fabrication geometry, the render shows
  pads, courtyard-sized highlight boxes and the outline only, and pad shapes collapse to
  rect/circle — every such gap is listed in `report.warnings` rather than hidden. The generic
  JSON is kept next to the HTML (`keep_json=True`) because it is what to inspect when a render
  looks wrong.
- **`jobset_run` needs the `.kicad_pro`**, not just the board, so `${KIPRJMOD}` resolves. The
  parsed job tally (`jobs_succeeded` / `jobs_failed`), not the process exit code, is the
  authoritative result.
- **`schematic_parity` is only reported when requested, and raw evidence is never suppressed.**
  `pcb_drc(schematic_parity=True)` answers "does this board still match its schematic?"; the
  `summary.schematic_parity` key is absent otherwise. When present, `raw_count` and
  `raw_total_violations` retain KiCad's findings while `effective_count` and
  `effective_total_violations` ignore only a UUID-resolved unnetted board pad whose matching pin
  is proven by a temporary sibling-schematic netlist export to be the exact generated
  `unconnected-(...-PadN)` no-connect pseudo-net. All generic `net_conflict` findings and
  unverified candidates remain active; unverified evidence makes `gate_status` `uncertain`.
- **Only one managed `api-server` is owned by this server at a time.** The session tools hold one
  project; `session_open` closes others by default; the child is killed on MCP exit.
- **Never call `KiCad().check_version()`** in a session script. kipy 0.8's guard still reports
  KiCad 10.0.6 and rejects the 10.99 fork even though ordinary commands work.
- **`session_save()` rewrites the board and its sibling `.kicad_pro`** in the fork's `20260831`
  format, after which stable KiCad 10.0 refuses the board. Use `session_open(copy_first=True)`,
  which copies the document and its sibling project files to scratch storage first, unless
  rewriting the user's project was explicitly requested.
- **Read a mutation back before trusting `ISC_OK`.** Session writers report `applied: true` only
  when a fresh read-back matches, because footprints and groups take a commit `Remove()` + `Add()`
  path rather than an in-place modify.
- **DRC and ERC are CLI-backed**; they are not available over the IPC session.

## Where the rest lives

| You want | Read |
| --- | --- |
| every tool's signature, schema and docstring | [MCP tool reference (generated)](mcp-tools-generated.md) |
| the same three tasks via chat, shell and raw `kicad-cli`, plus the journal workflow | [Driving PhiCad from a shell or an agent](../guides/driving-phicad.md) |
| the section map, the venv split, the registry, the three test suites | [`phicad/mcp/README.md`](https://github.com/couplet-physical-ai/PhiCad/blob/master/phicad/mcp/README.md) |
| how MCP relates to IPC, `kicad-cli` and s-expression libraries | [AI interfacing](ai-interfacing.md) |

## Background-only workflows

The full registry includes optional foreground and developer escape-hatch tools. It is not a
background-only policy by itself. Use the owned headless IPC child, typed operations and CLI
render/export/router tools, without opening a panel or attaching to a user's editor.

The draft background MCP profile (an unpublished design proposal) lists exclusions,
current source evidence, a scoped first implementation slice and a prompt for an existing
agent worker. No asynchronous API capability or enforced policy is implied by the current MCP
transport. The [integration source document](https://github.com/couplet-physical-ai/PhiCad/blob/master/phicad/integration/README.md)
distinguishes existing CLI adapters/digests/terminal sharing from proposed job lifecycle work.
