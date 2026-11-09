# AI View — an agent's-eye view of the open board

A toolbar button in the PCB editor that opens a window showing the **live board as
structured data** plus a chat pane grounded in that data. No C++ changes: it is an API
plugin, so it survives upstream merges.

The point is that an AI assistant reads the design over the IPC API — layers, stackup,
footprints, nets, routing state, extents — instead of looking at a screenshot of your
screen.

## Install

**Nothing to install: the plugin ships inside the application.** The build stages this
directory into `PhiCad.app/Contents/SharedSupport/plugins/ai-view/`
(`phicad/plugins/CMakeLists.txt`), which `API_PLUGIN_MANAGER` scans as the stock plugin
path — identically for build-tree runs and installed bundles. Every build of PhiCad has
the AI button (a sparkle) at the right end of the PCB **and schematic** editors' top
toolbars, and “New AI Project” in the project manager.

To hack on the plugin without rebuilding, install a per-user copy that shadows the stock
one (same identifier wins in the UI):

```bash
phicad/bin/phicad ai-view-install     # copies into ~/Documents/KiCad/<ver>/plugins/
```

or by hand:

```bash
mkdir -p ~/Documents/KiCad/10.99/plugins/ai-view
cp -R phicad/plugins/ai-view/* ~/Documents/KiCad/10.99/plugins/ai-view/
```

Then restart PhiCad.

> **A symlink does not work.** `API_PLUGIN_MANAGER::ReloadPlugins` walks the plugins
> directory with `wxDir::Traverse`, which does not follow symlinked directories, so a
> symlinked plugin is silently never discovered — no error, no log line, just no button.
> Copy the directory (verified both ways).

## How it connects

KiCad launches the action's entrypoint with `KICAD_API_SOCKET` and `KICAD_API_TOKEN`
already in the environment and `cwd` set to the plugin directory
(`common/api/api_plugin_manager.cpp`), so the plugin just calls `KiCad()` and is
connected to the app that launched it.

`ai_view.sh` exists to pick the interpreter. The manifest uses runtime type `exec`, not
`python`, deliberately: the `python` runtime would build a fresh venv and pip-install
`kipy` from the network, but this fork needs the **regenerated** bindings in the repo's
`build/venv` (see `phicad/bin/regen-kipy-proto`). The script walks up from its own real
location to find `build/venv/bin/python`, and honours `$PHICAD_AI_PYTHON` as an override.

That interpreter needs **`tkinter`, `kipy`, Pillow and (on Linux) `pypdfium2`**, which on
macOS means Homebrew's `python-tk@3.13` alongside `python@3.13` (`phicad bootstrap`
installs both). `phicad venv` installs Pillow and pypdfium2 for the image and datasheet
text paths. Check the runtime with:

```bash
build/venv/bin/python -c 'import tkinter, kipy, PIL, pypdfium2'
```

## The pieces

| File | Role |
| --- | --- |
| `plugin.json` | manifest; PCB Agent View and project-manager New AI Project actions |
| `ai_view.sh` | entrypoint; resolves the interpreter (bash 3.2 safe) |
| `board_context.py` | reads the open board over IPC; also runnable standalone |
| `chat_backend.py` | resolves the one AI harness — `phi` — and the shared subprocess environment; standalone fallback chat (one non-interactive CLI turn per message) |
| `phi_client_tag.mjs` | explicit, credential-free phi extension: tags standalone gateway calls `client=phicad` for spend attribution |
| `host_bridge.py` | inert placeholder: the host-terminal session bridge is not included in this build |
| `project_history.py` | standalone Git initialization or safe enclosing-repo discovery, baseline, and stable-save autosnapshots |
| `board_edits.py` | whitelisted write commands against the open board |
| `design_agent.py` | model-neutral design adapter, capability handshake, and automatic check/repair loop |
| `design_commands.py` | `/design` and `/synth`: compile a NEW project from a description |
| `view_commands.py` | `/view`, `/mode`, `/chat`, `/editor`: move the window's own view |
| `editor_control.py` | find and raise the running PhiCad editor; what Manual mode can reach |
| `bom_view.py` | the BOM/Datasheets tab: parts from the board file, and the datasheet cache |
| `ui_state.py` | remembers mode / chat / tab across relaunch |
| `ui_theme.py` | colours, type scale, and the reply formatter (markdown + JSON) |
| `ai_view_gui.py` | the tkinter window |
| `make_icons.py` | regenerates the toolbar icons |

## Two modes

The window has one control that changes everything else: `Agent View` ⇄ `Manual`, at the
left of the mode bar (which is present in both modes, because it is the only way back).

