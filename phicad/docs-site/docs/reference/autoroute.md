# Autoroute

A real autorouter, driven headless. `phicad autoroute` runs the whole Specctra loop as one
command — export the board to DSN, route it with [Freerouting](https://github.com/freerouting/freerouting),
import the session back:

```
board.kicad_pcb  --(kicad-cli pcb export dsn)-->  .dsn
                 --(freerouting.jar, headless)-->  .ses
                 --(kicad-cli pcb import-ses)-->  board-routed.kicad_pcb
```

```bash
phicad autoroute board.kicad_pcb                        # -> board-routed.kicad_pcb
phicad autoroute board.kicad_pcb --passes 5 --drc       # DRC before and after
phicad autoroute board.kicad_pcb --in-place --keep work/ --json
phicad autoroute board.kicad_pcb --cleanup --drc        # one shot to a DRC-clean board
phicad autoroute board.kicad_pcb --no-refill            # leave the pours stale (rarely wanted)
```

Source of truth: [`phicad/autoroute/README.md`](https://github.com/couplet-physical-ai/PhiCad/blob/master/phicad/autoroute/README.md)
(the code lives in `phicad/autoroute/`).

## Why this path exists

Every other routing path in the fork is closed on a dense multi-layer board:

| Path | Why it doesn't scale |
| --- | --- |
| PNS (`pcbnew/router/`) | interactive-only; rejected headless |
| `phicad/synth/route.py` | 2-layer-shaped grid A* that refuses rather than guess |
| model-emitted tracks | one track at a time against DRC — r0.2 took ~180 one-off scripts over 70 DRC iterations |

Freerouting is a real autorouter that speaks Specctra, and this fork can export DSN and import
SES **without the GUI** (`kicad-cli pcb export dsn`, `kicad-cli pcb import-ses` — upstream had the
capability but only `PCB_EDIT_FRAME` could reach it).

## What the driver does — and refuses to do

It is glue: it writes no copper of its own, holds no routing policy, and leaves every
intermediate file on disk (`--keep DIR` keeps the `.dsn`, the `.ses`, and the router's full log)
so any step can be re-run by hand. `--timeout` is enforced while the router's log is being
drained, not just between phases — a dense board that stalls mid-route is killed on budget
instead of running away.

It does **not** judge the result. Freerouting exits 0 on a partial route, so the driver
re-reports the router's own final line (`N unrouted, M violations`) and, with `--drc`, the fork's
DRC counts before and after plus which violation types are new. **Those numbers are the answer;
the exit code is not.**

Two post-import steps keep the result honest:

- **Refill zones** (default, `--no-refill` to opt out): a zone's filled polygons are cached in
  the board file, so freshly routed copper sits over pours knocked out for the *old* copper.
  On the `regulated-breakout` example this alone accounted for 23 phantom DRC violations that a
  refill cleared completely. It runs *after* rule carrying, so pours fill against the design's
  rules, not stock defaults.
- **Cleanup** (opt-in, `--cleanup`): deletes dangling copper and widens sub-minimum necks that a
  refill cannot fix — driven only by DRC's own findings, and judged against a fresh fill.

Order: route → import → carry rules → refill → cleanup. Both steps are also exposed to agents
as MCP tools.

## What the routed board keeps

Gaps found by routing real boards, each fixed in the driver because otherwise the board it
hands back fails DRC for reasons the router never caused:

- **Project rules travel with the result.** A board finds its net classes in the sibling
  `.kicad_pro` and custom rules in `.kicad_dru`, matched by file stem — neither path is recorded
  inside the board, so `b-routed.kicad_pcb` would otherwise silently lose all of them and DRC
  would gate against defaults.
- **Pours are refilled** (default; `--no-refill` opts out). A zone's filled polygons are cached
  in the file, so new copper leaves every pour knocked out for the *old* copper and filled over
  the new. Measured on `regulated-breakout`: a plain `pcb drc` reported **23 violations**
  (19 clearance, 4 hole_clearance) that a refill cleared completely. The refill runs *after* the
  sidecars are carried, so the pour is filled against the design's rules and not stock defaults.
- **Router debris is cleared on request** (`--cleanup`, destructive). Freerouting also leaves
  artifacts a refill cannot fix — on that board a single dangling via, the last violation
  standing. `--cleanup` sequences `phicad/autoroute/cleanup.py --apply`, which deletes dangling
  vias/tracks and widens sub-minimum necks, driven only by DRC's own findings, after the refill
  so it judges a fresh fill. Opt-in because it removes copper.
- **BGA fan-out** — power and ground balls are fanned out onto their planes first
  (`phicad/autoroute/stitch.py`), which is what makes a dense BGA routable at all.

With the refill and `--cleanup`, one command takes a placed board to a board the fork's own DRC
calls clean — `23 → 1 → 0` violations, `0` unconnected on the example above.

## Requirements

| | |
| --- | --- |
| `freerouting.jar` | `--jar`, else `$FREEROUTING_JAR`, else `~/.kicad-mcp/freerouting.jar` ([releases](https://github.com/freerouting/freerouting/releases)) |
| Java | any JRE on `PATH`, or `--java`; verified on OpenJDK 26 with Freerouting v2.3.0 |
| `kicad-cli` | **this fork's** — `$PHICAD_KICAD_CLI` or `$KICAD_CLI` (either spelling), else the repo build tree. Stable KiCad has no `pcb export dsn` and would rewrite the board with the 10.0 serializer |
| Python | stdlib only — no venv, no IPC session |

!!! note "Headless means headless"
    Freerouting is a Swing application and starts a GUI unless stopped; the driver always passes
    `-Djava.awt.headless=true`, which is what makes it a batch job.

## Verified result

On `prototype1-sensing-node-r1.kicad_pcb` — 90 footprints, 59 nets, 317 connected pads, four
copper layers, an i.MX RT1062 in a 196-ball 0.8 mm BGA, placed and completely unrouted — the DSN
carried all 59 nets, 317 pin references and 90 placements, and Freerouting ran to completion
headless.

## Fine-pitch BGA escape — measured 2026-09-10

Whether the router can escape a fine-pitch BGA was an open question carried as a known
failure. It is now a committed measurement:
`phicad/autoroute/measurements/bga-escape-r1-2026-09-10.json` (measured on a private board; not published).
A 15-pass run on that board — 0.80 mm pitch, 196 balls, **two** signal layers, 0.1/0.1 mm
rules, 0.4/0.2 mm vias:

| | placement in | routed out |
| --- | --- | --- |
| signal balls with same-net copper on the pad | **0 / 36** | **31 / 36** |
| power/ground balls with same-net copper on the pad | 34 / 53 | **51 / 53** |
| unconnected items (zones refilled) | 216 | **18** |
| DRC errors (zones refilled) | 0 | 2 (`track_width`, one 0.075 mm neck) |

Escape works. The seven remaining balls are attributed individually in the measurement and
none is channel exhaustion — the deepest signal rings are fully escaped while the open balls
sit at rings 1–3. They are the 90 Ω USB pair (no differential-pair support in the router), a
rail with no plane, two one-pitch same-net links to already-escaped siblings, and two nets
routed at the far end but never brought into the package.

!!! warning "Escape and net completion are different claims"
    The unconnected count cannot separate them: a net with one open link reports one item no
    matter *which* of its pads is stranded, so a ball that escaped cleanly still appears there
    when the far end is what is missing. `phicad/autoroute/escape_audit.py` reports the two
    separately, off the board's copper, and names the balls in each state. Give it a DRC report
    produced **with `--refill-zones`** — unfilled, every plane stitch reads as dangling and the
    same board reports 258 unconnected instead of 216.

```bash
kicad-cli pcb drc --format json --severity-all --refill-zones -o drc.json routed.kicad_pcb
python3 phicad/autoroute/escape_audit.py routed.kicad_pcb --ref U1 --drc-report drc.json

# gate a board against the committed numbers (exit 1 on drift)
python3 phicad/autoroute/escape_audit.py routed.kicad_pcb --ref U1 --drc-report drc.json \
    --expect phicad/autoroute/measurements/bga-escape-r1-2026-09-10.json
```

`phicad/autoroute/test_escape_audit.py` runs in `phicad check fast` (stdlib only): it pins the
audit tool's geometry and the measurement's internal consistency, so the ~22-minute routing run
stays out of the inner loop without the recorded numbers going stale.

## Where it fits

- Inside the app, the Agent View's design loop calls the same driver — see
  [The application](../guides/the-application.md).
- For one-net-at-a-time interactive routing, use the PCB editor's push-and-shove router as usual.
- For the fully generative path (description → placed, routed board), see
  [Board synthesis](synth.md), which hands dense boards to this driver.
