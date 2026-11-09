# PhiCad host integration boundary

This directory defines the seam between PhiCad's deterministic EDA workflows and hosts such
as the Tk plugin, an agent host, a terminal agent, or a future model runtime. It is intentionally
transport- and provider-neutral.

## The rule

**Models propose; PhiCad validates; people approve; PhiCad builds.**

A host may automate model/check/repair turns, but a revised electrical design cannot cross
into synthesis without a fresh human approval. Machine diagnostics move directly between the
checker and the model; making a person copy/paste them is not an integration contract.

## Three planes

| Plane | Existing implementation | Stable boundary |
| --- | --- | --- |
| Command | `phicad/mcp/server.py`, IPC, `kicad-cli`, synth CLI | MCP tools and JSON reports |
| Workflow | `plugins/ai-view/design_agent.py` | `DesignModel`, `ModelCapabilities`, versioned workflow events |
| Presentation | Tk AI View / New Project; loopback web viewer | host renders state; full action-schema service is still proposed |

The command plane remains the source of truth. A host panel must not duplicate DRC, routing,
rendering, or project mutation logic in JavaScript; it calls the PhiCad surfaces and displays
their results. The current handshake is available without a build or venv:

```bash
phicad/bin/phicad integration capabilities
```

## Design workflow protocol v1

`design_agent.py` emits objects matching
[`design-workflow-event.schema.json`](design-workflow-event.schema.json). Every object has:

- `schema`: `phicad.design-workflow.event/v1`;
- `kind`: a state transition such as `checking`, `validation_failed`, `repair_started`,
  `design_updated`, or `validated`;
- `session_id` / `sequence`: correlation and monotonic replay order for a remote host;
- `attempt` / `max_attempts`: bounded-loop progress;
- `message`: human-readable status, never the only source of truth; and
- `data`: structured report/design payload for a host that can render richer UI.

The Python event object has `as_dict()` and is JSON serializable. Tk consumes it through its
main-thread queue today. A host bridge can put the same dictionaries on JSON Lines, SSE, or a
WebSocket without changing the workflow.

Expected host actions for the first bidirectional transport are formalized in
[`host-action.schema.json`](host-action.schema.json):

```text
session.start       {workspace_root, project_path?, intent?}
intent.submit       {text}
design.review       {}
design.approve      {design_digest}
build.start         {destination, design_digest}
build.cancel        {}
project.open        {path}
session.close       {}
```

Approval carries a digest of the design that was shown. If an automatic repair changes the
design, the digest changes and the old approval is invalid. This prevents a stale host card
or browser tab from building an unseen revision.

## Model adapters and future GPT releases

PhiCad does not branch on a marketing model name. A model adapter advertises capabilities:

```json
{
  "protocol_version": 1,
  "provider": "phi-cli",
  "model": "configured-provider",
  "structured_output": false,
  "tool_calls": false,
  "streaming": false,
  "vision": false,
  "background_tasks": false,
  "session_continuity": true,
  "extras": {"transport": "subprocess"}
}
```

Two of those fields are live rather than static: after the first turn the phi adapter
reports the `provider`/`model` that actually answered (per-turn, from the backend's own
usage events — the user can switch models mid-session), and `extras.usage_reporting`
says whether real token/cost usage is being recorded to telemetry. A host that wants to
know "is the new model generation serving this session?" reads the handshake; nothing
branches on a name.

The source already implements phi and Codex CLI dialects in
[`chat_backend.py`](../plugins/ai-view/chat_backend.py) and
[`design_agent.py`](../plugins/ai-view/design_agent.py). Codex sessions resume using their
thread IDs. The adapter distinguishes requested model identity from provider-reported identity
and records token usage without inventing dollar cost. Its dialect-level `structured_output`
flag is separate from `extras.design_schema_bound`: open-key design IR maps may not bind to the
provider's strict schema subset, so local validation and bounded repair remain necessary.

Provider API asynchronous tool calls, mid-turn steering and other capabilities are not implied
by the CLI subprocess transport. Implement a transport against its actual supported API and
contract-test it before advertising those flags; do not branch workflow policy on a model name.

## Current host surfaces and remaining work

[`web_view.py`](../plugins/ai-view/web_view.py) serves an Agent View panel (the host-terminal
session bridge is not included in this build). It does not implement the full action-schema workflow service described above.
`design_agent.py` already computes design digests; `new_project_gui.py` binds validation and
approval to a digest and rejects an altered design at build time. Those are local workflow
protections, not proof of equivalent authority enforcement for every direct MCP mutation.

For the background-only workflow, an agent host should call a reviewed MCP tool profile and receive
artifacts/progress without opening either panel or desktop editor. The managed `kicad-cli
api-server` is a headless child, not the user's open application. DRC/ERC, exports, PNG renders
and Freerouting also have headless paths. Use typed item operations and explicit saved-file
checkpoints; selection, RunAction/PNS and GUI RevertDocument are not background capabilities.

The draft MCP profile proposal (an unpublished design proposal)
scopes the first enforcement slice. Existing generic tools and error hints can still request
foreground actions; documenting a profile does not mean the server enforces it today.

A durable background workflow service, if later approved, should expose job submit/status/
result/cancel through MCP, bind outputs to immutable input revisions, and preserve human
approval for the exact design. Reuse existing digest/event logic rather than creating a second
EDA implementation. A separate HTTP host-action service remains optional future work, not a
prerequisite for using the existing MCP command plane.

### Requirements for a future loopback workflow service

- loopback only; random bearer token delivered out of band;
- canonical workspace root and rejection of path traversal/symlink escapes;
- explicit allowlist of actions—no arbitrary shell endpoint;
- one session ID per worktree, resumable event sequence numbers, and idempotent commands;
- subprocess groups are cancellable and cleaned up when the host worktree/session closes;
- absolute project/artifact paths in machine payloads, concise relative paths in UI;
- model credentials stay in the model host (phi), never in a board project; and
- every mutation returns the before/after digest and gate evidence.

## Current implementation status

Implemented now:

- provider-neutral `DesignModel` and `ModelCapabilities` contracts;
- the bounded automatic synth check/repair loop;
- serializable v1 events consumed by New AI Project;
- automatic handoff of failed build diagnostics into repair;
- `phicad integration capabilities` and the matching MCP handshake tool; and
- tests with a high-capability fake model, so a future adapter is tested by behavior rather
  than model name.

Already present beyond the original integration draft:

- phi/Codex CLI adapters, session continuity and actual schema-binding capability metadata;
- design digests and digest-bound validation/approval in New AI Project;
- a loopback web viewer.

Still proposed or unqualified for background-only product use:

- an enforced MCP allowlist excluding foreground and arbitrary-execution surfaces;
- durable job ownership, resumable progress and cancellation across an entire job tree;
- revision-bound approval at each applicable product mutation boundary;
- the full authenticated action-schema service, if a future host requires it; and
- no-window/focus-preservation qualification against the installed CAD/router binaries.

No computer-use or GUI fallback is part of this background workflow. Existing foreground
features remain separate optional product surfaces; a background agent must not call them.
