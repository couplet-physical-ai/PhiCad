# Agent telemetry

Local, file-based records of what the AI agent suite actually did — how long turns take,
how often they fail and why, and which synth stage is failing builds.

```bash
phicad telemetry                 # last 7 days
phicad telemetry --days 30
phicad telemetry --json          # same numbers, for a dashboard
phicad telemetry --where         # where the files are, and whether it is on
```

Nothing to stand up. One JSON object per line, one file per day, in
`~/.phicad/telemetry/`. `jq` works on it.

## What is recorded

Two events. There is no single choke point — the product reaches the model and the
compiler by four different routes, and all four are instrumented, tagged by `source`:

| `source` | route |
| --- | --- |
| `chat_pane` | `ChatSession.ask()` — someone typing in AI View |
| `design_agent` | `PhiCliDesignModel.ask()` — the automatic repair loop's own `phi` call |
| `new_project` | the New Project window's build, which streams stages instead of `--json` |
| `mcp` | `server.py::_synth()` — an external agent driving the toolchain |

Instrumenting only the first would have reported a minority of real activity while
looking authoritative, which is worse than reporting nothing.

**`ai.turn`** — one per user turn.

| | |
| --- | --- |
| `session_id`, `turn` | a random per-conversation UUID, and the turn number within it |
| `backend`, `backend_version`, `backend_source` | which CLI answered, and how it was found |
| `has_context`, `context_bytes` | whether live board context was attached, and how big |
| `prompt_bytes`, `reply_bytes` | sizes only — see below |
| `duration_ms`, `timeout_s` | how long it took, against the limit it had |
| `outcome` | `ok`, `timeout`, `stopped`, `exit_nonzero`, `empty_reply`, `launch_failed`, `json_mode_unsupported` |
| `exit_code`, `signal`, `error_type` | whichever applies to that outcome |
| `model`, `provider` | who actually answered this turn, from the backend's own usage report (the user can switch models mid-session) |
| `tokens_in`, `tokens_out`, `tokens_reasoning`, `cache_read_tokens`, `cost_usd` | provider-reported usage, copied verbatim — absent when the backend reported nothing |

**`synth.run`** — one per compiler invocation.

| | |
| --- | --- |
| `command` | `check` or `build` |
| `streaming` | whether the caller asked for live stage lines |
| `stages_total`, `stages_passed`, `failed_stages` | which gates ran, and which failed by name |
| `duration_ms`, `timeout_s`, `outcome` | `ok`, `failed`, `timeout`, `no_json`, `launch_failed`, `error` |
| `run_id`, `attempt` | set when a repair loop is driving; see below |

### Repair cycles

`DesignValidationWorkflow` checks the same design once per repair attempt, so one user
action can file eight failures on its way to a design that ends up valid. Every record is
true on its own, but counting them flat would headline three self-repairing designs as
"failed 24, ok 3". Runs carrying a `run_id` are folded into one cycle and judged on their
last attempt; `phicad telemetry` reports cycles and attempts separately.

## What is deliberately not recorded

**No prompt or reply text, ever.** A prompt to this tool is a circuit someone is
designing and the reply is the design — the most confidential thing on the machine. Sizes
answer the questions a size can answer (is context blowing up, are replies being cut off)
without keeping the content. No project paths either: a path names the design as surely as
the design does.

`telemetry_selftest.py` asserts this against the real call site, with a known string pushed
through `ask()` and the log searched for it. Break the guarantee and check 5 fails.

**No derived token counts.** Token and cost fields are recorded only when the backend
reported them itself — the adapters run `phi --print --mode json` and copy its per-turn
usage verbatim (`chat_backend.TurnUsage`). A turn through a backend that reports nothing
records bytes and no usage at all: a byte count scaled by a guessed ratio is not a token
count, and putting one in a field called `tokens` would make every cost figure derived
from this file a fabrication. Absent field = not measured. Cost per design is therefore
a real query: `phicad telemetry` sums `cost_usd` per model, and the Grafana dashboard's
“Cost (USD), by model” / “Spend in range” panels do it fleet-wide.

## Turning it off

```bash
export PHICAD_TELEMETRY=0            # 0/false/no/off — writes nothing at all
export PHICAD_TELEMETRY_DIR=/path    # somewhere other than ~/.phicad/telemetry
```

## Reporting

Durations are reported as **median and p90**, never as a mean. One 300-second timeout among
twenty two-second turns moves a mean by fifteen seconds and tells you nothing about what a
turn is normally like. The median stays put; the p90 is where the timeout shows up.

## Grafana

```bash
export PHICAD_INFLUX_URL=https://influx.internal.example
export PHICAD_INFLUX_ORG=example
export PHICAD_INFLUX_BUCKET=phicad
export PHICAD_INFLUX_TOKEN_FILE=~/.phicad/influx-token    # chmod 600

phicad telemetry ship --dry-run    # see the line protocol, send nothing, no token needed
phicad telemetry ship              # send what has not been sent yet
```

Then import `grafana-dashboard.json` and point it at the bucket.

**Shipping is a separate step, not a sink on the write path.** A design session must never
wait on a metrics endpoint, and a laptop that is offline, on a plane, or outside the VPN
must still record everything and catch up later. The file stays the source of truth; the
shipper is a reader over it, and it is resumable — a cursor records the byte offset reached
in each daily file, so re-running sends only what is new. It ships whole lines only, so a
file being appended to while it runs is picked up exactly once.

Run it from cron, or by hand. There is **no default endpoint**: an endpoint that silently
accepts writes to the wrong place is worse than a missing one, so it refuses to guess.

### Cardinality, which is the thing that goes wrong

InfluxDB builds an index entry per unique combination of tag values. A UUID in a tag
creates one series per session forever — the index outgrows the data, queries slow to a
crawl, and the fix is a rewrite. So:

| | |
| --- | --- |
| **tags** | `event`, `source`, `outcome`, `command`, `backend`, `failed_stage`, `model`, `provider` — closed vocabularies, a few dozen combinations in total |
| **fields** | `session_id`, `run_id`, `attempt`, and every duration and byte count |

`session_id` and `run_id` are the fields you most want to group by, and they are exactly
the ones that must not be tags. Grafana can still filter on a field; it is the index that
cannot afford them.

`failed_stage` is derived rather than recorded. The record carries `failed_stages`, a list,
and a list cannot be a tag without one series per *combination* of gates — but the compiler
stops at the first failing gate, so the first name is both the useful one and a closed
vocabulary of about fourteen. That single tag is what makes "which gate is failing builds
this week" a one-line query instead of a text search.

### Sending it somewhere else entirely

`telemetry.set_sink(fn)` hands each finished record to `fn` as well as writing the file.
The sink runs inside the same exception guard as the write, so a sink that raises loses its
own record and nothing else. Use it for a live push if you want one — but prefer the
shipper, for the reason above.

## Failure policy

Telemetry never raises. A full disk or a read-only home directory must not be able to end a
design session, so every public function swallows its own errors. The cost is silent loss,
which is the right trade for a metrics file and the wrong one for anything else — so nothing
else uses this module.