**Agent View** — the rendered board gets the whole window. Maximised, no tab strip, no
status block; the chat becomes a small panel floating over the render at the bottom right
that you close with `Close` (or `Esc`) and reopen with the `Chat` button that takes its
place. Views are switched from the compact selector in the mode bar, or by chat with
`/view pcb`. Nothing is hidden that isn't recoverable: the mode bar still carries the
board name, the IPC state and whether `phi` was found.

**Manual** — the original split: status block, board views left, chat right of a draggable
sash. Plus a `Focus Editor` button, because Manual means *you* drive and the real tools
are in PhiCad, not here.

The two modes are the same widgets, re-managed — nothing is created or destroyed on a
switch, so an in-flight render, the transcript and the `phi` session all survive it. That
is forced, not chosen: **Tk cannot reparent a widget.** Packing a notebook child elsewhere
raises, and re-adding a rebuilt chat frame to the `PanedWindow` fails *silently* — an
unmapped pane, no exception. So the chat frame stays a child of the `PanedWindow` forever,
and `place(in_=left)` — legal only because `left` is a descendant of the chat's parent — is
what makes a floating panel possible at all.

Two calls in there are measurements, not preferences (Tk 9.0, aqua):

- `root.state("zoomed")`, **not** `wm attributes -fullscreen`. Fullscreen strips the title
  bar, so the window loses its close and zoom buttons, and clearing it does not restore the
  previous geometry. `zoomed` fills the display work area, keeps the chrome, stays in the
  current Space — so the editor can still be raised over it — and `state("normal")` puts the
  old geometry back exactly. `PHICAD_AI_FULLSCREEN=1` opts into fullscreen anyway.
- The sash restore is retried and read back. A `sashpos` written before the `PanedWindow`
  has laid out the re-inserted pane is dropped with no error, and the chat comes back as a
  zero-width pane — which looks exactly like it was lost.

A floating `Toplevel` was the other candidate and was rejected: an `overrideredirect`
window does not stay above its parent (`stackorder isabove` reads 0 after `lift()`), so it
would need `-topmost`, which would also float the chat over the KiCad editor.

The mode, whether the chat panel is open, and the last tab are remembered in
`<KiCad config home>/phicad/ai-view.json` — on macOS
`~/Library/Preferences/kicad/phicad/ai-view.json`, honouring `KICAD_CONFIG_HOME`, and
overridable with `PHICAD_AI_STATE`. Deliberately not under the `10.99/` version directory:
a window-layout preference is about a person, not a file format. The path is computed
client-side rather than through the `GetPluginSettingsPath` IPC call, because the window
has to lay itself out before the first round trip — and correctly when there is no
connection at all. If the file cannot be written the mode still switches; the transcript
says once that it will not be remembered, and where it tried.

## The rendered views

Three tabs, all drawn by `kicad-cli` because the IPC API has no "give me a picture" call
and the plugin cannot use the editor's GAL canvas from its own process.

- **PCB** opens on **Front**. `Both` paints back copper under front, and with the ground
  pour filled the back is a solid sheet — legible since the paint order was fixed
  (`B.Cu` before `F.Cu`; `--layers` order is paint order), but the view that answers
  "what is on this board" is the front one.
- **Schematic** keeps its background. `--no-background-color` leaves the PNG transparent
  and the tab composites onto the near-black canvas that board plots need, so a sheet
  drawn in navy on cream arrived as dark ink on a dark canvas. It is also cropped to its
  contents: a sheet is sized to its page, the demo's symbols cover a third of an A4, and
  there is no `--fit-page-to-board` for `sch export`. The crop measures in RGB on
  purpose — Pillow's `getbbox()` uses only the alpha channel when there is one, and every
  pixel of a difference against an opaque reference has alpha 0, which cropped the first
  attempt to an 1800x25 strip.
- **3D** is raytraced with the board's own stackup colours, and is the slowest thing here
  by an order of magnitude, which is why every tab renders lazily on first reveal.

The size/time readout is a caption on the canvas, not a toolbar label: at the manual
split's pane width the toolbar had 44 px left for it and clipped `1600x2416 · 0.6s` to
`16`. It gets a chip behind it because it sits on a cream schematic as readily as on a
black plot.

## BOM/Datasheets

A fifth tab, and the only one that is not a picture: a parts table over an inspector, both
read-only. It is the one view that does **not** go over IPC — the board file already holds
every field a BOM needs in its footprint properties, `pylib/sexp.py` parses it in
milliseconds, and keeping it off the API leaves the chat's session alone.

- **Grouped** is a BOM as a purchaser means it: same Value + Footprint (+ MPN, + DNP)
  collapse to one line with a reference list and a quantity, and `exclude_from_bom`
  footprints are left out. The counts line says how many were excluded so the missing
  references are explained rather than merely absent; **All parts** shows every footprint.
