# Exact-parts pipeline — BOM-driven design with real, orderable components

Turns "a 3.3 V LDO" into **LCSC C6186 (AMS1117-3.3)** with a project-local symbol,
footprint, and STEP/WRL 3D model bound `${KIPRJMOD}`-relative — so every board the
agents produce renders in the 3D viewer, exports to STEP/GLB, and has a BOM where
every line is a real part that can be put in a cart.

Verified end-to-end against the fork build (kicad-cli 10.99.0) and the live
LCSC/EasyEDA API: fetch → lib-tables → board → audit → 3D render picked up the
fetched model. See the render gate in §Verification.

## The flow

```
   chat: "USB-C powered sensor board…"
      │
      ▼
 [parts-scout]      research: functional need → exact MPN + LCSC id + price/stock
      │             writes/updates parts.json (the ledger, status: proposed)
      ▼
   human/orchestrator approves lines (status: approved)
      │
      ▼
 [parts-integrator] fetch_part.py --parts parts.json
      │             symbol+footprint+3D into <project>/lib/, lib-tables registered,
      │             ledger rows -> status: fetched
      ▼
 [sch-designer]     uses ONLY ledger symbols (project:NAME), sets LCSC field per ref
      ▼
 [pcb-placer/router/…]
      ▼
 audit_parts.py     gate: every footprint has a resolvable 3D model + LCSC/MPN id
```

## The ledger — `parts.json`

One JSON file per project, next to the `.kicad_pro`. The single source of truth
connecting *requirement → chosen part → CAD assets → BOM*.

```json
{
  "project": "sensor-board",
  "parts": [
    {
      "ref_group": ["C1", "C2", "C4"],
      "value": "100nF 50V X7R 0603",
      "requirement": "decoupling, one per VDD pin",
      "mpn": "CC0603KRX7R9BB104",
      "manufacturer": "YAGEO",
      "lcsc": "C14663",
      "package": "0603",
      "stock": 8834556,
      "price_usd_10": 0.004,
      "datasheet": "https://…",
      "sources": ["https://jlcpcb.com/partdetail/…"],
      "status": "approved",
      "notes": "basic part (no setup fee at JLC)"
    }
  ]
}
```

`status` lifecycle: `proposed` (scout) → `approved` (human/orchestrator) →
`fetched` (integrator; gains `symbol`, `footprint`, `model_step`, `fetched_at`) →
`placed` (optional, once on the board).

Required per line: `ref_group`, `value`, `status`, and `lcsc` **or** `mpn`
(fetching needs `lcsc`). Everything else is evidence for the human.

## Tools

`fetch_part.py` runs inside the automation venv (`phicad venv` once, then
`phicad py phicad/parts/fetch_part.py …`) — it shells out to **JLC2KiCadLib**
(MIT), with easyeda2kicad (AGPL-3.0) as a fallback for one release (§Backends).
`audit_parts.py` is stdlib-only (any python3) and parses **both** stable-KiCad
and fork (`version 20260831`) board formats via `phicad/pylib/sexp.py`.

### `fetch_part.py` — LCSC id → project library assets

```bash
phicad py phicad/parts/fetch_part.py --project demos/myboard --lcsc C14663 C6186
phicad py phicad/parts/fetch_part.py --project demos/myboard --parts parts.json
phicad py phicad/parts/fetch_part.py --project demos/myboard --lcsc C14663 \
    --backend easyeda2kicad          # the AGPL fallback, explicitly
```

- fetches symbol + footprint + STEP/WRL 3D model per id (JLC2KiCadLib, LCSC API);
- writes to `<project>/lib/<lib>.{kicad_sym,pretty,3dshapes}` (default lib name
  `project`); 3D binding inside the footprint is `${KIPRJMOD}`-relative;
- registers the lib in `sym-lib-table` / `fp-lib-table` (creates or appends,
  idempotent);
- `--parts` mode fetches every `approved`/`proposed` row with an `lcsc` id and
  promotes it to `fetched` with asset references;
- JSON report on stdout (`--report file` to save); exit 1 if any part failed.

Upstream quirks absorbed by the wrapper (do not fight them by hand):

