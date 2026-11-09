# Driving PhiCad from a shell or an agent

PhiCad's automation surface is **one set of functions with three front doors**. The same tools are
reachable from a chat client over MCP, from a terminal with `phicad tool`, and from Python through
the registry — because the shell front end reads the live FastMCP registry and calls `tool.fn`
in-process. Nothing is reimplemented, so nothing can drift.

(How many tools that is, right now: `phicad tool list --json | jq .tool_count`. This page does not
say, because the number changed with every tool-adding commit and the docs did not.)

Underneath all of it sits `kicad-cli` from this build tree. You can always drop to it directly;
this page shows when that is worth doing and when it costs you.

## Choose your route

<div class="grid cards" markdown>

- :material-rocket-launch: **New user**

    Start with [Getting started](getting-started.md) and the [visual product tour](product-tour.md)
    before putting an agent in the loop.

- :material-monitor: **Desktop user**

    Use [The application](the-application.md) for launch, Agent View, and GUI-to-headless mapping.

- :material-robot-outline: **Agent operator**

    Stay here for copyable, machine-readable examples and the boundaries of MCP, `phicad tool`,
    Python, IPC, and raw `kicad-cli`.

</div>

## The same three tasks, three ways

### 1. Gate a board (DRC)

=== "Chat / MCP"

    > Run DRC on `/tmp/proj/board.kicad_pcb` and tell me what is wrong.

    The agent calls `pcb_drc` and gets a parsed result: `summary.violations.total`,
    `by_severity`, `by_type`, and a few example violations with the items involved.

=== "`phicad tool`"

    ```bash
    phicad tool call pcb_drc --arg board_path=/tmp/proj/board.kicad_pcb
    # -> {"ok": true, "summary": {"violations": {"total": 24,
    #      "by_severity": {"error": 8, "warning": 16},
    #      "by_type": {"text_thickness": 8, "text_height": 8, ...}}}, ...}
    echo $?    # 0 = the tool ran; the violation count is data, not the exit code
    ```

=== "Raw `kicad-cli`"

    ```bash
    phicad run kicad-cli pcb drc --format json -o /tmp/drc.json \
        /tmp/proj/board.kicad_pcb
    jq '[.violations[].severity] | group_by(.) | map({(.[0]): length}) | add' /tmp/drc.json
    ```

    You get the same JSON, and you write the summarising yourself. Note `--exit-code-violations`
    if you want a non-zero exit on violations — without it the CLI exits 0 with a dirty board.

### 2. Change a schematic field, verifiably

=== "Chat / MCP"

    > Set R1's MPN to RC0603FR-0712KL on `/tmp/proj/board.kicad_sch`, in place.

    The agent calls `sch_set_field`, which writes the file, re-reads it with the fork's own
    parser (`loads: true`), reports `changes[]`, and — because the project is a git repo —
    commits a snapshot naming the tool.

=== "`phicad tool`"

    ```bash
    phicad tool call sch_set_field \
        --arg sch_path=/tmp/proj/board.kicad_sch \
        --arg ref=R1 --arg field=MPN --arg value=RC0603FR-0712KL \
        --arg out_path=/tmp/proj/board.kicad_sch      # out_path == input = edit in place
    # -> "change_count": 1, "loads": true, "in_place": true,
    #    "mutated": ["/tmp/proj/board.kicad_sch"],
    #    "project_history": {"journaled": true, "committed": true, "commit": "051fc8f…"}
    ```

    Leave `out_path` off and the edit lands in `$TMPDIR/phicad-mcp-out/schematic/` instead;
    your file is untouched. That is the default for every writer.

=== "Raw `kicad-cli`"

    There is no `kicad-cli` command for this. Editing a schematic means parsing the
    s-expression yourself — and the popular Python libraries lose data doing it (kiutils drops
    tokens it does not model; kicad-skip reflows the whole file). `sch_set_field` exists because
    `phicad/pylib/sexp_edit.py` splices character spans, so a one-field change is a one-line
    diff. Reach for the raw file only to *read* it.

### 3. Export a fabrication pack

=== "Chat / MCP"

    > Build the fab pack for `/tmp/proj/board.kicad_pro`.

    The agent calls `jobset_run` and reports `jobs_succeeded` / `jobs_failed` per job — the
    parsed tally, not just the process exit code. It also checks `fab/drc.json`: a clean result is
    trustworthy only when `included_severities` contains `error` (the starter pack includes
    `error`, `warning`, and `exclusion`).

=== "`phicad tool`"

    ```bash
    phicad tool call jobset_run \
        --arg jobset_path=/tmp/proj/fab_pack.kicad_jobset \
        --arg project_path=/tmp/proj/board.kicad_pro
    # -> "jobs": ["pcb_drc", "pcb_export_gerbers", "pcb_export_drill"],
    #    "jobs_succeeded": 3, "jobs_failed": 0, "output_paths": ["fab"]
    ```

    Or one export at a time: `phicad tool call pcb_export_gerbers --arg board_path=…`,
    `pcb_export_drill`, `pcb_export_odb`, `pcb_export_ipc2581`, …

