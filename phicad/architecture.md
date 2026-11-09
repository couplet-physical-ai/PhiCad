# Architecture map — PhiCad / KiCad 10.x

Where things live, so an agent can find the right file without grepping the whole tree.
Every bullet is anchored to a real path in this checkout. Companion docs:
[build-macos.md](build-macos.md), [testing.md](testing.md), [README.md](README.md).

## Repository shape

- `CMakeLists.txt` — root configuration; build order is `api`, `resources`, `thirdparty`, `libs`, `common`, `3d-viewer`, the editors/viewers, `plugins`, then `kicad` and optional QA/demos/translation.
- `.githooks/` — repository Git hooks.
- `.github/` — GitHub metadata/workflows.
- `.gitlab/` — GitLab CI and merge-request templates.
- `3d-viewer/` — reusable PCB/footprint 3D canvas, rendering, model cache, and 3D-viewer UI.
- `api/` — versioned protobuf schema sources for the KiCad IPC/API.
- `bitmap2component/` — bitmap-to-component conversion application.
- `cmake/` — project modules, platform detection, code/resource generators, lexer helpers, and install steps.
- `common/` — shared KiCad application/UI/domain glue plus the `common`, `kicommon`, and `pcbcommon` libraries.
- `cvpcb/` — footprint-assignment application KIFACE.
- `demos/` — optional sample/demo projects and build content.
- `doxygen/` — Doxygen configuration, layout, CSS, and `doxygen-docs` target.
- `eeschema/` — schematic editor, symbol editor/library handling, simulation, ERC, and schematic I/O.
- `gerbview/` — Gerber/Excellon viewer, parser, rendering, and conversion tools.
- `include/` — public/shared headers grouped by subsystem (`gal`, `view`, `tool`, `settings`, `plotters`, `api`, etc.).
- `kicad/` — project manager launcher and `kicad-cli`; also owns PCM subdirectory.
- `libs/` — small reusable static libraries: `core`, `kimath`, `snap`, `kiplatform`, `kinng`, `sexpr`.
- `pagelayout_editor/` — page-layout editor application/KIFACE.
- `pcb_calculator/` — PCB calculator application/KIFACE and data-file lexer.
- `pcbnew/` — PCB editor, footprint editor, board model, DRC, import/export, connectivity, and router.
- `plugins/` — loadable 3D model plugins (VRML/OCE/IDF) and legacy loader code.
- `qa/` — unit/integration tests, fixtures, fuzzers, mock headers, and QA tools.
- `resources/` — icons, MIME/application resources, translations support resources, and platform resources. In this fork the brand icon families there (`icon_kicad*`, `icon_<tool>*`, `project_kicad*`, platform `.ico`/`.icns`/hicolor marks) are **generated** by the PhiCad identity engine — `phicad/resources/branding/compose.py` (spec: `phicad/resources/branding/DESIGN.md`); regenerate rather than hand-edit.
- `signing/` — packaging/signing support.
- `thirdparty/` — vendored/build-integrated dependencies such as sentry and external libraries.
- `tools/` — developer/build utilities, coding checks, font generators, converters, and test helpers.
- `translation/` — translation extraction/build support, added conditionally by root CMake.
- `utils/` — auxiliary conversion/utilities (including `utils/idftools`).
- `scripts/` — absent in this checkout; scripting is under `pcbnew/python`, `eeschema/python_scripts`, and `common/api`.

## Executables, launchers, and KIFACE

