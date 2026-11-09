---
id: 0003-jlc2kicadlib-replaces-easyeda2kicad
title: Replace the AGPL easyeda2kicad subprocess in parts/fetch_part.py with MIT JLC2KiCadLib, keeping the old tool as fallback for one release
status: done
priority: 30
tier: strong
scope:
  - phicad/bin/phicad
  - phicad/parts/**
  - phicad/testing.md
  - phicad/docs-site/docs/**
  - phicad/README.md
depends_on:
  - 0002-klc-gate-lib-librarian
timeout_min: 60
max_attempts: 2
source: the OSS integration landscape review (unpublished proposal) ("Top recommendations", row 1; "License risk register")
branch: loop/0003-jlc2kicadlib-replaces-easyeda2kicad-20260922173458
---

## Definition of done

`phicad/parts/fetch_part.py` produces the same three assets (symbol, footprint, 3D model) for an
LCSC id through **JLC2KiCadLib** (MIT, PyPI `JLC2KiCadLib`) instead of easyeda2kicad (AGPL-3.0),
with the **public interface unchanged** (same CLI flags, same result dict keys, same on-disk
layout under the project's library folder), and:

1. A backend switch: `--backend jlc2kicad|easyeda2kicad` (default `jlc2kicad`); when the default
   backend is not installed the tool falls back to easyeda2kicad **with a printed warning that
   names the licence reason**, for one release. Record in `phicad/parts/README.md` that the
   fallback is scheduled for removal.
2. A comparison you actually ran (if the network allows): both backends on at least five LCSC
   ids used in existing tests/examples (search `phicad/` for `C[0-9]{4,}`), with a table in
   `phicad/parts/README.md` of what differed (pin count, footprint pad count, 3D model
   presence). If the network is blocked, land the code + tests and state that the comparison is
   pending, with the exact command to run it.
3. Tests: extend or add a stdlib test in `phicad/parts/` that exercises the argument
   construction and the fallback decision with a fake subprocess runner (no network), and
   confirm `phicad/bin/phicad check fast` runs it.
4. Docs: the "Third-party components" list in `phicad/README.md` and the parts README reflect
   the change; the proposal row's status may be updated to "adopted" in the docs-site page.

## Facts

- `phicad/bin/phicad` is in scope only for the `run_gate` line of the new test and, if needed, the venv package list in `cmd_venv`.

- JLC2KiCadLib CLI: `JLC2KiCadLib <LCSC id> -dir <out> -symbol_lib <name> -footprint_lib <name>`
  (read its `--help` after `pip install` into `build/venv`; do not trust this line blindly).
- The venv is `phicad/bin/phicad venv` (`build/venv`); install with the proxy variables stripped.
- Keep `run_easyeda2kicad()` intact for the fallback; add `run_jlc2kicadlib()` beside it and a
  `run_backend()` chooser. Path handling must keep the `Path.resolve()` discipline the module
  header explains.
