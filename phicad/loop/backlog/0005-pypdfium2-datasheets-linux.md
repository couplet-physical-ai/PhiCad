---
id: 0005-pypdfium2-datasheets-linux
title: Datasheet previews and page text on Linux with pypdfium2 (qlmanage is macOS-only)
status: done
priority: 50
tier: cheap
scope:
  - phicad/bin/phicad
  - phicad/plugins/ai-view/**
  - phicad/agents/parts-scout.md
  - phicad/docs-site/docs/**
timeout_min: 40
max_attempts: 2
source: the OSS integration landscape review (unpublished proposal) ("Top recommendations", row 8)
branch: loop/0005-pypdfium2-datasheets-linux-20260922185258
---

## Definition of done

`phicad/plugins/ai-view/bom_view.py` renders datasheet preview PNGs on Linux. Today
`preview_png()` shells out to `/usr/bin/qlmanage` (`QLMANAGE` constant), which exists only on
macOS, so the BOM panel silently has no previews on the Linux host.

1. Add a `pypdfium2` (Apache-2.0 / BSD-3) backend used when `qlmanage` is absent (and
   preferred on Linux): render page 1 to PNG at the requested size; import lazily so the module
   stays importable without the package; when neither backend is available return the existing
   "no preview" result with a reason string that names the fix
   (`build/venv/bin/pip install pypdfium2`).
2. Add `datasheet_text(pdf, max_pages=3) -> str` using the same library so `parts-scout` can be
   given page text instead of screenshots; mention the function in `phicad/agents/parts-scout.md`.
3. Tests under `phicad/plugins/ai-view/test_*.py`: backend selection by platform and
   availability with monkeypatched `shutil.which`/import, no real PDF rendering required (skip
   the render case when pypdfium2 is not importable — a skip, not a failure).
4. `phicad/plugins/ai-view/README.md` documents the Linux behaviour and the optional
   dependency; add `pypdfium2` to whatever requirements file `phicad venv` installs from, if one
   exists (check `phicad/bin/phicad` for how the venv is populated) — otherwise document the
   manual install.

## Facts

- `phicad/bin/phicad` is in scope only for the venv package list in `cmd_venv` (the ai-view tests are discovered automatically by the `ai-view suites` gate).

- Never adopt PyMuPDF for this (AGPL). pypdfium2 API: `pdfium.PdfDocument(path)`,
  `page.render(scale=...)`, `.to_pil()` needs Pillow — prefer `.to_numpy()`-free paths; check
  whether `PIL` is already in the venv before adding a dependency, and if not, write the PNG via
  pypdfium2's bitmap `.to_pil().save()` only when Pillow exists, otherwise document the limit.
- Cache file naming must keep the `<slug>-<8 hex>.pdf` convention described in the module.
