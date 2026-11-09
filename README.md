# PhiCad

Describe a board and get back a routed KiCad project that has been checked by gates. PhiCad is a fork of KiCad with a design compiler, independent DRC/ERC gates, an MCP tool server and an optional AI assistant.

![Status: experimental](https://img.shields.io/badge/status-experimental-orange) ![License: GPL--3.0--or--later](https://img.shields.io/badge/license-GPL--3.0--or--later-blue)

Created at [Exowatt](https://exowatt.com). Published under the vendor-neutral [couplet-physical-ai](https://github.com/couplet-physical-ai) organization.

## About this fork

PhiCad is a fork of [KiCad](https://www.kicad.org) ([source](https://gitlab.com/kicad/code/kicad)) maintained by couplet-physical-ai. It is not affiliated with or endorsed by the KiCad project.

- **Base:** KiCad `master` at commit `0c53452d53b0` (2026-09-01), a KiCad 11 development snapshot 3505 commits after tag `10.99.0`. Upstream history is kept unchanged. PhiCad's changes are the commits on top.
- **What PhiCad adds:**
  - the `phicad/` layer: a design-IR compiler (`phicad/synth`), the `phicad` CLI, an MCP server (`phicad/mcp`), the AI View plugin, tscircuit-based "PhiCad Verify" (`phicad/core`), parts and autoroute tooling, and docs;
  - a small set of changes to KiCad sources: Specctra DSN export and SES import jobs in `kicad-cli`, launcher and About-box changes, app display names, macOS bundle naming, and AI View plugin staging.
- **What it removes:** the upstream test and demo files that were licensed CC BY-NC-SA (see [NOTICE](NOTICE)).
- **Branding:** the application, document and launcher icons are PhiCad's own, generated from the PhiCad mark by `phicad/resources/branding/regen-icons.py`. Toolbar and menu bitmaps are KiCad's unchanged upstream artwork (credited in `resources/bitmaps_png/sources/CREDITS`).

Upstream code keeps its original licenses and notices. See [LICENSE](LICENSE), [LICENSE.README](LICENSE.README) (per-file exceptions) and [NOTICE](NOTICE).

## Status

Experimental. The `phicad/` layer has a self-contained test suite (`phicad check fast`), and CI runs it. A cold native build takes several hours, and only macOS (Apple Silicon) and Debian/Ubuntu have been exercised. Generated boards are drafts to review, not fabrication releases. There is no support commitment.

## Install

PhiCad builds from source like KiCad. The helper script installs dependencies, writes CMake presets and builds:

```bash
git clone https://github.com/couplet-physical-ai/PhiCad.git && cd PhiCad
export PATH="$PWD/phicad/bin:$PATH"
phicad bootstrap          # dependencies (macOS: Homebrew; Debian/Ubuntu: apt), one time
phicad configure dev      # build/mac-dev (macOS) or build/linux-dev (Linux)
phicad build              # ~3.5 h cold and ~16 GB RAM; ccache makes rebuilds cheap
phicad doctor             # what is missing?
```

Platform details are in [phicad/build-macos.md](phicad/build-macos.md) and [phicad/build-linux.md](phicad/build-linux.md). For the stock KiCad build documentation, see the [KiCad developer docs](https://dev-docs.kicad.org/en/build/).

## Quickstart

```bash
phicad new blinky             # a 3-part design that builds as-is
phicad build blinky.json      # place, route, pour, then run the gates (DRC, ERC, ...)
```

Every gate reports findings as data. Each finding has a stable id, a severity, the items involved, the layer, a coordinate and a suggested fix. The output is an ordinary KiCad project that you can open in PhiCad or stock KiCad.

The code-defined path builds a [tscircuit](https://github.com/tscircuit/tscircuit) `.tsx` design with the pinned engine from npm, then checks the exported board with this fork's own DRC:

```bash
cd phicad/core && bun install # pinned engine, from the committed bun.lock
phicad core capabilities
phicad core build design.tsx
```

Neither path needs network services beyond package registries. More detail:

- the [`phicad/` overview](phicad/README.md)
- the [getting-started guide](phicad/docs-site/docs/guides/getting-started.md)
- the [design IR and gates](phicad/synth/README.md)
- the [MCP tools](phicad/mcp/README.md)

## Configuration

Everything works offline by default. The optional integrations are:

| Variable | Default | Effect |
| --- | --- | --- |
| `PHICAD_LLM_GATEWAY` | *(unset: live model features disabled)* | Base URL of an OpenAI/LiteLLM-compatible gateway, used by `phicad models list/check` |
| `PHICAD_LLM_PROVIDER` | `gateway` | The `phi` provider id that holds your login for that gateway |
| `PHICAD_REPO`, `PHICAD_BUILD`, `PHICAD_VENV`, `PHICAD_KICAD_CLI` | the checkout / `build/<preset>` | Separate source and build trees for MCP/automation |
| `PHICAD_INFLUX_URL`, `PHICAD_INFLUX_ORG`, ... | *(unset: nothing shipped)* | Optional telemetry shipping. Telemetry is local-file only unless configured. See [phicad/pylib/TELEMETRY.md](phicad/pylib/TELEMETRY.md) |

The AI View assistant and the agent definitions in `phicad/agents/` run through the `phi` CLI and your own model provider. Agent `model:` refs use the provider id `gateway/<alias>`. Retarget them to your provider with `phicad models retarget`.

## Prerequisites

The KiCad build dependencies are wxWidgets, OpenCascade, ngspice, Boost, Python and the rest; `phicad bootstrap` installs them. Optional tools include Freerouting (autoroute), JLC2KiCadLib (parts fetch), Bun and Node.js 22+ (`phicad/core`), and `phi` (AI features). These are invoked as separate programs and are not bundled.

3Dconnexion SpaceMouse support uses the 3DxWare SDK headers in `thirdparty/3dxware_sdk/`. 3Dconnexion distributes these headers under GPL-3.0-or-later, as stated in each file header. On Windows and macOS the 3Dconnexion driver (navlib) is loaded dynamically if it is installed, and it is not required.

## License

GPL-3.0-or-later, the same as KiCad. See [LICENSE](LICENSE) and the per-file exceptions in [LICENSE.README](LICENSE.README). PhiCad's changes to KiCad and the `phicad/` tooling are distributed under the same terms. KiCad is a trademark of the KiCad project. See [NOTICE](NOTICE) for attribution.

## Contributing

See [CONTRIBUTING](https://github.com/couplet-physical-ai/.github/blob/main/CONTRIBUTING.md). Contributions require a DCO sign-off (`git commit -s`), and there is no CLA. Fork-specific work belongs under `phicad/`. Fixes to KiCad itself should go [upstream](https://dev-docs.kicad.org/en/contribute/).

## Security

Report vulnerabilities privately through [GitHub private vulnerability reporting](https://github.com/couplet-physical-ai/PhiCad/security/advisories/new) or by email to software@exowatt.com. Do not open a public issue. See the [security policy](https://github.com/couplet-physical-ai/.github/blob/main/SECURITY.md).

---

## Upstream KiCad README

The text below is KiCad's upstream README, kept for its credits and pointers.

For specific documentation about [building KiCad](https://dev-docs.kicad.org/en/build/), policies
and guidelines, and source code documentation see the
[Developer Documentation](https://dev-docs.kicad.org) website.

You may also take a look into the [Wiki](https://gitlab.com/kicad/code/kicad/-/wikis/home),
the [contribution guide](https://dev-docs.kicad.org/en/contribute/).

For general information about KiCad and information about contributing to the documentation and
libraries, see our [Website](https://kicad.org/) and our [Forum](https://forum.kicad.info/).

## Files
* [AUTHORS.txt](AUTHORS.txt) - The authors, contributors, document writers and translators list
* [CMakeLists.txt](CMakeLists.txt) - Main CMAKE build tool script
* [copyright.h](copyright.h) - A very short copy of the GNU General Public License to be included in new source files
* [Doxyfile](Doxyfile) - Doxygen config file for KiCad
* [INSTALL.txt](INSTALL.txt) - The release (binary) installation instructions
* [uncrustify.cfg](uncrustify.cfg) - Uncrustify config file for uncrustify sources formatting tool
* [_clang-format](_clang-format) - clang config file for clang-format sources formatting tool

## Subdirectories

* [3d-viewer](3d-viewer)         - Sourcecode of the 3D viewer
* [bitmap2component](bitmap2component)  - Sourcecode of the bitmap to PCB artwork converter
* [cmake](cmake)      - Modules for the CMAKE build tool
* [common](common)            - Sourcecode of the common library
* [cvpcb](cvpcb)             - Sourcecode of the CvPCB tool
* [demos](demos)             - Some demo examples
* [doxygen](doxygen)     - Configuration for generating pretty doxygen manual of the codebase
* [eeschema](eeschema)          - Sourcecode of the schematic editor
* [gerbview](gerbview)          - Sourcecode of the gerber viewer
* [include](include)           - Interfaces to the common library
* [kicad](kicad)             - Sourcecode of the project manager
* [libs](libs)           - Sourcecode of KiCad utilities (geometry and others)
* [pagelayout_editor](pagelayout_editor) - Sourcecode of the pagelayout editor
* [patches](patches)           - Collection of patches for external dependencies
* [pcbnew](pcbnew)           - Sourcecode of the printed circuit board editor
* [phicad](phicad)           - PhiCad's fork layer (not part of upstream KiCad)
* [plugins](plugins)           - Sourcecode for the 3D viewer plugins
* [qa](qa)                - Unit testing framework for KiCad
* [resources](resources)         - Packaging resources such as bitmaps and operating system specific files
    - [bitmaps_png](resources/bitmaps_png)       - Menu and program icons
    - [project_template](resources/project_template)          - Project template
* [thirdparty](thirdparty)           - Sourcecode of external libraries used in KiCad but not written by the KiCad team
* [tools](tools)             - Helpers for developing, testing and building
* [translation](translation) - Translation data files (managed through [Weblate](https://hosted.weblate.org/projects/kicad/master-source/) for most languages)
* [utils](utils)             - Small utils for KiCad, e.g. IDF, STEP, and OGL tools and converters
