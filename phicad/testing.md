# Testing, quality gates and automation surfaces

Anchored to `qa/`, `cmake/KiCadQABuildUtils.cmake`, `tools/check_coding.sh` and `api/` in this
checkout. Wrapper commands come from `phicad/bin/phicad`.

## 1. How the suite is wired

- `KICAD_BUILD_QA_TESTS=ON` (default) pulls in `qa/` (`CMakeLists.txt`, `qa/CMakeLists.txt`).
- `cmake/KiCadQABuildUtils.cmake` defines the meta-targets `qa_all_tests`, `qa_all_tools`,
  `qa_all`, and `kicad_add_boost_test(target,name)` which registers a CTest entry per binary.
- Tests are **Boost.Test** executables (`qa/qa_utils/include/qa_utils/wx_utils/unit_test_utils.h`).
- `qa/CMakeLists.txt:setup_qa_env` puts the build `kicad`, `common`, `api`, `common/gal`,
  `pcbnew`, `eeschema`, `cvpcb` dirs on `PATH`, sets `KICAD_RUN_FROM_BUILD_DIR=1` and
  `KICAD_BUILD_PATHS`. Running a QA binary by hand without that env can load a stale/installed
  KIFACE — use `phicad boost`, which sets it for you.
- Fixture data lives in `qa/data/` (compiled-in default root; override with `QA_DATA_ROOT`).
- **Do not set `KICAD_CONFIG_HOME` yourself when running a QA binary.**
  `KI_TEST::SetMockConfigDir()` (`qa/qa_utils/wx_utils/unit_test_utils.cpp:116`) points it at
  `qa/data/config` *and* sets `KICAD_INHIBIT_SETTINGS_WRITES` — but only if the variable is
  unset. Pre-setting it skips the write guard and the run dirties tracked fixtures
  (`qa/data/config/*/kicad_common.json` turns up modified in `git status`).

## 2. Test targets

| CTest name(s) | Binary / source | Covers |
| --- | --- | --- |
| `qa_kimath` | `qa/tests/libs/kimath/` | geometry/math primitives, arcs, béziers, polygons, R-trees |
| `qa_sexpr`, `qa_kinng` | `qa/tests/libs/` | s-expression parser, NNG IPC wrapper |
| `qa_common` | `qa/tests/common/` | strings/IDs/settings/files, plotting, jobs, libraries, git, DB |
| `qa_eeschema` | `qa/tests/eeschema/` | schematic load/save, connectivity, ERC, symbols, importers |
| `qa_pcbnew_zone`, `_allegro`, `_creepage`, `_ipc2581`, `_tracks_cleaner`, `_multichannel`, `_drc_regression`, `_drc_solder_mask`, `_constraints`, `_other` | one binary `qa_pcbnew` (`qa/tests/pcbnew/CMakeLists.txt`) | board model/IO, zones/fill, DRC, constraints, router, import/export |
| `qa_api` | `qa/tests/api/` | protobuf IPC API handlers, jobs, actions (180 s timeout) |
| `qa_spice` | `qa/tests/spice/` (needs `KICAD_SPICE_QA=ON`) | ngspice simulation, netlist export |
| `qa_gerbview`, `qa_diff_merge` (+ `qa_diff_merge_mergetool_e2e`, `_workflow_e2e`) | `qa/tests/gerbview/`, `qa/tests/diff_merge/` | gerber parsing, diff/merge, mergetool end-to-end |
| `qa_pns_regressions` | `qa/tools/pns/` — **only with `-DKICAD_BUILD_PNS_DEBUG_TOOL=ON`** (`qa/tools/CMakeLists.txt:35`); absent from a default build | push-and-shove router regressions |
| `qa_cli` | `qa/tests/cli/` (pytest, not C++) | end-to-end `kicad-cli` behavior |
| `qa_mcp_registry`, `qa_mcp_tools`, `qa_mcp_handshake` | `phicad/mcp/test_*.py` via `qa/tests/mcp/CMakeLists.txt` (python, not C++) | the MCP server's 123 tools — see §3b |

`qa/fuzz/` is opt-in (`KICAD_BUILD_FUZZ_TESTS=OFF`). `qa/tools/*` are utilities, not tests.

## 3. Commands

