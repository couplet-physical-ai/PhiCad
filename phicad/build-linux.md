# Building PhiCad on Linux (Debian 13 / Ubuntu, x86-64)

Verified on `build-host-01`: Debian 13 (trixie) x86-64, GCC 14.2, CMake 3.31, Ninja 1.12,
10 cores / 15 GB RAM, 2026-09-22. `phicad configure dev` passes from a clean apt install with the
list in `phicad/bin/bootstrap-debian.sh`; the first target built was `kicad-cli`. A Linux build
is what a hosted PhiCad (a Linux container behind an app-hosting platform) needs; it is **not**
required for the agent surface — `phicad mcp …`, `models check`, `synth check` and the phi agents
run without it.

## Verified on Linux: synth evidence

The 2026-09-22 `sensor-node.json` synth run is captured verbatim in
[`build-attestations/linux-synth-sensor-node.json`](build-attestations/linux-synth-sensor-node.json).
With `PHICAD_KICAD_CLI` selecting the fork's Linux `kicad-cli` 10.99.0, the exact `synth build`
command exited 1 after 0.100 s: footprint validation failed for all six parts (`0/1 stages
passed`). The loader searched the example-local libraries and the macOS-only
`~/Library/Application Support/kicad/stock/footprints` path; this host has no footprint
libraries at either location. No output project was created, and ERC, schematic/board parity,
the synth router, fill, and DRC were therefore not reached. This differs from the documented
macOS result of 14/14 stages, ERC 0 errors / 1 warning, and DRC 0 errors / 0 warnings / 0
unconnected.

`~/.kicad-mcp/freerouting.jar` was present, but the separate Freerouting autorouter did not run;
the pipeline stopped before routing. The attestation records the empty stderr exactly (the
validation diagnostics were printed to stdout), the duplicated `FAIL validate` verdicts, host,
checkout commit, CLI version, command, duration, and all unavailable later gate counts as null
rather than inventing results.

The later [stock-root lookup attestation](build-attestations/linux-synth-library-paths.json)
records the repaired Linux search paths and an explicit `PHICAD_FOOTPRINT_DIR` override.
The stock host still lacked those libraries in that run: validation stopped at 0/1 stages.
For an isolated library checkout, set `PHICAD_FOOTPRINT_DIR` to the footprint repository root
and `PHICAD_SYMBOL_DIR` to a directory of packed `Library.kicad_sym` files; neither setting
changes host installation. KiCad 10 symbol sources are unpacked `.kicad_symdir` directories:
use their supplied `tools/kicad_lib_pack.py -i <symbol-source> -o <temporary-packed-output>`
and point the symbol override at that output, rather than at the unpacked source root.

The native CLI also needs a footprint library table for netlist import. For an isolated test,
copy the official footprint repository's `fp-lib-table` into
`$KICAD_CONFIG_HOME/<CLI-major.minor>/fp-lib-table` (10.99 for the verified binary), and set
`KICAD10_FOOTPRINT_DIR` to the same footprint root. `PHICAD_FOOTPRINT_DIR` controls the Python
loader; it does not install a global native table. With packed symbols, the explicit overrides,
and this temporary table, the [isolated-library run](build-attestations/linux-synth-isolated-libraries.json)
passed 14/14 stages: ERC 0 errors / 9 warnings and DRC 0 errors / 0 warnings / 0 unconnected.
The attestation pins both official library commits and preserves the setup commands.

For project merge drivers using a flat Linux CMake build, `ProjectHistory.install_merge_driver()`
configures `KICAD_RUN_FROM_BUILD_DIR=1` in the generated command so the CLI finds sibling kifaces.
It recognizes the build's `CMakeCache.txt` and `pcbnew/` directory, repairs exact older generated
commands, and preserves custom commands and installed-binary behavior. The
[merge-driver attestation](build-attestations/linux-merge-driver-resources.json) records the
baseline failure and successful normal Git merges after configuration repair.

**Use the wrapper, not raw cmake:**

```bash
phicad/bin/phicad bootstrap        # apt (runs phicad/bin/bootstrap-debian.sh), one time
phicad/bin/phicad configure dev    # -> build/linux-dev (RelWithDebInfo + QA tests)
phicad/bin/phicad build headless   # kicad-cli + pcbnew/eeschema/cvpcb/gerbview kifaces: all the CLI,
                                   # MCP server and agents load (~2400 steps); the GUI is 'build kicad'
phicad/bin/phicad run kicad-cli version
```

