# Build PhiCad

PhiCad is built from the KiCad source tree with the `phicad/bin/phicad` wrapper as the normal entry point.

- `bootstrap` prepares dependencies and build presets (Homebrew on macOS, apt on Debian/Ubuntu).
- `configure [dev|debug|release]` selects a build flavor under `build/` (`build/mac-dev`, `build/linux-dev`, …).
- `build <target>` is preferred over a full build; common targets include `kicad-cli` and editor KIFACEs.
- The macOS setup includes explicit ngspice and OpenCASCADE discovery overrides; Linux needs none.
- macOS builds an app bundle (`kicad/PhiCad.app/Contents/MacOS/…`); Linux trees are flat (`kicad/kicad-cli`, `<editor>/_<editor>.kiface`).
- Binaries can run directly from the build tree with `KICAD_RUN_FROM_BUILD_DIR=1`.

Source of truth: [phicad/build-macos.md](https://github.com/couplet-physical-ai/PhiCad/blob/master/phicad/build-macos.md),
[phicad/build-linux.md](https://github.com/couplet-physical-ai/PhiCad/blob/master/phicad/build-linux.md), and for a remote
Debian/Ubuntu host [phicad/bin/bootstrap-debian.sh](https://github.com/couplet-physical-ai/PhiCad/blob/master/phicad/bin/bootstrap-debian.sh).
The unattended improvement loop that runs on that host (backlog → phi → fork gates → PRs into `dev`) is
documented in [phicad/loop/README.md](https://github.com/couplet-physical-ai/PhiCad/blob/master/phicad/loop/README.md).
