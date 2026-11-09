# Circuit JSON → native PhiCad (`phicad core apply`)

`phicad core apply` turns a [tscircuit](https://tscircuit.com) **Circuit JSON** document into a
real native KiCad project — a `.kicad_pcb`, `.kicad_pro`, `.kicad_sch` and `.net` produced by the
[synth pipeline](synth.md) — and records the change as **exactly one** project revision.

It is deliberately not a converter. It is a *plan-then-commit* service with a closed element
policy: every element type in the input is mapped, ignored with a stated reason, or rejected with
a typed code. An element type it has never heard of fails closed.

```bash
phicad core apply --circuit-json <in.json> --project <dir-or-.kicad_pro> \
                  --plan <plan.json> [--result <result.json>] \
                  [--expected-revision <hash>] [--commit]
```

| Mode | Behaviour | Exit |
| --- | --- | --- |
| without `--commit` | **plan only.** Not one byte of the project may change. | `0` planned, `3` rejected |
| with `--commit` | apply the plan: **one** revision plus **one** undo entry, or the project is left byte-identical. Never a partial write. | `0` applied, `3` rejected/unavailable |

The two documents are the deliverable; the console output is a convenience. `plan.json`
(`io.github.couplet-physical-ai.phicad.apply-plan/v1`) is key-sorted, newline-terminated and self-hashing, so the
same input bytes against the same project state give a byte-identical plan — diff two plans to see
what changed. `result.json` (`io.github.couplet-physical-ai.phicad.apply-result/v1`) adds the revision accounting.

## Why plan-then-commit

A board is a shared artifact under version control, and "the tool rewrote my project and I am not
sure what it did" is unrecoverable in review. So:

- **`plan_hash` binds the two phases.** The hash in `result.json` is the hash of the plan that was
  reviewed. If they match, the commit applied exactly that plan.
- **`--expected-revision` makes the write optimistic-concurrent.** A mismatch is
  `status: rejected`, code `stale_revision`, and no write. The project revision is re-checked
  *after* planning as well, so a concurrent editor cannot slip between plan and commit.
- **A missing capability is `status: unavailable`, never a silent success.** No `kicad-cli`, no
  project, no Git history, or a dirty working tree each produce a reason string and no write.

## Coverage: the closed element policy

`phicad/synth/circuit_json.py` holds one table, `ELEMENT_POLICIES`, and every element in the input
is looked up in it. There are three verdicts, and a fourth outcome for anything absent.

| Verdict | Meaning | In the plan |
| --- | --- | --- |
| `mapped` | becomes native geometry, connectivity or placement | `create_footprint`, `create_net` |
| `ignored` | deliberately not carried across, with a reason | `ignore_element`, `ignore_field` |
| `rejected` | design intent the Design IR cannot express | `rejections[]` + `coverage.rejected` |
| *absent* | **fails closed** — `unknown_element_type` | `rejections[]` |

The fourth row is the point of the table. A new tscircuit element type cannot quietly become a
board that silently omits it; it stops the plan until someone decides what it means.

### Derived artifacts versus design intent

The interesting line is not "PCB versus schematic" — it is **who owns the geometry**.

A courtyard, a paste aperture and a package silkscreen outline are *outputs of a footprint*. When
such an element is attached to a component this translation mapped, the native stock footprint
already emits the same artifact, and the package identity is not a guess: every pad had to match
the selected native footprint's pad geometry **to within 1 nm** or the design is rejected. Ignoring
the engine's copy of a 0402 courtyard therefore loses nothing.

Free-standing, the same element types are intent that the Design IR has no field for — a silk logo,
a hand-drawn courtyard, a paste aperture that is not a pad — and they are rejected. So:

| Element | Attached to a mapped component | Free-standing |
| --- | --- | --- |
| `pcb_courtyard_rect` | ignored — the native footprint emits it | **rejected** |
| `pcb_solder_paste` | ignored — generated from the footprint's own pads | **rejected** |
| `pcb_silkscreen_path` | ignored — package silk is native | **rejected** |
| `pcb_silkscreen_text` | ignored **only if** the text is the refdes or the value | **rejected** |

`pcb_silkscreen_text` carrying anything else — `DO NOT POPULATE`, a revision marking — is rejected,
because the native silk stage emits reference designators and that marking would disappear.

### What is rejected, and what it would take to accept it

| Element | Why it is rejected |
| --- | --- |
| `pcb_trace` | **Copper is the board.** The Design IR expresses *connectivity*, and synth's route stage produces the copper. Accepting an engine-routed board and then re-routing it would return a different board from the one described. Accepting it losslessly needs an explicit-route field in `design-ir.schema.json` plus a board writer that emits segments — a real feature, not a flag. |
| `schematic_component`, `schematic_port`, `schematic_trace`, `schematic_group` | The coordinates refer to the *engine's own* symbol geometry. Native `Device:R` and `Device:LED` symbols have different bodies and pin positions, so placing native symbols at engine coordinates produces a wrong-looking schematic, not a faithful one. PhiCad generates a native schematic from the same netlist and cross-checks it pad-for-pad (the `sch-netlist` gate) plus ERC. |
| `pcb_via`, `pcb_zone`, `pcb_keepout`, `pcb_hole`, `pcb_plated_hole`, `pcb_cutout`, `pcb_slot` | No Design IR representation. A rectangular outline with a ground-pour flag cannot carry a cutout or a keepout, and approximating one is worse than refusing. |

Connectivity survives every one of these rejections: nets come from `source_port`
connectivity keys and `source_trace`, never from `pcb_trace`.

### The 3D body, and why footprints are never rewritten

`cad_component` is the one element with a genuinely narrow line, because of a defect this
repository already measured: **kipy footprint round-trips destroy `(model …)` nodes** (measured
model = 6 → 0; see the docstring on `phicad/synth/emit.py::set_positions`). So this pipeline never
moves or rewrites a footprint through kipy — it uses emit.py's lossless text-edit discipline, or it
rejects.

| `cad_component` carries | Verdict |
| --- | --- |
| a custom model (`model_obj_url`, `model_step_url`, `model_jscad`, …) | **rejected** — attaching it means rewriting the footprint's `(model …)` node, which is the lossy path |
| rotation about x or y (a tilted or flipped body) | **rejected** — no native equivalent |
| `position.z < 0` (a body inside the board) | **rejected** |
| `position.z > 0` (the ordinary case) | **`ignore_field`**, with a reason |

That last row is a judgement worth stating plainly. `z` is where the engine puts the 3D body; the
native stock footprint carries its own `(model …)` node placed by the library for that exact
package, and it is preserved untouched. Rejecting every real engine document over a 3D preview
offset would refuse designs while losing no information by accepting them — but the omission is
still recorded in the plan as an `ignore_field` operation, because a silent omission is the thing
this service exists to prevent.

## Worked example: a real engine document

The `blinky` design — a 1 kΩ 0402 resistor in series with a red 0603 LED on an 18 × 12 mm two-layer
board — as produced by the pinned tscircuit engine: **53 elements, 25 distinct types**.

Applied unmodified, it is **rejected**, and that is the correct answer:

```console
$ phicad core apply --circuit-json blinky.circuit.json --project board/ \
    --plan plan.json --result result.json --commit
$ echo $?
3
```

```json
{ "status": "rejected",
  "counts": { "components": 2, "pads": 4, "nets": 1, "traces": 1, "vias": 0, "zones": 0 },
  "coverage": { "rejected": ["pcb_trace", "schematic_component", "schematic_group",
                             "schematic_port", "schematic_trace"] } }
```

Twenty of the twenty-five types are accounted for; the engine had already routed the copper and laid
out its own schematic, and neither is representable. The project is left byte-identical, with no new
revision.

The same document with the engine's own layout output removed — the 9 `pcb_trace` and `schematic_*`
elements, i.e. "PhiCad, you do the layout" — plans and commits:

```json
{ "status": "applied",
  "plan_hash": "<sha256 of plan.json>",
  "revision": { "before": "2b75485…", "after": "a71d1ab…", "undo_entries": 1 } }
```

(`plan_hash` is illustrative here: its value is the SHA-256 of the exact plan document, so a
literal hash on this page would go stale; your run prints the real one.)

and produces a real native project:

- `LED_SMD:LED_0603_1608Metric` and `Resistor_SMD:R_0402_1005Metric` — stock native footprints,
  with **both `(model …)` nodes intact**;
- one routed copper segment on net `series`, `F.Cu`, 0.2 mm;
- `kicad-cli pcb drc`: **0 violations, 0 unconnected items, 0 schematic-parity errors**;
- exactly **2** revisions (the baseline and the apply), one undo entry, and a clean working tree.

## Environment

| Variable | Purpose |
| --- | --- |
| `PHICAD_KICAD_CLI` | the fork's `kicad-cli`. Required for `--commit`; stable KiCad is not a substitute |
| `PHICAD_VENV` | the automation venv (`phicad venv`). The netlist-import stage needs `kipy` |
| `PHICAD_SYNTH_PYTHON` | an explicit interpreter for the synth subprocess, overriding `PHICAD_VENV` |

The child process environment is built from an allowlist plus those pointers, so a credential in
the calling shell never reaches a board writer or a DRC run.

## Tests

```bash
python3 phicad/synth/test_circuit_json.py    # the element policy, the nm boundary, determinism
python3 phicad/synth/test_circuit_apply.py   # plan purity and every rejection path (stdlib + git)
bun phicad/core/test_apply.mjs               # the service contract, incl. a real commit (SLOW)
bun phicad/core/test_hardening.mjs           # undo accounting, limits, cancellation, env
```

The first two are stdlib-only and run in `phicad check fast`. Every rejection test asserts the
project is byte-identical afterwards by hashing every file before and after — not by reading a
status field.
