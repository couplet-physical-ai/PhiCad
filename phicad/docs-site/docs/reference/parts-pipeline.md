# Parts pipeline

BOM-driven design with real, orderable components: a research agent (parts-scout) turns
functional requirements into exact MPN + LCSC rows in a per-project `parts.json` ledger;
after human approval, an integration agent (parts-integrator) fetches each part's symbol,
footprint, and STEP/WRL 3D model into the project-local library with `${KIPRJMOD}`-relative
bindings — so every board renders in the 3D viewer, exports to STEP/GLB, and emits an
order-ready BOM.

- `fetch_part.py` — verified LCSC id → project library assets (JLC2KiCadLib, MIT; runs in
  `phicad venv`), lib-tables registered idempotently, ledger rows promoted to `fetched`.
  `--backend easyeda2kicad` selects the AGPL-3.0 predecessor, which is also the automatic —
  and loudly warned about — fallback while JLC2KiCadLib is missing, for one release only.
- `audit_parts.py` — the fab-readiness gate: every footprint has a **resolvable** 3D model
  binding and an LCSC/MPN identity. Stdlib-only; parses both stable and fork board formats.
- Ledger schema, research-source ranking, and verified quirks: the module README.

Source of truth: [phicad/parts/README.md](https://github.com/couplet-physical-ai/PhiCad/blob/master/phicad/parts/README.md)
