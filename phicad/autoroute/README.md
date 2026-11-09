# autoroute — a real autorouter, driven headless

`freeroute.py` runs the whole Specctra loop as one command:

```
board.kicad_pcb  --(kicad-cli pcb export dsn)-->  .dsn
                 --(freerouting.jar, headless)-->  .ses
                 --(kicad-cli pcb import-ses)-->  board.kicad_pcb
```

```bash
phicad autoroute board.kicad_pcb                        # -> board-routed.kicad_pcb
phicad autoroute board.kicad_pcb --passes 5 --drc       # DRC before and after
phicad autoroute board.kicad_pcb --in-place --keep work/ --json
```

## Why

Every other routing path in this fork is closed on a dense multi-layer board. PNS
(`pcbnew/router/`) is interactive-only and is rejected headless. `phicad/synth/route.py`
is a 2-layer-shaped grid A* that refuses rather than guess. What was left was a model
emitting one track at a time against DRC — the r0.2 revision was routed that way, in
~180 one-off scripts over 70 DRC iterations, and it does not scale to a 196-ball BGA.

Freerouting is a real autorouter that speaks Specctra, and as of the commit that added
this directory the fork can export DSN and import SES **without the GUI**
(`kicad-cli pcb export dsn`, `kicad-cli pcb import-ses` — the capability existed in
`pcbnew/specctra_import_export/` but only `PCB_EDIT_FRAME` could reach it).

## What it does and does not do

It is glue: it writes no copper of its own, holds no routing policy, and leaves every
intermediate file on disk (`--keep DIR` gives you the `.dsn`, the `.ses` and the router's
full log) so any step can be re-run by hand.

It does **not** judge the result. Freerouting exits 0 on a partial route, so the driver
re-reports the router's own final line (`N unrouted, M violations`) and, with `--drc`, the
fork's DRC counts before and after plus which violation types are new. Those numbers are
the answer; the exit code is not.

Two things it *does* do to the board after the import, because otherwise the board it
hands back fails DRC for reasons the router never caused:

| step | why | escape |
| --- | --- | --- |
| **refill zones** (default) | a zone's filled polygons are cached in the file, so new copper leaves every pour knocked out for the *old* copper. Measured on `regulated-breakout`: a plain `pcb drc` reported **23 violations** (19 clearance, 4 hole_clearance) that a refill cleared **completely** | `--no-refill` |
| **cleanup** (opt-in) | Freerouting also leaves artifacts a refill cannot fix — on the same board one dangling via, the last violation standing. `--cleanup` sequences `cleanup.py --apply`, which deletes dangling copper and widens sub-minimum necks, driven only by DRC's own findings | omit `--cleanup` |

Order is fixed and load-bearing: carry the project sidecars → refill (so the pour is filled
against the design's rules, not stock defaults) → cleanup (so it judges a fresh fill).
With both, one command takes a placed board to a board the fork's own DRC calls clean:

```
phicad autoroute placed.kicad_pcb --in-place --passes 8 --cleanup --drc
  copper   0 seg / 0 via  ->  110 seg / 5 via  (243.8 mm)
  cleanup  removed 1 dangling via(s), 0 dangling track(s)
  zones    1 refilled against the carried rules
  drc      0 violations / 27 unconnected  ->  0 / 0
```

## Requirements

| | |
| --- | --- |
| `freerouting.jar` | `--jar`, else `$FREEROUTING_JAR`, else `~/.kicad-mcp/freerouting.jar` ([releases](https://github.com/freerouting/freerouting/releases)) |
| Java | any JRE on `PATH`, or `--java`. Verified on OpenJDK 26 with Freerouting v2.3.0 |
| `kicad-cli` | **this fork's** — `$KICAD_CLI`, else the repo build tree. Stable KiCad has no `pcb export dsn` and would rewrite the board with the 10.0 serializer |
| Python | stdlib only (no venv, no IPC session) |

Freerouting is a Swing application and starts a GUI unless stopped; the driver always
passes `-Djava.awt.headless=true`, which is what makes it a batch job. Its options were
established by running it, not from documentation: `-de` design in, `-do` session out,
`-mp` autoroute pass ceiling, `-mt` optimizer threads.

## Verified

On `prototype1-sensing-node-r1.kicad_pcb` (90 footprints, 59 nets, 317 connected pads,
four copper layers, an i.MX RT1062 in a 196-ball BGA, placed and completely unrouted):
the DSN carried all 59 nets, 317 pin references and 90 placements; Freerouting ran to
completion headless; the session imported to 643 segments and 44 vias; **unconnected
items fell 258 → 68** and the only new DRC violation class was `track_width` (39) — the
router lays 0.2 mm where a net class wants more. Zero new clearance violations. That was
a deliberately short 3-pass run; the pass ceiling is `--passes`.

## Does it escape a fine-pitch BGA? Measured, 2026-09-10

`measurements/bga-escape-r1-2026-09-10.json` is the committed answer, and
`escape_audit.py` is what produced it. A 15-pass run on the same board
(0.80 mm pitch, 196 balls, **two** signal layers, 0.1/0.1 mm rules, 0.4/0.2 vias):

| | placement in | routed out |
| --- | --- | --- |
| signal balls with copper on the pad | **0 / 36** | **31 / 36** |
| power/ground balls with copper on the pad | 34 / 53 | **51 / 53** |
| unconnected items (zones refilled) | 216 | **18** |
| DRC errors (zones refilled) | 0 | 2 (`track_width`, a 0.075 mm neck) |

So escape works. The seven balls without copper are attributed individually in the
measurement and **none of them is channel exhaustion** — the deepest signal rings (4+)
are fully escaped while the open ones sit at rings 1–3. Two are the 90 Ω USB pair the
router has no differential-pair support for, one is a rail with no plane, two want a
one-pitch same-net link to an already-escaped sibling, and two are nets routed at the far
end but never brought into the package.

This supersedes the conclusion in a private board project's
`hardware/kicad/r1/validation/ROUTING-ATTEMPTS.md`, which recorded 28 unconnected and 2
errors at the same 15 passes and read the residual as a BGA escape/fanout limit.

**Escape and net completion are different claims**, and the unconnected count cannot
tell them apart: a net with one open link reports one item no matter which of its pads is
stranded, so a ball that escaped cleanly still shows up there when the far end is what is
missing. `escape_audit.py` reports them separately and names the balls in both states.

```bash
# after a routing run, with a REFILLED DRC report
kicad-cli pcb drc --format json --severity-all --refill-zones -o drc.json routed.kicad_pcb
python3 phicad/autoroute/escape_audit.py routed.kicad_pcb --ref U1 --drc-report drc.json

# gate a board against the committed measurement (exit 1 on drift)
python3 phicad/autoroute/escape_audit.py routed.kicad_pcb --ref U1 --drc-report drc.json \
    --expect phicad/autoroute/measurements/bga-escape-r1-2026-09-10.json
```

The refill is load-bearing: unfilled, a via whose only job is to drop a ball onto an inner
plane reads as dangling, and the same board reports 258 unconnected instead of 216.
`test_escape_audit.py` (stdlib, in `phicad check fast`) pins the tool's geometry and the
measurement's consistency; the routing run itself is the `reproduce` block in the JSON.