```bash
# build only what you need
phicad/bin/phicad build qa_kimath
phicad/bin/phicad build qa_all_tests          # everything (slow)

# run
phicad/bin/phicad tests                       # list registered ctest names
phicad/bin/phicad test '^qa_kimath$'          # one registration
phicad/bin/phicad test 'qa_pcbnew_drc'        # a group
phicad/bin/phicad test                        # whole suite, parallel (10+ min)

# single Boost suite / case, with QA env set for you
phicad/bin/phicad boost qa_common                       # whole binary
phicad/bin/phicad boost qa_common LibId                 # one suite
phicad/bin/phicad boost qa_common LibId/ParseValid --log_level=all
phicad/bin/phicad boost qa_pcbnew --list_content        # discover suite names
```

Python CLI tests (need a built `kicad-cli` and `pip install -r qa/tests/requirements.txt`):

```bash
KICAD_CLI=build/mac-dev/kicad/PhiCad.app/Contents/MacOS/kicad-cli \
KICAD_RUN_FROM_BUILD_DIR=1 QA_DATA_ROOT=qa/data \
python3 -m pytest -c qa/tests/cli/pytest.ini qa/tests/cli -q
```

## 3a. The fork layer (`phicad/`) — no C++ build needed

Everything above tests upstream KiCad through `qa/`. The PhiCad layer in `phicad/` is python
and shell, has its own suites, and is gated separately — **one command runs all of it**:

```bash
phicad/bin/phicad check fast    # nothing but python3 needed; this is what CI runs (~40 s)
phicad/bin/phicad check         # also the venv/kicad-cli suites (run before a release cut)
```

`check fast` is the one to run habitually. The full `check` also drives real boards through
real tools, so it takes **~7 minutes** — it is a pre-release/pre-PR gate, not an inner-loop
command.

| Gate | What it covers | Needs |
| --- | --- | --- |
| compile | `compileall` over `phicad/` — catches a syntax error in any tool | python3 |
| shell syntax | `bash -n` on `phicad/bin/phicad`, `phicad-tool` | bash |
| style | `phicad/bin/check_style.py` — 120 columns (code only), tabs, trailing whitespace, final newline | python3 |
| schemas | `phicad/bin/check_schemas.py` — the published host, measurement, design, and feedback contracts parse, every `required` name exists, and shipped IR/measurement plus the synthetic feedback example validate | python3 (+`jsonschema` for full validation) |
| release install smoke | `phicad/bin/test_release_smoke.py` — release configuration cannot target `/usr/local`; clean staging, required installed bundle content, signature invocation, and a CLI launch with build-tree environment removed | python3 (temporary fixtures; no C++ build) |
| version identity | `phicad/bin/test_version_command.py` — exact PhiCad release tag or `unreleased`, commit, dirty state, native CLI version, and JSON output | python3 + git (temporary fixture repo) |
| support bundle privacy | `phicad/pylib/test_support_bundle.py` — fixed archive shape, `0600` output, independent time/output/file/directory/entry/byte bounds, path/secret redaction, safe JSON summary, and project-metadata-only opt-in | python3 (temporary fixtures; no user files) |
| docs release source | `phicad/docs-site/test_docs_release.py` and `test_feedback_widget.py` — nav/assets, local links, video fallback, feedback privacy boundary, and GitHub draft behavior | python3 (+Node for the runtime widget cases when available) |
| ai-view suites | `phicad/plugins/ai-view/test_*.py` — repair loop, digests, workflow events, gate parsing, and window layout (§3d) | python3 (Tk tests self-skip) |
| ruff | `pyproject.toml` at the repo root — pyflakes + pycodestyle over `phicad/**/*.py`, and the python-3.9 syntax boundary (§3c) | `ruff>=0.13` |
| python 3.9 boundary | `/usr/bin/python3 -m py_compile` on the files `phicad` runs with bare `python3` (§3c) | a real 3.9 interpreter |
| mcp registry | `phicad/mcp/test_registry.py` — checks on tool shape, docs, the py3.9 boundary, and the starter jobset's explicit fail-closed DRC settings | `build/mcp-venv` |
| swarm selftest | `phicad/swarm/selftest.py` — netlist parsing and partitioning agree across both s-expression styles, round-trips are lossless, `sexp` accessors refuse a bare atom instead of counting zero, and `score_board` pairs a candidate with its project's real rules | python3 |
| dev loop selftest | `phicad/loop/test_loop.py` — backlog item format and validation (the shipped `backlog/` must validate), pick order with dependencies/requirements/pending PRs/attempts, scope globs, gate-summary parsing, secret discipline (no credential in argv or brief), cost accounting and termination, fixed authorization-window expiry and host watchdog installation, and scratch-origin iterations with a fake `phi` (worktree, autosave, status flip, push, outbox, PAUSE/BREAKER) | python3 + git |
| DRC open classification selftest | `phicad/pylib/drc_opens_selftest.py` — pad-pad, pad-track, track-via, zone island, isolated copper, ambiguous findings, raw preservation, and UUID/object-type evidence | python3 |
| ibom generic-json builder | `phicad/pylib/test_ibom_json.py` — 33 checks on `phicad/pylib/ibom_json.py`: captured `pcb export pos` CSV, IPC-D-356 and Edge.Cuts SVG text in, InteractiveHtmlBom's generic-JSON document out (Y flipped once, 0.0001 inch → mm, through vs SMD pads, component/footprint length equality, nets, outline from the SVG or flagged as a guess) plus the structural validator saying no to eight broken documents | python3 |
| parts KLC adapter | `phicad/parts/test_klc_check.py` — captured official-checker output parsing, structured error/warning verdicts, subprocess isolation, and wrapper exit codes 0/1/2 | python3 (offline; checker not needed) |
| parts fetch backends | `phicad/parts/test_fetch_part.py` — JLC2KiCadLib argv (the flags that put symbol/footprint/3D in the layout `audit_parts.py` can resolve), captured-log parsing for both backends, the `${KIPRJMOD}` model-binding rewrite, the exit-0-on-unknown-id trap, and the backend choice (AGPL fallback warns and names the licence; an explicit backend is never swapped) | python3 (offline; fake subprocess runner, no backend or network needed) |
| synth library paths | `phicad/synth/test_library_paths.py` — Linux/macOS roots, project precedence, explicit override, spaces, and missing-root diagnostics | python3 (temporary fixtures; no installed libraries) |
| synth selftest | `phicad/synth/selftest.py` — library, IR, netlist, place, route, emit, silk, symbols, schematic, board | `build/venv` |
| mcp tool suite | `phicad/mcp/test_tools.py --quick` — 164 checks over 29 real tool calls on real boards; the only gate that catches a tool which imports fine but no longer works | both venvs + built `kicad-cli` |