- `kicad/CMakeLists.txt` — builds the GUI `kicad` project manager and console `kicad-cli`; they link shared/common libraries and `pcm`, but are not editor KIFACEs.
- `common/single_top.cpp` — generic small executable top; `TOP_FRAME` selects the editor frame and `BUILD_KIWAY_DLL` makes the top load its matching KIFACE.
- `include/kiway.h` — defines the in-process KIWAY software bus, KIFACE ABI/version (`KIFACE_VERSION 1`), project channels, frame-to-face mapping, and DSO loading.
- `include/kiface_base.h` — base implementation for each DSO: startup/shutdown, window creation, settings, action export, and `FACE_T` identity.
- `KIWAY` — one project-scoped bus per open project; KIFACEs communicate through virtual calls and wx events without linking directly to one another (`include/kiway.h`).
- `KIFACE` — exported DSO vtable with `OnKifaceStart`, `CreateKiWindow`, `IfaceOrAddress`, `GetActions`, job/API hooks, and lifecycle methods (`include/kiway.h`).
- `kicad/CMakeLists.txt` — on Unix creates in-build symlinks from `kicad/_*_kiface` to editor DSOs, making the launcher runnable from the build tree.
- `eeschema/CMakeLists.txt` — builds `eeschema` launcher plus `_eeschema.kiface`; object target `eeschema_kiface_objects` holds the editor implementation and `eeschema.cpp` exports KIFACE.
- `pcbnew/CMakeLists.txt` — builds `pcbnew` launcher plus `_pcbnew.kiface`; `pcbnew_kiface_objects` holds the editor and `pcbnew.cpp` exports KIFACE.
- `gerbview/CMakeLists.txt` — builds `gerbview` launcher plus `_gerbview.kiface`; `gerbview_kiface_objects` and `gerbview.cpp` form the DSO.
- `pagelayout_editor/CMakeLists.txt` — builds `pl_editor` launcher plus `_pl_editor.kiface`; `pl_editor.cpp` exports the face.
- `pcb_calculator/CMakeLists.txt` — builds `pcb_calculator` launcher plus `_pcb_calculator.kiface`; lexer/data sources are included in the DSO.
- `cvpcb/CMakeLists.txt` — builds `_cvpcb.kiface` only; CVPCB is launched through the project manager rather than a paired standalone executable in this CMake file.
- `bitmap2component/CMakeLists.txt` — builds standalone `bitmap2component` from `common/single_top.cpp` and converter sources; it is a standalone utility, not a `*_kiface` module.
- `include/kiway.h` — the seven registered face IDs are `FACE_SCH`, `FACE_PCB`, `FACE_CVPCB`, `FACE_GERBVIEW`, `FACE_PL_EDITOR`, `FACE_PCB_CALCULATOR`, and `FACE_BMP2CMP`.

## Shared libraries and graphics

- `libs/core/CMakeLists.txt` — `core` static library: base64, observables, profiles, UTF-8, version comparison, wx/STL compatibility; public headers are `libs/core/include`.
- `libs/kimath/CMakeLists.txt` — `kimath` static geometry/math library: transforms, trig, Bezier, convex hull, arcs, circles, segments, polygons, shapes, and geometry utilities.
- `libs/snap/CMakeLists.txt` — `snap` static snapping/inference/resolution library, linked publicly to `kimath`.
- `libs/kiplatform/CMakeLists.txt` — `kiplatform` static OS/wx-port abstraction: environment, policy, secrets, printing, UI, I/O, and platform drivers.
- `libs/kinng/CMakeLists.txt` — `kinng` static wrapper around NNG messaging (`libs/kinng/src/kinng.cpp`).
- `libs/sexpr/CMakeLists.txt` — `sexpr` static generic S-expression parser (`sexpr.cpp`, `sexpr_parser.cpp`); file-format-specific lexers are elsewhere.
- `common/CMakeLists.txt` — `kicommon` shared library contains broad reusable KiCad application infrastructure; `common` static library contains most UI/widgets/tools/plotting; `pcbcommon` isolates PCB-shared code.
- `include/` — headers are intentionally flat-by-domain include roots: `core`, `gal`, `view`, `tool`, `settings`, `project`, `properties`, `libraries`, `io`, `plotters`, `widgets`, `dialogs`, and `api` are the main navigation anchors.
- `common/gal/CMakeLists.txt` — `gal` shared library combines GAL core, painter, fonts, view classes, OpenGL implementation, Cairo implementation, shaders, and HiDPI canvases.
- `include/gal/graphics_abstraction_layer.h` — `GAL` is the renderer-neutral drawing API; implementations include `OPENGL_GAL` (`include/gal/opengl/opengl_gal.h`) and Cairo (`include/gal/cairo/cairo_gal.h`).
- `include/view/view.h` and `include/view/view_item.h` — `VIEW` owns/indexes visible `VIEW_ITEM`s, delegates drawing to `PAINTER`, and is the editor-side scene graph above GAL.
- `common/gal/opengl/` and `common/gal/cairo/` — concrete GPU/CPU backends, compositor, shader, cached vertex-container, and print paths.

