# PhiCad Specialized EDA Agents

**Vision in chat, execution by tuned agents.** The human describes a board in the main phi
chat session; a swarm of specialized subagents — each a real phi named-agent definition
in this directory — turns that vision into fabrication outputs, with git commits and
machine-checkable gates between every stage.

## The agents

| File | Agent | Job | Model (+why) |
| --- | --- | --- | --- |
| `parts-scout.md` | parts-scout | Exact-part research → `parts.json` ledger (MPN, LCSC, stock, price) | `gateway/bulk` (no reasoning support — never suffix it) — requirements-vs-datasheet matching, hard-oracle verified |
| `parts-integrator.md` | parts-integrator | Ledger → project libs: symbol + footprint + 3D per part, audit gate | `gateway/strong:high` — procedural pipeline with exit-code oracles |
| `sch-designer.md` | sch-designer | Schematic capture, ERC-clean netlists | `gateway/frontier:high` — topology errors poison everything downstream |
| `synth-designer.md` | synth-designer | Circuit intent → design IR → placed, routed, DRC-clean project | `gateway/frontier:high` — the work is topology and part selection; coordinates are deterministic code, so no `:max` search premium |
| `pcb-placer.md` | pcb-placer | Footprint placement / floorplanning | `gateway/frontier:max` — hardest spatial/global-optimization stage |
| `pcb-router.md` | pcb-router | Trace routing, vias, zones | `gateway/fast:max` — spatial but highly iterative with a cheap external oracle (DRC); the fast tier is cheap enough for hundreds of steps |
| `drc-fixer.md` | drc-fixer | drc→fix→drc loop, max 3 iterations | `gateway/fast:high` — tight loop, perfect oracle, objective metric |
| `lib-librarian.md` | lib-librarian | Symbols, footprints, 3D bindings | `gateway/bulk` (no reasoning support) — meticulous datasheet transcription, checklist-gated |
| `fab-releaser.md` | fab-releaser | Jobset-driven fab pack + manifest | `gateway/strong:high` — procedural rigor, no design edits (no `edit` tool) |

Review gate: **`gateway/review`** (the review tier) is used by the
*orchestrator*, not defined as a file here — see the DAG below.

The model refs use five **neutral tier aliases** — `frontier` (hardest judgment), `review`
(stage review gates), `strong` (procedural rigor), `fast` (cheap, iterative, oracle-checked)
and `bulk` (high-throughput, no reasoning: never give it a thinking suffix). They are roles,
not models: either serve these aliases on your gateway (e.g. LiteLLM `model_group_alias`) or
map them once onto the models your provider serves with
`phicad models retarget frontier=<model> review=<model> strong=<model> fast=<model> bulk=<model>`.

## Schema (per phi's subagent extension)

