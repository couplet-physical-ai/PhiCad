# PhiCad configuration profiles

A profile is a set of seeds for the **per-user** KiCad settings tree: colour theme,
panel layout, hotkeys and toolbars, applied across every editor in one command.
No C++ change and no rebuild are involved.

```bash
phicad profile list                      # profiles, themes, and what is installed now
phicad profile install altium --backup   # seed all seven editors (quit PhiCad first)
phicad profile capture altium            # snapshot the live settings back into the repo
phicad profile theme phicad-altium       # just the colours, selected everywhere
phicad profile revert                    # restore the most recent --backup
```

`install` is an idempotent read-modify-write: it touches only the key paths an overlay
declares, so library paths, window geometry and recent projects survive. Re-running it
prints `already current`.

## Where things go

| | macOS | Linux |
| --- | --- | --- |
| settings **root** | `~/Library/Preferences/kicad` | `$XDG_CONFIG_HOME/kicad` |
| settings dir | `<root>/<major.minor>/` | same |
| backups | `<root>-phicad-backups/<ver>/<timestamp>/` | same |

`KICAD_CONFIG_HOME` overrides the **root**, not the versioned dir — KiCad appends
`major.minor` itself (`common/paths.cpp`). Handing it an already-versioned path silently
creates `<path>/10.99/10.99/` and nothing is ever found. `--config-home` takes the same
kind of path.

The seeds are pinned to a settings-dir name (`10.99` today). A KiCad version bump creates
a new dir that the profile has to be re-installed into, and any schema migration has to be
re-checked against the overlays; `phicad profile install --version` selects which one.

## Profile layout

```
profiles/<name>/
  profile.json                  manifest: description, themes, opaque key paths
  settings/<app>.json           SPARSE overlays deep-merged into <settings>/<app>.json
  toolbars/<app>-toolbars.json  copied whole into <settings>/toolbars/
  user.hotkeys                  TSV overlay merged by action name
```

Themes are *not* copied into the profile — `profile.json`'s `themes` list names files in
[`../themes/`](../themes/), which stays the one place a theme is edited.

An overlay's own leaves are the profile's contract: `install` writes exactly those paths
and `capture` reads exactly those paths back. A capture can therefore add no new surface,
and reviewing one means reading a diff of the same keys rather than auditing whatever the
GUI happened to write that session.

## The `altium` profile

Built from the Altium/KiCad UX comparison in
[`phicad/research/altium/ux.md`](../../research/altium/ux.md) (its "A. Ship profiles"
recommendation).

### Colours — all seven editors

`appearance.color_theme` is registered by `APP_SETTINGS_BASE`, so **every** editor carries
it: `pcbnew`, `eeschema`, `fpedit`, `symbol_editor`, `3d_viewer`, `cvpcb`, `kicad`. Setting
only the first two — as the old `phicad-theme` did — meant the Altium look collapsed the
moment a footprint or symbol editor opened. The profile sets all seven to `phicad-altium`,
whose mapping table is in [`../themes/README.md`](../themes/README.md).

### Panels

`aui.show_*` booleans, not a serialized AUI blob: `PCB_EDIT_FRAME` and `SCH_EDIT_FRAME`
apply them *after* `RestoreAuiLayout()`, so they win over `window.aui_state` and they still
work when the `EnableUseAuiPerspective` advanced setting is off. The profile pins
Properties and Appearance on and sets their widths.

Two behaviours worth knowing: in Pcbnew one flag (`aui.show_layer_manager`) drives the
Appearance panel **and** the Selection Filter, and in Eeschema the Selection Filter has no
flag at all — it appears whenever Properties, Hierarchy or the Net Navigator is shown and
docked.

### Hotkeys

`user.hotkeys` is a TSV of `<action.name>\tKey\tAltKey`. The profile ships **four**
provisional mappings, each keeping KiCad's own default as the alt key so nothing a KiCad
user already knows stops working:

| Action | PhiCad | KiCad default (kept as alt) | Altium |
| --- | --- | --- | --- |
| Route single track | `Ctrl+W` | `X` | Interactively Route Connections |
| Place wire (schematic) | `Ctrl+W` | `W` | Place Wire |
| Select connection | `Ctrl+H` | `U` | Select > Connected Copper |
| Zoom to fit | `Ctrl+PgDn` | `Ctrl+0` | Fit Document |

**These are provisional.** `ux.md` was written with no live Altium Designer to check
against — its own report contract records BLOCKED — so this is the intersection of what is
widely documented for AD and what is provable here, not a transcription of a real install.
Spot-check it against the Altium version you actually use.

The file itself records why the other obvious candidates (`Ctrl+M`, `L`, `Q`, `2`/`3`,
`F11`) are *not* mapped; each collides with a KiCad action registered in the same editor,
or with a macOS system gesture. Three constraints govern any addition:

- `Ctrl+` and `Cmd+` both parse to `MD_CTRL` everywhere, but `Alt+` parses only off macOS
  (`MODIFIER_ALT` is `Option+` there), so no Alt binding is used.
- an unparseable key name does **not** fall back — it silently *removes* the action's
  default binding, so every literal must come from `hotkeyNameList`
  (`common/hotkeys_basic.cpp`): `Esc`, `Del`, `Back`, `Ins`, `PgUp`, `PgDn`, `Return`, …
- an action name with no matching action is ignored, but sticks: KiCad's hotkey editor
  re-reads the whole file and writes every line back, typos included.

`#` comments live in the repo copy only; `phicad profile` strips them, because KiCad would
store a comment as an action named `#…` and keep rewriting it forever.

### Toolbars

`toolbars/pcbnew-toolbars.json` is Pcbnew's own default configuration plus three buttons:
**Find by Properties** and **Clear Highlight** beside Find (Altium's PCB Filter / Clear
Filter), and **Flip Board View** beside the 3D viewer.

The JSON is inert unless `appearance.custom_toolbars` is `true` in the app's settings file
— `TOOLBAR_SETTINGS::GetToolbarConfig` never consults the stored config otherwise — so the
profile sets both together, for Pcbnew only. Eeschema deliberately ships no toolbar seed:
a custom configuration **replaces** the whole toolbar, freezing it against upstream
additions, and Eeschema's defaults needed no Altium change.

## What this does not do

**It cannot make macOS dark beyond the canvas.** `appearance.app_theme` is registered only
under `__WXMSW__` and `appearance.icon_theme` only under `__WXGTK__`/`__WXMSW__`; on macOS
both are hard-assigned to `AUTO`. The canvas, layers and schematic sheet follow the theme —
the window chrome, menus, panels and toolbars follow the system appearance, and no profile
can change that.

The profile is also **copied**, never linked. A running KiCad writes settings, AUI state,
recent files and schema migrations back to whatever `KICAD_CONFIG_HOME` names, so pointing
the app at the repo would have it edit tracked files (`ux.md:83`). For the same reason
`install` refuses to run while the GUI is open: every frame rewrites its settings on close
and would discard the seeds.

## Adding a profile

1. `mkdir profiles/<name>` and write `profile.json` (`description`, `themes`, `opaque`).
2. Add sparse `settings/<app>.json` overlays containing only the keys you mean to own.
3. `phicad profile install <name> --dry-run` to see exactly what would change.
4. Tune the rest in the GUI, then `phicad profile capture <name>` and commit the diff.
