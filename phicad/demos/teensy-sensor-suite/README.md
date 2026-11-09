# Teensy 4.0 Prototyping Sensor Suite — PhiCad demo board

A generated, openable 2-layer carrier board for prototyping an I²C sensor suite on a
Teensy 4.0. It exists to exercise the fork end to end: it is written as s-expressions by
a script, opened by `kicad-cli`, DRC'd, rendered, exported to gerbers/STEP, and read back
over the IPC API by an agent.

![top](render-top.png)

## The design

| Ref | Part | Role |
| --- | --- | --- |
| J1, J2 | 1×14 2.54 mm headers, 15.24 mm (0.6 in) row pitch | Teensy 4.0 sockets |
| U2 | Bosch LGA-8 (`LSM6DSO`) | IMU on the I²C bus |
| U3 | Bosch LGA-8 (`BME280`) | environmental sensor on the I²C bus |
| R1, R2 | 4k7 0805 | SDA / SCL pull-ups to 3V3 |
| C1, C2 | 100 nF 0805 | per-sensor decoupling |
| C3 | 10 µF 0805 | 3V3 bulk |
| D1, R3 | LED 0805 + 1k | status LED on a spare GPIO |
| J3, J4 | 1×4 headers | I²C sensor breakouts (GND / 3V3 / SDA / SCL) |
| J5 | 1×5 header | SPI breakout (GND / SCK / MOSI / MISO / CS) |
| H1–H4 | 3.2 mm | M3 mounting holes |

Signals follow the real Teensy 4.0 pinout: I²C on pins 18/19 (SDA/SCL), SPI on 11/12/13
with CS on 10. Board is 50 × 43 mm with chamfered corners, a `GND` pour on B.Cu, and
3V3 + I²C routed on F.Cu.

## Regenerating

```bash
build/venv/bin/python phicad/demos/teensy-sensor-suite/generate_board.py
# or write elsewhere:
build/venv/bin/python phicad/demos/teensy-sensor-suite/generate_board.py -o /tmp/scratch
```

Output is deterministic — UUIDs are `uuid5` of a stable namespace, so regenerating an
unchanged design produces a byte-identical file and `git diff` shows only real changes.
Footprints come from the stock libraries under
`~/Library/Application Support/kicad/stock/footprints/`.

## Three fork-specific syntax facts

Verified against a board **written by this fork**, not assumed from upstream 10.0:

1. Placement is `(transform (translate X Y) (rotate A) (scale 1 1))`, not `(at x y rot)`.
2. A pad's `(at …)` is its **local, unrotated** offset from the footprint origin; the
   footprint's `(rotate …)` applies on top, so pad coordinates don't change when a part
   is rotated.
3. Nets are referenced **by name only** — this fork writes no numeric `(net N "name")`
   table. A net exists purely by being named on a pad, segment or zone.

To re-derive them: copy a fixture to `/tmp`, open **and save** it through the fork
(saving is what rewrites a board to version `20260831`; opening alone does not modify
it), then read the result.

## Checking it: `check_routing.py`

DRC is the authority, but it costs ~25 s and reports violations in board coordinates that
are tedious to trace back to the generator's intent. `check_routing.py` reads the emitted
board and answers the same questions in **0.07 s** — 350× faster — in the generator's own
vocabulary:

```bash
python3 check_routing.py            # exit 0 = clean, 1 = problems
```

It checks, in the generator's own vocabulary:

| Check | Catches |
| --- | --- |
| track-to-track clearance, per layer, between different nets | shorts and crossings |
| tracks through holes they don't own (skipping the pad's own net) | a lane run through a header pin |
| refdes overlapping pads or another refdes | `silk_over_copper`, `silk_overlap` |
| zone outline vs the board edge | a pour overhanging the chamfers |
| track endpoints and pads vs the board edge | a drop column that walked off the board |
| more than one track width on a single net | a call site that forgot to pass one |
| zero-length segments | dangling stubs |

For clearance failures it prints the worst offenders with coordinates and net names, which
is what makes a fix obvious rather than guesswork.

Every one of those checks except the first two was added **after** the corresponding bug had
already shipped past the others, so each is a regression test as much as a check. Each was
also verified by deliberately reintroducing its bug in a throwaway fixture and confirming it
fires — a check that has never failed on purpose is not known to work.

Run it after every generator edit; run DRC once it is clean. This loop is what took the
routing from 32 conflicting net pairs to zero.

