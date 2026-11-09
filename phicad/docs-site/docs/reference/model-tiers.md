# Model tiers and GPT-6 readiness

How the agent suite decides which model does what, how that stays true as model
generations change underneath it, and exactly which levers a stronger model
generation (a GPT-6 line on the gateway, say) turns — without a single line of
code branching on a marketing name.

## The contract

1. **The gateway is the source of truth.** Aliases, prices, context windows and
   descriptions come from `llm-gateway.example` at run time (`phicad models`),
   never from tables copied into docs. A new family is visible — priced,
   described, usable — the day the gateway serves it.
2. **No code branches on a model name.** Adapters advertise capabilities and
   hosts negotiate them (`phicad/integration/README.md`, "Model adapters and
   future GPT releases"). The design workflow asks *"does this adapter report
   usage? support session continuity?"* — never *"is this GPT-6?"*.
3. **Model identity is reported, not assumed.** Every AI turn records the
   model that actually answered it (the user can switch models in phi
   mid-session), so telemetry and the capability handshake stay honest for
   free when the fleet retiers.

## The tools

| Command | Job |
| --- | --- |
| `phicad models` | live gateway table: alias, $/1M in/out, context, output cap — agent-suite aliases marked |
| `phicad models check` | every `agents/*.md` model ref resolves on the gateway with a legal thinking suffix; exit code gates a PR |
| `phicad models retarget OLD=NEW …` | one-command retiering: rewrites agent frontmatter, preserves provider prefix + thinking suffix, refuses aliases the gateway does not serve |
| `phicad telemetry` | per-model cost/token rollup from provider-reported usage — the data a retier decision is made on |

## Tier map (roles, not names)

The stable thing is the **role** each rung plays; the alias filling it is
expected to change across generations:

| Role | Alias | What fills it under a new generation |
| --- | --- | --- |
| Frontier judgment: orchestrator review gates, hardest synthesis | review | strongest available tier |
| Substantive default: placement, schematic design, synthesis | frontier | the new generation's workhorse tier |
| Procedural rigor: part integration, fab release | strong | a disciplined mid tier |
| Cheap-parallel: routing swarms, DRC repair loops, wide fan-outs | fast | the new generation's economy tier — swarm width scales with its price |
| Bulk mechanical, no reasoning: datasheet transcription, part lookups | bulk | any cheap high-throughput rung (**never** a thinking suffix) |

The aliases are neutral roles, not model names: serve them on your gateway (for example as
LiteLLM model-group aliases) or map them onto the models your provider serves with
`phicad models retarget frontier=<model> review=<model> strong=<model> fast=<model> bulk=<model>`.

Per-agent assignments live in `phicad/agents/*.md` frontmatter and the routing
table in `phicad/agents/README.md`; `phicad models check` keeps them legal.

## One harness, one user identity

AI-View and the design loop run through **phi only**. The harness is a
build property, not a user setting: `PHICAD_AI_CMD`, `PHICAD_AI_BACKEND`,
`PHICAD_AI_MODEL` and `PHICAD_CODEX_HOME` are inert, and there is no automatic
Codex fallback. If phi is absent, the surface reports that instead of sending
the design context to whichever unrelated CLI happens to be installed.

The gateway identity is the user who signed in to phi. PhiCad does not embed a
shared key and does not parse phi's private credential store. The one direct
gateway client (`phicad models`) obtains the key through the stable
`phi auth token --json` pipe contract, introduced in phi 0.83.0-exo.45. The
key is captured only for the request, never put in argv or an environment
variable, and never included in diagnostics.

Standalone Agent View turns also carry LiteLLM's request tag
`client=phicad`. That tag permits per-application spend slices while the
per-user key remains the identity and budget owner. The tscircuit engine has
no model runtime at all: it receives no credential and makes no LLM request;
phi or Studio invokes it as a deterministic build tool.

A new model generation therefore reaches PhiCad when it reaches phi and the
gateway. There is no second provider adapter or login to update.

## The day a GPT-6 line lands on the gateway

```bash
phicad models                       # new aliases appear, priced + described
phicad models retarget fast=<new-economy> frontier=<new-top> --dry-run
phicad models retarget fast=<new-economy> frontier=<new-top>
phicad models check                 # refs resolve, suffixes legal
# run one representative task per retargeted agent, then:
phicad telemetry --days 1           # cost/quality per model, side by side
```

One diff, reviewed like any other PR. Nothing else changes: adapters already
report the new identity per turn, telemetry already splits cost by model, and
the capability handshake already tells hosts what the new model can do.

## What a stronger generation actually buys, surface by surface

These levers exist today and scale with the model plugged into them:

- **Best-of-N economics** (`layout-swarm`): placement candidates are scored
  best-of-N and routing is partitioned per-group. Candidate width is set by
  the economy tier's price — a cheaper/stronger economy rung means wider
  swarms at the same budget, and the merge/score gates already pick the
  winner objectively.
- **Repair-loop depth** (`synth`, drc-fixer): check/repair cycles are capped
  by attempts (`PHICAD_AI_REPAIR_ATTEMPTS`); a stronger model converges in
  fewer, cheaper cycles — measured directly by `phicad telemetry` cycle
  outcomes vs cost.
- **Whole-board context**: 1M-token rungs hold a full board s-expression,
  its DRC report and the relevant datasheets in one turn, so agents reason
  over the real artifact instead of a summary. Larger context in a new
  generation raises the board-size ceiling with no plumbing change.
- **Reasoning laddering**: every agent ref carries a thinking level
  (`:low`…`:max`); rung × level is tuned per stage today and re-tuned by
  editing frontmatter, with `check` catching illegal combinations.
- **Session continuity**: the design loop replays one `--session-id`
  conversation, so a generation with better long-horizon coherence
  automatically produces better multi-turn repairs.
- **Capability flags** (`vision`, `structured_output`, …) are negotiated per
  adapter: when a transport can genuinely carry an image (board renders to a
  vision-capable model) or enforce a schema, the flag flips to true and hosts
  may start relying on it — the handshake schema is already published.

## What is deliberately not here

A guessed GPT-6 endpoint, payload shape, or capability list. The integration
contract (`phicad/integration/README.md`) is explicit: implement against real
APIs when they exist; until then, keep every seam capability-shaped. That rule
is why this page will not need rewriting when the generation actually ships.

Source of truth: [phicad/pylib/gateway_models.py](https://github.com/couplet-physical-ai/PhiCad/blob/master/phicad/pylib/gateway_models.py),
[phicad/agents/README.md](https://github.com/couplet-physical-ai/PhiCad/blob/master/phicad/agents/README.md),
[phicad/integration/README.md](https://github.com/couplet-physical-ai/PhiCad/blob/master/phicad/integration/README.md)