- **A blank cell means the board does not say.** The demo's passives carry no MPN and no
  datasheet on purpose, so nothing here ever guesses one.
- **Datasheets cache to `<project>/datasheets/`**, beside the board, named
  `<MPN>-<8 hex of the URL>.pdf` — readable for a human browsing the folder, derivable
  from the URL alone, because vendors do publish different documents as `datasheet.pdf`.
  The folder is created when something goes into it, not when the tab opens.
- **Nothing is downloaded on its own.** Fetch / Fetch all are the only things that touch
  the network; urllib's proxy is disabled at the handler (the in-process twin of
  `chat_backend._child_env`, and for the same reason). A response that is not a PDF is
  refused rather than saved, so a vendor's consent wall never lands in the folder looking
  like a datasheet, and a download writes `.part` then renames so an interrupted one
  cannot read as cached.
- **Preview is page 1 of a cached PDF.** macOS uses `qlmanage -t`; Linux prefers the
  optional `pypdfium2` backend and renders the page with Pillow, scaling its longest edge
  to the requested size. If pypdfium2 is unavailable, Linux can still use qlmanage when
  one is present. Only cached files preview; an uncached one says so and waits for Fetch.
  The fix for a missing Linux backend is `build/venv/bin/pip install pypdfium2` (or
  `phicad venv`).
- **Page text is available without a screenshot.** `bom_view.datasheet_text(pdf,
  max_pages=3)` extracts the first pages with pypdfium2 for agents such as parts-scout.
  Pillow is required for PNG previews; pypdfium2 text extraction itself does not use
  Pillow.

Two scrollbars are load-bearing, both added after measuring: the columns want 882 px and
the manual split gives the table 559, which put MPN, LCSC and Datasheet off the right edge
with no way to reach them; and the inspector's fields overflow its fixed 296 px for a part
with a description and a vendor URL. The counts and any fetch error each get their own
full-width row — sharing the toolbar clipped them to `13 pa` and
`could not reach www.bosch-se`.

## The chat pane

The pane is the product, not a log window, so four things it does are deliberate.

**Replies are formatted, not printed.** A model answer is markdown, and a design is
sixty lines of JSON the user is asked to *read* before running `/design save`. So
`ui_theme.split_blocks` separates prose from fenced blocks; a block becomes a monospace
card on the board's own background with JSON syntax colouring and a caption that says
what it is (`design IR · 12 parts, 9 nets · /design save to keep it` — and only when the
object really carries `parts` and `nets`). `**bold**` and `` `code` `` are honoured
inline, bullets get a hanging indent, and a preformatted table (`/help`, a stage report)
keeps its columns.

Code lines are padded to the columns that *fit*, not to the longest line: padding past
the wrap point turns every line into two, one of them blank, which in the floating panel
is the difference between a card and a mess. `layout_snapshot` reports `code_columns` so
that number is measurable — 49 in the manual split, 40 in the floating panel.

**Commands are discoverable.** Typing `/` opens a completion popup: arrow keys move, Tab
completes to the command *name* (never the `<REF>` placeholder, which would produce a
command that fails on its own argument spelling), Esc dismisses, click selects. It is
filtered from `COMMANDS` tables in `board_edits`, `view_commands` and `design_commands` —
the same tables their `HELP` strings and this pane's `/help` are generated from, so a
command cannot exist in one place and be missing from another. The popup is `place()`d
inside the chat frame rather than being a `Toplevel`, for the same reason the floating
panel is: an `overrideredirect` window does not reliably stay above its parent here, and
`-topmost` would float it over the PhiCad editor too.

`/help` is rendered from those tables as usage-over-summary rows rather than printed as
the modules' aligned text: two columns of monospace are 70 characters wide and the
floating panel fits about 45.

**Work in progress says so.** One live line serves a model turn and a build: a spinner,
the elapsed seconds, and the latest real fact. For a build that fact is an artefact seen
to land in the output directory — `--json` silences every stage line
`phicad/synth/cli.py` prints, so there is no progress stream to read, and a checklist of
stages we merely *expect* would be inventing progress. A turn can be stopped (the child
is a `Popen`, and `Stop` kills it); a build cannot, and the button is only present when
it can do something.

The line is written once and each tick rewrites only what lies between `linestart` and
`lineend`. `delete(i, "end")` also removes the newline ending the line *before* `i`, so
an index-based rewrite collapses upward and appends instead of replacing — that shipped
once as a transcript accumulating four copies of "Compiling the design 0s" a second.

**Failures look like failures.** Every message carries a role chip, and errors get the
theme's red rather than the grey used for routine chatter. A message typed while a turn
is in flight says so and keeps what was typed; slash commands run regardless, because
they are local. The entry box grows from one row to eight with its content.