## How long does prompt → PCB take?

Two very different numbers, and conflating them is how "AI designs a PCB in seconds" claims
get made.

**Regenerating this board: 0.21 s**, measured end to end — edit a constant, emit the
`.kicad_pcb`, run the full geometric check, and produce a PNG you can look at:

| Step | Time |
| --- | --- |
| `generate_board.py` (18 footprints, 101 segments, 35 vias, 27 nets, 4305 lines) | **0.02 s** |
| `check_routing.py` (seven checks: clearance, holes, silk, zone, edge, widths, dangling) | **0.16 s** |
| `plot_board.py` → SVG + PNG | **0.12 s** |
| whole loop | **0.41 s** |

For comparison `kicad-cli pcb drc` is ~25 s, so the fast loop is roughly **350×** quicker.
That ratio is the entire reason this board got to zero violations: it makes a wrong guess
cost a fifth of a second instead of half a minute.

**Designing it the first time: about 2 hours of agent work**, in six commits (18:23 → 20:13
on 2026-09-02), and it was not one prompt — it was a feedback loop:

| | |
| --- | --- |
| first board that opened and exported | ~25 min, **46 DRC violations** |
| routing rebuilt on a declared corridor/lane plan | conflicting net pairs 32 → **0** |
| silkscreen placed per part instead of a fixed offset | 15 warnings → **0** |
| real DRC run after the checker said CLEAN | **26 errors** → 0 (5 further commits) |
| last 4 advisory warnings fixed, not downgraded | 4 → **0** |
| J5 orientation + SPI approach fixed | found only by **plotting and looking at it** |

So the honest answer: **a change to an existing board is sub-second; a new board is hours,
and most of that is iteration, not generation.** The generator is fast; getting the design
*right* is the work. What the tooling buys is that each iteration is 0.2 s instead of 25 s.

One lesson worth keeping: the numeric checks all passed while J5 sat with its pads off the
bottom edge of the board and its SPI tracks landing on bare laminate. **A picture caught
what four checks missed** — which is why `plot_board.py` exists and why it is part of the
loop rather than an afterthought. Looking at the rendered board then found three more faults
in a row that no check modelled: a ground pour overhanging the chamfers, the 3V3 drop column
sitting on the west board edge, and two I2C trunks silently wider than their own taps. Each
is now a check, but the render is what found them.

The corollary is uncomfortable and worth stating: **"CLEAN" only ever means "none of the
things I thought to measure".** Every check in the table above was added after its bug had
already passed all the earlier ones.

## Verifying: `./verify.sh`

One command runs everything: regenerate, determinism check, `check_routing.py`, DRC,
both renders, and all four exports, then prints a PASS/FAIL table.

```bash
./verify.sh          # full run
./verify.sh -q       # summary only
```

Before doing any work it checks two things that otherwise masquerade as a broken board:

1. **Directory services.** If `opendirectoryd` is wedged, *every* `kicad-cli` invocation
   aborts with `Abort trap: 6` / exit 134 and **no message at all** — identical to a corrupt
   board. The script tests `dscl` first and tells you to `sudo killall opendirectoryd`.
   (Other symptoms of the same fault: `sysmond service not found`, `ps ax` truncating, DNS
   failing. The tell in the unified log is `mbr_check_membership_ext(): Input/output error`.)
2. **Stale kifaces.** If `libkicommon`/`libkigal` were rebuilt but `_pcbnew.kiface` was not
   relinked, board loading aborts the same way. The script relinks if ninja reports pending
   steps.

Both were hit while building this demo, and both cost real time because the failure mode is
a silent 134 with no output. Rule the environment out before debugging the board.

## Verification status

| Check | Result |
| --- | --- |
| `check_routing.py` | **CLEAN** — 0 clearance/short pairs, 0 hole violations, 0 silk overlaps, 101 segments |
| `plot_board.py` (no kicad-cli needed) | `plot-current.png` — renders the **current** revision |
| silkscreen placement | all 18 refdes clear their nearest pad (min gap 0.38 mm) and each sits by its own part |
| regenerate twice | byte-identical (`md5` match) — deterministic |
| s-expression integrity | balanced parens; 18 footprints, 96 segments, 35 vias, 1 zone |
| `kicad-cli pcb drc --refill-zones` | **0 errors, 0 warnings, 0 unconnected** |
| `kicad-cli pcb render` top + bottom | OK (see `render-top.png`, `render-bottom.png`) |
| `kicad-cli pcb export svg` / gerbers / step | OK — 21 gerber files |
| read back over IPC (`session_inspect.py`) | 18 footprints, 75 pads, 27 nets, 106 tracks, 35 vias, 1 zone |

