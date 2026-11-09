# Look and feel

Configuration profiles that make PhiCad read like Altium Designer without forking the UI:
one command seeds colour theme, panel layout, hotkeys and toolbars across every editor, as
an idempotent read-modify-write of the per-user KiCad settings tree. No C++ change, no
rebuild.

```bash
phicad profile install altium --backup   # quit PhiCad first
phicad profile capture altium            # tune it in the GUI, commit the diff
```

- **Colours** — `appearance.color_theme` set to `phicad-altium` in all seven settings files
  that carry it (`pcbnew`, `eeschema`, `fpedit`, `symbol_editor`, `3d_viewer`, `cvpcb`,
  `kicad`); setting only the first two lets the look collapse when a footprint or symbol
  editor opens.
- **Panels** — the `aui.show_*` booleans, which the frames apply *after* the AUI layout is
  restored, so Properties / Appearance / Selection Filter are up on first open.
- **Hotkeys** — four **provisional** Altium mappings (`Ctrl+W` route/wire, `Ctrl+H` select
  connection, `Ctrl+PgDn` zoom to fit), each keeping KiCad's own default as the alt key.
  `phicad/research/altium/ux.md` was written with no live Altium to check against, so treat
  the table as documented-and-provable, not verified.
- **Toolbars** — Pcbnew's default configuration plus Find by Properties, Clear Highlight
  and Flip Board View; inert unless `appearance.custom_toolbars` is also set, which the
  profile does.
- **Reversible** — `--backup` writes a timestamped copy beside the settings root and
  `phicad profile revert` restores it; `--dry-run` reports without writing.

Two limits worth knowing before you promise anyone a dark IDE. On macOS the profile cannot
darken anything but the canvas: `appearance.app_theme` is registered only on Windows and
`appearance.icon_theme` only on GTK/Windows, so window chrome, menus and panels follow the
system appearance. And the seeds are pinned to a settings-dir name (`10.99` today), so a
KiCad version bump means re-installing into the new directory and re-checking the overlays
against any schema migration.

Source of truth: [phicad/resources/profiles/README.md](https://github.com/couplet-physical-ai/PhiCad/blob/master/phicad/resources/profiles/README.md)