## What the windows look like

Native shell, ink canvas. On Tk 9.0.4 / aqua, backgrounds on most `ttk` controls are
ignored: frames, buttons, entries, notebooks and tabs follow the current macOS appearance.
PhiCad keeps those controls native instead of rebuilding inaccessible imitations, then
resolves Tk's semantic window colour and chooses readable text, dividers and cards for
that luminance. Light and dark mode therefore share one hierarchy without the old
light-only greys disappearing on a dark shell.

Colour lives where it is honoured *and* carries meaning: the render canvas, code blocks,
status dots, and the role of each message. Every accent is traceable to
`phicad/resources/themes/phicad-altium.json` — `board.background` for the canvas and code
cards, `copper.f` red for errors, `copper.b` blue for your turns, `f_silks` for strings in
JSON, `edge_cuts` magenta for design replies, `plated_hole` cyan for board and view
replies, and `copper.in1` green for a gate that passed. Each accent has a darkened text
twin on light surfaces and a lightened twin on dark ones; the derivation is in
`ui_theme.py`.

The New AI Project window uses the same palette and type scale. Its transcript is the
only expanding row, so the composer never gets squeezed out at the minimum window size;
a proposed design appears as a compact workflow card with destination, review, approval,
build and open actions in that order. Backend probing happens after the first frame, so
a slow `phi --version` cannot leave an empty window on launch.

### Text is fitted, never clipped

The mode-bar status line is assembled from segments and measured against the width the
label actually has, because it used to be one f-string in a label with `anchor="w"` and no
wrapping: when the bar ran out of room Tk simply cut the end off, and on a 1512 px window --
the widest common MacBook, so the ordinary case -- it read `Git waitin`. The states that
overflow are unremarkable, not extreme: a board name with a model and a running cost wants
592 px in a 416 px slot.

Segments are ordered least- to most-important (git state, AI backend, IPC state, board
name). Whole segments are dropped from the cheap end until the line fits, and a dropped
segment is disclosed with a trailing ellipsis; segment text is never chopped mid-word.
Board name and IPC state are last to go, because they are what the window is about. A
truncated status line is worse than a short one -- the reader cannot tell a severed word
from a real value, and this window's whole promise is to be honest about what it cannot
reach.

### An empty view says why it is empty

`board_paths()` returns `None` for four different reasons and a render pane cannot tell
them apart, so every empty pane used to print *"Open a board in PhiCad, then press
Refresh."* That is the wrong instruction whenever the blocker is not a missing board:
observed with PhiCad running and a board open, where the real cause was `No module named
'kipy'` and the Context tab diagnosed it correctly while the PCB tab sent the reader to the
editor. `AiViewApp.no_board_message()` is the single source for that sentence and names the
actual state instead:

| State | What the pane says |
| --- | --- |
| client error / connect failed | `Cannot read the board: IPC <state>.` + points at the Context tab, and says outright this is not a missing board |
| still connecting | `Not connected to PhiCad yet (IPC <state>).` + press Refresh once the editor is up |
| connected, nothing open | the original wording, which is correct here |
| schematic host, no saved PCB | the project has no board yet; create or open the PCB |
| board open, file not on disk | named as never saved, so there is no file to plot from |

The render tabs and the BOM pane share it; both had their own copy of the wrong sentence.
A failed refresh also invalidates the render panes, exactly as a successful one does --
otherwise a pane keeps a render of a board the window can no longer reach, or an
empty-state message composed from the *previous* `ipc_state`, which is how the PCB tab came
to read `IPC connecting...` while the mode bar already said `client error`. A pane that
cannot be re-read must not keep asserting the old answer.

When a render succeeds but cannot be displayed because Pillow is missing, the message says
so *and* names the remedy (`phicad venv`, or launching through `ai_view.sh` so the repo's
`build/venv` is used). Nothing is wrong with the board or the connection in that case, so a
bare path with no next step is a dead end.

### Bounds and environment

| Variable | Default | What it bounds |
| --- | --- | --- |
| `PHICAD_AI_REPAIR_ATTEMPTS` | 8 | model turns per `synth check` repair loop |
| `PHICAD_AI_BUILD_REPAIRS` | 3 | repair rounds after a *failed build gate* (each costs a full place/route/DRC pass) |
| `PHICAD_IPC_TIMEOUT_MS` | 120000 | per-request IPC budget for synth's own api-server |

## Automatic design repair

New Project does not make a person relay compiler output to the model. As soon as a reply
contains a design, `DesignValidationWorkflow` saves it to a temporary session and runs the
machine-readable `synth check --json` gate. A failed report and the complete current design
are sent back through the same model session; the corrected full design is checked again.
The loop continues until the gate passes or the configured safety bound is reached
(`PHICAD_AI_REPAIR_ATTEMPTS`, default 8). Missing or unchanged model replies are themselves
fed back automatically rather than becoming user work.