> **Re-measured 2026-09-03 after a reboot, and now fully clean.** Every row above is from
> the current revision: `./verify.sh` passes all ten steps, and DRC reports
> **0 errors, 0 warnings and 0 unconnected items** -- the board is fully routed.
>
> An earlier version of this note claimed the 10 unconnected items were deliberate
> (`J2.VIN` with no consumer, `J3/J4.GND` returning through the pour). **That was wrong, and
> wrong in a way worth recording:** the actual 10 were the spare VDD and GND pads of the two
> LGA-8 sensors, which the router never touched. A floating VDD pad is a real defect -- the
> die bonds to all of them, so leaving one open puts the supply through a single bond wire
> and defeats the decoupling. They are now bonded (see `bond_power_pads`). Do not describe
> an unconnected item as intentional without reading the DRC item list.
>
> The earlier caveat here said these numbers were unverified because `kicad-cli` aborted on
> every board-loading command. That was chased to a root cause and it was **not** a board or
> fork defect: **`opendirectoryd` had died**, so `kicad-cli` aborted with SIGABRT while
> resolving the user at startup. Two corrections to what that caveat used to claim:
>
> * `kicad-cli version` does **not** keep working. The wedge degrades; by the second
>   occurrence even `version` aborted. Do not use it as an "environment is fine" probe.
> * `sudo killall opendirectoryd` is **not** a fix -- `sudo` needs the very daemon that is
>   down (`you do not exist in the passwd database`), and `launchctl kickstart` fails with
>   `141: Reentrancy avoided`. **Only a reboot works.** `verify.sh` now checks `dscl` first
>   and refuses to run rather than reporting misleading failures.

### Suppressions: 9 → 4, each removal earned

`rule_severities` started with nine downgraded checks, which is how a board reports "46
violations" and still looks acceptable. Six of the nine were **stale or simply wrong**, and
were removed once the underlying fault was measured and fixed rather than assumed cosmetic:

| Removed | Why it was safe to remove |
| --- | --- |
| `solder_mask_bridge` | tightest mask web on the board is **+0.200 mm** against a 0.100 mm limit. This one should never have been downgraded — a merged aperture between two nets shorts them in reflow. |
| `silk_over_copper`, `silk_overlap` | 0 refdes-over-pad and 0 refdes-over-refdes overlaps after `REF_POS` placed labels per part |
| `silk_edge_clearance` | every refdes box sits ≥ 0.2 mm inside the outline |
| `pth_inside_courtyard`, `npth_inside_courtyard` | 0 holes fall inside another footprint's courtyard (14 courtyards checked) |

The four that remain are structural, not cosmetic hand-waving:

| Kept | Why |
| --- | --- |
| `lib_footprint_mismatch` (ignore) | the generator strips the library Reference/Value and emits its own, so **every** footprint mismatches its library by construction |
| `footprint_type_mismatch` (ignore) | stock through-hole footprints reused for headers this board defines itself |
| `lib_footprint_issues` (warning) | belongs to the stock library, not to this board |
| `copper_sliver` (warning) | zone-fill artefact; the filler resolves it and it cannot be evaluated without running the filler |

The general rule this taught: **measure the thing, then delete the suppression.** Downgrading
a check because it is inconvenient converts a real defect into a clean-looking report.

### Honest DRC status

The board **opens, renders and exports**. The routing is geometrically clean by construction
(see the corridor plan below), and the silkscreen overlaps were **fixed rather than
suppressed** — `REF_POS` gives each part a reference-designator offset that clears its own
pads and its neighbours, which is what removed the `silk_over_copper` / `silk_overlap`
population. What remains downgraded in `rule_severities` is genuinely advisory, for the
reasons listed below.

Counts from the last successful DRC run of the previous revision (before the silk fix):

| Type | Count |
| --- | --- |
| `solder_mask_bridge` | 9 |
| `lib_footprint_mismatch` | 8 |
| `silk_over_copper` | 8 |
| `silk_overlap` | 7 |
| `shorting_items` | 3 |
| `hole_clearance` | 2 |
| `courtyards_overlap` | 2 |
| `pth_inside_courtyard` | 2 |
| `via_dangling` | 2 |
| `tracks_crossing` | 1 |
| `clearance` | 1 |
| `npth_inside_courtyard` | 1 |

