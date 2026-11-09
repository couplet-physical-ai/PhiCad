# Architecture map

PhiCad is a full KiCad 10.x clone with a thin PhiCad layer kept under `phicad/`.

- Shared libraries live in `libs/`, `common/`, and `include/`.
- Editors and viewers occupy application directories such as `pcbnew/`, `eeschema/`, and `gerbview/`.
- KiCad editors use launcher binaries and dynamically loaded KIFACE modules connected through KIWAY.
- Domain models, file I/O, QA, and the protobuf IPC API have dedicated top-level areas.
- `kicad-cli` and the IPC API are the primary fork-native automation surfaces.

Source of truth: [phicad/architecture.md](https://github.com/couplet-physical-ai/PhiCad/blob/master/phicad/architecture.md)