Everything below is the *why*.

## 1. Dependencies

Debian's archive satisfies every `find_package` in the top-level `CMakeLists.txt` unaided, so
unlike macOS ([build-macos.md](build-macos.md) §2) there are **no mandatory overrides**:
`cmake_common_flags()` passes only the generator, ccache and `KICAD_VERSION_EXTRA=phicad`.
The finders that need help on Homebrew (`libngspice.so.0`, `/usr/include/opencascade`) are
written for exactly this layout.

| Dependency | Debian package(s) | Note |
| --- | --- | --- |
| wxWidgets 3.2 (gl aui adv html core net base propgrid xml stc richtext **webview**) | `libwxgtk3.2-dev libwxgtk-webview3.2-dev libwxgtk-media3.2-dev` | `wx-config --libs webview` prints a flag even when the library is not installed; FindwxWidgets then fails on `wxWidgets_LIBRARIES`. The webview package is the fix. |
| 3Dconnexion SpaceMouse | `libspnav-dev` | `find_package(SPNAV)` is **REQUIRED** on Linux (`CMakeLists.txt:814`) |
| Poppler (PDF import) | `libpoppler-glib-dev libpoppler-private-dev` | `find_package(Poppler … Glib)` |
| Boost | `libboost-dev libboost-{filesystem,system,locale,test}-dev` | 1.83 |
| OpenCASCADE | `libocct-{foundation,modeling-data,modeling-algorithms,data-exchange,ocaf,visualization}-dev` | headers in `/usr/include/opencascade` |
| ngspice shared lib | `libngspice0-dev` | `libngspice.so.0`, the finder's default |
| Protobuf + protoc | `libprotobuf-dev protobuf-compiler` | 3.21 (CONFIG mode fails, FindProtobuf.cmake fallback is used) |
| nng | `libnng-dev` | ships a CMake config package and **no** `.pc`; `doctor` looks for `nng-config.cmake` |
| libgit2, cairo/pixman, GLEW/GLM, curl, zstd, zlib, harfbuzz/freetype/fontconfig, GTK 3, unixODBC, libsecret | see `KICAD_PKGS` in the bootstrap script | |
| Toolchain | `build-essential cmake ninja-build ccache pkg-config swig gettext` | GCC 14; clang is optional (doctor prints "not installed") |
| Python | `python3 python3-venv python3-dev` | distro 3.13; `phicad venv` uses it directly |
| Freerouting (autoroute) | `default-jre-headless` + `~/.kicad-mcp/freerouting.jar` | the jar is a manual download (GPL-3) |