A missing venv or unbuilt `kicad-cli` is reported as **SKIP with the command that fixes it**,
never as a failure — so `check` is safe to run on a bare checkout, and a red result always
means a real regression. When another worktree already has the missing venv or CLI, the skip
also prints the `export` that reuses it, because a checkout without its own build tree is
normal here (worktrees are cheap; a build tree is ~3.5 h / 16 GB).

A gate process that exits 2 is different: it means the thing being checked could not run (for
example, a syntax error or a startup crash). `run_gate` reports that result as **BROKEN** and
fails the overall check. `skip_gate` remains reserved for a genuinely missing optional
prerequisite, so an absent linter or venv is still a deliberate **SKIP**.

`phicad/mcp/test_tools.py --quick` runs inside the full `check`. Its `--full` mode (exports,
the jobset, a real IPC session) and `test_handshake.py` need a built CLI — run those via
`phicad mcp selftest`, `test_tools.py --full`, or the ctest registrations in §3b when touching
the MCP transport.

Ruff is configured entirely in the repo-root `pyproject.toml` — which is **tool config only**:
no `[project]`, no `[build-system]`, nothing that makes the repo an installable package, and
nothing the C++ build reads. Run it with no arguments:

```bash
ruff check .          # ~2 s cold; scope and rules come from pyproject.toml
```

`phicad check` runs this too, so a lint error is caught before the PR rather than by CI (it is
*skipped*, not failed, when ruff is not installed — a missing dev tool must not read as a broken
branch). Install it with `pip install ruff` in any venv on PATH, or in `build/mcp-venv`.

The rule set (`E`, `F`, `W`) is deliberately **exactly what is already clean on the tree**, so
the gate landed without reformatting a line — the house rule against sweeps outranks a bigger
rule set. Rules left off are listed in `pyproject.toml` with their measured violation counts
(`I` 13, `UP` 403, `D` 223, `EM` 218, `PLR` 489, …); enabling one is its own PR, and `UP` in
particular would rewrite the 3.9 files to 3.10 syntax and must never be run here. `ruff format`
is not configured and must not be used on this tree.

