# Building PhiCad on macOS (Apple Silicon)

Verified on this machine: macOS 26.6.1 arm64, Apple clang 21, Homebrew `/opt/homebrew`,
CMake 4.4, 10 cores. Configure and a full build from an empty build tree passed on this exact setup;
the dated, machine-readable result is in `build-attestations/2026-09-10-macos-arm64.json`.

**Use the wrapper, not raw cmake:**

```bash
phicad/bin/phicad bootstrap     # Homebrew deps (one time, ~5 min)
phicad/bin/phicad configure     # -> build/mac-dev (RelWithDebInfo + QA tests)
phicad/bin/phicad build         # full build: 3431-step measured plan, ~16 GB; see §4
phicad/bin/phicad run kicad-cli --version
```

Everything below is the *why*, for when something breaks.

## 1. Dependencies

Enforced by the top-level `CMakeLists.txt` (`find_package` block, lines ~788–1061):

| Dependency | Minimum (CMake) | Homebrew formula | Installed here |
| --- | --- | --- | --- |
| wxWidgets (gl aui adv html core net base propgrid xml stc richtext webview) | 3.2.0 (`CMakeLists.txt:960-966`) | `wxwidgets` | 3.3.3 |
| Boost (`locale`, `unit_test_framework` for QA) | 1.71.0 (`CMakeLists.txt:835-842`) | `boost` | 1.92.0 |
| OpenCASCADE | 7.6.0 (`CMakeLists.txt:866-877`) | `opencascade` | 7.9.3 |
| Protobuf (+ `protoc`) | 3.15 (`CMakeLists.txt:880-915`) | `protobuf` | 36.0 |
| GLM | 0.9.8 (`CMakeLists.txt:794`) | `glm` | 1.0.3 |
| Cairo / Pixman | 1.12 / 0.30 (`CMakeLists.txt:819-825`) | `cairo`, `pixman` | 1.18 / 0.46 |
| libgit2 | 1.5 (`CMakeLists.txt:827`) | `libgit2` | 1.9.7 |
| ngspice **shared** lib | presence (`CMakeLists.txt:861`) | `ngspice` | 47 |
| nng | presence (`CMakeLists.txt:1056`) | `nng` | 1.12.4 |
| ODBC driver manager | fatal in `thirdparty/nanodbc/CMakeLists.txt:86-131` | `unixodbc` | 2.3.14 |
| FreeType / HarfBuzz / Fontconfig / zstd / curl (keg-only) | presence | `freetype harfbuzz fontconfig zstd curl` | ✔ |
| Python interpreter | 3.6 (`CMakeLists.txt:947-955`) | `python@3.13` | 3.13.15 |
| Tk bindings for that interpreter | not needed to build; needed by agent-facing Python GUIs (`phicad/plugins/`) | `python-tk@3.13` | 3.13.15 |
| Build tooling | CMake ≥ 3.22, C++20 (`CMakeLists.txt:22-25,287`) | `cmake ninja ccache pkgconf clang-format` | ✔ |
| Optional | `gettext` (only with `KICAD_BUILD_I18N=ON`), `doxygen` (docs target) | | gettext ✔ |

OpenGL comes from the Apple SDK — do not install an X11 GL stack.

`python-tk@3.13` is not a build dependency: PhiCad itself never imports it. It is listed because
Homebrew ships Python's Tk bindings as a **separate formula**, so `build/venv` (created from
`python@3.13`) has `kipy` but no `_tkinter`, while Apple's `/usr/bin/python3` has `_tkinter` but
no `kipy`. Any Python GUI that also talks to the IPC API therefore needs the two in one
interpreter, and the formula version must match the venv's Python exactly (3.13.x).
Check with:

```bash
build/venv/bin/python -c 'import tkinter, kipy'
```

## 2. The three mandatory macOS overrides

A stock Homebrew tree does **not** configure without these; they are baked into
`cmake_common_flags()` in `phicad/bin/phicad` and into `phicad/CMakePresets.json`.

