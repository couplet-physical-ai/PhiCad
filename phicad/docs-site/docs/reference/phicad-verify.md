# PhiCad Verify

Build a tscircuit design with a pinned engine, then check the result with an independent CAD
engine. This page is the contract: commands, MCP tools, limits, security posture, and what the
result does **not** mean.

## What it is

| | |
| --- | --- |
| **Input** | a `.tsx` / `.ts` tscircuit design |
| **Step 1** | the pinned tscircuit CLI builds it to Circuit JSON |
| **Step 2** | the build is exported to `.kicad_pcb` and checked by this fork's `kicad-cli pcb drc` |
| **Output** | two independent result sets, each with typed findings and tool provenance |

The two verdicts are **never merged**, because a tool's opinion of its own output is not an
independent check.

!!! warning "The CAD check is not a fabrication verdict"
    `kicad-cli pcb drc` evaluates a board against **the design rules recorded in the file it is
    given**. It consults no fabricator's process, capability or acceptance criteria. A DRC-clean
    board can still be unmanufacturable, and a board with violations may be perfectly buildable at
    a given house.

    It is also **not a manufacturing export**. That is `kicad-cli pcb export` (gerbers, drill,
    ODB++, IPC-2581, position files) in the same native PhiCad application — a separate step, which
    this checker does not run.

!!! note "No AI runtime lives here"
    **phi is the only AI harness.** PhiCad Verify contains no model client, no provider
    configuration and no credentials. It is a tool Phi calls over MCP or the CLI.

## Install

The engine is a pinned dependency installed from a committed lockfile — never fetched at run time.

```bash
cd phicad/core
env -u HTTPS_PROXY -u https_proxy -u ALL_PROXY -u all_proxy \
    -u HTTP_PROXY -u http_proxy bun install
```

Proxy variables must be stripped: if your shell points `HTTP(S)_PROXY` at an LLM proxy, the
npm registry is unreachable until they are removed.

| Requirement | How it is found | Override |
| --- | --- | --- |
| `bun` | absolute path, `/opt/homebrew/bin/bun` then `/usr/local/bin/bun` | `PHICAD_BUN` |
| `tsci` | `phicad/core/node_modules/.bin/tsci`, from the lockfile | — |
| `kicad-cli` | this fork's build tree | `PHICAD_KICAD_CLI`, or `PHICAD_BUILD` |

Confirm the installation before trusting a result — every field is a checked fact, not a claim:

```bash
phicad core capabilities        # add --json for machine output
```

A missing binary is always reported as missing. It is **never** downloaded silently, and **never**
reported as a pass.

## CLI

```console
$ phicad core --help
PhiCad Verify — build a tscircuit design, check it independently

  phicad-core capabilities [--json]           what this machine can do, and tool versions
  phicad-core check    <file.tsx> [--json]    evaluate, report tscircuit diagnostics
  phicad-core build    <file.tsx> [--json]    evaluate, then run KiCad's CAD check
  phicad-core manifest <file.tsx> [--json]    build and report artifacts + digests
  phicad-core export   <file.tsx> -f <fmt> -o <out>
```

`phicad check x.tsx` and `phicad build x.tsx` dispatch here on the file extension, so the short
verbs work for both a `.json` design IR and a `.tsx` design.

| Command | Runs the engine | Runs the CAD check | Exit code |
| --- | --- | --- | --- |
| `capabilities` | no | no | always 0 |
| `check` | yes | no | 1 if the design has errors |
| `build` | yes | yes | 1 if either step reports errors |
| `manifest` | yes | no | 1 if the design has errors |
| `export` | yes | no | 1 on export failure |

### Reading a build

```console
$ phicad core build examples/blinky.tsx
PASS  engine        2 components, 4 pads, 2 traces, 18 x 12 mm  (3 warnings)
FAIL  kicad drc     5 errors, 4 warnings
  [995b532f9437] error  drc.clearance
      Clearance violation ( clearance 0.2000 mm; actual 0.1569 mm)
      at:   (104.12, 99.29) mm on F.Cu
      fix:  widen the gap, or lower the clearance for this net class
```