CI: `.woodpecker/ci.yaml` on Roadrunner (Woodpecker + Dagger; `gh rr status`), step **fork-layer**, runs on every push
and PR: `phicad check fast`, the headless ai-view suites, the schema contracts, `ruff check`,
the python-3.9 byte-compile, `phicad/mcp/test_registry.py`, and a `demos/` cleanliness check.
It deliberately does not build C++ (a cold build is ~3.5 h / 16 GB), so the two MCP suites that
shell out to `kicad-cli` are not in it. Upstream's own pipelines live in `.gitlab-ci.yml` and
are untouched.

## 3b. The MCP suites (`phicad/mcp/test_*.py`)

Three suites, 689 checks, registered with CTest by `qa/tests/mcp/CMakeLists.txt` so
`phicad tests` lists them and `phicad test` runs them. They are standalone python programs
with their own runners, not Boost and not pytest — the MCP venv holds `fastmcp` and must not
grow a test framework.

| CTest name | Source | Checks | Needs a built `kicad-cli`? |
| --- | --- | --- | --- |
| `qa_mcp_registry` | `test_registry.py` | all static checks | **no** — static tool shape + the `phicad-tool` CLI |
| `qa_mcp_tools` | `test_tools.py --quick` | 206 at `--full` | yes — shells out to it |
| `qa_mcp_handshake` | `test_handshake.py` | 16 sections | yes — spawns `server.py` as an MCP client child |

```bash
phicad/bin/phicad test '^qa_mcp'            # all three
phicad/bin/phicad test '^qa_mcp_registry$'  # the one that needs no build

# or directly, which is what CI does for the registry suite
build/mcp-venv/bin/python phicad/mcp/test_registry.py
phicad/bin/phicad-tool registry-test
```

Notes on the registration:

- The interpreter is `build/mcp-venv/bin/python` (fastmcp, **never** kipy), not
  `${PYTHON_EXECUTABLE}`. Override the location with `-DPHICAD_MCP_VENV=<dir>`; the automation
  venv the workers use is `-DPHICAD_AUTOMATION_VENV=<dir>`.
- Every registration is guarded on that interpreter existing, so a checkout without
  `phicad mcp setup` still configures — it just registers no MCP tests, and says so at
  configure time.
- Each test gets a private `TMPDIR` / `PHICAD_MCP_TEST_ROOT` / `PHICAD_MCP_OUT` under
  `<build>/qa/tests/mcp/<test-name>/`, so `ctest -j` cannot make them collide. The suites also
  isolate themselves internally (`mkdtemp` inside that root); the ctest env is the second layer,
  because a gate must not depend on the thing it gates.
- `KICAD_CLI` and `PHICAD_KICAD_CLI` are both set to `$<TARGET_FILE:kicad-cli>` — the first is
  the `qa_cli` convention, the second is what `phicad/mcp/server.py` actually reads.
- Timings on an M-series Mac: `qa_mcp_registry` ~3.5 min (its wall time is 17 `phicad-tool`
  subprocesses each paying a ~15 s `import fastmcp`), the other two minutes each.

## 3c. The python 3.9 boundary

`phicad/bin/phicad` invokes some fork-layer files with **bare `python3`** — on macOS that is
`/usr/bin/python3`, currently **3.9.6** — while everything else in `phicad/` runs under the
3.13 venvs. Those files must stay 3.9-compatible:

| File | Run by |
| --- | --- |
| `phicad/pylib/*.py` | `phicad check` gates, telemetry, `phicad models` |
| `phicad/mcp/register.py` | `phicad mcp register` |
| `phicad/mcp/doctor.py` | `phicad mcp doctor` |
| `phicad/mcp/sch_worker.py` | spawned by `tools_schematic.py` (automation venv, but also run by hand) |
| `phicad/mcp/session_worker.py` | spawned by `tools_session.py` (same) |

Three layers, because each sees something the others cannot:

1. **ruff** — `[tool.ruff.per-file-target-version]` in `pyproject.toml` pins those paths to
   `py39` and the rest of `phicad/` to `py310`, so a `match` statement in `phicad/pylib/`
   is a parse error while the same code in `phicad/mcp/registry.py` is fine. This needs
   **ruff >= 0.13**; 0.9–0.12 accept the key and silently ignore it, which is why
   `required-version = ">=0.13"` is set — a gate that stopped gating is worse than no gate.
2. **A real 3.9 byte-compile** — `/usr/bin/python3 -m py_compile <those files>` (~1 s), also a
   CI step against an `actions/setup-python` 3.9. Catches anything 3.9's own parser rejects.