=== "Raw `kicad-cli`"

    ```bash
    phicad run kicad-cli jobset run --file /tmp/proj/fab_pack.kicad_jobset \
        /tmp/proj/board.kicad_pro
    ```

    Run it against the **`.kicad_pro`**, not the board, or `${KIPRJMOD}` will not resolve. And
    `exit_code == 0` is not the whole story: read the per-job tally, which is exactly what the
    tool parses for you. In a `.kicad_jobset`, DRC `severity` is a bitmask rather than an enum
    ordinal: the starter pack uses `52` (`error | warning | exclusion`). Audit the generated
    report's `included_severities`; an empty report that omitted `error` is a configuration
    failure, not evidence that the board is clean. Set the DRC job's registered
    `"fail_on_error": true` setting explicitly. `"exit_code_violations"` is not a recognized
    jobset setting and is ignored, so it does not encode the intended failure policy.

## Which one to use

| Situation | Use |
| --- | --- |
| A human is asking questions about a design in chat | **MCP** — the agent picks the tool, and the parsed result is what an LLM can reason about |
| A script, a CI job, a Makefile, or an agent working in a terminal | **`phicad tool call`** — the exit code is the contract (`0` = `ok: true`, `1` = `ok: false`, `2` = usage error), stdout is JSON |
| You are exploring what the fork's CLI can do, or you need a flag no tool exposes | **raw `kicad-cli`** (via `phicad run kicad-cli`), or `phicad tool call kicad_cli --arg args=…` which is the same thing with the outputs it produced reported back (and `kicad_cli_help --arg command=…` for a subcommand's real flags) |
| You need something the CLI cannot do at all: schematic edits, live board edits, project history | **the tools** — those sections have no `kicad-cli` equivalent |
| You are writing Python | `registry.collect(server)` → `spec.fn(**kwargs)`; same functions again |

Discover the surface rather than memorising it:

```bash
phicad tool list                      # every tool, grouped by section (W = writes)
phicad tool list --section schematic  # one section
phicad tool list --mutating           # only the tools that can change existing files
phicad tool describe sch_set_field    # signature, JSON schema, full docstring, mutate verdict
```

`--arg key=value` values are coerced by the tool's own JSON Schema, so `severity_all=false`
reaches a `bool` as `False` and `passes=100` as an `int`. Use `--json '{...}'` for nested
arguments and `--dry-run` to see the coerced arguments without calling.

## The git journal workflow

A standalone KiCad project gets its own git repository. A board directory inside an existing
checkout reuses the nearest repository instead of silently creating a nested `.git`; snapshots
are scoped to that project subdirectory, so unrelated monorepo files are not staged. An existing
checkout path that does not identify one KiCad project, or anything inside the PhiCad source tree,
is refused with an explanation. Once initialized, every mutating tool call journals itself and
commits a snapshot, so "who changed this board, with what, and when" is answerable, and any step
is revertible. The journal also installs
a **KiCad-aware merge driver** for `.kicad_pcb`/`.kicad_sch`: git's default text merge would write
`<<<<<<<` markers *into* a 600 KB s-expression board and stop it being a board; with the driver,
merging two lines of work on one project cannot destroy the files.

```bash
# 1. init — a baseline commit plus .phicad/journal.jsonl
phicad project init /tmp/proj
# -> Git main @ 6ec0e92 · clean

# 2. edit — through any tool; the journal hook fires by itself
phicad tool call sch_set_field --arg sch_path=/tmp/proj/board.kicad_sch \
    --arg ref=R1 --arg field=MPN --arg value=RC0603FR-0712KL \
    --arg out_path=/tmp/proj/board.kicad_sch
# -> "project_history": {"journaled": true, "committed": true, "commit": "051fc8f…"}

# 3. snapshot — for changes the tools did not make (a save from the GUI, a hand edit)
phicad project snapshot /tmp/proj -m "moved the connector cluster" --actor me
phicad project watch /tmp/proj --loop        # or let the watcher commit after every save

# 4. log — what happened, with the actor and tool behind each revision
phicad project log /tmp/proj -n 5
# f55dd610ee  2026-09-07T02:38:59Z  phicad(mcp:sch_set_field): sch_set_field  [mcp:sch_set_field]
# e227578204  2026-09-07T02:38:31Z  phicad(init): start project history       [init]
phicad project journal /tmp/proj             # the raw action log, including failed calls

# 5. semantic diff — not just "9 insertions"
phicad project diff /tmp/proj --rev-a HEAD~1 --rev-b HEAD
# microwave.kicad_pcb | 2 +-
#  1 file changed, 1 insertion(+), 1 deletion(-)
# microwave.kicad_pcb pcb diff: exit 5 — differs (1 change)
#     1 FOOTPRINT
#     modified FOOTPRINT POLY: Position X 203179680 → 208179680
#     report: $TMPDIR/phicad-diff-…/microwave.HEAD-1--microwave.HEAD.pcbdiff.json
phicad project diff /tmp/proj --rev-a HEAD --rev-b WORKTREE   # uncommitted work vs HEAD
phicad project diff /tmp/proj --no-semantic       # git stat only
phicad project diff /tmp/proj --json             # adds semantic[] + semantic_change_count

# 6. restore / undo — dry-run by default, both of them
phicad project restore /tmp/proj HEAD~1                    # what WOULD change
phicad project restore /tmp/proj HEAD~1 --apply            # and commit the rollback
phicad project undo /tmp/proj --apply                      # git revert, history preserved

# 7. tag — name the states that matter
phicad project tag /tmp/proj drc-clean-v1 -m "0 errors, sent for review"
```