Each file follows the agent-definition format from phi's subagent example
(`~/.local/share/phi/examples/extensions/subagent/README.md`, "Agent Definitions" section,
and `agents.ts` `discoverAgents()`): a Markdown file whose YAML frontmatter carries
`name` + `description` (both **required** — files without them are skipped), optional
`tools` (comma-separated) and `model` (any phi model ref, thinking-level shorthand like
`:max` supported per the main README's "Model with thinking level shorthand"). Model refs
are validated against the live LLM gateway by `phicad models check` (alias must be served,
thinking suffix must be legal for it — bulk takes none); retiering the whole suite onto a
new model family is `phicad models retarget OLD=NEW …` — one command, one reviewable diff
(see docs-site → "Model tiers & GPT-6 readiness"). The body is
the agent's system prompt. The subagent tool invokes them as `{ agent: "pcb-router", task: … }`,
in parallel via `{ tasks: [...] }`, or chained via `{ chain: [...] }`; `agentScope`
controls whether user-level (`~/.pi/agent/agents/`, default) or project-level
(`.pi/agents/`) definitions load.

## Install

These files are deliberately **not** installed by the build. The repo is the source of truth;
installing is a copy, and `phicad agents` makes it one step and shows drift:

```bash
phicad/bin/phicad agents install            # -> ~/.pi/agent/agents (user scope, always discovered)
phicad/bin/phicad agents install --project  # -> .pi/agents (needs agentScope: "project"/"both")
phicad/bin/phicad agents status             # ok / differs / missing per agent; exit 1 on drift
```

(`status` is how stale personal copies with a hard-coded build path were found: a copy made
in September 2026 still pointed at a `Main/build/mac-dev/.../KiCad.app` that no longer existed.)
Every prompt resolves the fork's `kicad-cli` as `$PHICAD_KICAD_CLI`, else
`phicad which kicad-cli` — the bundle path on macOS, `build/linux-dev/kicad/kicad-cli` on Linux —
so the same files work on a laptop and on a remote Linux host.

The subagent extension itself must be installed (`~/.pi/agent/extensions/subagent/`) per its
README. Agents are discovered fresh on each invocation, so files can be edited mid-session.

## Orchestration: vision → DAG

The main chat session (your interactive phi, strongest model) is the **orchestrator**. It
never pushes copper itself; it decomposes, dispatches, gates, and commits.

```
        chat: "4-layer USB-C sensor board, STM32, fits a 40×30 enclosure"
                                │
                    orchestrator writes SPEC.md, git init / branch: board/rev-a
                                │
                          [parts-scout]           (wave 0: sourcing)
                    requirement → exact MPN/LCSC rows in parts.json
                                │
                    human approves ledger rows (status: approved)
                                │
                        [parts-integrator]
                    fetch symbol+footprint+3D per part → project lib,
                    audit gate; unfetchable parts → lib-librarian
                                │
        ┌───────────────────────┴───────────────────────┐
        ▼ (parallel wave 1)                             ▼
  [lib-librarian]                                 [sch-designer]  ← uses ledger symbols,
  parts with no EasyEDA assets                    sets LCSC field per ref
        └───────────────► git commit ◄────────────────┘
                                │  "libs + schematic, ERC clean"
                                ▼
                     ★ review review gate #1 ★
             orchestrator runs a review-model review pass over
             sch diff json + ERC json + review.pdf; human sees
             the render in chat and approves the topology
                                │
                                ▼
                          [pcb-placer]           git commit "placement, courtyards clean"
                                │                (evidence: drc.json, placement.png, diff exit 5)
                                ▼
                          [pcb-router]           git commit "routed, 0 unconnected"
                                │                (evidence: drc.json --exit-code-violations=0,
                                ▼                 diff exit 5, routed.png)
                          [drc-fixer]            git commit per iteration (max 3)
                                │                (evidence: violation trajectory 24→9→0)
                                ▼
                     ★ review review gate #2 ★
             review-model pass over the full pcb diff (branch base → HEAD),
             final DRC json, renders; human sign-off in chat
                                │
                                ▼
                          [fab-releaser]         git commit "fab pack rev A" (+tag, if asked)
                                │                (evidence: jobset output inventory,
                                ▼                 immutability diff exit 0, MANIFEST.md)
                        gerbers → fab house
```

### The short path: synth-designer

For boards it can handle, **synth-designer collapses the whole
`sch-designer → pcb-placer → pcb-router → drc-fixer` span into one stage.** It writes a
design IR — parts, nets, and relative placement intent — and `phicad/synth/cli.py build`
compiles it: KiCad's own netlist updater loads the footprints, and deterministic code
places, routes, pours and solves the silkscreen, with a gate after each stage.

```
                            [parts-integrator]
                                    │
        ┌───────────────────────────┴───────────────────────────┐
        ▼ long path (any board)                                 ▼ short path
  [sch-designer]                                          [synth-designer]
        │                                                  design.json
        ▼                                                       │
  ★ review review gate #1 ★  (schematic + ERC)                   ▼  synth build
        │                                            validate · place · scaffold ·
        ▼                                            import · position · connectivity ·
  [pcb-placer]                                       outline · route · silk · drc
        ▼                                                       │
  [pcb-router]                                                  │
        ▼                                                       │
  [drc-fixer]  ◄╌╌╌╌ re-enter the long path at the stage that ╌╌┤
        │            owns the failure, on the board synth       │
        │            already built (placer / router / fixer /   │
        │            sch-designer — see synth-designer.md)      │
        └───────────────────────────┬───────────────────────────┘
                                    ▼
                        ★ review review gate #2 ★
                                    ▼
                             [fab-releaser]
```

The fallback is not a restart: synth hands over the board it already built, and the
per-stage agents continue from it. Synth's limits are listed plainly in
[../synth/README.md](../synth/README.md) §"What it does not do yet" —
no rip-up-and-reroute, a 0.1 mm router grid, `F.Cu`/`B.Cu` only, shelf-packing rather than
floorplanning. **Review gate #1 does have a schematic to review on the short path**: synth
emits a `.kicad_sch`, runs ERC on it, and proves it equals the board's connectivity pad for
pad (`sch-netlist`), so the orchestrator reviews the sheet, the design IR and the `drc.json`
together. A board needing real floorplanning judgement or a four-layer route still takes the
long path.

Contract between stages:

- **Git commit between every stage**, made by the orchestrator (or the agent when its task
  says so) on the working branch — never `git push`, by every agent's safety rules. Each
  commit message cites its evidence artifacts (DRC json, diff exit codes, renders).
- **Every agent ends with machine evidence**, not prose: `pcb diff --format json` exit 5
  proves the change landed; `drc/erc --format json` proves cleanliness; `pcb render` PNGs
  give the human a visual in chat. The orchestrator refuses to advance the DAG on missing
  evidence.
- **Failure bounces, not retries-in-place**: fab-releaser bounces DRC failures to drc-fixer;
  drc-fixer bounces placement-rooted violations to pcb-placer; drc-fixer's 3-iteration cap
  guarantees the loop terminates.
- **The review gates** are cheap review-tier passes the orchestrator inserts before the two
  expensive/irreversible transitions (schematic→layout, layout→fab). Run them as a subagent
  chain step with `model: gateway/review` and read-only tools, or simply have the
  orchestrator switch itself to review for the review turn.

## Routing table (task type → agent → model)

| Task smells like… | Agent | Model |
| --- | --- | --- |
| "find/source a part", "exact MPN", stock/price checks | parts-scout | bulk |
| "fetch the approved parts", "set up project libs from the ledger" | parts-integrator | strong:high |
| "draw / change the schematic", ERC, netlist, BOM fields | sch-designer | frontier:high |
| "generate a board for this circuit", "make me a sensor node PCB" (small/medium, 2-layer, poured) | synth-designer | frontier:high |
| "place / floorplan / rearrange components" | pcb-placer | frontier:max |
| "route / pour zones / diff pairs / stitch vias" | pcb-router | fast:max |
| "fix these violations / make DRC pass" | drc-fixer | fast:high |
| "route it in parallel / swarm the layout" | swarm of pcb-router (+ swarm tools) | fast:max ×N |
| "make a footprint / symbol / audit the libs" | lib-librarian | bulk |
| "gerbers / fab pack / release rev N" | fab-releaser | strong:high |
| "review this stage before we proceed" | (orchestrator gate) | review |

## Swarm mode (parallel placement & routing)

For big boards the placer/router stages fan out: N placers produce whole-board
candidates scored best-of-N, and routing is partitioned into disjoint net groups
routed in parallel and merged losslessly. The tools (`partition_nets.py`,
`merge_routes.py`, `score_board.py`), the dispatch recipe, and the concurrency
rules (one agent = one file copy = one socket; orchestrator owns git) live in
[../swarm/README.md](../swarm/README.md); the agent-side contract is
pcb-router.md §Swarm mode.

## Shared ground rules baked into every agent

- Fork CLI only: `KICAD_RUN_FROM_BUILD_DIR=1` +
  `$PHICAD_KICAD_CLI (or `phicad which kicad-cli`: the bundle path on macOS, `build/linux-dev/kicad/kicad-cli` on Linux)`
  (or `phicad run kicad-cli …`).
- **Version boundary** (data-loss rule): the fork saves `version 20260831`; stable KiCad 10.0
  refuses those files, and anything stable *can* load it would rewrite with the 10.0
  serializer. Stable-KiCad tools never touch fork-format files.
- kipy traps: `position` getters return **copies** — always assign a whole
  `Vector2.from_xy_mm(...)`; always set a real layer or the item is silently dropped at save;
  never call `check_version()`; one IPC connection, poll `Ping` past `AS_NOT_READY`.
- Copies (`cp original work`) or an orchestrator-owned git branch; never `git push`; never
  touch `.git/` internals.
- **Merge design files with the driver, never as text.** A `project_init`-ed repo routes
  `*.kicad_pcb`/`_sch`/`_sym`/`_mod` to `kicad-cli git-mergedriver`, so branches from a parallel
  wave merge structurally: different footprints merge clean keeping both, the same footprint
  conflicts for a human, and the board stays loadable either way. Plain text merge writes
  `<<<<<<<` into the file and destroys it. If `project_init` reported
  `merge_driver.installed: false`, say so and stop rather than merging by hand.
- No DRC/ERC over IPC — `kicad-cli` json is the single oracle, and `--exit-code-violations`
  when the exit code is the gate.
- **Two channels, and no third.** Everything reaches PhiCad either as a `kicad-cli`
  subprocess or as protobuf IPC to a `kicad-cli api-server` (kipy / the `session_*` tools).
  **Never drive the GUI**: no screenshots, no synthetic clicks or keystrokes, no `osascript` /
  System Events / accessibility automation, no "computer use". The MCP and CLI surfaces
  contain no such code path and must not grow one — a UI-automation step is unreproducible,
  untestable in CI, races the user's own mouse, and silently breaks on any layout change.
- **A headless refusal is an answer, not an obstacle.** Some IPC commands need a real editor
  frame (selection, visible/active layer, appearance settings, `RunAction`, `RevertDocument`);
  against `kicad-cli api-server` they return `ok=false` with `headless: true` and a reason.
  That is the contract working. Report BLOCKED and name what you needed — do **not** work
  around it by taking over the screen. If a capability is genuinely missing, the fix is a new
  tool or CLI flag, which is reviewable.

Source of truth for the EDA surfaces: `phicad/ai-interfacing.md` in this repo.