Two lines, two verdicts. `PASS engine` means the engine built the board under its own rules; it is
not a claim about the board.

## Findings

```json
{
  "id": "995b532f9437",
  "code": "drc.clearance",
  "severity": "error",
  "message": "Clearance violation ( clearance 0.2000 mm; actual 0.1569 mm)",
  "refs": [
    "Track [.D1 > .cathode to .R1 > .pin1] on F.Cu, length 7.9212 mm",
    "Pad 1 [.R1 > .pin2 to .D1 > .anode] of D1 on F.Cu"
  ],
  "where": "F.Cu",
  "at": { "x_mm": 104.118094, "y_mm": 99.293094 },
  "fix": "widen the gap, or lower the clearance for this net class"
}
```

| Prefix | Source |
| --- | --- |
| `tsc.` | a tscircuit diagnostic element |
| `drc.` | a KiCad DRC violation |
| `verify.` | PhiCad Verify itself could not complete (timeout, cancellation, stale artifact, missing binary) |

`id` is a sha256 over **(code, sorted refs, position rounded to 1 µm)**, truncated to 12 hex
characters. It excludes the message, so rewording a diagnostic does not invalidate a waiver, and it
rounds position, so a nanometre nudge does not either. Moving a violation a visible distance
legitimately produces a new id.

The derivation matches `phicad/synth/findings.py` exactly. For
`code="drc.clearance", refs=["U1.2","VIN"], where="F.Cu", at=(114.69, 111.53)` both
implementations produce **`ec342ac60052`** with different message text; `test_core.mjs` pins that
value so the two halves cannot drift apart.

## Artifact manifest

```console
$ phicad core manifest phicad/core/examples/blinky.tsx
PhiCad Verify  manifestId 3338fad9f113a6a8
  source    phicad/core/examples/blinky.tsx
  sha256    c3274ba69e4b2976c987e1dc34b72af6d609f4da3cfe75138f16bb572091ac65
  artifacts 2
    blinky.tsx  562 B  c3274ba69e4b2976
    blinky/circuit.json  24444 B  a9fb81082aebec18
  Circuit JSON is a versioned build output of one toolchain, not authored source;
  round-tripping it back to TSX is not supported.
```

`manifestId` is **deterministic** for a given source and toolchain. It is derived from the source
digest, the pinned tool versions and the sorted artifact content digests, and **excludes** paths,
timestamps and durations.

It also normalises two measured sources of engine non-determinism:

| Source | What was measured | Handling |
| --- | --- | --- |
| A nanoid per diagnostic element | two runs differed only in four `*_warning_id` values | generated `*_id` keys removed |
| Diagnostic **order** | parts resolve concurrently; element 52 was `R1/C11702` in one run, `D1/C965799` in the next | elements sorted canonically |

Sorting reorders without discarding, so an added, removed or changed diagnostic still changes
identity. Each artifact therefore carries:

| Field | Meaning |
| --- | --- |
| `sha256` | the raw bytes |
| `contentSha256` | the artifact with generated ids removed — what identity uses |

Both are kept because reproducing an exact byte stream and reproducing a design are different
questions.

### Circuit JSON is a build boundary

The `.tsx` is the source of truth. Circuit JSON is a versioned build/interchange artifact,
`lossless: false`, and round-tripping it back into authored source is **not supported**.

## MCP tools

Four tools on the `phicad` server. Each shells out to the same CLI a human runs, so an agent and a
terminal cannot receive different answers.