3. **Running it** — neither of the above can see a *runtime* 3.10-ism whose syntax is legal 3.9:
   `isinstance(x, int | str)` compiles fine and raises `TypeError` at call time. Only executing
   the code finds that, so keep the rule in mind rather than trusting the gate:
   `from __future__ import annotations` makes *annotations* safe, not runtime uses.

```bash
/usr/bin/python3 -m py_compile phicad/pylib/*.py phicad/mcp/doctor.py \
    phicad/mcp/register.py phicad/mcp/sch_worker.py phicad/mcp/session_worker.py
```

## 3c-bis. The two-channel rule (no GUI automation)

Everything agent-facing reaches PhiCad one of exactly two ways:

| Channel | Used by | Oracle |
| --- | --- | --- |
| `kicad-cli` subprocess | export/DRC/ERC/render tools, the synth gates | exit code + json report |
| protobuf IPC to `kicad-cli api-server` | `session_*` tools, kipy scripts | typed responses |

There is no third channel, and specifically **no screen control**: no screenshots, synthetic
clicks or keystrokes, `osascript`/System Events, or accessibility automation. Such a step is
unreproducible, cannot run in CI, races the user's own pointer, and breaks on any UI change.

`test_registry.py::test_no_gui_automation` enforces it across `phicad/mcp`, `phicad/pylib`
and `phicad/synth` (~43 files). It greps source text, so even a *mention* trips it — the
explanation belongs in docs, not in code that looks like the thing it disclaims. The gate
assembles its own token list at runtime so it does not match itself; skipping the file
instead would leave the one file naming every token unchecked.

Deliberately out of scope: the `ai-view` GUI plugin may raise its own editor window with
`/usr/bin/open -a`, a window-manager call rather than screen control (it needs no Automation
TCC permission and shows no consent dialog), and it uses `osascript` only to show a human an
error alert. `editor_control.py` documents why the alternative does not exist — no message in
`api/proto/` commands the *editor's* viewport (the `zoom`/`pan` fields in
`board_jobs.proto` are 3D-render job parameters, not canvas control), and every
editor-state command is `checkForHeadless`-gated.

When a capability is genuinely missing, a headless refusal is the correct outcome: those RPCs
return `ok=false` with `headless: true` and a reason. Add a tool or a CLI flag — reviewable
— rather than working around it.

## 3d. Testing the Tk windows (AI View, New AI Project)

GUI defects do not appear in the source. Every one found so far surfaced by launching the
window and looking at it, so both halves matter: **look at it**, then **pin the property** so
the defect cannot come back silently.

**A screenshot proves a layout was right once, on one window size.** Assert the invariant
instead. `phicad/plugins/ai-view/test_ui_layout.py` builds a real `ttk.Label` with the real
font and calls the real renderer, without constructing the whole app (which would need a
board, a backend and a config home). It self-skips when Tk is absent or has no display, so
CI runners are fine — but note that the gate's own interpreter
(`$(brew --prefix)/bin/python3.13`) *does* have Tk 9.0, so these run for real locally.
After adding a Tk test, confirm it actually ran rather than skipped:

```bash
PYTHONPATH=phicad/plugins/ai-view python3 -m unittest \
    discover -s phicad/plugins/ai-view -p 'test_ui_layout.py' -v   # "Ran N", not "skipped"
```

**The clipping invariant is `reqw <= w`.** Tk reports what a widget *wants* (`reqw`) and what
it *got* (`w`); when `reqw > w` the text is silently cut off. Do not estimate widget widths
from font metrics — an estimate gave 1128 px for a mode bar that really wanted 1591 px. Read
it from a live widget through the probe:

```bash
PHICAD_AI_PROBE='/probe' PHICAD_AI_PROBE_OUT=/tmp/p.json PHICAD_AI_PROBE_HOLD=1 \
    python3 phicad/plugins/ai-view/ai_view_gui.py
```

The snapshot carries `ipc_state`, window geometry and state, and `w`/`reqw`/`mapped` for
every container that moves — including the mode bar's children, which is what makes an
overflow *attributable* to one control rather than just visible on the bar. When adding a
widget that can hold variable-length text, add it to that list; a widget clipped to nothing
still answers `winfo_ismapped`, so size is what the assertion has to be about.