| Backend | Quirk | What the wrapper does |
| --- | --- | --- |
| JLC2KiCadLib 1.3.2 | `-model_dir` is resolved **relative to the footprint library**, so the binding it stores is `$(KIPRJMOD)/../<lib>.3dshapes/x.step` — wrong directory, unresolvable | rewrites it to `${KIPRJMOD}/lib/<lib>.3dshapes/x.step` in the `.kicad_mod` (idempotent) |
| JLC2KiCadLib 1.3.2 | logs `Creating symbol NAME_0` — a unit suffix that is **not** the symbol name it writes | strips the suffix and confirms the name against the written `.kicad_sym` |
| JLC2KiCadLib 1.3.2 | **exits 0 for an unknown part id**, only logging `- ERROR -` | an `ERROR` log line, a missing footprint file or an unwritten symbol fails the fetch |
| JLC2KiCadLib 1.3.2 | default layout is `symbol/<lib>.kicad_sym` + `footprint/` | `-symbol_lib_dir .`, `-footprint_lib <lib>.pretty`, `-model_dir ../<lib>.3dshapes` reproduce the layout above |
| easyeda2kicad 1.0.1 | `--project-relative` needs an absolute, **symlink-resolved** `--output` (macOS `/tmp`!) and cwd == the project dir | resolves every path, runs with `cwd=<project>` |
| easyeda2kicad 1.0.1 | the output dir must pre-exist | creates `<project>/lib/` first |
| both | proxy env vars break the EasyEDA/JLCPCB API (rule 7) | stripped from the child env (`clean_env()`) |

### Backends — and why the AGPL one is on death row

