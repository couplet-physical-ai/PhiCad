---
id: 0004-model-viewer-3d-panel
title: Serve the fork's GLB export to a <model-viewer> panel in the Agent web view
status: done
priority: 40
tier: standard
scope:
  - phicad/plugins/ai-view/**
  - phicad/docs-site/docs/**
requires:
  - $PHICAD_BUILD/pcbnew/_pcbnew.kiface
timeout_min: 45
max_attempts: 2
source: the OSS integration landscape review (unpublished proposal) ("Top recommendations", row 2)
branch: loop/0004-model-viewer-3d-panel-20260922181658
---

## Definition of done

The web view (`phicad/plugins/ai-view/web_view.py`) gains an interactive **3D** panel: the
existing `data-kind="3d"` button, which today shows a static render, offers an "interactive"
variant that (1) runs `kicad-cli pcb export 3d --format glb` through the existing kicad-cli
plumbing into the view's cache directory, (2) serves that `.glb` from the loopback server under
the same token gate every other asset uses, and (3) renders it with Google's `<model-viewer>`
web component (Apache-2.0) loaded from a **pinned, vendored copy under
`phicad/plugins/ai-view/static/`** (no CDN at runtime; record the version and licence in a
`static/THIRD-PARTY.md`).

1. Cache by board content hash like the other renders; regenerate only when the board changes.
2. Keep the static PNG path as the default so hosts without the GLB exporter still work; the
   panel must degrade with a visible reason when export fails.
3. Tests: the web view has unit tests under `phicad/plugins/ai-view/test_*.py`; add cases for
   the new route (token required, correct content type `model/gltf-binary`, 404 before export)
   using the existing fake-kicad-cli pattern in those tests.
4. Docs: `phicad/plugins/ai-view/README.md` and the docs-site Agent View page describe the
   panel and the licence note.

## Facts

- The GLB exporter exists in the fork (`kicad/cli/command_pcb_export_3d.cpp`) and is wrapped by
  `pcb_export_3d` in `phicad/mcp/tools_cli.py` — reuse its argument construction.
- `model-viewer` releases: https://github.com/google/model-viewer/releases (v4.x); the UMD/ESM
  bundle is a single JS file ~1 MB; vendor only the minified bundle and the LICENSE.
- The web view must stay stdlib `http.server`; no FastAPI here (see `phicad-web-view-surface`
  notes in `plugins/ai-view/README.md`).