**Driving a real window** needs a real IPC session, since `ai_view_gui.py` takes no
argument and reads the board only over IPC — a board path on the command line is silently
ignored. See the AI View README (§How it connects) and note that `kicad-cli api-server
--socket` wants a bare path while `KICAD_API_SOCKET` wants the `ipc://` URL.

## 4. Fast feedback per area

| Editing | Run first |
| --- | --- |
| `libs/kimath`, geometry | `phicad test '^qa_kimath$'` (fast) |
| `common/`, settings, plotters | `phicad boost qa_common <Suite>` then `^qa_common$` |
| `eeschema/` | `phicad boost qa_eeschema <Suite>` then `^qa_eeschema$` |
| `pcbnew/` DRC/zones/etc. | the matching `qa_pcbnew_*` group, e.g. `qa_pcbnew_drc_regression` |
| `pcbnew/router/` | relevant `qa_pcbnew` PNS suite; add `qa_pns_regressions` only if configured with `KICAD_BUILD_PNS_DEBUG_TOOL=ON` |
| `api/`, `common/api/` | `^qa_api$` |
| `kicad/cli/` | `qa_cli` pytest subset |
| `phicad/synth/` | `python3 phicad/synth/selftest.py` (stdlib only, no build tree) |
| `phicad/pylib/telemetry*`, `plugins/ai-view` telemetry | `python3 phicad/pylib/telemetry_selftest.py` |
| `phicad/plugins/ai-view/` connectivity or Git | `python3 -m unittest -v phicad/plugins/ai-view/test_connectivity.py` |
| `phicad/plugins/ai-view/` window chrome, status line, empty states | `PYTHONPATH=phicad/plugins/ai-view python3 phicad/plugins/ai-view/test_ui_layout.py` |
| `phicad/mcp/` api-server lifecycle / orphan reaping | `build/mcp-venv/bin/python phicad/mcp/test_reap_orphans.py` (stdlib, no build tree) |
| `phicad/mcp/` — any tool signature, docstring or new tool | `ruff check .` then `phicad test '^qa_mcp_registry$'` (~3.5 min, no build) |
| `phicad/mcp/` — a tool that shells out to `kicad-cli` | `phicad test '^qa_mcp_tools$'` (needs a built CLI); `'^qa_mcp_handshake$'` for the MCP protocol surface |
| `phicad/pylib/` (any file) | `ruff check .` **and** `/usr/bin/python3 -m py_compile phicad/pylib/*.py` — this dir is python-3.9 territory (§3c) |
| `phicad/mcp/{doctor,register,sch_worker,session_worker}.py` | same as `phicad/pylib/` — also python 3.9 (§3c) |

Known-heavy: zone fill (~40 s), Allegro (~31 s), creepage (~16 s), IPC-2581 (~15 s), the whole
`qa_eeschema` fixture set, and anything in `qa/tests/cli`.

## 5. Style and quality gates

- The fork layer's python/shell rules are enforced by `phicad check` (§3a), not clang-format:
  120 columns for code, spaces not tabs, no trailing whitespace, newline at end of file. The
  column limit is deliberately **not** applied to markdown/JSON, where wrapping a table or a
  generated file would change what it renders as.
- On top of that, `ruff check .` (config in the repo-root `pyproject.toml`, §3a) adds pyflakes
  and pycodestyle over `phicad/**/*.py` only — upstream KiCad's python and `thirdparty/` are
  excluded, so a bare `ruff check` at the root cannot go red on code that is not ours.
- `_clang-format`: LLVM base, Allman braces, 4-space indent, 120 columns, spaces inside parens,
  no tabs, left pointer alignment. `uncrustify.cfg` is a legacy equivalent.
- The upstream MR gate is `tools/check_coding.sh --diff --ci --commit <merge-base>`
  (`.gitlab/mr_formatting.yml`, `allow_failure: true`) → use `phicad fmt` / `phicad fmt check`,
  which wraps the same script and only touches lines you changed.
- **Never** reformat whole files or let clang-format reorder existing includes
  (`CONTRIBUTING.md` is explicit about this).
- `banned_functions` target (`cmake/CheckBannedFunctions.cmake`) runs as part of ALL and rejects
  `strtod`, `strtof`, `atof` and unapproved `%f`/`%g` in `common/io`, `pcbnew/pcb_io`,
  `eeschema/sch_io`. Escape hatch: `// format:allow`.
- Warnings: `cmake/Warnings.cmake` adds `-Wall` + shadow/conversion/fallthrough/etc.; VLA and
  missing-return are errors. No blanket `-Werror`.