| Tool | Arguments | Returns |
| --- | --- | --- |
| `verify_capabilities` | — | tool availability and versions; no side effects |
| `verify_check` | `source_path`, `timeout_s` | design census + tscircuit diagnostics |
| `verify_build` | `source_path`, `timeout_s` | `engine` and `verification` as **separate** result sets |
| `verify_manifest` | `source_path`, `timeout_s` | artifacts, digests, `manifestId`, boundary |
| `verify_circuit_json` | `circuit_json_path`, `output_dir`, `result_path`, `timeout_s` | the Platform service operation: checks the **supplied** document, no TSX re-evaluation |
| `verify_launch_targets` | — | the launch contract and each view's real resolvability |
| `verify_open_view` | `source_path`, `view`, `timeout_s` | a manifest-bound launch request; **emits only, opens nothing** |

`verify_build` never merges its two verdicts:

```json
{
  "engine":       { "ok": true,  "tool": "tscircuit", "findings": [...] },
  "verification": { "ok": false, "tool": "kicad-cli", "skipped": false, "findings": [...],
                    "note": "independent CAD check against the rules recorded in the exported
                             board; not a fabrication-readiness verdict" }
}
```

`verification.skipped` is `true` only when `kicad-cli` is absent, and is **never** a pass: a checker
that could not run is not a checker that was satisfied.

### Timeout and cancellation

`timeout_s` defaults to 300 s and is clamped to 1800 s. A timeout is a structured result, not an
exception:

```json
{ "ok": false, "timed_out": true, "timeout_s": 1,
  "error": "PhiCad Verify exceeded 1 s and was terminated",
  "fix": "raise timeout_s, or simplify the design" }
```

The child is sent `SIGTERM`, then `SIGKILL` two seconds later, so a tool that ignores `SIGTERM`
cannot outlive the caller's decision to stop it. Within the core, cancellation is an `AbortSignal`,
honoured both before spawn and mid-flight.

## The Platform service operation

The PhiCad Platform holds the Circuit JSON already and needs it checked without a
re-evaluation. That is a different operation from `build`, and it has its own argv:

```bash
phicad core verify --circuit-json <in.json> --output-dir <fresh-dir> --result <out.json>
```

### Why it does not take a TSX path

Re-evaluating a source is **not guaranteed to reproduce the artifact the caller is holding** — the
engine mints random element ids and orders diagnostics by whichever supplier lookup finished first
(both measured; see the manifest section). Checking a board the caller never saw and reporting it as
the answer to their question is a confident wrong answer.

So this operation exports **the document it is given** and never reads a `.tsx`. `source_hash` is
always `null`, and `circuit_json_hash` identifies what was actually checked.

**Verified no fidelity is lost:** `tsci export <circuit.json> -f kicad_pcb` and
`tsci export <source.tsx> -f kicad_pcb` produced **byte-identical** boards for the same design
(sha256 `94841b7baf0955f2…`, 8,273 B). Taking the Circuit JSON path removes the re-evaluation and
nothing else.

### Any filename is accepted

The pinned `tsci export` decides whether its input is Circuit JSON **by basename** — it accepts only
`*.circuit.json` or a file named exactly `circuit.json`, and there is no input-format flag. So
`--circuit-json /tmp/platform-real-circuit.json` failed with `Element type … got: array` purely
because of its **name**, while the identical bytes named `circuit.json` passed.

The caller's filename is not a contract term. The service stages a private copy at
`<output-dir>/.phicad-input/circuit.json` and **removes it before artifacts are enumerated**, so it
can never appear in the result. The copy stays inside the supplied directory, needs no network, and
reads no TSX. `circuit_json_hash` is computed from the **original caller-supplied bytes**, never the
copy.

Verified: `not-named-circuit.json`, `platform-real-circuit.json` and `circuit.json` all export a
**byte-identical** board (`94841b7baf0955f2…`) with the same verdict and the same
`circuit_json_hash`.

### Result document — service contract v1 (stable)