Each prompt also lists **what the gate already rejected** — one line per earlier attempt,
quoting the failing stage or the first problems. Without that, every round looked like the
first one to the model: on a real routing failure it widened tracks when the hint said
narrow, then proposed a variation of the same change, because nothing showed the approach
had already failed. Naming the rejected approaches is what makes the attempt bound
meaningful instead of letting the loop circle one wrong idea.

The final validated design still needs **human approval before build**. If a later build
gate fails, its captured log starts the same repair/check loop and the revised design needs
fresh approval before another build. Infrastructure failures (for example, no synth
interpreter) are not misrepresented as circuit problems and do not trigger model edits.

`design_agent.py` is deliberately independent of Tk. It exposes a `DesignModel` protocol,
a capability record, and versioned `WorkflowEvent` objects. The current
`PhiCliDesignModel` is one adapter; a future model or agent host implements/consumes those
contracts without changing synthesis or the approval boundary. The host protocol is
specified in `phicad/integration/`.

## Moving the view from chat

The assistant can recommend these; you run them. A slash command is intercepted in
`send_message()` and never reaches the model, so this is the same structural
human-in-the-loop as the write commands.

```
/view pcb | sch | 3d | context      show that tab
/view layers front|back|both        which copper set the PCB tab plots
/view side top|bottom|front|left    which angle the 3D tab renders
/view zoom in|out|fit|<factor>      zoom the visible tab
/view render                        re-render the visible tab from the board file
/view                               report what is on screen right now
/mode agent | manual                switch mode
/chat open | close                  show or hide the floating chat panel
/editor raise | status              raise the PhiCad editor / what we can reach
```

**These move *this window's* picture of the board, not the PCB editor's canvas** — and that
is a hard limit, not an omission. The IPC API has no viewport message of any kind: no zoom,
no pan, no scroll, no cursor, anywhere in `api/proto/`. The single generic escape hatch is
`RunAction`, and its own proto says the action names "may change as code is refactored" and
that it is "provided for low-level prototyping purposes only", so nothing here is built on
it. Everything that *would* touch editor state — `RunAction`, `Get/SetActiveLayer`,
`Get/SetVisibleLayers`, `HighlightNets`, `SyncSelection`, the appearance settings — is gated
by `checkForHeadless` (`pcbnew/api/api_handler_board.cpp:105`) and answers
`"<Command> is not available in headless mode"` unless a real editor frame is attached;
measured for all of them against `kicad-cli api-server`. `FocusOnItem` and `RefreshEditor`
are declared in the protos but have no handler registered at all.

What is left that Manual mode can honestly do is put the editor window in front of you.
`editor_control.py` finds the editor as the parent process first (KiCad launches an `exec`
plugin as a direct child and `ai_view.sh` execs python in place), then by scanning for a
running `*.app/Contents/MacOS/{PhiCad,KiCad,…}`, and activates it with `/usr/bin/open -a`
— `open` rather than the System Events trick KiCad itself uses because it needs no
Automation permission and never shows a consent dialog. If no editor process is running it
**refuses and says so** instead of launching a second PhiCad you are not connected to.

`board_context.py` on its own is a useful debugging tool:

```bash
KICAD_API_SOCKET=ipc:///tmp/kicad/api.sock build/venv/bin/python board_context.py
# exit 0 = printed a summary, 2 = no board open, 3 = cannot reach the API
```

## Designing a new board in the pane

`board_edits.py` edits the board that is open. `design_commands.py` is the other half:
describing a board that does not exist yet and getting a routed project back. It keeps the
same contract — the model proposes, the user commits, we report what every gate said.

```
you   "a BME280 sensor node, I2C broken out to a 4-pin header"
AI    one fenced ```json block: the design IR (parts, nets, placement intent)
you   /design save        keep it
      /synth check        validate: every pad netted, every footprint real (milliseconds)
      /synth build        scaffold, netlist, place, route, silkscreen, DRC
      /design open        load the result in this PhiCad