## Tools, actions, settings, plotting, and parsers

- `include/tool/tool_manager.h` — `TOOL_MANAGER` registers tools, dispatches events, invokes actions, and manages tool state machines/coroutines.
- `include/tool/tool_base.h` and `include/tool/tool_interactive.h` — base tool identity/lifecycle and interactive `Go(...)` transition model.
- `include/tool/actions.h` and `common/tool/actions.cpp` — global `ACTIONS` registry (`save`, selection, zoom, interactive cancellation, etc.) represented as `TOOL_ACTION`s.
- `include/tool/action_manager.h` and `common/tool/action_manager.cpp` — action registration, menu/toolbar binding, hotkeys, and condition evaluation.
- `include/tool/selection_conditions.h`, `include/tool/editor_conditions.h`, and `common/tool/selection_conditions.cpp` — composable selection/UI conditions; PCB and schematic tools use these to enable/show menu items.
- `common/tool/` — common selection, group, picker, grid, properties, dispatcher, action, menu, and construction tools; editor-specific tools are in `eeschema/tools` and `pcbnew/tools`.
- `include/settings/json_settings.h` and `common/settings/json_settings.cpp` — `JSON_SETTINGS` maps typed `PARAM`s to JSON paths, schema versions, migration/default behavior, and disk load/save.
- `include/settings/settings_manager.h` and `common/settings/settings_manager.cpp` — `SETTINGS_MANAGER` owns/registers settings objects, loads/saves them, caches app settings, and handles reset/history/locking.
- `common/settings/` — concrete common/color/AUI/grid/CVPCB/KiCad/BOM settings and nested settings implementations.
- `include/plotters/` and `common/plotters/` — shared plotter abstraction and Gerber, DXF, PDF, PNG, PS, SVG backends; PCB options/parser live at `pcbnew/pcb_plot_params.*`.
- `eeschema/sch_plotter.*` and `pcbnew/pcb_plotter.*` — domain adapters that traverse schematic/board objects and feed common plotter backends.
- `cmake/Functions.cmake` — `make_lexer`/`make_lexer_export` generate format token sources in the binary tree and add generated include paths/dependencies.
- `cmake/BuildSteps/TokenList2DsnLexer.cmake` — converts one-token-per-line `*.keywords` into generated `*_keywords.cpp` and `*_lexer.h`; generated files explicitly say not to modify them.
- `common/CMakeLists.txt` — generates common lexers for stroke parameters, netlists, plot params, DRC rules, PCB, library tables, template fields, drawing sheets, and embedded files.
- `eeschema/CMakeLists.txt` — generates `dialogs/dialog_bom_cfg_lexer.h`/keywords and `schematic_lexer.h`/keywords from the corresponding keyword files.
- `pcbnew/CMakeLists.txt` — generates `specctra_import_export/specctra_lexer.h` and `specctra_keywords.cpp`; `pcb_calculator/CMakeLists.txt` generates its data-file lexer.
- `libs/sexpr/` — use for the general S-expression parser; use `common/dsnlexer.cpp` plus the generated domain lexer when a KiCad token-list format needs keyword-level parsing.

## Domain ownership