| Field | Meaning |
| --- | --- |
| `ok` | the **independent KiCad check only**. tscircuit warnings in the input never flip it |
| `diagnostics` | flat list; **every item carries `origin`** and `severity` |
| `diagnostic_counts` | flat `error`/`warning`/`info` plus `by_origin`. Per-origin *severity* breakdowns are derived by the consumer from `diagnostics[]` |
| `artifacts` | **a list of path strings, relative to `--output-dir`**, each verified inside it |
| `artifact_hashes` | object **keyed by those same relative paths**, sha256 values |
| `tool_versions` | **exact scalar version strings only**: `tscircuit`, `@tscircuit/eval`, `circuit-json`, `kicad-cli`, `bun`, `service-contract` |
| `tool_paths` | resolved binary paths (`bun`, `tsci`, `kicad-cli`) — for diagnosis, kept **out of** `tool_versions` |
| `circuit_json_hash` | sha256 of the document that was checked |
| `source_hash` | always `null` — no TSX is read, so none is claimed |

`tool_versions` holds versions and `tool_paths` holds paths because **a path is not a version**: two
machines can share a path and hold different binaries, which would make a version lock unverifiable.

`--result` may live **inside** `--output-dir`; it is never listed as an artifact, because the result
is a report *about* the run rather than a product of it (and its own hash cannot appear in a file
that does not exist yet).

### Diagnostic origins: exactly two, closed

| Origin | Meaning |
| --- | --- |
| `kicad` | what this checker **independently produced** |
| `tscircuit` | a diagnostic found **inside the supplied Circuit JSON**, relayed unchanged |

That is the whole set (manifest schema **V2**). Origins are **preserved, never merged** — collapsing
them destroys the only property that makes an independent check worth running — and the enum is
**closed**: any other value, including the **retired `openpcb` lane**, is rejected
(`verify.invalid_origin`).

Accepting a value is itself a contract. A consumer that parses an origin this service can never
justify has been handed a promise nobody keeps, so an unknown origin **fails closed** rather than
travelling onward as though Verify had vouched for it.

Origins are **assigned by the service**, never read from the supplied document: an element carrying
its own `origin` field cannot inject one, and such a finding is still reported — relabelled
`tscircuit`, since that is who wrote it into the document — rather than silently dropped. A test
asserts both halves.

```console
$ phicad core verify --circuit-json cj.json --output-dir /tmp/fresh --result r.json
FAIL  kicad check  5 errors, 7 warnings
  artifacts 3 in /tmp/fresh
  result    r.json
  circuit_json_hash d1138ae577addfe4
```

### Refusals

| Condition | Code |
| --- | --- |
| `--output-dir` already holds artifacts | `verify.output_dir_not_fresh` — refused, never mixed into |
| The input sits inside `--output-dir` | `verify.bad_request` (this reason wins over freshness) |
| Not an array of typed elements | `verify.circuit_json_invalid` (names the element index) |
| Unparseable / empty / oversized | `verify.circuit_json_unparseable` / `_empty` / `verify.source_too_large` |
| `kicad-cli` absent | `verify.kicad_cli_missing` — **never a pass**; the independent check is the point |
| The staging copy could not be made | `verify.stage_failed` |
| Board or report predates the run | `verify.stale_artifact_only` / `verify.stale_drc_report` |
| DRC wrote no report | `verify.drc_produced_no_report` — the board is unchecked, which is a failure |

Dotfiles (`.gitkeep`) do not make a directory non-fresh.

**Exit codes:** `0` when the service answered (including a board with violations — that is
`ok: false`, a successful check), `2` when the service **could not** answer. A board's quality and
the tool's health are different questions.

MCP equivalent: `verify_circuit_json`.

## Cross-view navigation

Every product view must be reachable from every other one **for the same manifest-bound project**.
Verify's part of that is to **emit a request**, not to open anything.

```console
$ phicad core open design.tsx --view studio
Open in PhiCad Studio
  project   0712efa622e4e04f
  revision  3338fad9f113a6a8  a9fb81082aebec18
  view      visual-editor  handled by platform-launcher
  NOT resolvable here  no Studio application and no platform launcher are configured
  emitted only — PhiCad Verify does not open other applications
```

### The contract

`io.github.couplet-physical-ai.phicad.launch`, version 1. It carries **only**:

| Field | Purpose |
| --- | --- |
| `project.id` | stable identity, derived from the authored source — **not** the manifest id, so a project does not become a different project when someone edits it |
| `revision.manifestId` | which build the sender believes is current |
| `revision.expectedHash` | the artifact `contentSha256` — stable across the engine's random ids and diagnostic ordering |
| `desiredView` | which view to open, and who handles it |
| `carriesCredentials: false` | asserted by a test over the serialised payload |

**No credentials, ever.** Authorisation belongs to phi (the only harness) and to the
platform launcher — never to a link. A test scans the serialised request for `token`, `secret`,
`api_key`, `password` and `bearer`.

### A stale revision is rejected, never reconciled

`validateLaunchRequest()` returns `action: "open"` or `action: "reject"`. It has no third option.

| Condition | Result |
| --- | --- |
| Identity and revision agree | `open` |
| Receiver holds different content | `reject` — `stale_revision` |
| Different `manifestId` | `reject` — `manifest_mismatch` |
| Different project | `reject` — `project_mismatch` |
| No `expectedHash` to compare | `reject` — `no_expected_hash` (unverifiable is not a pass) |
| Unknown contract version | `reject` — `contract_version_mismatch` |
| Foreign payload | `reject` — `contract_unrecognised` |

Verify **does not** sync, pick a winner, or apply last-writer-wins. Silently choosing a revision for
someone is how two tools come to disagree about what the board is. On a mismatch the caller rebuilds
and issues a fresh request.

### Targets, and what is actually built

| Target | Label | Same application as Verify? | Built here? |
| --- | --- | --- | --- |
| `studio` | Open in PhiCad Studio | **yes** — the native PhiCad application in its Studio editing mode | contract only — needs a launcher that can focus a view |
| `source` | Open Circuit Source | no — the authored source, or the thin web Circuit preview | contract + resolvability check |
| `phi` | Open in phi | no | contract only — needs the platform launcher |
| `docs` | Open Platform Docs | no | contract only — needs a configured docs location |

**`studio` is a mode, not a second editor.** PhiCad is one native authority that provides both Studio
and Verify, so the request carries `mode: "studio"`, `focus: "board-editor"` and
`sameApplicationAsVerify: true` — an intent for the launcher to focus a view, not an instruction to
start another product. The identifier stays `studio` because it names the **view a caller wants**,
and callers should not have to change their code when the implementation behind a view changes.

Sharing a binary does **not** make the target automatically reachable, and it does not relax the
manifest binding. A request still needs something that can focus a view on a project, and an editor
session opened earlier can be holding older content — so a hash mismatch still **rejects**. Verify
never starts the application itself.

The **thin web Circuit preview** stays a separate surface, reported unavailable with a reason until
`PHICAD_CIRCUIT_PREVIEW` is configured.

!!! warning "Honest status: the UI portion is not built"
    Verify emits and validates requests. **Nothing here renders a button, and nothing here launches
    an application.** Routing a request to a running view is the **PhiCad Platform launcher's** job,
    and no launcher is installed on this machine — so with nothing configured **all four targets
    report `resolvable: false` with a reason**. That is deliberate: a dead deep link is worse than an
    honest "unavailable".

    Configure with `PHICAD_PLATFORM_LAUNCHER` (routes a view request), `PHICAD_STUDIO` (names the
    native application when it is not the one running), `PHICAD_CIRCUIT_PREVIEW` (the thin web
    preview surface) and `PHICAD_PLATFORM_DOCS`.

### Version and help probes

```console
$ phicad core --version
PhiCad Verify (phicad-core), service contract v1
  tscircuit          0.0.2561
  @tscircuit/eval    0.0.1407
  circuit-json       0.0.491
  service-contract   1
```

`--version --json` emits the same under `tool_versions`; `--help` prints usage and exits 0. Both
work without a positional file, because a packaging or media probe that runs them first should not
make a working install look broken. An **unknown** flag is still an error (exit 2).