```

The model never writes a file and never starts a build; `/design save` is the moment a
human agreed to the design. What makes this reviewable at all is that the IR is small —
a board used to arrive as a thousand lines of s-expressions, which nobody reads, and sixty
lines of parts and nets is something a person can actually check first. Which is why the
pane renders it as a syntax-coloured card and captions it with its own part and net
counts: "small enough to read" is only true if it is also *legible*, and it was not.
`/design show` prints the saved design the same way, so what you are about to build is
never harder to read than what was proposed.

Everything shells out to `phicad/synth/cli.py --json`, so the pane, the MCP tools
(`synth_check`, `synth_build`) and the command line cannot drift apart. `/synth build`
runs on a worker thread — it takes tens of seconds — and it strips `KICAD_API_SOCKET` from
the child, so a build opens its own document-less server rather than reaching into the
editor session you are sitting in.

The system prompt in `chat_backend.py` carries the IR schema and the rules the compiler
enforces, above all that every pad of every part must be in exactly one net or explicitly
in `no_connect`.

## Chat backend

### The same panel in a browser (`web_view.py`)

This build does not include the host-terminal session bridge (`host_bridge.py` is an inert
placeholder with `AVAILABLE = False`), so Agent View chats through local phi only (see
*Standalone fallback*). `web_view.py` serves the same board panel over loopback for any browser:

```bash
phicad/bin/phicad web-view   path/to/project [--port N] [--open]  # serve (and open), prints a URL
phicad/bin/phicad web-view                                        # discover the board from cwd
```

**One board, one server, one tab.** A phi session reopens the panel after every generation, so
`web-view` reuses what is already running instead of stacking: a launch for a board that is
already served returns the *same* URL and port (`"reused": true`) rather than binding another,
and a second `--open` does not start a second server. Verified: three launches (including via
the `.kicad_pro` name) yielded one process. `--new` forces a separate server when you genuinely want two.

The record under the config root is a *hint*, never an authority — it is only reused after the
recorded server answers an authenticated request, so a crashed process, a recycled pid, or an
unrelated service on the port all fall through to a fresh bind instead of handing back a dead URL.

It is a **skin, not a fork**: it imports `host_bridge`, `board_views`, `board_context`, and
`project_history` — the same modules this GUI uses, none of which import tkinter. Keep it
that way. Logic belongs in those modules so both front ends inherit it; only presentation
belongs in `ai_view_gui.py` or `web_view.py`.

**The path argument is optional** (`discover_board`), because a launcher may run it
with no path from the project's directory. Order: one board in cwd → one within
3 levels (skipping `.git`/`build`/`node_modules`/`qa`) → **error naming the candidates** if
several → the remembered board only when the tree has none. Two rules to preserve: an
ambiguous tree must never be a guess (you cannot tell from a render which project you got,
so the wrong board reads as a broken panel), and ambiguity *here* outranks the remembered
board (otherwise launching from a KiCad checkout silently reopens an unrelated project). The
walk is bounded because unbounded it finds hundreds of `qa/data` fixtures.

**Keep the two front ends at parity.** A feature added to this window is not done until the
web panel has it. Its argument also
accepts a `.kicad_sch` / `.kicad_pro` / directory and resolves to the board, since the action
is reachable from eeschema and the project manager as well as pcbnew — everything downstream
is keyed off the board and derives the schematic from it.

It binds `127.0.0.1` with a per-run token and rejects unauthenticated data requests, because
the panel can trigger a real agent turn and commit to Git. The page's CSP must keep `blob:`
in `img-src`: renders are fetched with the auth header and handed to `<img>` as blob URLs, so
without it every view silently decodes to a 0×0 image.

The web panel's **3D** tab remains the static PNG render by default. **3D interactive** runs the
fork's `kicad-cli pcb export glb` support lazily and presents the result in Google's
`<model-viewer>` with orbit, pan, and zoom controls. GLBs are cached under the view's temporary
cache by a SHA-256 of the saved board content, so reopening the tab does not export again and a
board edit selects a new model automatically. If this fork's exporter or a referenced 3D model
fails, the panel shows the export reason and the static tab remains usable.

`<model-viewer>` 4.3.1 is an Apache-2.0 third-party component. Its minified module and licence are
vendored in [`static/`](static/THIRD-PARTY.md); Agent View never loads it from a CDN. Both that
script and the generated `.glb` are served by the loopback server behind the same per-run token
gate as every other asset.

### Standalone fallback

Without a project bridge, answers come from the signed-in user's local **phi**. There is
one harness, not a provider picker: `phi --print --mode json` with `--session-id` provides
continuity, and the per-turn event stream reports usage, cost and the model that actually
answered.

Resolution is deliberately not configurable. `$PHICAD_AI_CMD`, `$PHICAD_AI_BACKEND`,
`$PHICAD_AI_MODEL` and `$PHICAD_CODEX_HOME` are inert and reported when set. There is no
Codex fallback: if phi is absent, the window says so rather than silently sending the
board context and prompt to a different binary, vendor or credential.

Phi owns the user's gateway credential end to end. PhiCad never reads `auth.json` and
never puts a key in argv or the environment. The one direct gateway client (`phicad
models`) obtains the signed-in user's key on demand through phi's public
`phi auth token --json` pipe contract (phi 0.83.0-exo.45 or newer). Standalone turns load
one explicit bundled extension while all extension *discovery* remains disabled. It adds
LiteLLM's standard `x-litellm-tags: client=phicad` request tag, preserving any existing
tags; the per-user key remains the identity and budget owner while the tag permits
per-application spend slices.

The tscircuit engine has no model runtime and receives no credential. It evaluates/builds
circuit input when phi or the application invokes it; it does not call an LLM itself.

If neither a shared session nor local phi is available, the window still runs and says so,
showing the context it *would* have sent. It never fabricates an answer.

## Project Git history

Every opened or generated board project gets Git history without silently nesting a
repository. A standalone project is initialized at its own root; a board subdirectory inside
an existing repository reuses the nearest repository and scopes snapshots to that project
subdirectory. If the path is an ambiguous checkout (or the PhiCad source tree), Agent View
refuses with an explanation instead of choosing a repository or staging unrelated files.
Generated projects copy the design IR into the project before the baseline commit. While
Agent View is open, manual saves and external agent edits are committed after the file set
has remained stable for two seconds; closing the window takes a final snapshot. KiCad
lock/backups and the live bridge descriptor are ignored. Use the MCP `project_git_snapshot`
tool for named verified checkpoints.

## Verified

- Manifest validates against `api/schemas/api.v1.schema.json` (`jsonschema.validate`).
- Button appears in the PCB editor toolbar after installing as a real directory.
- Reads a real board: the Teensy demo came back as 18 footprints / 75 pads / 28 nets /
  13 tracks / 3 vias / 1 zone, board size 50.1 × 43.1 mm from the actual Edge.Cuts, in
  0.35 s.
- End-to-end chat: asked how many footprints and nets the board has and got a correct
  grounded reply — "18 footprints and 28 nets, and measures 50.1 × 43.1 mm".
- Status line proves the wiring: socket path, token presence, KiCad version, board
  filename, shared/local transport, active model, and Git branch/head.
- Project history tests cover standalone initialization, reuse and project-scoped commits in
  an enclosing repository, nearest nested-repository selection, and explicit refusal of
  ambiguous checkouts and the PhiCad source tree. They also prove generated design IR is
  retained, the runtime descriptor is ignored, and a stable edit creates exactly one
  autosnapshot commit.
- Both modes driven for real against `regulated-breakout` and photographed. Manual
  1200x780 with a 648/535 split; Agent 1512x917 zoomed, notebook 1512x873 filling it,
  `panes()` down to one child, chat re-managed from `panedwindow` to `place` at 423x483 in
  the corner; `/chat close` swaps it for a 69x28 button; and back to Manual restores
  648/535 exactly.
- Degradation, asserted rather than eyeballed: with `KICAD_API_SOCKET` pointed at a socket
  that does not exist, the context pane reads `COULD NOT READ THE BOARD`, the mode bar
  reads `(no board open) · IPC not connected · phi ready`, the PCB tab reads `Not connected
  to PhiCad yet (IPC not connected). Press Refresh once the editor is up.`, `/editor raise`
  refuses because no editor is running, and stderr is empty. Re-measured after the
  empty-state rewrite: no mapped widget has `reqw > w` (mode bar 1392 of 1512).
- BOM/Datasheets against both demo projects, driven and photographed: `regulated-breakout`
  reads 13 parts into 8 BOM lines with 2 excluded (the mounting holes, dimmed in All
  parts), `sensor-node` 6 into 5 with none excluded. Both real datasheet URLs fetched over
  the network — AP2112 738 KB in 0.5 s, BME280 1.6 MB in 2.0 s — into
  `<project>/datasheets/`, and page 1 of each rendered inline by `qlmanage` in 0.1 s. Every
  widget measured `reqw <= w` in both modes afterwards, which is what caught the clipped
  counts line and the unreachable MPN column in the first pass. That sweep covered the BOM
  tab, not the mode bar, which is how the clipped status line below survived it.
- Fetch failures, each producing a message and no file: a 404 (`www.diodes.com answered
  404 Not Found`), an unresolvable host, a non-PDF 200 (`did not send a PDF (Content-Type:
  text/html, 304244 bytes). Nothing was saved.`), a non-http scheme, and a dead proxy
  (`could not reach www.bosch-sensortec.com: [Errno 61] Connection refused`) — after which
  the table still reads, the `datasheets/` folder does not exist, and no `.part` remains.
- The proxy strip is a measurement, not a claim: with `HTTPS_PROXY` pointed at a closed
  port, `urllib.request.urlopen` fails and `bom_view.fetch_datasheet` still returns the
  755270-byte PDF.
- Window chrome is asserted as a property, not photographed: `test_ui_layout.py` builds a
  real label with the real font and calls the real renderer — the status line fits every
  width the mode bar hands it, only whole segments are shown, a drop is disclosed, board
  name and IPC state survive a narrow window, an unmapped label (`winfo_width() == 1`) still
  shows everything, and all five empty-state branches say something different. Confirmed the
  suite bites by restoring the old unbounded join: 3 of 7 layout cases fail.
- Driven against a real `kicad-cli api-server` on a real board over live IPC: mode bar
  `sensor-node.kicad_pcb · IPC connected · phi ready …` with a green dot, front copper
  rendered at 2916x5101 px in 3.1 s, and mode bar `reqw` 1437 of 1512 — the same window that
  wanted 1591 px before the status line was fitted.

Verification is scripted, not manual: `PHICAD_AI_PROBE="/mode agent;/chat close"` runs
those lines through the real `send_message()` dispatch on a timer and writes a JSON
snapshot after each — mode, window geometry and state, notebook style, the `PanedWindow`'s
own pane list, `winfo_ismapped`/`winfo_manager`/size for every container that moves, the
first line of the context pane, and the pane's own reply. `PHICAD_AI_PROBE_MS`, `_OUT` and
`_HOLD` set the step delay, the output file, and whether the window stays open (topmost, to
be photographed) at the end.

A step spelled `type:/vie` types without sending, which is the only way to snapshot a
state that exists only *while* typing — the completion popup — and `type:one\ntwo` (the
two characters, since the probe list is split on `;`) is how the multi-line paste case is
exercised. Each widget entry carries `reqw`/`reqh` beside `w`/`h`, because a control
clipped by a short cavity still answers `winfo_ismapped` and comparing the two is the
only way to see the clip: that is how "Re-render" clipped to a one-letter button and
"Fit" to "Fi" were found and fixed.

Measured this round, on the demo board:

- Manual 1200x780 — transcript 508x559, entry 438x35 growing to 182 on an eight-line
  paste, Send 75x37 mapped throughout, hint 523x22, completion popup 523x184 sitting
  directly above the entry; the 3D tab's control group `reqw == w == 301`, so nothing is
  clipped in the widest toolbar the pane has.
- Agent 1512x917 zoomed — floating panel 453x538, transcript 426x415, entry 356x35,
  popup `reqw` 209 against 441 available, hint `reqw` 347 against 441. The panel drops
  the popup's summary column and puts the selected command's summary in the hint line
  instead, because at 441 px the two-column rows wanted 552 and clipped mid-word.
- A real `/synth build` from a known-good design: 14 stages, DRC clean at 0 errors /
  0 unconnected, one working line throughout that ticked seconds and named each artefact
  as it appeared on disk (netlist, schematic, ERC, DRC).

## Limitations

- **Write authority depends on the transport.** A standalone `phi --print` assistant has
  the narrow, read-only AI View prompt and recommends local slash commands. In every mode, slash commands are intercepted locally and run only when the user types
  them; model output is never parsed as a command.

  Every local command re-reads the item from a **fresh `get_board()`** and compares before
  reporting success, because `ISC_OK` is also returned for edits the server discards, and
  because kipy getters return **copies** — `fp.position.x = ...` is a silent no-op. That
  no-op is real in this fork and was reproduced deliberately while testing the verifier.

  Local edits are not written to disk until `/save`, so a wrong command is one Cmd-Z away
  in the editor. `/save` rewrites the file in 10.99 format, which stable KiCad 10.0.x will
  refuse to open. Shared-agent edits must report their own DRC/ERC or synth gate evidence.
- The window **does not raise itself** over the editor — an assistant should not steal
  focus, so after clicking the button you may need to switch windows. The reverse *is*
  wired: `Focus Editor` (Manual mode) and `/editor raise` bring PhiCad back to the front,
  on macOS only, and refuse with a reason on any other platform or when no editor is
  running.
- **Chat cannot move the PCB editor's canvas**, only this window's rendered view — see
  "Moving the view from chat" above for why that is a property of the API rather than a
  gap here.
- `get_board()` is a snapshot; use **Refresh** after changing the board.
- Launched from the **schematic editor**, board facts come from the last saved
  `.kicad_pcb` on disk — but the schematic itself is read **live**: net names with
  connectable-member counts and the sheet hierarchy come from the running eeschema
  over this fork's schematic IPC commands (`phicad/pylib/sch_api.py`, dynamic
  bindings from the descriptor set `phicad venv` compiles — PyPI kipy does not know
  these commands). When that plumbing is missing the facts are omitted with a note,
  never guessed from files.
- Dielectric stackup rows are labelled from the layer *type* (`dielectric`), because
  they carry no user name and report `BL_UNDEFINED`.