- `eeschema/schematic.h` — `SCHEMATIC` owns project-linked hierarchical sheets, screens, item resolution, connection graph, and schematic content.
- `eeschema/sch_*.h`/`.cpp` — `SCH_ITEM` family: symbols, wires/lines, pins, labels, junctions, sheets, text, tables, shapes, markers, and screen/view logic.
- `eeschema/sch_symbol.h` — `SCH_SYMBOL` is the placed schematic symbol and derives from the shared `SYMBOL` model.
- `eeschema/lib_symbol.*`, `eeschema/symbol_library_manager.*`, and `eeschema/libraries/` — `LIB_SYMBOL`, symbol-library adapters/managers, legacy libraries, cache and library I/O.
- `eeschema/sch_io/` — native and foreign schematic readers/writers; `eeschema/schematic.keywords` drives native parser token generation.
- `eeschema/erc/`, `eeschema/connection_graph.*`, and `eeschema/sch_rule_area.*` — electrical rules checking, connectivity/net semantics, and schematic rule areas.
- `pcbnew/board.h` — `BOARD` is the central PCB container for footprints, tracks, drawings, zones, groups, markers, points, generators, nets, constraints, settings, and board outline.
- `pcbnew/footprint.h`, `pcbnew/pad.*`, `pcbnew/pcb_track.*`, `pcbnew/pcb_via.*`, `pcbnew/pcb_shape.*`, `pcbnew/pcb_text.*` — PCB domain object hierarchy; class names identify `FOOTPRINT`, `PAD`, `PCB_TRACK`, `PCB_VIA`, and other `PCB_*` items.
- `pcbnew/pcb_io/` — native `.kicad_pcb` parser plus import/export plugins for Altium, Allegro, Cadstar, EasyEDA, Fabmaster, IPC-2581, ODB++, PADS, Sprint, DipTrace, AutoTrax, and PCAD.
- `pcbnew/board_design_settings.*`, `pcbnew/constraints/`, and `pcbnew/drc/` — board design rules/settings, constraint expressions, DRC engine/test providers, rule editor, markers, and violation reporting.
- `pcbnew/drc/drc_engine.h` — `DRC_ENGINE` evaluates board/design settings/netlist constraints and emits `PCB_MARKER` violations.
- `pcbnew/router/` — PNS push-and-shove router; `router_tool.h` integrates `ROUTER_TOOL` with the editor tool framework and `pns_*` holds routing geometry/solvers.
- `pcbnew/connectivity/` and `pcbnew/ratsnest/` — connectivity graph, net topology, ratsnest calculation, and update services.
- `3d-viewer/3d_canvas/`, `3d-viewer/3d_rendering/`, and `3d-viewer/3d_cache/` — board-to-3D adapter, OpenGL/render pipeline, model cache/plugin manager, and 3D scene data.
- `plugins/3d/vrml`, `plugins/3d/oce`, and `plugins/3d/idf` — loadable 3D model format plugins built before the application (`plugins/CMakeLists.txt`).
- `gerbview/gerber_file_image.*`, `gerbview/rs274*.cpp`, `gerbview/excellon_image.h`, and `gerbview/excellon_read_drill_file.cpp` — Gerber RS-274/X, aperture, and Excellon/drill model/parsers.

## UI and generated files

- `common/dialogs/`, `eeschema/dialogs/`, `pcbnew/dialogs/`, `gerbview/dialogs/`, and `3d-viewer/dialogs/` — wxWidgets dialogs/panels; editor-specific behavior generally lives in the non-`_base` class.
- `pcbnew/dialogs/dialog_plot_base.fbp` + `dialog_plot_base.h/.cpp` — representative wxFormBuilder source/generated pair; many such pairs exist throughout the tree.
- `*_base.fbp` — edit the form definition in wxFormBuilder; generated C++ follows the same basename and carries the `C++ code generated with wxFormBuilder` marker.
- `*_base.h/.cpp` — generated UI construction only; do not hand-edit generated files. Put event handlers, business logic, and custom controls in the paired non-`_base` files (for example `pcbnew/dialogs/dialog_plot.cpp`).
- `cmake/BuildSteps/{markdown2C,Txt2C,Html2C}.cmake` — other generated sources explicitly emit `Do not edit` headers; source markdown/text/HTML is the edit point.
- `CONTRIBUTING.md` — says to follow the tool-generated-content policy and use existing file formatting when clang-format conflicts with project nuance.

## Python and IPC/API