1. `-DNGSPICE_LIB_NAME=libngspice.0.dylib` — `cmake/Findngspice.cmake:60-66` hardcodes the
   Linux name `libngspice.so.0` for *all* UNIX, so it never finds Homebrew's
   `/opt/homebrew/lib/libngspice.0.dylib`. Symptom: `Could NOT find ngspice (missing:
   NGSPICE_LIBRARY NGSPICE_DLL)` even though the header is found.
2. `-DNGSPICE_ROOT_DIR=/opt/homebrew` — keeps the search inside the brew prefix.
3. `-DOCC_INCLUDE_DIR=/opt/homebrew/include/opencascade` — `cmake/FindOCC.cmake:128-146` is a
   legacy module that only looks in `/usr`, `/usr/local`, `/opt`. Symptom:
   `*** OpenCascade header files missing ***`.

Plus `-DCMAKE_PREFIX_PATH` including the keg-only kegs (`curl`, `openssl@3`, `unixodbc`) and
`-DPython3_EXECUTABLE` pinned to a Homebrew interpreter.

Two things that were *not* needed here but are classic escape hatches:
`-DCMAKE_POLICY_VERSION_MINIMUM=3.5` (vendored `thirdparty/*` projects declaring CMake 3.0–3.14
under CMake 4) and explicit `-DwxWidgets_CONFIG_EXECUTABLE=/opt/homebrew/bin/wx-config`.

## 3. Build flavors

| Flavor | Build dir | Type | QA tests | Use |
| --- | --- | --- | --- | --- |
| `dev` (default) | `build/mac-dev` | RelWithDebInfo | ON | day-to-day, agent loop |
| `debug` | `build/mac-debug` | Debug | ON | asserts, stepping; slower and larger |
| `release` | `build/mac-release` | Release | OFF | clean-stage and smoke an installed bundle |

All `build*` paths are already gitignored (`.gitignore:5`), as is a root `CMakePresets.json`
(`.gitignore:122`) — that is why the preset file is stored under `phicad/` and copied on bootstrap.

Useful extra options (see `CMakeLists.txt` option block): `KICAD_SPICE`, `KICAD_SPICE_QA`,
`KICAD_BUILD_FUZZ_TESTS` (default OFF), `KICAD_USE_SENTRY`, `KICAD_BUILD_I18N`,
`KICAD_STDLIB_DEBUG`, `KICAD_SANITIZE_*`.

## 4. Cost and narrow builds

- Full dev build: **3431 Ninja progress steps** at commit `107d2b3aad` (the exact count changes
  with the source). Measured here from an empty build tree (10-core M-series, RelWithDebInfo + QA),
  with a fully warm ccache: **611.65 s wall clock, 1.258 CPU-hours, and a 16,848,105,472-byte
  `build/mac-dev` tree** (~15.69 GiB). The immediately preceding empty-tree attempt had only 11
  cache hits and 1409 misses; it reached the final packaging stage in 2011.59 s, then exposed the
  clean-build race fixed by that commit. Because that near-cold attempt failed, a passing cold-cache
  duration remains **unknown** rather than being inferred. See the committed attestation for both
  results and the toolchain details. A second configured build tree still costs about 16 GB.
- `phicad status` reports "up to date" rather than 0 pending steps: the git version header is
  regenerated on every ninja run, so `build_version.cpp` recompiles and everything linking
  `kicommon` relinks (~41 steps). That floor is normal, not a stale tree.
- Touching one `.cpp` in `pcbnew/` is usually a recompile + relink of `_pcbnew.kiface`
  (tens of seconds), not a full rebuild.
- Prefer narrow targets:

```bash
phicad/bin/phicad build kicad-cli            # CLI + its deps
phicad/bin/phicad build pcbnew_kiface        # PCB editor DSO only
phicad/bin/phicad build eeschema_kiface
phicad/bin/phicad build qa_kimath qa_common  # just the tests you need
phicad/bin/phicad targets qa_               # discover target names
```

Editing a header in `include/` or `common/` fans out widely — expect a long rebuild.

## 5. Running from the build tree

The build produces a nested app bundle; nothing needs installing for development:

```
build/mac-dev/kicad/PhiCad.app/Contents/MacOS/PhiCad     # project manager GUI (kicad = compat symlink)
build/mac-dev/kicad/PhiCad.app/Contents/MacOS/kicad-cli  # headless CLI
```

Run with `KICAD_RUN_FROM_BUILD_DIR=1` (what `phicad run` does). The stock-data override is
`KICAD_STOCK_DATA_HOME` (`common/paths.cpp:237-259`) — *not* `KICAD_STOCK_DATA_PATH`.

### Clean-stage release smoke (macOS only)

A build-tree launch does not exercise the install-time dependency fixup or prove that resource
lookup works without `KICAD_RUN_FROM_BUILD_DIR`. Use the release path for that evidence:

```bash
phicad/bin/phicad configure release
PHICAD_BUILD=build/mac-release phicad/bin/phicad build
phicad/bin/phicad install
```

`configure release` pins `CMAKE_INSTALL_PREFIX` to the disposable, gitignored
`build/install/mac-release`. This is required because the upstream macOS install rules embed
absolute bundle destinations at configure time: `cmake --install --prefix <somewhere-else>` does
**not** safely redirect every install/signing step. `phicad install` therefore refuses a build tree
configured for `/usr/local` or any other prefix, removes only that known staging directory, runs
`cmake --install`, verifies the staged app's ad-hoc signature, checks the PhiCad executable, the PCB
and schematic KIFACEs, installed SharedSupport data, and the bundled AI View plugin, then runs the
staged `kicad-cli --version` by absolute path with build-tree and `DYLD_*` overrides removed.

Prerequisites are a complete `Release` build from the commands above, the same Homebrew build
inputs listed in section 1, CMake, Python 3, and macOS `codesign`. The build-tree application uses a
`Contents/SharedSupport` symlink back into the build root so Finder launches can find staged data.
`phicad install` removes only that exact build-owned link before CMake creates one real, relocatable
`SharedSupport` directory containing the bundled AI View plugin.

Release configuration also copies the selected `libngspice.0.dylib` and its codemodel directory
into a narrow build input. This is necessary because Homebrew exposes libngspice from its broad
`/opt/homebrew/lib` prefix while the upstream install rule copies every dylib below the selected
library directory. The narrow input prevents unrelated Homebrew libraries and relative Cellar links
from being mistaken for simulator payload.

The current evidence is deliberately narrow: this is a non-GUI CLI launch from an ad-hoc-signed
staging directory on the local Apple Silicon macOS host. It does **not** create or test a DMG,
notarization, Gatekeeper download/quarantine,
a Developer ID signature, Finder/GUI launch, Intel macOS, Windows, or Linux compatibility. A passing
release smoke is therefore packaging evidence, not a claim that an end-user release has been cut.

macOS builds are ad-hoc signed by default (`CMakeLists.txt:1139-1150`); a relocatable,
dependency-fixed bundle only comes from the install path
(`cmake/InstallSteps/InstallMacOS.cmake`, `RefixupMacOS.cmake`, `signing/`).

## 6. Homebrew vs kicad-mac-builder

Upstream CI uses an external `kicad-mac-builder` toolchain (`.gitlab/macOS-CI.yml:12-19`) that
pins dependency versions and prepares a relocatable `Python.framework`
(`qa/tests/cli/README.txt`). We build against plain Homebrew instead, which is why the
overrides in §2 exist. Consequences to remember:

- Homebrew drift can break the build after `brew upgrade`; `phicad doctor` prints the versions.
- Some CLI QA tests that expect an embedded Python framework may not pass locally.

## 7. Network gotcha (managed laptops)

`HTTPS_PROXY=https://llm-gateway.example` is exported globally and rejects Homebrew, GitHub API
and gitlab.com traffic (`curl 405`, `malformed HTTP response`). `phicad` strips the proxy env for
every network command. Manually:

```bash
env -u HTTPS_PROXY -u https_proxy -u ALL_PROXY -u all_proxy -u HTTP_PROXY -u http_proxy brew install …
```

git is already handled: `http.https://github.com/.proxy=""` globally and
`http.https://gitlab.com/.proxy=""` in this repo's config.