- Generated code must not be hand-edited: `*_base.cpp/.h` (wxFormBuilder — edit the `.fbp`),
  `*_keywords.cpp` / `*_lexer.h` (generated from `*.keywords` by
  `cmake/BuildSteps/TokenList2DsnLexer.cmake`).

## 6. Commit conventions

- Concise, component-prefixed subjects: `pcbnew: …`, `eeschema: …`, `api: …`.
- Bug references in the body: `Fixes https://gitlab.com/kicad/code/kicad/-/issues/NNNN`.
- Changelog tags recognised by `tools/git/fixes_alias`: `ADD:`, `NEW:`, `REMOVE[D]:`, `CHANGE[D]:`.
- Upstream history contains `Assisted-by: <tool>` trailers for AI-assisted commits — use that
  trailer for agent-written commits here too.
- No DCO / `Signed-off-by` requirement in this tree.

## 7. Automating KiCad without touching C++

Ranked for KiCad 10:

1. **`kicad-cli`** — headless, deterministic, best first choice. Command groups (from
   `kicad/kicad_cli.cpp`, `kicad/cli/`): `pcb` (`drc`, `render`, `import`, `import-ses`, `diff`,
   `upgrade`,
   `export {gerbers,drill,dsn,step,glb,pdf,svg,png,pos,odb,ipc2581,stats,stackup,vrml,stl,…}`),
   `sch` (`erc`, `diff`, `import`, `export {netlist,bom,python-bom,pdf,svg,png,dxf,…}`),
   `fp`/`sym` (`diff`, `export svg`, `upgrade`), `gerber` (`info`, `convert png`, `diff`),
   `jobset run`, `mergetool`, `api-server`, `version`.
2. **IPC API** — protobuf-over-NNG (`api/proto/`, `common/api/api_server.cpp`). Start with
   `kicad-cli api-server [project] [--socket PATH]`; client library is the external
   `kicad-python` / `kipy` package (see `pcbnew/python/wizards/qfp/qfp.py`). Use it for live
   inspection/mutation of an open board or schematic.
3. **SWIG `pcbnew` module** — legacy. This tree has no SWIG build target; do not target it for
   new automation.

There are also two prebuilt MCP servers on this machine (`~/tools/kicad-mcp`,
`~/tools/KiCAD-MCP-Server`) if the goal is agent-driven *use* of KiCad rather than development
of it.

## Background-only documentation and validation work

`phicad check fast` is not currently guaranteed to leave the desktop untouched. Its AI View
unittest discovery imports `plugins/ai-view/test_ui_layout.py`, whose availability probe and
tests construct `tk.Tk()` windows. Missing-display behavior can abort native Tk rather than
produce a Python skip. Do not retry with a display, open a GUI, or claim the complete gate passed
when the session requires MCP-only product operation and a free desktop.

For documentation-only changes, run the existing `phicad/bin/check_style.py` and
`git diff --check`, validate changed relative links and docs navigation targets, and render the
site when its dependencies are already installed:

```bash
python3 phicad/bin/check_style.py
git diff --check
uvx --with-requirements phicad/docs-site/docs-requirements.txt mkdocs build --strict -f phicad/docs-site/mkdocs.yml -d /tmp/phicad-docs-review
```

The site requires `phicad/docs-site/docs-requirements.txt` (`mkdocs-material`). Missing packages
block the rendered-site check; local link checks do not replace it. Hand-authored
reference pages link to source documents; only `reference/mcp-tools-generated.md` is generated
from the MCP registry. Do not duplicate or hand-edit that generated API reference.

For an approved background code change, select the relevant non-UI component checks from the
suite map, plus the new feature's tests. Report the unexecuted GUI suite explicitly; creating an
approved headless selection is separate test-infrastructure work. Backend smoke operations
must use typed MCP tools and owned files/sockets; a unit-test runner is not authority to open
an application, attach a desktop editor or invoke a foreground fallback.

### Inherited upstream documentation exceptions

The upstream root `README.md` references absent `Doxyfile` and `patches` paths in this checkout.
The inherited Antmicro demo `demos/jetson-agx-thor-baseboard/README.md` references a missing
`img/jetson-thor-baseboard-render.png`. These are recorded source gaps, not locally generated
artifacts or PhiCad documentation routes. Preserve upstream ownership; this documentation
repair does not fabricate a demo render or modify the upstream README/demo.