- `pcbnew/python/` — only in-tree Python example/plugin content in this checkout (for example `python/wizards/qfp/qfp.py` and `plugin.json`); no SWIG source/binding directory is present under `pcbnew`.
- `include/kiway.h` — retains historical documentation that a `_pcbnew.so` could be used under a Python top, but current CMake has no pcbnew SWIG target; do not search for a nonexistent `pcbnew/swig` tree.
- `common/api/python_manager.*` — launches an external Python interpreter and captures stdout/stderr; it is process execution support, not a C++ SWIG binding generator.
- `common/api/api_plugin_manager.*`, `include/api/api_plugin.h`, and `include/api/plugin_action_scope.h` — external API plugin process/action integration and environment/socket handoff.
- `api/CMakeLists.txt` — runs `protoc` over `api/proto/{common,board,schematic}` into the build directory and builds shared `kiapi`.
- `api/proto/` — protobuf wire surface: envelope/common types, editor commands, board commands/jobs/rules/types, and schematic commands/jobs/rules/types.
- `include/api/` and `common/api/` — C++ API client/server, handlers, serialization, cross-probing, plugin manager, and common API utilities.
- `pcbnew/api/` and `eeschema/api/` — board/footprint and schematic handlers/contexts that expose domain objects through the protobuf API.
- `tools/tests/test_plugin.py` and `tools/tests/test_kicad_plugin.py` — in-tree tests/examples for plugin behavior; action-plugin UI support is visible in `pcbnew/dialogs/panel_pcbnew_action_plugins.*`.

## Contributor traffic map

- `pcbnew/board.h` / `pcbnew/board.cpp` — central board model; most PCB features eventually touch this or a `PCB_*` item.
- `pcbnew/tools/` and `common/tool/` — interactive behavior, actions, selection, and event/state-machine work.
- `pcbnew/pcb_io/` — file format, import/export, and round-trip changes.
- `pcbnew/drc/` and `pcbnew/router/` — high-complexity rules/constraint and routing changes.
- `eeschema/schematic.h`, `eeschema/sch_item.*`, and `eeschema/sch_symbol.*` — schematic model and editing behavior.
- `eeschema/sch_io/` and `eeschema/symbol_library_manager.*` — schematic/symbol file and library changes.
- `common/gal/`, `include/gal/`, and `include/view/` — rendering, display, canvas, scene graph, and interaction visuals.
- `common/settings/` and `include/settings/` — preferences, project settings, JSON schema/version/migration changes.
- `common/plotters/`, `pcbnew/pcb_plotter.*`, and `eeschema/sch_plotter.*` — output/plotting bugs and new export formats.
- `kicad/cli/` and `api/` — automation, headless jobs, IPC, protobuf, and CLI surface changes.

## Observable style conventions

- `pcbnew/board.h`, `eeschema/sch_symbol.h`, and `include/tool/tool_manager.h` — classes/types are commonly uppercase with underscores (`BOARD`, `SCH_SYMBOL`, `TOOL_MANAGER`); methods use PascalCase.
- `pcbnew/board.h` and `include/settings/json_settings.h` — data members use `m_` prefix (`m_LegacyVisibleLayers`, `m_filename`); parameters commonly use `a` prefix (`aBoard`, `aFilename`).
- `_clang-format` — LLVM base, 4-space indentation, Allman braces, 120-column limit, left pointer alignment, spaces inside parentheses, no tabs, and no space before function-call parentheses.
- `uncrustify.cfg` — legacy/alternate formatter config also specifies 4-space indentation, spaces-only output, and left-style pointer/reference spacing.
- `CONTRIBUTING.md` — CI checks formatting, but says to preserve existing formatting and not allow clang-format to reorder existing includes without reason.
- `pcbnew/board.h` and `include/tool/tool_manager.h` — header guards are uppercase project names, often with `_H`/`_H_`; newer headers also use `#pragma once` (for example `pcbnew/router/router_tool.h`).
- `eeschema/schematic.h`, `pcbnew/board.h`, and `common/kiway.cpp` — source files begin with the KiCad GPL license/copyright header; retain it in new files.