| Backend (`--backend`) | Tool | Licence | Status |
| --- | --- | --- | --- |
| `jlc2kicad` (default) | [JLC2KiCadLib](https://github.com/TousstNicolas/JLC2KiCad_lib) | **MIT** | the supported path |
| `easyeda2kicad` | [easyeda2kicad](https://github.com/uPesy/easyeda2kicad.py) | **AGPL-3.0** | fallback, **scheduled for removal after one release** |

AGPL-3.0 is incompatible with this fork's GPL-2.0-or-later and its network clause is
triggered by *serving* the tool, which a hosted PhiCad would do — so easyeda2kicad
cannot stay in the production path (`phicad/README.md`, "Third-party components").
It is only ever a subprocess, never vendored.

The fallback is **automatic but loud**: if `JLC2KiCadLib` is not installed,
`fetch_part.py` runs easyeda2kicad and prints a warning naming the licence reason.
An explicitly requested backend is never swapped — `--backend jlc2kicad` with the
tool missing is an error with the `pip install` line. When the removal lands, the
fallback, `--backend`, and the easyeda2kicad entry in `cmd_venv`'s package list go
away together; `run_jlc2kicadlib()` and the rest of the wrapper do not change.

### Backend comparison — measured, both tools on the live API

Six LCSC ids used elsewhere in `phicad/` (2026-09-22; JLC2KiCadLib 1.3.2,
easyeda2kicad 1.0.1). **Symbol pin count, footprint name and pad count were identical
for all six**; the columns below are the only differences:

| LCSC | Pins | Footprint (both) | Pads | Symbol name differs? | 3D model file name (jlc → easyeda) |
| --- | --- | --- | --- | --- | --- |
| C14663 | 2 | `C0603` | 2 | no (`CC0603KRX7R9BB104`) | `C0603` → `C0603_L1.6-W0.8-H0.8` |
| C6186 | 4 | `SOT-223-3_L6.5-W3.4-P2.30-LS7.0-BR` | 4 | **yes**: `AMS1117-3_3` vs `AMS1117-3.3` | `SOT-223-3_…-BR` → `SOT-223-4P_L6.5-W3.5-H1.6-LS7.0-P2.30` |
| C25804 | 2 | `R0603` | 2 | no (`0603WAF1002T5E`) | `R0603` → `R0603` (same) |
| C11702 | 2 | `R0402` | 2 | no (`0402WGF1001TCE`) | `R0402` → `R0402_L1.0-W0.5-H0.4` |
| C92489 | 8 | `LGA-8_BME280_BL` | 8 | no (`BME280`) | `LGA-8_BME280_BL` → `LGA-8_L2.5-W2.5-H0.9-P0.65` |
| C965799 | 2 | `LED0603-RD_RED` | 2 | no (`XL-1608SURC-06`) | `LED0603-RD_RED` → `LED0603-RD_RED` (same) |

Both backends produced a STEP **and** a WRL model for all six ids. Two systematic
differences, neither of which changes what the board sees:

- **Symbol names**: JLC2KiCadLib sanitises `.` to `_` (`AMS1117-3.3` → `AMS1117-3_3`).
  The wrapper reports the name it actually wrote, and the ledger row records it, so
  `sch-designer` stays correct — but a `parts.json` fetched with easyeda2kicad keeps
  the old `symbol` string; re-fetch a row if you switch backends.
- **3D binding**: JLC2KiCadLib binds the **STEP** file, easyeda2kicad binds the WRL
  (both write the pair, and KiCad substitutes the sibling for render/export).

Reproduce with (needs network; ~10 s per id):

```bash
for id in C14663 C6186 C25804 C11702 C92489 C965799; do
  for be in jlc2kicad easyeda2kicad; do
    mkdir -p /tmp/cmp/$be/$id
    phicad py phicad/parts/fetch_part.py --project /tmp/cmp/$be/$id \
        --lcsc $id --backend $be --report /tmp/cmp/$be-$id.json
  done
done
```

### `test_fetch_part.py` — the offline gate

```bash
python3 phicad/parts/test_fetch_part.py     # also in: phicad check fast
```

Stdlib `unittest` with a fake subprocess runner replaying **captured** tool logs: it
pins the JLC2KiCadLib argv (the layout flags above), the log parsing, the
`${KIPRJMOD}` rewrite, the exit-0-on-unknown-id trap, and the fallback decision
(warning names the licence; explicit backends are never swapped). No network, no venv,
no LCSC API.

### `klc_check.py` — the official library-convention gate

Every generated symbol library and footprint directory must pass the upstream KiCad Library
Convention (KLC) checkers before `lib-librarian` declares the part ready:

```bash
python3 phicad/parts/klc_check.py project/lib/project.kicad_sym
python3 phicad/parts/klc_check.py project/lib/project.pretty
# Add --json for {tool, checked, violations, ok} output.
```

The wrapper is stdlib-only and invokes `check_symbol.py` or `check_footprint.py` in a child
process. It does not import checker code. Exit 0 means clean, 1 means KLC warnings or errors
were found, and 2 means the external checker is unavailable. Install a build-local copy with
the command printed for exit 2:

```bash
env -u HTTPS_PROXY -u https_proxy -u ALL_PROXY -u all_proxy -u HTTP_PROXY -u http_proxy \
  git clone https://gitlab.com/kicad/libraries/kicad-library-tools.git build/kicad-library-tools
```

`kicad-library-tools` is GPL-3.0-or-later. PhiCad neither vendors nor copies it: the separately
installed tool is called only as a subprocess, preserving the fork boundary and the tool's
licence terms.

### `audit_parts.py` — the fab-readiness gate

```bash
python3 phicad/parts/audit_parts.py demos/myboard/myboard.kicad_pcb \
    --parts demos/myboard/parts.json --require-id
```

Per footprint: 3D model binding present **and resolvable** (`${KIPRJMOD}` →
board dir; `${KICAD*_3DMODEL_DIR}` → env if set, else "global, unverified");
sourcing id present (`LCSC`/`MPN`/`JLCPCB` property; warning by default,
failure with `--require-id`). DNP and BOM-excluded footprints (fiducials,
logos, mounting holes) are exempt from the id rule; `--exempt 'TP*'` for the rest.
Cross-checks board ids against the ledger when `--parts` is given.

JSON report on stdout, summary on stderr, **exit 1 on gaps** — use as a CI/agent
gate exactly like `kicad-cli pcb drc --exit-code-violations`.

## Part identity on the board

The convention (matches JLCPCB/Fabrication-Toolkit tooling): each schematic symbol
carries an **`LCSC` field** (and optionally `MPN`); the field flows to the footprint
at netlist import. `sch-designer` sets it from the ledger row when instantiating the
symbol; `sch export bom --fields 'Reference,Value,Footprint,LCSC,MPN,${QUANTITY},${DNP}'`
then emits an order-ready BOM with zero manual mapping.

## Research sources, ranked (for parts-scout)

| Source | What it gives | Access |
| --- | --- | --- |
| JLCPCB parts pages / LCSC | stock, price breaks, basic-vs-extended, datasheet | web search + page fetch |
| JLC2KiCadLib hydration | **verifies an LCSC id is real** + gets the CAD assets | `fetch_part.py` |
| DigiKey/Mouser/Octopart | parametric search, western-supply-chain alternates | web search (APIs need keys — out of scope) |
| Datasheets | pinout, land pattern, thermal | follow ledger `datasheet` link |

Direct `curl` against LCSC/EasyEDA *search* endpoints is bot-blocked (CloudFront/
Akamai 403, verified) — search goes through web pages/search engines; **id
verification** goes through the fetch (the components API accepts tool traffic).

Both backends read the same EasyEDA/JLCPCB CAD-asset API, so coverage is identical:
huge (everything JLC can assemble) but not universal: exotic/new parts may have no model. `fetch_part.py` reports these as
failures; the fallback order is: SnapEDA/UltraLibrarian (manual download),
lib-librarian drawing it from the datasheet, or picking a different part.

## Version-boundary note

Fetched assets are plain KiCad-8-era s-expressions — the 10.99 fork reads them
fine (verified: DRC + 3D render + GLB export). They contain nothing fork-specific,
so the libraries stay portable to stable KiCad too.