## Limits

Every limit is finite and produces its own failure code. Nothing is silently truncated — a trimmed
JSON document parses as garbage, which is worse than a reported failure.

| Limit | Value | Result |
| --- | --- | --- |
| Wall clock | 300 s (`timeout_s`, max 1800 s) | `verify.timeout` |
| Child output | 8 MiB | `verify.output_limit` |
| Source file | 2 MiB | `verify.source_too_large` |
| Artifact file | 64 MiB | `verify.artifact_too_large`; in a manifest, recorded with `oversized: true` and no digest |
| Artifact count | 2000 | manifest `truncated: true` |

## Security posture

**Environment.** Children receive an **allowlist**: `PATH`, `HOME`, `TMPDIR`, `LANG`, `LC_ALL`,
`TERM`, `USER`, `SHELL`, plus `NO_COLOR=1` and `CI=1`. A denylist of secret-shaped names is always
incomplete, so nothing else propagates — no `*_TOKEN`, `*_KEY`, `*_SECRET`, no LLM gateway
credentials, and no `*_proxy`. A test asserts that an unlisted variable does not reach a child.

`PATH` is the one documented inheritance, because the toolchain shells out to platform utilities. It
follows that **a hostile entry on `PATH` is in scope** for anything the build tool invokes.

**No shell.** Arguments are passed as a vector with `shell: false`, so a path containing a space or
a metacharacter cannot become another command.

**Pinned binaries.** Execution uses `node_modules/.bin/tsci` installed from the committed lockfile,
run by an absolute `bun` path. `bunx` is never used: it resolves and may download at run time.

**No network assumption.** The engine is expected to work from its local install and cache.
`bun install` needs the registry; a build does not. tscircuit's parts engine may attempt lookups for
a design that requests supplier data.

**Isolated output.** Every run gets a fresh directory and the source is staged into it, because
`tsci build <abs-path>` writes its output next to the **source**. Artifacts are accepted only if
written during the current run; anything older is rejected as `verify.stale_artifact_only`.

### Known limitations

| Limitation | Consequence |
| --- | --- |
| The engine is 73 upstream npm packages | supply-chain surface is the lockfile's; audit it as such |
| Arbitrary TSX is **executed** to build a design | treat an untrusted design as untrusted code; the environment allowlist limits blast radius but does not sandbox the filesystem |
| `PATH` is inherited | a hostile `PATH` entry is in scope |
| Engine ids are random per run, and diagnostic order varies | byte-identical rebuilds are unavailable; `contentSha256` normalises both |
| `kicad-cli pcb drc` exits 0 with violations | exit status is never used as the verdict; the report is the only truth |
| The CAD check reads only the exported board | rules not expressed in that file are not checked |

## Third-party components

| Component | Licence | Use |
| --- | --- | --- |
| `tscircuit`, `@tscircuit/cli`, `@tscircuit/eval` | MIT | pinned build engine, invoked as a binary |
| `circuit-json` | see upstream | interchange schema |
| KiCad (this fork) | GPL-2.0-or-later | independent CAD check; upstream attribution and notices preserved |
| PhiCad Studio | GPL-2.0-or-later | **the same native application as Verify**, in its Studio editing mode — not a separate product and not a separate repository |

KiCad is a trademark of the KiCad project. Exact installed versions are reported by
`phicad core capabilities --json`.

## Tests

```bash
bun phicad/core/test_core.mjs        # 14 findings/contract tests
bun phicad/core/test_hardening.mjs   # 46 execution, manifest and launch tests
bun phicad/core/test_service.mjs     # 29 Platform service-operation tests
```

The hardening suite covers success, syntax failure, runtime throw, timeout, cancellation (pre-spawn
and mid-flight), stale output, oversized output, oversized source, oversized artifact, missing
binary, missing source, environment isolation, and deterministic manifest identity — including two
real engine builds that must agree, because synthetic fixtures cannot see ordering
non-determinism.