Every subcommand takes `--json` for machine-readable output, and the project directory **or any
file in it** as its first argument (default: the current directory), so `phicad project log
board.kicad_pcb` works from inside a project.

Notes that save time:

- **`project_diff` is the review tool.** `git diff` on a `.kicad_pcb` is noise; the semantic diff
  reports the changed item, its UUID and its before/after properties. The `project_diff` MCP tool
  and `phicad project diff` run the same semantic step, so a CI gate written from this guide
  compares boards, not temp-file names. The step shells out to the fork's `kicad-cli` (resolved from
  `PHICAD_KICAD_CLI`, else `PHICAD_BUILD`, else the build tree beside the checkout) because
  `project_history.py` is stdlib-only; with no built binary it prints
  `semantic diff skipped: <reason>` and still gives you the git stat, and `--json` carries the same
  as `semantic_skipped`.
- **Exports are not journaled.** Anything written under `$TMPDIR/phicad-mcp-out/` is outside the
  project repo, so analysis runs leave no commits.
- **Failed calls are journaled, never committed** — history has no commit for a change that did
  not happen.
- **Opt out of the auto-commit** with `PHICAD_TOOL_SNAPSHOT=0` in the environment; the journal
  entry is still written. `phicad tool call --project-snapshot` forces it on for one call.
- **Nothing is ever rewritten.** `undo` is `git revert`; `restore` commits the rollback. Both are
  dry-run until you pass `--apply`.

## Setup and health

```bash
phicad mcp setup            # build/mcp-venv (fastmcp) — separate from build/venv (kipy)
phicad mcp register         # write the `phicad` entry of ~/.pi/agent/mcp.json for THIS layout
phicad mcp doctor           # paths, kicad-cli, venvs, kipy protos, client config, orphans
phicad mcp selftest --full  # real stdio client against this build tree
phicad mcp docs             # regenerate the generated tool reference
```

## Support and feedback

The [PhiCad product tour](product-tour.md) shows the desktop and current-output story before you
choose an operator path. Prepare a user-facing bug, feature, or page-improvement draft through the
[feedback guide](feedback.md). Maintainers use the
[feedback administration reference](../reference/feedback-admin.md).

Generate bounded diagnostics locally, then inspect the archive before sharing it:

```bash
phicad support bundle [--project PATH] [--output PATH] [--json]
```

The default bundle contains fixed metadata and command outcomes, not raw stdout/stderr. Supplying
`--project` adds only file names, sizes, and SHA-256 hashes; it never copies design contents.

Source tree, build tree and venvs routinely live in different worktrees. Every entry point honours
`PHICAD_REPO`, `PHICAD_BUILD`, `PHICAD_VENV`, `PHICAD_KICAD_CLI` (plus `PHICAD_MCP_VENV`), and they
must go in the **`env` block of the MCP client config** — the stdio client strips shell exports, so
a `export PHICAD_BUILD=…` in your profile never reaches the server. `phicad mcp register` writes
them for you.

Full details: [MCP tools](../reference/mcp-tools.md) for the narrative,
[MCP tool reference (generated)](../reference/mcp-tools-generated.md) for every signature and
schema, and [AI interfacing](../reference/ai-interfacing.md) for how the four channels relate.

## Next steps

<div class="grid cards" markdown>

- :material-rocket-launch: **[Build a first board](getting-started.md)**

    Start with a real design, real output, and the complete limitations table.

- :material-monitor: **[Use the desktop application](the-application.md)**

    Open Agent View when a human needs the live project context beside the editors.

- :material-map: **[Review the product tour](product-tour.md)**

    See current build output, product direction, and the route back to feedback.

</div>