`PHICAD_BOOTSTRAP_BUILD_DEP=1 bootstrap-debian.sh` additionally runs `apt-get build-dep kicad`
(Debian's 9.0.x packaging). It is off by default because it drags in TeX Live for the docs.

## 2. Build tree layout

Linux trees are **flat**; there is no app bundle:

| | macOS (`build/mac-dev`) | Linux (`build/linux-dev`) |
| --- | --- | --- |
| kicad-cli | `kicad/PhiCad.app/Contents/MacOS/kicad-cli` | `kicad/kicad-cli` |
| GUI | `kicad/PhiCad.app/Contents/MacOS/PhiCad` | `kicad/kicad` (upstream's name; only the bundle is renamed) |
| kifaces | `kicad/PhiCad.app/Contents/PlugIns/_*.kiface` | `<editor>/_<editor>.kiface` (`pcbnew/_pcbnew.kiface`, …) |

`KICAD_RUN_FROM_BUILD_DIR=1` makes the launcher look for kifaces one directory up and into each
editor's build dir (`common/kiway.cpp`, `dso_search_path`); `phicad run` sets it.

Both the shell (`phicad/bin/phicad`: `PLATFORM`, `tree_layout`, `built_bin`) and the Python layer
(`phicad/pylib/build_layout.py`) read the layout **from the tree when it exists**, falling back to
the platform. That is what lets `ubuntu-latest` CI keep inspecting the macOS-style fixtures the gate
tests build, and lets a Linux tree be found by the MCP server, `register`, `doctor`, synth and the
support bundle. `PHICAD_PLATFORM=mac|linux` overrides detection for tests.

## 3. Parallelism and memory

`default_jobs()` uses `nproc` capped at **RAM/2 GB**: a `pcbnew` translation unit peaks near 2 GB,
and a 16 GB box at `-j10` swaps itself to a standstill. On the 15 GB / 10-core host that is `-j7`;
`PHICAD_JOBS=N` overrides. When another agent shares the machine, run long builds under
`nice -n 15 ionice -c3` (ccache keeps progress across interruptions; `ccache -M 20G` is set by the
bootstrap).

Costs measured on that host (cold ccache, `nice -n 15`, `-j5`): configure 1.2 s; `kicad-cli` is an
880-step plan and took ~35 min; `pcbnew_kiface` + `eeschema_kiface` (what `pcb drc`, `sch erc` and the
exports load) add a 1422-step plan.

`kicad-cli` alone only answers `version`: each command family dlopens a kiface (`pcb …` →
`pcbnew/_pcbnew.kiface`, `sch …` → `eeschema/_eeschema.kiface`, which loads `cvpcb/_cvpcb.kiface` for
ERC, `gerber …` → `gerbview/_gerbview.kiface`), and a missing one is reported as
`Failed to load kiface library`. `phicad build headless` names exactly that set.

`phicad build <target>` appends `api_schema_build_copy` whenever a binary or kiface target is named:
run-from-build-dir mode reads the IPC schemas from `build/<plat>-dev/schemas/`, which only that
`ALL` target stages, and a narrow first build would otherwise log `schema file … not found` on every run.

## 4. Flavors and presets

`phicad configure dev|debug|release` → `build/linux-dev|linux-debug|linux-release`, mirroring the
macOS flavors. `phicad/CMakePresets.json` carries `linux-{base,dev,debug,release}` presets
conditioned on `hostSystemName == Linux` (the copy at the repo root is gitignored). The ngspice
staging step that `configure release` performs on macOS does not apply on Linux (the distro
library is linked in place). `phicad install` remains **macOS-only**: the Linux install rules are
upstream's and have not been qualified here.

## 5. Running from the build tree

```bash
phicad/bin/phicad run kicad-cli version           # KICAD_RUN_FROM_BUILD_DIR=1
phicad/bin/phicad doctor                          # "artifact layout: flat", ldd-based GUI link check
phicad/bin/phicad open                            # runs kicad/kicad directly; refuses without DISPLAY
```

`doctor` on Linux asks the tools CMake itself asks (`wx-config`, `pkg-config`, include dirs,
`nng-config.cmake`) rather than dpkg, so a package installed under another name on Ubuntu still
reports as present.

## 6a. Linux-only defect in the upstream CLI, fixed in the fork (PR #121)

`kicad-cli sch erc` on a schematic **without a sibling `.kicad_pro`** writes its report and then
segfaults at exit (139). Cause: the empty-path default project's `PROJECT_FILE` outlives the
eeschema kiface's `ERC_ITEM` statics; `PGM_KICAD::OnPgmExit()` (upstream code) unloads only
`GetOpenProjects()`, which skips the empty path. Effects: `sch erc` exit 139 after a complete
report; the MCP schematic write tools report `loads: false` for their standalone output copies
(`phicad mcp selftest --full` fails `sch_set_field`/`sch_add_label`). With a sidecar `.kicad_pro`,
and for every `pcb …` command, exit codes are normal. The 11-line fix and its QA test
(`qa/tests/cli/test_sch.py::test_sch_erc_standalone_schematic_exits_cleanly`) landed through
PR #121 under a one-time, content-pinned boundary exception (`phicad/fork-boundary-exceptions.json`,
see `phicad/git-workflow.md`); the loop it fixes is upstream code, so the fix is also to be submitted
upstream and the exception dropped when `phicad sync` returns their version. A `kicad-cli` built
from `dev` at or after that merge exits 0 (or 5 with `--exit-code-violations`) on a standalone
schematic; verify with the QA test above.

## 6. Known differences from macOS

* No `otool`: the GUI's "built but will not run" check uses `ldd … | grep "not found"`.
* `pgrep -q` does not exist in procps; the wrapper redirects instead.
* `/tmp` is usually its own mount: git prints "Stopping at filesystem boundary" after its
  `fatal:` line, which `check_style.py` now quotes correctly (the copied-tree gate test depends on it).
* `apt-get build-dep kicad` targets Debian's KiCad 9 and pulls the documentation toolchain; the
  explicit list in the bootstrap is what `doctor` verifies.

## 7. Network gotcha (HTTP proxy variables)

If your shell exports `HTTP(S)_PROXY` for some other purpose (for example an LLM proxy), `phicad`
strips those variables for GitHub/PyPI/apt traffic so builds and fetches go direct.