What they are, and why they are left:

- **`shorting_items` / `clearance` / `tracks_crossing` (5)** — the F.Cu routing is
  hand-written centre-line geometry in the generator, so a few segments pass closer to a
  pad than the 0.2 mm clearance allows. These are the ones a real design would fix by
  routing properly (or by letting the router do it).
- **`solder_mask_bridge` (9)** — 0805 pads and 2.54 mm header pads sit close enough that
  their mask apertures merge. Normal for this geometry; fixed with a mask-expansion rule.
- **`lib_footprint_mismatch` (8)** — expected and harmless: the generator strips the
  library's `Reference`/`Value` properties and emits its own, so the board footprint is
  intentionally not byte-identical to the library original.
- **`silk_over_copper` / `silk_overlap` (15)** — stock silkscreen outlines overlapping
  pads and each other at this density. Cosmetic.
- **`hole_clearance` / `*_inside_courtyard` / `via_dangling` (7)** — stitching vias and
  courtyards near the M3 holes and header rows.

## How the routing is kept legal

The first revisions picked every track coordinate by hand and produced 20–30 crossing and
short errors, because whether two tracks collided depended on component positions that kept
moving. The generator now derives routing from three declared plans, and *nothing* picks a
coordinate by hand:

- **`CORRIDOR`** — one reserved x per F.Cu trunk (`SDA`, `SCL`, `3V3`), 2 mm apart. Power
  is deliberately on the **far side** of the sensor column from the signals, so a part's
  power tap and signal taps leave in opposite directions and never cross.
- **`LANE`** / **`DROP`** — one reserved y *and* one reserved drop column per B.Cu
  crossing, allocated from a single ordered list so sharing either is impossible by
  construction. The band clears the last header pin row by ≥ `min_hole_clearance` + pad
  radius.
- **`JOG`** — per-pad escape offsets for pads that share a row (an LGA row, a header on
  2.54 mm pitch tapping trunks 2 mm apart). Escapes leave **perpendicular** to the row and
  step sideways in proportion to the jog, so two pads escaping by different amounts also
  use different columns.

`tap()` then routes a pad to its trunk, automatically diving under any *other* trunk in the
way via `bcu_cross()`. Because it reads `CORRIDOR`, placement can change freely and the
routing stays legal.

Do not treat this board as a fabrication-ready reference — it has no schematic, and the
remaining warnings would want a real review. Treat it as proof the toolchain works on a
non-trivial design, and as a fixture for agent-driven edits.

## Bugs this demo caught

Worth recording, because each produced a *silently* wrong board rather than an error:

- Pad numbers in a `.kicad_mod` are **strings**. Passing `{1: "GND"}` matched nothing and
  left every header pin netless — the file loaded fine and looked right in a render, but
  had 28 fewer nets. Fixed by normalising keys with `str()`.
- Attaching a net after the pad's `(layers …)` line breaks on stock pads that carry
  trailing keys such as `(remove_unused_layers no)`; only the first pad got a net. The net
  is now emitted just before the pad's closing paren.
- A vertical 1×14 header already runs pin 1 → pin 14 along **+y** at rotation 0. Rotating
  it 270° laid both rows along −x, overlapping each other — which DRC reported as 8
  co-located holes rather than as a placement error.
- **A GND stub was attached to J1 pin 1, which is `T_D0` on a Teensy 4.0 — not ground.**
  The Teensy's left row has no ground pin at all; ground is on J2 pins 1 and 13. The
  geometric check surfaced this only as "a track sits exactly on a hole of another net";
  the real fault was electrical. Worth remembering that a clean geometry check does not
  imply a correct netlist.
- **Two pads of one part can be electrically equivalent but not interchangeable.** A
  BME280's pads 1 and 7 are both VDD, but pad 1 sits west of the SDA pad while the power
  trunk is east, so a tap from it had to cross SDA. No jog fixes that — the part had to be
  rotated 180° so its VDD pads face the power trunk. Rotating U3 removed the last
  clearance violations on the board.
- **`%` binds tighter than the surrounding subtraction.** `TEENSY_X - 4.0 - 1.2 * i % 5`
  collapsed four SPI lanes onto nearly the same corridor, which appeared as 20
  `tracks_crossing` errors rather than as an arithmetic mistake.
