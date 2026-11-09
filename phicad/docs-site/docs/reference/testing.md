# Testing and quality gates

PhiCad's QA layer is built around Boost.Test executables registered with CTest, with fixtures under `qa/data/`.

- Use the matching `phicad test` registration for an area rather than running the whole suite.
- Use `phicad boost` for one binary or suite so the build-tree QA environment is set correctly.
- The `qa_cli` tests exercise the headless `kicad-cli` surface with pytest.
- `phicad fmt` applies formatting only to changed lines.
- Full builds and full test runs are intentionally expensive; choose narrow feedback first.

Source of truth: [phicad/testing.md](https://github.com/couplet-physical-ai/PhiCad/blob/master/phicad/testing.md)
