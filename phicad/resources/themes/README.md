# PhiCad color themes

Color themes for the PhiCad editors (PCB, schematic, GerbView). A theme is a
single JSON file that KiCad/PhiCad loads from the per-user colors directory —
no C++ changes and no rebuild are involved.

- macOS: `~/Library/Preferences/kicad/<version>/colors/<name>.json`
- Linux: `$XDG_CONFIG_HOME/kicad/<version>/colors/<name>.json`

The per-app selection lives under `appearance.color_theme` and stores the theme
**filename without `.json`**. Every editor has that key, not just the PCB and
schematic ones — see [Install and switch](#install-and-switch).

## Themes

### `phicad-altium` — Altium Designer look-alike

Makes the PCB editor immediately readable to a daily Altium Designer user.
Mapping decisions (Altium 2D defaults → PhiCad):

| Element | Color | Rationale |
| --- | --- | --- |
| Background | near-black `rgb(10,10,10)` | Altium 2D workspace; not pure black so the grid reads |
| F.Cu | warm red `rgb(235,45,40)` | Altium Top Layer red, softened to avoid bloom on dark bg |
| B.Cu | royal blue `rgb(60,100,240)` | Altium Bottom Layer blue, lifted — pure `#00F` dies on black |
| In1..In30 | green / olive / teal / amber cycle | Altium mid-layer greens & olives (`#008000`, `#808000`, …) |
| F.SilkS | yellow `rgb(255,235,60)` | Altium Top Overlay yellow — the most recognizable cue |
| B.SilkS | olive-yellow `rgb(160,150,50)` | Bottom Overlay, dimmer so sides are distinguishable |
| F.Mask / B.Mask | purple / magenta @ 0.45α | Altium Top/Bottom Solder purple & magenta |
| F.Paste / B.Paste | gray / maroon | Altium Top/Bottom Paste |
| Edge.Cuts | magenta `rgb(230,65,220)` | Altium boards are outlined on magenta Keep-Out/Mechanical |
| Courtyard / Margin | hot pink / magenta | keep-out family, per the Altium pink-magenta convention |
| Fab layers | gray / blue-gray | assembly-drawing neutrals |
| User/mechanical + Dwgs/Cmts/Eco | magenta/violet/pink/plum cycle | Altium Mechanical 1..N magenta family |
| Pads (plated) / via walls | silver-gray | Altium multi-layer pad gray; vias read as silver rings |
| Via holes | dark `rgb(45,45,45)` | hole is a void, like Altium |
| NPTH | cyan | must contrast plated pads |
| Ratsnest | thin gray-green @ 0.55α | Altium "connection lines" |
| DRC error | bright green @ 0.8α | THE Altium signature: green violation overlay |
| DRC warning | amber | distinguishable from errors at a glance |
| Grid | `rgb(75,75,75)` dots | visible on near-black, like Altium's default grid |
| Schematic sheet | warm off-white `rgb(252,252,245)` | Altium sheet white/pale warm tone |
| Wires / buses / pins / outlines | navy `rgb(0,0,132)` | Altium draws virtually the whole symbol + wiring navy |
| Component body fill | pale yellow `rgb(255,255,185)` | Altium part fill |
| Junctions, net/power labels | dark red `rgb(128..132,0,0)` | Altium junction dots and net labels are dark red |
| No-connect | red X | Altium No-ERC marker |
| GerbView | black bg + red/green/yellow/blue/… cycle | Altium CAM view feel |

Layers with no direct Altium counterpart (via stitching, constraint overlays,
locked-item shadows, 3D viewer materials) keep sane KiCad defaults so nothing
falls back to a text color. All 301 theme keys are set.

## Install and switch

```bash
phicad profile list                    # repo themes + what each editor is using
phicad profile theme phicad-altium     # install and select it in all seven editors
```

`phicad profile theme` read-modify-writes only `appearance.color_theme`; every
other setting is preserved. It covers **all seven** settings files that carry
that key — `pcbnew`, `eeschema`, `fpedit`, `symbol_editor`, `3d_viewer`,
`cvpcb`, `kicad` — because the footprint and symbol editors have their own
selection, and leaving those at `_builtin_default` means the theme collapses the
moment one of those windows opens. Restart PhiCad afterwards. You can also
switch per-app in the GUI: Preferences → PCB Editor → Colors.

The colours are one part of the wider Altium profile (panels, hotkeys,
toolbars): see [`../profiles/README.md`](../profiles/README.md).

## Making your own theme

1. Copy `phicad-altium.json` to `phicad/resources/themes/<my-theme>.json`.
2. Keep the `meta` block; `meta.version` is **required** (loading fails
   silently without it) and `meta.filename` should match the file name.
3. Edit colors. Values are CSS strings: `"rgb(r, g, b)"` or
   `"rgba(r, g, b, a)"`. A malformed value does NOT error — it silently falls
   back — so validate with `python3 -m json.tool` and eyeball the result.
4. Structure gotcha: `gerbview.layers` is a JSON **array** (64 entries), not
   an object. Everything else is nested objects: `board.*`, `board.copper.*`,
   `schematic.*`, `3d_viewer.*`.
5. The authoritative key list lives in
   `common/settings/color_settings.cpp` (JSON keys) and
   `common/settings/builtin_color_themes.h` (defaults). Cover every key or
   users will see default-theme colors bleeding through.
6. Verify without the GUI:
   `kicad-cli pcb export svg --theme <my-theme> ...` honors themes
   (`pcb render` does not).
